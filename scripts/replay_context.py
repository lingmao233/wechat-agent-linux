"""Compare the old flat context window with the new topic-segment window.

Read-only replay: reads stored messages from a SQLite database (or a JSON dump
produced by a read-only query) and prints the two context payloads the model
would receive.  It never sends a message, never writes to the database and
never calls the model unless ``--llm`` is explicitly passed.

Typical use for one real case::

    python scripts/replay_context.py --json state/replay/apple-3456-raw.json \
        --trigger-id 3456

The same database layout is used on the VM, so ``--db`` also works against a
copied ``memory.db``; the file is opened with ``mode=ro``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from wechat_agent.config import Settings
from wechat_agent.llm import LLMClient
from wechat_agent.models import ConversationMessage
from wechat_agent.topic_segment import build_topic_segment, media_placeholder

DEFAULT_ACCOUNT = "wxid_placeholder001"
DEFAULT_GROUP = "12345678@chatroom"
DEFAULT_TRIGGER = 3456

_COLUMNS = (
    "id, external_message_id, member_name, member_id, observed_at, "
    "source_created_at, text_kind, message_type, lifecycle_status, is_self, "
    "source_role, text, text_normalized, image_paths_json, media_metadata_json, "
    "reply_to_sender, reply_to_text"
)


def _json_dict(value) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _aware(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def load_rows_from_db(db_path: str, account_id: str, group_id: str, trigger_id: int, scan: int) -> list[dict]:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"select {_COLUMNS} from messages "
            "where account_id = ? and group_id = ? and id < ? "
            "order by id desc limit ?",
            (account_id, group_id, trigger_id, max(1, int(scan))),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in reversed(rows)]


def load_rows_from_json(path: str) -> list[dict]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("JSON dump must be a list of message rows")
    return [dict(row) for row in rows]


def row_text(row: dict) -> str:
    """Mirror ``Storage._row_text`` for replay rows."""

    if str(row.get("message_type") or "") == "voice":
        voice = _json_dict(row.get("media_metadata_json")).get("voice_transcription") or {}
        if voice.get("status") == "ok" and voice.get("text"):
            return str(voice["text"])
        return "[语音]"
    value = str(row.get("text_normalized") or "").strip()
    return value or str(row.get("text") or "")


def to_messages(rows: list[dict], *, trigger_id: int | None = None, normalize_media: bool = False) -> list[ConversationMessage]:
    """Convert raw rows into the ordered context messages the prompt uses.

    ``normalize_media=False`` reproduces the pre-change window, where a video
    XML row counted against the char budget; ``True`` reproduces the new
    runtime fetch, which normalizes media before budgeting.
    """

    messages: list[ConversationMessage] = []
    for row in sorted(rows, key=lambda item: int(item.get("id") or 0)):
        if str(row.get("lifecycle_status") or "active") != "active":
            continue
        if str(row.get("text_kind") or "") == "system":
            continue
        if trigger_id is not None and int(row.get("id") or 0) >= int(trigger_id):
            continue
        text = row_text(row)
        if normalize_media:
            text = media_placeholder(text) or text
        if not text:
            continue
        observed = _aware(row.get("source_created_at")) or _aware(row.get("observed_at"))
        images = _json_dict(row.get("image_paths_json"))
        image_paths = images if isinstance(images, list) else []
        messages.append(
            ConversationMessage(
                sender_name=str(row.get("member_name") or ""),
                sender_id=str(row.get("member_id") or ""),
                text=text,
                message_id=str(row.get("external_message_id") or row.get("id") or ""),
                observed_at=observed,
                image_paths=image_paths,
                source_role="assistant" if row.get("is_self") else str(row.get("source_role") or "human"),
                reply_to_sender=str(row.get("reply_to_sender") or ""),
                reply_to_text=str(row.get("reply_to_text") or "")[:200],
            )
        )
    return messages


def old_flat_window(messages: list[ConversationMessage], *, limit: int, max_chars: int) -> list[ConversationMessage]:
    """The exact pre-change algorithm: newest-first fill with a char budget."""

    selected: list[ConversationMessage] = []
    used = 0
    for message in reversed(messages):
        text = message.text or ""
        if used + len(text) > max_chars:
            continue
        selected.append(message)
        used += len(text)
        if len(selected) >= limit:
            break
    return list(reversed(selected))


def build_new_window(messages: list[ConversationMessage], settings: Settings, *, trigger_at=None):
    scan = messages[-max(1, int(settings.context_topic_scan_messages)):]
    segment = build_topic_segment(
        scan,
        trigger_at=trigger_at or (scan[-1].observed_at if scan else None),
        gap_seconds=settings.context_topic_gap_seconds,
        max_minutes=settings.context_topic_max_minutes,
        max_messages=settings.context_topic_max_messages,
        scan_limit=settings.context_topic_scan_messages,
        head_messages=settings.context_topic_head_messages,
        timezone_name=settings.group_timezone,
    ).trim_to_chars(settings.wechat_context_max_chars)
    topic = segment.to_dict(timezone_name=settings.group_timezone, include_ids=True)
    topic["enabled"] = True
    return segment, topic


def _render_lines(payload: list[dict], settings: Settings) -> list[str]:
    lines: list[str] = []
    for item in payload:
        if "note" in item:
            lines.append(f"    {item['note']}")
            continue
        sender = item.get("senderName") or "?"
        sent_at = item.get("sentAt") or ""
        content = str(item.get("content") or "")
        suffix = ""
        if item.get("truncated"):
            suffix = f"  [截断，共{item.get('chars')}字]"
        lines.append(f"    {sent_at} {sender}: {content}{suffix}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", help="path to memory.db (opened read-only)")
    source.add_argument("--json", help="JSON row dump fetched read-only from the VM")
    parser.add_argument("--account-id", default=DEFAULT_ACCOUNT)
    parser.add_argument("--group-id", default=DEFAULT_GROUP)
    parser.add_argument("--trigger-id", type=int, default=DEFAULT_TRIGGER)
    parser.add_argument("--scan", type=int, default=40, help="rows scanned backwards from the trigger")
    parser.add_argument("--old-limit", type=int, default=12)
    parser.add_argument("--output", help="write the comparison as JSON")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=None)
    if args.json:
        rows = load_rows_from_json(args.json)
    else:
        rows = load_rows_from_db(args.db, args.account_id, args.group_id, args.trigger_id, args.scan)
    messages = to_messages(rows, trigger_id=args.trigger_id)
    normalized = to_messages(rows, trigger_id=args.trigger_id, normalize_media=True)
    if not messages:
        raise SystemExit("no active stored messages matched the replay query")

    old = old_flat_window(messages, limit=args.old_limit, max_chars=settings.wechat_context_max_chars)
    segment, topic = build_new_window(normalized, settings)
    client = LLMClient(settings)
    old_payload = client._context_payload(old)
    new_payload = client._context_payload(segment.messages, topic)

    old_raw_chars = sum(len(item.text or "") for item in old)
    old_chars = sum(len(media_placeholder(item.text) or item.text) for item in old)
    new_chars = sum(len(item.text or "") for item in segment.messages)
    print("== 旧窗口：最近 N 条 ==")
    print(f"  行数={len(old)} 原始字数={old_raw_chars} 媒体按占位渲染={old_chars}字")
    print("\n".join(_render_lines(old_payload, settings)))
    print("\n== 新窗口：话题段 ==")
    print(
        f"  起点={topic['startedAt']} {topic['startSender']}「{topic['startText']}」 "
        f"持续={topic['durationText']} 行数={len(segment.messages)}/{topic['messageCount']} "
        f"省略中间={topic['omittedMiddle']} 截断前缀={topic['truncatedPrefix']} 边界={topic['boundary']}"
    )
    print("\n".join(_render_lines(new_payload, settings)))
    print("\n== 差异 ==")
    print(f"  行数 {len(old)} -> {len(segment.messages)}；原始字数 {old_chars} -> {new_chars}")
    old_ids = {item.message_id for item in old}
    new_ids = {item.message_id for item in segment.messages}
    print(f"  只出现在旧窗口的消息: {sorted(old_ids - new_ids)}")
    print(f"  只出现在新窗口的消息: {sorted(new_ids - old_ids)}")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(
                {
                    "trigger_id": args.trigger_id,
                    "topic": topic,
                    "old": old_payload,
                    "new": new_payload,
                    "old_chars": old_chars,
                    "new_chars": new_chars,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n已写入 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
