from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import uuid

from sqlalchemy import inspect, select, text

from .memory_schema import AnalysisJobRow, LifecycleRow, SchemaVersionRow
from .models import MessageEvent, QuotedMessage, utc_now

BACKOFF = (5, 30, 120, 300, 900, 1800, 3600)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item))


def stable_id(*parts: object) -> str:
    return hashlib.sha256(encode(parts).encode()).hexdigest()


def initialize_schema(storage) -> None:
    additions = {
        "messages": {
            "source_created_at": "TIMESTAMP", "ingest_sequence": "INTEGER",
            "source_role": "VARCHAR(24) DEFAULT 'human'", "time_quality": "VARCHAR(24) DEFAULT 'observed'",
            "message_type": "VARCHAR(24) DEFAULT 'text'", "media_metadata_json": "TEXT DEFAULT '{}'",
            "reply_to_text": "TEXT DEFAULT ''", "reply_to_sender": "VARCHAR(256) DEFAULT ''", "reply_to_time": "TIMESTAMP",
            "reply_to_svrid": "VARCHAR(256) DEFAULT ''", "sticker_md5": "VARCHAR(32) DEFAULT ''",
            "lifecycle_status": "VARCHAR(24) DEFAULT 'active'", "object_version": "INTEGER DEFAULT 1",
        },
        "ai_profiles": {"semantic_version": "INTEGER DEFAULT 0", "source_watermark": "INTEGER DEFAULT 0"},
    }
    inspector = inspect(storage.engine)
    with storage.engine.begin() as connection:
        for table, columns in additions.items():
            current = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in current:
                    connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')
    with storage.Session.begin() as session:
        if session.get(SchemaVersionRow, 8) is None:
            session.add(SchemaVersionRow(version=8, applied_at=utc_now()))


@contextmanager
def scope_transaction(storage, account_id: str, group_id: str):
    """Serialize a group in SQLite and across PostgreSQL processes; no I/O inside."""
    with storage.Session.begin() as session:
        if storage.engine.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        elif storage.engine.dialect.name == "postgresql":
            lock = int.from_bytes(hashlib.sha256(encode([account_id, group_id]).encode()).digest()[:8], "big", signed=True)
            session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
        yield session


def enqueue_ingest(storage, session, row, event: MessageEvent) -> None:
    now = utc_now()
    object_id = "raw-" + stable_id(event.account_id, event.group_id, event.external_message_id or row.message_hash)
    session.add(LifecycleRow(object_id=object_id, account_id=event.account_id, group_id=event.group_id, version=1, status="active", updated_at=now))
    session.add(AnalysisJobRow(message_id=row.id, account_id=event.account_id, group_id=event.group_id, analyzer_version=1,
        effect_enabled_at_ingest=bool(storage.settings.emotion_layer_enabled and not event.historical),
        status="awaiting_voice" if event.message_type == "voice" else "pending" if storage.settings.memory_event_extraction_enabled or event.message_type in {"image", "sticker", "quote"} else "disabled", next_retry=now, created_at=now))


def event_from_row(row) -> MessageEvent:
    quoted = None
    metadata = json.loads(row.media_metadata_json or "{}")
    voice = metadata.get("voice_transcription", {})
    text = (voice.get("text") or "[语音]") if row.message_type == "voice" and voice.get("status") == "ok" else "[语音]" if row.message_type == "voice" else row.text
    reference = json.loads(row.reply_to_json or "{}")
    if reference:
        quoted = QuotedMessage(text=reference.get("text", ""), sender_name=reference.get("sender_name", ""), message_id=reference.get("message_id"), sender_id=reference.get("sender_id") or row.reply_to_sender or "", created_at=aware(row.reply_to_time) if row.reply_to_time else None, raw_xml=reference.get("raw_xml", ""), message_type=reference.get("message_type", "text"), image_paths=reference.get("image_paths", []))
    # ``MessageEvent`` defaults to group because most adapters are group-scoped;
    # a durable reply job rebuilds the event from this row, so the stored
    # conversation type must be restored explicitly.  Otherwise one-to-one
    # messages are replayed as group messages and rejected by the group
    # allowlist (``group_not_allowlisted``) instead of the private allowlist.
    conversation_type = str(metadata.get("conversation_type") or "").strip().casefold()
    if conversation_type not in {"group", "private"}:
        # Legacy rows predate the stored conversation type; the WeChat chat id
        # is the same authoritative signal the Linux parser uses.
        conversation_type = "group" if str(row.group_id or "").endswith("@chatroom") else "private"
    return MessageEvent(account_id=row.account_id, group_id=row.group_id, group_name=row.group_name,
        member_id=row.member_id, member_name=row.member_name, text=text, observed_at=aware(row.observed_at),
        source_created_at=aware(row.source_created_at) if row.source_created_at else None, external_message_id=row.external_message_id,
        source_role=row.source_role, time_quality=row.time_quality, ingest_sequence=row.id, is_self=row.is_self,
        source=row.source, reply_to=quoted, thread_id=row.thread_id, task_id=row.task_id,
        image_paths=json.loads(row.image_paths_json or "[]"), message_type=row.message_type, sticker_md5=row.sticker_md5,
        sticker_url=metadata.get("sticker_url", ""), sticker_size=metadata.get("sticker_size", 0), media_metadata=metadata,
        audio_paths=metadata.get("audio_paths", []), conversation_type=conversation_type)


class MemoryWorker:
    def __init__(self, runtime):
        self.runtime, self.storage = runtime, runtime.storage
        self.owner = uuid.uuid4().hex
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        for name, task in (("analysis", self.runtime.process_analysis), ("profiles", self.runtime.maintenance)):
            thread = threading.Thread(target=self._loop, args=(task,), name="memory-" + name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def _loop(self, task) -> None:
        while not self._stop.is_set():
            try:
                active = task()
            except Exception:
                import logging
                logging.getLogger(__name__).exception("memory worker failed")
                active = False
            self._stop.wait(0.05 if active else 0.5)

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()

