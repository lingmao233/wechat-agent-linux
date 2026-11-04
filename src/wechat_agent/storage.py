from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import Settings
from .embedding import EmbeddingClient, EmbeddingError
from .group_style import (
    build_group_style_profile,
    new_style_stats,
    style_target_band,
    update_style_stats,
)
from .models import ConversationMessage, MemoryFact, MessageEvent, OutboundAction, QuotedMessage, utc_now
from .embedding import EmbeddingClient
from .profiles import extract_ai_profile_patch, extract_member_profile_patch
from .textnorm import normalize_chat_text, normalizer_version


log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    group_name: Mapped[str] = mapped_column(String(256))
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(Text)
    # Derived, canonical view of ``text``.  All profile, prompt and retrieval
    # paths read this column; ``text`` remains the raw audit copy.
    text_normalized: Mapped[str] = mapped_column(Text, default="")
    text_kind: Mapped[str] = mapped_column(String(16), default="text")
    text_meta_json: Mapped[str] = mapped_column(Text, default="{}")
    external_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    reply_to_json: Mapped[str] = mapped_column(Text, default="{}")
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(64), default="weixin_ui")
    image_paths_json: Mapped[str] = mapped_column(Text, default="[]")
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingest_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_role: Mapped[str] = mapped_column(String(24), default="human")
    time_quality: Mapped[str] = mapped_column(String(24), default="observed")
    message_type: Mapped[str] = mapped_column(String(24), default="text")
    media_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    reply_to_text: Mapped[str] = mapped_column(Text, default="")
    reply_to_sender: Mapped[str] = mapped_column(String(256), default="")
    reply_to_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reply_to_svrid: Mapped[str] = mapped_column(String(256), default="")
    sticker_md5: Mapped[str] = mapped_column(String(32), default="")
    lifecycle_status: Mapped[str] = mapped_column(String(24), default="active")
    object_version: Mapped[int] = mapped_column(Integer, default=1)
    # float32 little-endian vector, filled asynchronously by the analysis
    # worker.  ``embedding_model`` records which model produced it so a model
    # change can be detected instead of silently mixing spaces.
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(256), default="")


class GroupStyleProfileRow(Base):
    """Aggregate speaking-style profile scoped to one account and group.

    ``stats_json`` contains counters only.  It deliberately does not retain
    raw group messages; the normal ``messages`` table remains responsible for
    bounded conversation context and retention.
    """

    __tablename__ = "group_style_profiles"
    __table_args__ = (UniqueConstraint("account_id", "group_id", name="uq_group_style_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    group_name: Mapped[str] = mapped_column(String(256))
    profile_text: Mapped[str] = mapped_column(Text, default="")
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    observed_count: Mapped[int] = mapped_column(Integer, default=0)
    last_refresh_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FactRow(Base):
    __tablename__ = "memory_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    memory_type: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(32), default="candidate", index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    embedding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Additive fields used by the scoped memory contract.  They remain
    # nullable for old SQLite/PostgreSQL databases; ``init_schema`` upgrades
    # existing local tables without dropping any rows.
    memory_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    task_id: Mapped[str | None] = mapped_column(String(256), index=True, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    embedding_model: Mapped[str] = mapped_column(String(256), default="")


class UserProfileRow(Base):
    __tablename__ = "user_profiles"
    __table_args__ = (
        UniqueConstraint("account_id", "group_id", "member_id", name="uq_user_profile_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256), default="")
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AIProfileRow(Base):
    __tablename__ = "ai_profiles"
    __table_args__ = (UniqueConstraint("account_id", "group_id", name="uq_ai_profile_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    group_name: Mapped[str] = mapped_column(String(256), default="")
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    semantic_version: Mapped[int] = mapped_column(Integer, default=0)
    source_watermark: Mapped[int] = mapped_column(Integer, default=0)


class TaskProfileRow(Base):
    __tablename__ = "task_profiles"
    __table_args__ = (
        UniqueConstraint("account_id", "group_id", "task_id", "member_id", name="uq_task_profile_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    task_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), default="", index=True)
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class BindingRow(Base):
    __tablename__ = "identity_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256))
    platform: Mapped[str] = mapped_column(String(32), index=True)
    platform_account_id: Mapped[str] = mapped_column(String(256), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ActionRow(Base):
    __tablename__ = "action_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    action_type: Mapped[str] = mapped_column(String(32), index=True)
    target_member_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    at_names_json: Mapped[str] = mapped_column(Text, default="[]")
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ReplyRecordRow(Base):
    """Persistent idempotency and answer ledger for inbound messages."""

    __tablename__ = "reply_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    question_hash: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    group_name: Mapped[str] = mapped_column(String(256))
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(512), default="")
    # Why this message was answered: addressed|keyword|random|schedule|idle|manual.
    basis: Mapped[str] = mapped_column(String(32), default="")
    # Which policy items matched, as JSON, so the console can explain a reply.
    rules_json: Mapped[str] = mapped_column(Text, default="{}")
    origin: Mapped[str] = mapped_column(String(32), default="")
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="weixin_ui")
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformActionRow(Base):
    __tablename__ = "platform_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    action_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(256), index=True)
    parent_comment_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    target_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(512), default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformIdentityRow(Base):
    """Stable three-way identity: one WeChat member maps to each platform user.

    The stable platform user ID is the only authorization key; display names
    are kept for the console and native mention suggestions but are never used
    to decide who may be mentioned.
    """

    __tablename__ = "platform_identities"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "platform", "platform_user_id", name="uq_platform_identity_scope"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256), default="")
    platform: Mapped[str] = mapped_column(String(32), index=True)
    platform_user_id: Mapped[str] = mapped_column(String(256), index=True)
    platform_user_name: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PlatformMemoryRow(Base):
    """Cross-group platform memory shared by the three linked accounts.

    ``kind`` is one of ``browse``/``comment_seen``/``comment_posted``/``mention``.
    It stores only what the visible browser actually read or planned, so the
    group reply can reuse context without treating a platform author as a
    WeChat member.
    """

    __tablename__ = "platform_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    member_id: Mapped[str] = mapped_column(String(256), default="", index=True)
    platform: Mapped[str] = mapped_column(String(32), default="", index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    item_id: Mapped[str] = mapped_column(String(256), default="")
    item_url: Mapped[str] = mapped_column(String(2048), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    query: Mapped[str] = mapped_column(String(512), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


@dataclass(frozen=True, slots=True)
class ProcessingClaim:
    event_key: str
    question_hash: str
    claimed: bool
    is_new_event: bool
    status: str
    reason: str


class Storage:
    def __init__(self, settings: Settings):
        url = settings.database_url
        if url.startswith("sqlite:///"):
            db_path = url.removeprefix("sqlite:///")
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine_kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite:///") else {}
        self.engine = create_engine(url, future=True, **engine_kwargs)
        if url.startswith("sqlite:///"):
            from sqlalchemy import event as engine_event
            @engine_event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _record):
                connection.execute("PRAGMA busy_timeout=30000")
                if not url.endswith(":memory:"):
                    connection.execute("PRAGMA journal_mode=WAL")
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.settings = settings
        self.embedder = EmbeddingClient(settings)
        self.fts_available = False

    def init_schema(self) -> None:
        from . import memory_schema, reply_jobs  # register additive tables before create_all
        Base.metadata.create_all(self.engine)
        self._ensure_legacy_columns()
        from .memory_jobs import initialize_schema
        initialize_schema(self)
        self.init_message_index()
        self.migrate_opening_credits()

    def migrate_opening_credits(self, *, now: datetime | None = None) -> int:
        """Carry the pre-ledger affection balance into the credit ledger.

        Before ``relationship_credits`` existed the accumulated goodwill of a
        member lived in the single ``relationship_states.positive_credit``
        scalar.  The snapshot now sums credit rows, so without this step every
        member silently reset to 50 on upgrade.  The stored balance is written
        once as an ``opening_balance`` credit dated at its last update, which
        keeps its decay clock honest.  Idempotent: a member that already owns a
        credit row is never seeded twice.
        """

        from .memory_jobs import aware, encode, stable_id
        from .memory_schema import RelationshipCreditRow, RelationshipStateRow

        moment = now or utc_now()
        inserted = 0
        with self.Session.begin() as session:
            states = list(
                session.scalars(
                    select(RelationshipStateRow).where(RelationshipStateRow.positive_credit > 0)
                )
            )
            for state in states:
                if _is_self_member(state.account_id, state.member_id):
                    continue
                existing = session.scalar(
                    select(RelationshipCreditRow.credit_id).where(
                        RelationshipCreditRow.account_id == state.account_id,
                        RelationshipCreditRow.group_id == state.group_id,
                        RelationshipCreditRow.member_id == state.member_id,
                    )
                )
                if existing:
                    continue
                identifier = stable_id("opening", state.account_id, state.group_id, state.member_id)
                occurred = aware(state.credit_updated_at or state.last_interaction_at or moment)
                session.add(
                    RelationshipCreditRow(
                        credit_id=identifier,
                        account_id=state.account_id,
                        group_id=state.group_id,
                        member_id=state.member_id,
                        origin_event_id=identifier,
                        amount=float(state.positive_credit),
                        occurred_at=occurred,
                        rule_version=0,
                        status="active",
                        metadata_json=encode(
                            {"kind": "opening_balance", "legacy_value": float(state.positive_credit)}
                        ),
                    )
                )
                inserted += 1
        if inserted:
            log.info("migrated %d legacy affection balances into relationship_credits", inserted)
        return inserted

    def init_message_index(self) -> bool:
        """Create and backfill the FTS5 message index when SQLite supports it.

        The index is a plain (non external-content) FTS5 table whose ``rowid``
        is ``messages.id``, so a scope-filtered ``MATCH`` can be joined back to
        the authoritative row.  ``trigram`` was verified on the deployment
        SQLite 3.45.1: it indexes fixed three-character grams, which is what
        makes Chinese substring search work without a segmenter.  Queries
        shorter than three characters therefore cannot use it and fall back to
        ``LIKE`` inside :meth:`search_messages_lexical`.
        """

        self.fts_available = False
        if self.engine.dialect.name != "sqlite":
            return False
        try:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                    "text, account_id UNINDEXED, group_id UNINDEXED, tokenize='trigram')"
                )
        except SQLAlchemyError as exc:  # pragma: no cover - depends on build
            log.warning("messages_fts unavailable error=%s", type(exc).__name__)
            return False
        self.fts_available = True
        try:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO messages_fts(rowid, text, account_id, group_id) "
                    "SELECT id, COALESCE(NULLIF(text_normalized, ''), text), account_id, group_id "
                    "FROM messages WHERE id NOT IN (SELECT rowid FROM messages_fts)"
                )
        except SQLAlchemyError as exc:  # pragma: no cover - depends on build
            log.warning("messages_fts backfill failed error=%s", type(exc).__name__)
        return True

    @staticmethod
    def _index_message(session, row) -> None:
        """Keep ``messages_fts`` in the same transaction as the raw row."""

        if not getattr(session.get_bind().dialect, "name", "") == "sqlite":
            return
        try:
            session.execute(
                text("DELETE FROM messages_fts WHERE rowid = :rowid"), {"rowid": row.id}
            )
            session.execute(
                text(
                    "INSERT INTO messages_fts(rowid, text, account_id, group_id) "
                    "VALUES (:rowid, :body, :account_id, :group_id)"
                ),
                {
                    "rowid": row.id,
                    "body": row.text_normalized or row.text,
                    "account_id": row.account_id,
                    "group_id": row.group_id,
                },
            )
        except SQLAlchemyError:  # pragma: no cover - index is a rebuildable cache
            return

    @staticmethod
    def _unindex_message(session, message_id: int) -> None:
        try:
            session.execute(
                text("DELETE FROM messages_fts WHERE rowid = :rowid"), {"rowid": message_id}
            )
        except SQLAlchemyError:  # pragma: no cover - index is a rebuildable cache
            return

    def search_messages_lexical(
        self,
        account_id: str,
        group_id: str,
        queries: list[str],
        *,
        limit: int = 50,
        before_id: int | None = None,
        since=None,
        until=None,
    ) -> list[dict]:
        """Return the best lexical matches inside one group scope.

        ``bm25()`` decides the order (SQLite returns more negative = better);
        the caller only needs ids and a comparable relevance in ``score``, so a
        rank-based value is returned instead of the raw BM25 number.
        """

        terms: list[str] = []
        since, until = _naive_utc(since), _naive_utc(until)
        for query in queries:
            for token in re.findall(r"[A-Za-z0-9_]{3,}", str(query or "")):
                terms.append(token.casefold())
            for run in re.findall(r"[\u4e00-\u9fff]{3,}", str(query or "")):
                # trigram matches substrings of length >= 3; longer runs are
                # split into overlapping windows so any 3-gram can hit.
                terms.extend(run[index:index + 3] for index in range(len(run) - 2))
        unique = list(dict.fromkeys(terms))[:24]
        aliases = [alias for alias in (self.settings.wechat_ai_aliases or []) if len(alias) >= 3]
        unique.extend(alias.casefold() for alias in aliases if alias.casefold() not in unique)
        if not unique:
            return self._search_messages_like(account_id, group_id, queries, limit=limit, before_id=before_id, since=since, until=until)
        filters = ["messages.account_id = :account_id", "messages.group_id = :group_id", "messages.lifecycle_status = 'active'"]
        params: dict[str, object] = {"account_id": account_id, "group_id": group_id, "limit": limit}
        if before_id is not None:
            filters.append("messages_fts.rowid < :before_id")
            params["before_id"] = before_id
        if since is not None:
            filters.append("messages.observed_at >= :since")
            params["since"] = _sql_time(since)
        if until is not None:
            filters.append("messages.observed_at <= :until")
            params["until"] = _sql_time(until)
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in unique)
        rows: list[tuple[int, float]] = []
        if self.fts_available:
            params["match"] = match
            try:
                with self.engine.begin() as connection:
                    rows = [
                        (int(rowid), float(score))
                        for rowid, score in connection.exec_driver_sql(
                            "SELECT messages_fts.rowid, bm25(messages_fts) FROM messages_fts "
                            "JOIN messages ON messages.id = messages_fts.rowid "
                            "WHERE messages_fts MATCH :match AND " + " AND ".join(filters) +
                            " ORDER BY bm25(messages_fts) LIMIT :limit",
                            params,
                        )
                    ]
            except SQLAlchemyError as exc:  # pragma: no cover - build dependent
                log.warning("messages_fts query failed error=%s", type(exc).__name__)
                rows = []
        if not rows:
            return self._search_messages_like(account_id, group_id, queries, limit=limit, before_id=before_id, since=since, until=until)
        return [
            {"message_id": message_id, "rank": rank, "bm25": score}
            for rank, (message_id, score) in enumerate(rows)
        ]

    def _search_messages_like(
        self,
        account_id: str,
        group_id: str,
        queries: list[str],
        *,
        limit: int,
        before_id: int | None,
        since,
        until,
    ) -> list[dict]:
        """Short-query fallback: a two-character Chinese word has no trigram."""

        probes = []
        for query in queries:
            for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", str(query or "")):
                probes.append(token)
        probes = list(dict.fromkeys(probes))[:12]
        if not probes:
            return []
        statement = select(MessageRow.id, MessageRow.text_normalized, MessageRow.text).where(
            MessageRow.account_id == account_id,
            MessageRow.group_id == group_id,
            MessageRow.lifecycle_status == "active",
            or_(*[MessageRow.text_normalized.like("%" + probe + "%") for probe in probes]),
        )
        if before_id is not None:
            statement = statement.where(MessageRow.id < before_id)
        if since is not None:
            statement = statement.where(MessageRow.observed_at >= since)
        if until is not None:
            statement = statement.where(MessageRow.observed_at <= until)
        with self.Session() as session:
            rows = list(session.execute(statement.order_by(MessageRow.id.desc()).limit(limit * 4)))
        scored = []
        for message_id, normalized, raw in rows:
            body = (normalized or raw or "")
            hits = sum(1 for probe in probes if probe in body)
            if hits:
                scored.append((hits / len(probes), -message_id, message_id))
        scored.sort(reverse=True)
        return [
            {"message_id": message_id, "rank": rank, "bm25": -float(hits)}
            for rank, (hits, _, message_id) in enumerate(scored[:limit])
        ]

    def search_messages_semantic(
        self,
        account_id: str,
        group_id: str,
        vector: list[float],
        *,
        limit: int = 50,
        before_id: int | None = None,
        until=None,
        model: str = "",
    ) -> list[dict]:
        """Brute-force cosine over the scoped message vectors.

        Three groups of a few tens of thousands of rows are well inside a
        single-process numpy matmul; introducing an ANN index would add a
        service and a trust boundary for no measurable gain at this size.
        """

        if not vector:
            return []
        statement = select(
            MessageRow.id, MessageRow.embedding, MessageRow.embedding_model
        ).where(
            MessageRow.account_id == account_id,
            MessageRow.group_id == group_id,
            MessageRow.embedding.is_not(None),
            MessageRow.lifecycle_status == "active",
        )
        if before_id is not None:
            statement = statement.where(MessageRow.id < before_id)
        if until is not None:
            statement = statement.where(MessageRow.observed_at <= until)
        if model:
            statement = statement.where(MessageRow.embedding_model == model)
        with self.Session() as session:
            rows = list(session.execute(statement))
        pairs = [
            (message_id, blob)
            for message_id, blob, row_model in rows
            if blob and (not model or row_model == model)
        ]
        if not pairs:
            return []
        matrix = numpy.frombuffer(b"".join(blob for _, blob in pairs), dtype=numpy.float32)
        dimension = matrix.size // len(pairs)
        if dimension == 0 or matrix.size != dimension * len(pairs):
            return []
        matrix = matrix.reshape(len(pairs), dimension)
        query = numpy.asarray(vector, dtype=numpy.float32)
        if query.size != dimension:
            return []
        norms = numpy.linalg.norm(matrix, axis=1) * (numpy.linalg.norm(query) or 1.0)
        scores = (matrix @ query) / numpy.maximum(norms, 1e-9)
        order = numpy.argsort(-scores)[:limit]
        return [
            {"message_id": int(pairs[int(index)][0]), "rank": rank, "cosine": float(scores[int(index)])}
            for rank, index in enumerate(order)
        ]

    def save_message_embedding(self, message_id: int, vector: list[float], model: str) -> bool:
        if not vector:
            return False
        blob = numpy.asarray(vector, dtype=numpy.float32).tobytes()
        with self.Session.begin() as session:
            row = session.get(MessageRow, message_id)
            if row is None or row.lifecycle_status != "active":
                return False
            row.embedding, row.embedding_model = blob, model
        return True

    def index_status(self, account_id: str, group_id: str) -> dict:
        """Local corpus counters for ``memory-status``: index, vectors, narrative."""

        from .memory_schema import GroupTimelineRow, JudgeBudgetRow
        from zoneinfo import ZoneInfo
        day = utc_now().astimezone(ZoneInfo(self.settings.group_timezone)).date().isoformat()
        with self.Session() as session:
            active = session.scalar(
                select(func.count()).select_from(MessageRow).where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.lifecycle_status == "active",
                )
            ) or 0
            indexed = 0
            if self.fts_available:
                try:
                    indexed = session.execute(
                        text("SELECT count(*) FROM messages_fts WHERE account_id = :a AND group_id = :g"),
                        {"a": account_id, "g": group_id},
                    ).scalar() or 0
                except Exception:
                    indexed = 0
            embedded = session.scalar(
                select(func.count()).select_from(MessageRow).where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.lifecycle_status == "active",
                    MessageRow.embedding.isnot(None),
                )
            ) or 0
            last_at = session.scalar(
                select(func.max(MessageRow.observed_at)).where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.lifecycle_status == "active",
                )
            )
            timeline = session.scalar(
                select(func.count()).select_from(GroupTimelineRow).where(
                    GroupTimelineRow.account_id == account_id,
                    GroupTimelineRow.group_id == group_id,
                )
            ) or 0
            latest_day = session.scalar(
                select(func.max(GroupTimelineRow.day)).where(
                    GroupTimelineRow.account_id == account_id,
                    GroupTimelineRow.group_id == group_id,
                )
            )
            budget = session.get(JudgeBudgetRow, (account_id, group_id, day))
        return {
            "active_messages": int(active),
            "fts_rows": int(indexed),
            "fts_available": bool(self.fts_available),
            "embedded": int(embedded),
            "last_message_at": (
                _naive_utc(last_at).replace(tzinfo=timezone.utc).isoformat() if last_at else None
            ),
            "timeline_days": int(timeline),
            "latest_timeline_day": latest_day,
            "judge_calls_today": int(budget.count) if budget else 0,
        }

    def consume_judge_budget(self, account_id: str, group_id: str, day: str, limit: int) -> bool:
        """Reserve one model judgement for this group and day.

        Returns False when the day's allowance is exhausted; the caller then
        leaves the analysis job pending so the message is judged tomorrow
        instead of being dropped.
        """

        from .memory_schema import JudgeBudgetRow
        from .memory_jobs import scope_transaction
        with scope_transaction(self, account_id, group_id) as session:
            row = session.get(JudgeBudgetRow, (account_id, group_id, day))
            if row is None:
                row = JudgeBudgetRow(account_id=account_id, group_id=group_id, day=day, count=0, updated_at=utc_now())
                session.add(row)
                session.flush()
            if row.count >= limit:
                return False
            row.count += 1
            row.updated_at = utc_now()
            return True

    def judge_budget_used(self, account_id: str, group_id: str, day: str) -> int:
        from .memory_schema import JudgeBudgetRow
        with self.Session() as session:
            row = session.get(JudgeBudgetRow, (account_id, group_id, day))
            return int(row.count) if row else 0

    def apply_judged_facts(self, event: MessageEvent, facts: Iterable[MemoryFact]) -> int:
        """Persist the facts a judgement produced and refresh the profile view.

        The fact ledger is the durable copy; ``user_profiles`` is rebuilt from
        the active rows here so the two can never disagree.
        """

        if _is_unknown_member(event.member_id):
            return 0
        fields = {
            "preference": "preferences",
            "interest": "interests",
            "tool": "tools",
            "constraint": "constraints",
            "relation": "confirmed_info",
            "habit": "expression_habits",
            "experience": "confirmed_info",
            "correction": "corrections",
        }
        patch: dict[str, list[str]] = {}
        stored = 0
        for fact in facts:
            if fact.member_id and fact.member_id != event.member_id:
                continue
            try:
                self.add_scoped_memory(fact)
            except Exception:
                log.exception("failed to save judged memory type=%s", fact.memory_type)
                continue
            stored += 1
            field = fields.get(fact.memory_type)
            if field:
                patch.setdefault(field, []).append(fact.content)
        if patch:
            self.upsert_user_profile(
                account_id=event.account_id,
                group_id=event.group_id,
                member_id=event.member_id,
                member_name=event.member_name,
                patch=patch,
                source_message_ids=[str(event.external_message_id or event.ingest_sequence or "")],
            )
        return stored

    def embed_pending_messages(self, *, days: int = 90, limit: int = 16) -> int:
        """Embed one batch of recent messages that still have no vector.

        Runs on the existing background analysis thread; a failure leaves the
        rows unembedded so the next pass retries them instead of failing the
        analysis job or the reply path.
        """

        if not self.embedder.configured:
            return 0
        pending = self.messages_without_embedding(days=days, limit=limit)
        if not pending:
            return 0
        try:
            result = self.embedder.embed([item["text"][:2000] for item in pending])
        except EmbeddingError as exc:
            log.warning("embedding batch unavailable error=%s", type(exc).__name__)
            return 0
        stored = 0
        with self.Session.begin() as session:
            for item, vector in zip(pending, result.vectors):
                row = session.get(MessageRow, item["message_id"])
                if row is None or row.lifecycle_status != "active":
                    continue
                row.embedding = numpy.asarray(vector, dtype=numpy.float32).tobytes()
                row.embedding_model = result.model
                stored += 1
        return stored

    def messages_without_embedding(self, *, days: int = 90, limit: int = 16) -> list[dict]:
        cutoff = utc_now() - timedelta(days=days)
        with self.Session() as session:
            rows = list(session.execute(
                select(MessageRow.id, MessageRow.account_id, MessageRow.group_id, MessageRow.text_normalized, MessageRow.text)
                .where(
                    MessageRow.embedding.is_(None),
                    MessageRow.lifecycle_status == "active",
                    MessageRow.observed_at >= cutoff,
                )
                .order_by(MessageRow.id)
                .limit(limit)
            ))
        return [
            {"message_id": row[0], "account_id": row[1], "group_id": row[2], "text": row[3] or row[4] or ""}
            for row in rows
        ]

    def sticker_observation(self, md5: str) -> dict | None:
        from .memory_schema import StickerObservationRow
        with self.Session() as session:
            row = session.get(StickerObservationRow, md5)
            return {"description": row.description, "emotion": row.emotion, "confidence": row.confidence, "model": row.model, "image_path": row.image_path} if row else None

    def save_sticker_observation(self, md5: str, observation: dict) -> None:
        from .memory_schema import StickerObservationRow
        from .memory_jobs import scope_transaction
        with scope_transaction(self, "sticker", md5) as session:
            if session.get(StickerObservationRow, md5) is None:
                session.add(StickerObservationRow(md5=md5, **observation, created_at=utc_now()))

    def reserve_sticker_analysis(self, md5: str, owner: str) -> bool:
        from .memory_schema import MediaAnalysisLeaseRow, StickerObservationRow
        from .memory_jobs import aware, scope_transaction
        now = utc_now()
        with scope_transaction(self, "sticker", md5) as session:
            if session.get(StickerObservationRow, md5):
                return False
            row = session.get(MediaAnalysisLeaseRow, md5)
            if row and aware(row.expires_at) > now:
                return False
            expires = now + timedelta(seconds=self.settings.sticker_timeout_seconds + 5)
            if row:
                row.owner, row.expires_at = owner, expires
            else:
                session.add(MediaAnalysisLeaseRow(md5=md5, owner=owner, expires_at=expires))
        return True

    def release_sticker_analysis(self, md5: str, owner: str) -> None:
        from .memory_schema import MediaAnalysisLeaseRow
        with self.Session.begin() as session:
            session.execute(delete(MediaAnalysisLeaseRow).where(MediaAnalysisLeaseRow.md5 == md5, MediaAnalysisLeaseRow.owner == owner))

    def update_media_message(self, event: MessageEvent, message_id: int) -> None:
        from .memory_jobs import scope_transaction, stable_id
        from .memory_schema import AnalysisJobRow, LifecycleRow
        with scope_transaction(self, event.account_id, event.group_id) as session:
            row = session.get(MessageRow, message_id)
            if row and row.account_id == event.account_id and row.group_id == event.group_id and row.lifecycle_status == "active":
                previous = (row.image_paths_json, row.text_normalized, row.reply_to_json, row.media_metadata_json)
                row.image_paths_json = json.dumps(event.image_paths, ensure_ascii=False)
                if event.reply_to:
                    row.reply_to_json = json.dumps(asdict(event.reply_to), ensure_ascii=False, default=str)
                if event.message_type == "sticker" and event.text.startswith("[表情:"):
                    row.text_normalized = event.text
                    metadata = _json_dict(row.media_metadata_json)
                    metadata["derived_description"] = event.text
                    row.media_metadata_json = json.dumps(metadata, ensure_ascii=False)
                if event.message_type == "voice":
                    metadata = _json_dict(row.media_metadata_json)
                    voice = event.media_metadata.get("voice_transcription", {})
                    metadata.update(voice_transcription=voice, audio_paths=list(event.audio_paths))
                    row.media_metadata_json = json.dumps(metadata, ensure_ascii=False)
                    row.text_normalized = self.normalize_event(event).text if voice.get("status") == "ok" else "[语音]"
                    job = session.scalar(select(AnalysisJobRow).where(AnalysisJobRow.message_id == row.id))
                    if job and job.status == "awaiting_voice":
                        job.status = "pending" if voice.get("status") == "ok" and self.settings.memory_event_extraction_enabled else "disabled"
                if previous != (row.image_paths_json, row.text_normalized, row.reply_to_json, row.media_metadata_json):
                    row.object_version += 1
                    object_id = "raw-" + stable_id(event.account_id, event.group_id, row.external_message_id or row.message_hash)
                    lifecycle = session.get(LifecycleRow, object_id)
                    if lifecycle:
                        lifecycle.version = row.object_version
                    # The indexed body changed (sticker description or voice
                    # transcription): refresh the shadow row and drop the stale
                    # vector so the analysis worker re-embeds it.
                    self._index_message(session, row)
                    row.embedding, row.embedding_model = None, ""

    def _ensure_legacy_columns(self) -> None:
        """Apply additive columns to a pre-scoped local database.

        SQLAlchemy's ``create_all`` intentionally does not alter an existing
        table.  The small migration keeps old memory/message rows intact and
        is safe to run repeatedly for SQLite and PostgreSQL deployments.
        """

        additions = {
            "memory_id": "VARCHAR(128)",
            "scope_json": "TEXT DEFAULT '{}'",
            "source_message_ids_json": "TEXT DEFAULT '[]'",
            "task_id": "VARCHAR(256)",
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
            "valid_until": "TIMESTAMP",
            "importance": "FLOAT DEFAULT 0.5",
            "embedding_model": "VARCHAR(256) DEFAULT ''",
            "embedding_json": "TEXT",
        }
        inspector = __import__("sqlalchemy").inspect(self.engine)
        try:
            columns = {item["name"] for item in inspector.get_columns("memory_facts")}
        except Exception:
            return
        if not self.engine.dialect.name == "sqlite":
            # PostgreSQL migrations are supplied explicitly; create_all is
            # enough for a fresh database and ALTER here would need dialect
            # quoting/locking policy owned by deployment tooling.
            return
        with self.engine.begin() as connection:
            for name, definition in additions.items():
                if name in columns:
                    continue
                connection.exec_driver_sql(
                    f'ALTER TABLE memory_facts ADD COLUMN "{name}" {definition}'
                )

        # Message metadata was added after the original local schema.  Keep
        # existing raw rows and add only nullable/defaulted columns.
        try:
            message_columns = {item["name"] for item in inspector.get_columns("messages")}
        except Exception:
            return
        message_additions = {
            "external_message_id": "VARCHAR(256)",
            "thread_id": "VARCHAR(256)",
            "task_id": "VARCHAR(256)",
            "reply_to_json": "TEXT DEFAULT '{}'",
            "is_self": "BOOLEAN DEFAULT FALSE",
            "image_paths_json": "TEXT DEFAULT '[]'",
            "text_normalized": "TEXT DEFAULT ''",
            "text_kind": "VARCHAR(16) DEFAULT 'text'",
            "text_meta_json": "TEXT DEFAULT '{}'",
            "embedding": "BLOB",
            "embedding_model": "VARCHAR(256) DEFAULT ''",
        }
        with self.engine.begin() as connection:
            for name, definition in message_additions.items():
                if name in message_columns:
                    continue
                connection.exec_driver_sql(
                    f'ALTER TABLE messages ADD COLUMN "{name}" {definition}'
                )

        # P2 records why a reply happened next to the idempotency ledger.
        try:
            reply_columns = {item["name"] for item in inspector.get_columns("reply_records")}
        except Exception:
            return
        reply_additions = {
            "basis": "VARCHAR(32) DEFAULT ''",
            "rules_json": "TEXT DEFAULT '{}'",
            "origin": "VARCHAR(32) DEFAULT ''",
        }
        with self.engine.begin() as connection:
            for name, definition in reply_additions.items():
                if name in reply_columns:
                    continue
                connection.exec_driver_sql(
                    f'ALTER TABLE reply_records ADD COLUMN "{name}" {definition}'
                )

    def normalize_event(self, event: MessageEvent):
        """Canonical view used by every write-side analysis path."""

        return normalize_chat_text(
            event.text,
            sender_id=event.member_id,
            sender_name=event.member_name,
            ai_aliases=self.settings.wechat_ai_aliases,
        )

    def _row_text(self, row: MessageRow) -> str:
        """Return the normalized text for a stored row (backfill-safe)."""

        if row.message_type == "voice":
            voice = _json_dict(row.media_metadata_json).get("voice_transcription", {})
            return str(voice.get("text") or "[语音]") if voice.get("status") == "ok" else "[语音]"
        value = (getattr(row, "text_normalized", "") or "").strip()
        if value:
            return value
        return normalize_chat_text(
            row.text or "",
            sender_id=row.member_id or "",
            sender_name=row.member_name or "",
            ai_aliases=self.settings.wechat_ai_aliases,
        ).text

    def _bot_identity_names(self) -> set[str]:
        names = {str(self.settings.wechat_self_name or "").strip().casefold()}
        names.update(str(alias or "").strip().casefold() for alias in self.settings.wechat_ai_aliases)
        names.discard("")
        return names

    def _is_bot_row(self, row: MessageRow) -> bool:
        if bool(getattr(row, "is_self", False)):
            return True
        names = self._bot_identity_names()
        return bool(names) and (row.member_name or "").strip().casefold() in names

    def _sent_reply_texts(self, account_id: str, group_id: str, limit: int = 400) -> set[str]:
        """Recent verified replies, used to recognize historical self echoes."""

        with self.Session() as session:
            values = list(
                session.scalars(
                    select(ReplyRecordRow.reply_text)
                    .where(
                        ReplyRecordRow.account_id == account_id,
                        ReplyRecordRow.group_id == group_id,
                        ReplyRecordRow.reply_text.is_not(None),
                    )
                    .order_by(ReplyRecordRow.updated_at.desc())
                    .limit(max(1, min(int(limit), 2_000)))
                )
            )
        echoes: set[str] = set()
        for value in values:
            normalized = normalize_chat_text(value or "").text
            if normalized:
                echoes.add(normalized[:200])
        return echoes

    @staticmethod
    def _looks_like_echo(text: str, echoes: set[str]) -> bool:
        if not echoes or not text:
            return False
        probe = text[:200]
        for echo in echoes:
            if probe == echo:
                return True
            if len(echo) >= 20 and echo in text:
                return True
        return False

    @staticmethod
    def _looks_like_bot_output(text: str, member_id: str) -> bool:
        """Unknown-sender rows that self-identify as the bot are UI echoes."""

        if not _is_unknown_member(member_id):
            return False
        probe = str(text or "").strip()
        return probe.startswith(("我是 AI", "对，我是 AI", "本鱼", "大肥鱼", "🐋"))

    @staticmethod
    def hash_event(event: MessageEvent) -> str:
        # UI polling timestamps change on every scan.  A timestamp must never
        # be part of the idempotency key, otherwise a restart can answer the
        # same visible row again.
        external_id = (event.external_message_id or "").strip()
        stable_part = f"external:{external_id}" if external_id else f"text:{_normalize_text(event.text)}"
        if event.message_type == "voice" and not external_id:
            # Voice has no textual identity before ASR. Real DB/UI adapters
            # supply an external ID; other adapters use the captured timestamp.
            stable_part = "voice:" + (event.source_created_at or event.observed_at).isoformat()
        quoted = event.reply_to
        quote_part = ""
        if quoted is not None and quoted.text.strip():
            quote_part = "\x1fquote:" + _normalize_text(quoted.text)
            if quoted.sender_name.strip():
                quote_part += "\x1fsender:" + _normalize_text(quoted.sender_name)
            if quoted.message_id and quoted.message_id.strip():
                quote_part += "\x1fid:" + _normalize_text(quoted.message_id)
        raw = "\x1f".join(
            [
                event.account_id,
                event.group_id,
                event.member_id,
                event.thread_id or "",
                event.task_id or "",
                stable_part,
            ]
        )
        raw += quote_part
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def question_hash(event: MessageEvent, aliases: Iterable[str] = ()) -> str:
        """Hash a normalized question for durable answer de-duplication."""
        normalized = _normalize_question(event.text, aliases)
        if event.message_type == "voice":
            normalized += "\x1fvoice:" + (event.external_message_id or Storage.hash_event(event))
        quoted = event.reply_to
        quote_text = _normalize_text(quoted.text) if quoted is not None else ""
        quote_sender = _normalize_text(quoted.sender_name) if quoted is not None else ""
        quote_id = _normalize_text(quoted.message_id) if quoted is not None and quoted.message_id else ""
        raw = "\x1f".join(
            [
                event.account_id,
                event.group_id,
                event.member_id,
                event.thread_id or "",
                event.task_id or "",
                normalized,
                quote_sender,
                quote_text,
                quote_id,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def record_message(
        self,
        event: MessageEvent,
        *,
        enqueue_reply: bool = False,
        basis: str = "",
        origin: str = "human",
        rules: dict[str, object] | None = None,
    ) -> int | None:
        now = event.observed_at
        expires = now + timedelta(days=self.settings.raw_retention_days)
        normalized = self.normalize_event(event)
        row = MessageRow(
            message_hash=self.hash_event(event),
            account_id=event.account_id,
            group_id=event.group_id,
            group_name=event.group_name,
            member_id=event.member_id,
            member_name=event.member_name,
            text=event.text,
            text_normalized=normalized.text,
            text_kind=normalized.kind,
            text_meta_json=json.dumps(
                {
                    "prefix_sender": normalized.prefix_sender,
                    "addressed_as": normalized.addressed_as,
                    "quoted_text": normalized.quoted_text,
                    "sticker_count": normalized.sticker_count,
                    "image_count": normalized.image_count,
                    "truncated": normalized.truncated,
                    "normalizer_version": normalizer_version,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            external_message_id=event.external_message_id,
            thread_id=event.thread_id,
            task_id=event.task_id,
            reply_to_json=json.dumps(
                asdict(event.reply_to),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            if event.reply_to
            else "{}",
            is_self=bool(event.is_self),
            observed_at=now,
            expires_at=expires,
            source=event.source,
            source_created_at=event.source_created_at,
            source_role="assistant" if event.is_self else event.source_role,
            time_quality=event.time_quality,
            message_type=event.message_type,
            media_metadata_json=json.dumps({**event.media_metadata, "conversation_type": event.conversation_type, "sticker_url": event.sticker_url, "sticker_size": event.sticker_size}, ensure_ascii=False),
            reply_to_text=event.reply_to.text if event.reply_to else "",
            reply_to_sender=event.reply_to.sender_id or event.reply_to.sender_name if event.reply_to else "",
            reply_to_time=event.reply_to.created_at if event.reply_to else None,
            reply_to_svrid=event.reply_to.message_id or "" if event.reply_to else "",
            sticker_md5=event.sticker_md5,
            image_paths_json=json.dumps(
                [str(path) for path in (event.image_paths or []) if str(path).strip()][:12],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        from .memory_jobs import enqueue_ingest
        from .reply_jobs import enqueue as enqueue_reply_job
        try:
            with self.Session.begin() as session:
                existing = session.scalar(select(MessageRow).where(MessageRow.message_hash == row.message_hash))
                if existing:
                    event.ingest_sequence = existing.id
                    return existing.id
                session.add(row)
                session.flush()
                row.ingest_sequence = row.id
                event.ingest_sequence = row.id
                self._index_message(session, row)
                enqueue_ingest(self, session, row, event)
                if enqueue_reply:
                    # Same transaction as the raw row: the cursor may only be
                    # acknowledged once the reply obligation is durable.
                    enqueue_reply_job(session, event, row.id, basis=basis, origin=origin, rules=rules)
                return row.id
        except IntegrityError:
            with self.Session() as session:
                existing = session.scalar(select(MessageRow).where(MessageRow.message_hash == row.message_hash))
                if existing is None:
                    raise
                event.ingest_sequence = existing.id
                return existing.id

    def mirror_raw_message(self, event: MessageEvent, message_id: int | None) -> bool:
        """Report whether one newly inserted message is durably stored.

        SQL is the single retention and audit source of truth, and
        ``record_message`` already committed the row together with its analysis
        job, so there is nothing left to mirror anywhere else.
        """

        del event
        return bool(message_id)

    def observe_group_style(
        self,
        event: MessageEvent,
        *,
        min_messages: int,
        refresh_messages: int,
        sample_messages: int,
        max_chars: int,
    ) -> str:
        """Record one ordinary message and rebuild the windowed style profile.

        Counters are rebuilt from the last N normalized messages instead of
        accumulating for the lifetime of the group.  This makes the "最近 N 条"
        label true and lets the profile drift with the group instead of
        averaging years of old behavior.
        """

        normalized = self.normalize_event(event)
        if normalized.is_system:
            return self.group_style_profile(event.account_id, event.group_id)
        if not normalized.visible_text and not normalized.sticker_count and not normalized.image_count:
            return self.group_style_profile(event.account_id, event.group_id)
        now = utc_now()
        event_hash = self.hash_event(event)
        min_count = max(1, int(min_messages))
        refresh_count = max(1, int(refresh_messages))
        fingerprint_limit = max(1, min(int(sample_messages), 2_000))
        with self.Session.begin() as session:
            row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == event.account_id,
                    GroupStyleProfileRow.group_id == event.group_id,
                )
            )
            if row is None:
                row = GroupStyleProfileRow(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    group_name=event.group_name,
                    profile_text="",
                    stats_json=json.dumps(new_style_stats(), ensure_ascii=False),
                    observed_count=0,
                    last_refresh_count=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()

            try:
                stats = json.loads(row.stats_json or "{}")
            except (TypeError, ValueError):
                stats = {}
            if not isinstance(stats, dict):
                stats = {}
            seen_hashes = stats.get("seen_event_hashes")
            if not isinstance(seen_hashes, list):
                seen_hashes = []
            seen_hashes = [str(item) for item in seen_hashes if item]
            if event_hash in seen_hashes:
                return row.profile_text or ""

            # The replay guard is the only thing carried between rebuilds.
            # All real counters are re-derived from the window below.
            seen_hashes.append(event_hash)
            stats["seen_event_hashes"] = seen_hashes[-fingerprint_limit:]
            row.stats_json = json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
            row.observed_count = int(row.observed_count or 0) + 1
            row.group_name = event.group_name or row.group_name
            row.updated_at = now
            should_refresh = int(row.observed_count) >= min_count and (
                not row.profile_text
                or int(row.observed_count) - int(row.last_refresh_count or 0) >= refresh_count
            )
        if not should_refresh:
            return self.group_style_profile(event.account_id, event.group_id)
        self.rebuild_group_profiles(
            event.account_id,
            event.group_id,
            group_name=event.group_name,
            window_messages=self.settings.group_style_window_messages,
            max_chars=max_chars,
            force_remote=True,
        )
        return self.group_style_profile(event.account_id, event.group_id)

    def record_reply_style(self, account_id: str, group_id: str, text: str) -> dict[str, object]:
        """Track whether the bot's sent replies fit the group's length band."""

        normalized = normalize_chat_text(text or "").visible_text.replace("@AI", " ")
        length = len("".join(normalized.split()))
        if length <= 0:
            return {}
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == account_id,
                    GroupStyleProfileRow.group_id == group_id,
                )
            )
            if row is None:
                return {}
            stats = _json_dict(row.stats_json)
            low = int(stats.get("target_low") or 0)
            high = int(stats.get("target_high") or 0)
            if low <= 0 or high <= 0:
                low, high = style_target_band(stats)
            fit = stats.get("style_fit") if isinstance(stats.get("style_fit"), dict) else {}
            samples = int(fit.get("samples") or 0) + 1
            in_band = int(fit.get("in_band") or 0) + (1 if low <= length <= high else 0)
            total_chars = int(fit.get("total_chars") or 0) + length
            stats["target_low"] = low
            stats["target_high"] = high
            stats["style_fit"] = {
                "samples": samples,
                "in_band": in_band,
                "total_chars": total_chars,
                "last_length": length,
                "target_low": low,
                "target_high": high,
                "updated_at": now.isoformat(),
            }
            row.stats_json = json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
            row.profile_text = build_group_style_profile(
                stats, max_chars=self.settings.group_style_max_chars
            )
            row.updated_at = now
        return dict(stats["style_fit"])

    def rebuild_group_profiles(
        self,
        account_id: str,
        group_id: str,
        *,
        group_name: str = "",
        window_messages: int | None = None,
        max_chars: int | None = None,
        force_remote: bool = False,
    ) -> dict[str, object]:
        """Recompute group style and group AI profile from a bounded window.

        Both profiles are aggregate-only.  Rebuilding (rather than folding
        forever) is what makes them track the group's *current* habits.
        """

        window = max(20, min(int(window_messages or self.settings.group_style_window_messages), 5_000))
        char_limit = int(max_chars or self.settings.group_style_max_chars)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                        MessageRow.is_self.is_(False),
                    )
                    .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                    .limit(window)
                )
            )
        rows.reverse()
        stats = new_style_stats()
        ai_profile: dict[str, Any] = {}
        evidence: list[str] = []
        echoes = self._sent_reply_texts(account_id, group_id)
        used = 0
        resolved_group_name = group_name
        for row in rows:
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            if self._is_bot_row(row):
                continue
            text = self._row_text(row)
            if not text or self._looks_like_echo(text, echoes) or self._looks_like_bot_output(text, row.member_id):
                continue
            if not resolved_group_name:
                resolved_group_name = row.group_name or ""
            update_style_stats(stats, text)
            patch = extract_ai_profile_patch(
                text,
                str(row.external_message_id or row.id),
                ai_aliases=self.settings.wechat_ai_aliases,
            )
            if patch:
                ai_profile = _merge_ai_profile(ai_profile, patch)
                evidence.extend(str(item) for item in patch.get("evidence_message_ids") or [])
            used += 1

        stats["message_count"] = used
        stats["target_low"], stats["target_high"] = style_target_band(stats)
        profile_text = build_group_style_profile(stats, max_chars=char_limit)
        now = utc_now()
        with self.Session.begin() as session:
            group_row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == account_id,
                    GroupStyleProfileRow.group_id == group_id,
                )
            )
            if group_row is None:
                group_row = GroupStyleProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    group_name=resolved_group_name or group_id,
                    profile_text="",
                    stats_json=json.dumps(new_style_stats(), ensure_ascii=False),
                    observed_count=0,
                    last_refresh_count=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(group_row)
                session.flush()
            previous_stats = _json_dict(group_row.stats_json)
            stats["seen_event_hashes"] = list(previous_stats.get("seen_event_hashes") or [])[-2_000:]
            stats["style_fit"] = previous_stats.get("style_fit") or {}
            if stats["style_fit"]:
                profile_text = build_group_style_profile(stats, max_chars=char_limit)
            group_row.profile_text = profile_text
            group_row.stats_json = json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
            group_row.group_name = resolved_group_name or group_row.group_name
            # A rebuild itself counts as a refresh point.  Keep the lifetime
            # trigger counter at least at the window size so a newly created
            # row does not wait for a second full window.
            group_row.observed_count = max(int(group_row.observed_count or 0), used)
            group_row.last_refresh_count = int(group_row.observed_count or 0)
            group_row.last_refreshed_at = now
            group_row.updated_at = now

            ai_row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == account_id,
                    AIProfileRow.group_id == group_id,
                )
            )
            if ai_row is None:
                ai_row = AIProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    group_name=resolved_group_name or group_id,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(ai_row)
                session.flush()
            previous_ai = _json_dict(ai_row.profile_json)
            synthesized = previous_ai.get("synthesized")
            if isinstance(synthesized, dict) and synthesized:
                ai_profile["synthesized"] = synthesized
                ai_profile["synthesized_at"] = previous_ai.get("synthesized_at")
            ai_row.profile_json = json.dumps(ai_profile, ensure_ascii=False, separators=(",", ":"))
            ai_row.source_message_ids_json = json.dumps(
                _bounded_unique(evidence, 500),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            ai_row.group_name = resolved_group_name or ai_row.group_name
            ai_row.updated_at = now
            # Old history entries carried a full snapshot of the previous
            # profile on every message (~126 KB in the audited group).  Rebase
            # them onto the compact delta shape during the rebuild.
            ai_row.history_json = json.dumps(
                [
                    _compact_ai_history_entry(entry)
                    for entry in _json_list(ai_row.history_json)[-100:]
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            observed_count = int(group_row.observed_count or 0)
            ai_result = self._profile_dict(
                ai_row.profile_json,
                scope={"account_id": account_id, "group_id": group_id},
                source_message_ids=ai_row.source_message_ids_json,
                updated_at=ai_row.updated_at,
                history_json=ai_row.history_json,
            )

        return {"profile": profile_text, "window_messages": used, "ai_profile": ai_result}

    def group_style_profile(self, account_id: str, group_id: str) -> str:
        """Return the latest fitted style text for one account/group scope."""

        with self.Session() as session:
            row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == account_id,
                    GroupStyleProfileRow.group_id == group_id,
                )
            )
            return (row.profile_text or "").strip() if row else ""

    def group_style_snapshot(self, account_id: str, group_id: str) -> dict[str, object]:
        """Return dashboard-safe metadata without exposing the aggregate JSON."""

        with self.Session() as session:
            row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == account_id,
                    GroupStyleProfileRow.group_id == group_id,
                )
            )
            if row is None:
                return {
                    "enabled": True,
                    "ready": False,
                    "observed_count": 0,
                    "last_refresh_count": 0,
                    "profile": "",
                    "updated_at": None,
                    "last_refreshed_at": None,
                }
            return {
                "enabled": True,
                "ready": bool((row.profile_text or "").strip()),
                "group_name": row.group_name,
                "observed_count": int(row.observed_count or 0),
                "last_refresh_count": int(row.last_refresh_count or 0),
                "profile": (row.profile_text or "").strip(),
                "updated_at": _aware(row.updated_at).isoformat(),
                "last_refreshed_at": (
                    _aware(row.last_refreshed_at).isoformat() if row.last_refreshed_at else None
                ),
            }

    @staticmethod
    def is_self_member(account_id: str, member_id: object) -> bool:
        """True when ``member_id`` is the logged-in account, not a member."""

        return _is_self_member(account_id, member_id)

    def member_overview(self, account_id: str, group_id: str, *, limit: int = 30) -> list[dict]:
        """One compact row per known member, most recently active first.

        The retrieval planner no longer requires the question to name somebody:
        "你对大家印象怎么样" must still reach real member profiles.  This is the
        bounded projection used for that case (one line per member, newest
        speakers first), so the prompt cost stays predictable.
        """

        with self.Session() as session:
            profiles = list(session.scalars(
                select(UserProfileRow).where(
                    UserProfileRow.account_id == account_id,
                    UserProfileRow.group_id == group_id,
                )
            ))
            last_seen = {
                member_id: (name, _aware(observed))
                for member_id, name, observed in session.execute(
                    select(MessageRow.member_id, func.max(MessageRow.member_name), func.max(MessageRow.observed_at))
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                        MessageRow.is_self.is_(False),
                    )
                    .group_by(MessageRow.member_id)
                )
            }
            # The durable content of a profile lives in the fact ledger (the
            # profile JSON only mirrors it), so highlights come from facts
            # first and from the remaining profile lists second.
            facts: dict[str, list[str]] = {}
            for member_id, content in session.execute(
                select(FactRow.member_id, FactRow.content)
                .where(
                    FactRow.account_id == account_id,
                    FactRow.group_id == group_id,
                    FactRow.status.in_(["active", "confirmed"]),
                )
                .order_by(FactRow.updated_at.desc(), FactRow.id.desc())
            ):
                bucket = facts.setdefault(member_id, [])
                if content and content not in bucket and len(bucket) < 2:
                    bucket.append(content)
        rows: list[dict] = []
        for row in profiles:
            if _is_self_member(account_id, row.member_id):
                continue
            profile = self._profile_dict(
                row.profile_json,
                scope={"account_id": row.account_id, "group_id": row.group_id, "member_id": row.member_id},
                source_message_ids=row.source_message_ids_json,
                updated_at=row.updated_at,
                history_json=row.history_json,
            )
            highlights: list[str] = list(facts.get(row.member_id, []))
            for key in ("interests", "preferences", "expression_habits", "work_style", "confirmed_info"):
                for item in profile.get(key) or []:
                    text = str(item.get("content") if isinstance(item, dict) else item).strip()
                    if text and text not in highlights:
                        highlights.append(text)
                    if len(highlights) >= 2:
                        break
                if len(highlights) >= 2:
                    break
            name, observed = last_seen.get(row.member_id, (row.member_name, None))
            rows.append({
                "member_id": row.member_id,
                "member_name": name or row.member_name or "",
                "one_liner": "；".join(highlights)[:120],
                "last_seen_at": observed.isoformat() if observed else "",
            })
        rows.sort(key=lambda item: item["last_seen_at"], reverse=True)
        return rows[:limit]

    def upsert_group_timeline(
        self,
        *,
        account_id: str,
        group_id: str,
        day: str,
        summary: str,
        topics: list[str],
        active_members: list[dict],
        emotion: dict,
        source_message_ids: list[str],
        message_count: int = 0,
        model: str = "",
    ) -> None:
        from .memory_schema import GroupTimelineRow
        from .memory_jobs import scope_transaction
        now = utc_now()
        members = [
            item for item in active_members
            if not _is_self_member(account_id, item.get("member_id") if isinstance(item, dict) else item)
        ]
        with scope_transaction(self, account_id, group_id) as session:
            row = session.get(GroupTimelineRow, (account_id, group_id, day))
            if row is None:
                row = GroupTimelineRow(account_id=account_id, group_id=group_id, day=day, created_at=now)
                session.add(row)
            row.summary = summary[:2000]
            row.topics_json = json.dumps(topics[:12], ensure_ascii=False)
            row.active_members_json = json.dumps(members[:20], ensure_ascii=False)
            row.emotion_json = json.dumps(emotion, ensure_ascii=False)
            row.source_message_ids_json = json.dumps(source_message_ids[:200], ensure_ascii=False)
            row.message_count = message_count
            row.model = model[:128]
            row.updated_at = now

    def recent_timeline(self, account_id: str, group_id: str, *, days: int = 3) -> list[dict]:
        """Newest-first timeline summaries; the caller decides how many to use."""

        from .memory_schema import GroupTimelineRow
        with self.Session() as session:
            rows = list(session.scalars(
                select(GroupTimelineRow)
                .where(GroupTimelineRow.account_id == account_id, GroupTimelineRow.group_id == group_id)
                .order_by(GroupTimelineRow.day.desc())
                .limit(max(1, days))
            ))
        return [
            {
                "day": row.day,
                "summary": row.summary,
                "topics": json.loads(row.topics_json or "[]"),
                "active_members": json.loads(row.active_members_json or "[]"),
                "emotion": json.loads(row.emotion_json or "{}"),
                "message_count": row.message_count,
            }
            for row in rows
        ]

    def timeline_days(self, account_id: str, group_id: str, *, limit: int = 400) -> list[str]:
        from .memory_schema import GroupTimelineRow
        with self.Session() as session:
            return list(session.scalars(
                select(GroupTimelineRow.day)
                .where(GroupTimelineRow.account_id == account_id, GroupTimelineRow.group_id == group_id)
                .order_by(GroupTimelineRow.day.desc())
                .limit(limit)
            ))

    def upsert_member_relation(
        self,
        *,
        account_id: str,
        group_id: str,
        member_id: str,
        other_member_id: str,
        relation: str,
        strength: float,
        evidence: list[str],
    ) -> None:
        from .memory_schema import MemberRelationRow
        from .memory_jobs import scope_transaction
        if _is_self_member(account_id, member_id) or _is_self_member(account_id, other_member_id):
            return
        with scope_transaction(self, account_id, group_id) as session:
            row = session.get(MemberRelationRow, (account_id, group_id, member_id, other_member_id))
            if row is None:
                row = MemberRelationRow(account_id=account_id, group_id=group_id, member_id=member_id, other_member_id=other_member_id)
                session.add(row)
            row.relation = relation[:64]
            row.strength = max(0.0, min(1.0, float(strength)))
            row.evidence_json = json.dumps(evidence[:20], ensure_ascii=False)
            row.updated_at = utc_now()

    def member_relations(self, account_id: str, group_id: str, member_id: str, *, limit: int = 6) -> list[dict]:
        """Both directions of ``member_id``'s relations, strongest first."""

        from .memory_schema import MemberRelationRow
        with self.Session() as session:
            rows = list(session.scalars(
                select(MemberRelationRow).where(
                    MemberRelationRow.account_id == account_id,
                    MemberRelationRow.group_id == group_id,
                    or_(MemberRelationRow.member_id == member_id, MemberRelationRow.other_member_id == member_id),
                )
            ))
        relations = []
        for row in rows:
            relations.append({
                "member_id": row.member_id,
                "other_member_id": row.other_member_id,
                # ``counterparty`` is the member on the other side *from the
                # caller's point of view*; the stored direction is preserved.
                "counterparty": row.other_member_id if row.member_id == member_id else row.member_id,
                "relation": row.relation,
                "strength": row.strength,
                "evidence": json.loads(row.evidence_json or "[]"),
            })
        relations.sort(key=lambda item: -item["strength"])
        return relations[:limit]

    def top_relations(self, account_id: str, group_id: str, *, limit: int = 4) -> list[dict]:
        """Strongest relations in the group, newest first on ties."""

        from .memory_schema import MemberRelationRow
        with self.Session() as session:
            rows = list(session.scalars(
                select(MemberRelationRow)
                .where(MemberRelationRow.account_id == account_id, MemberRelationRow.group_id == group_id)
                .order_by(MemberRelationRow.strength.desc(), MemberRelationRow.updated_at.desc())
                .limit(limit)
            ))
        return [
            {
                "member_id": row.member_id,
                "other_member_id": row.other_member_id,
                "relation": row.relation,
                "strength": row.strength,
                "evidence": json.loads(row.evidence_json or "[]"),
            }
            for row in rows
        ]

    def remember_member_address(
        self,
        *,
        account_id: str,
        group_id: str,
        member_id: str,
        address: str,
        origin_message_id: str = "",
        source_text: str = "",
        now: datetime | None = None,
    ) -> str:
        """Save the address a close member asked to be called by.

        The value is sanitized to a short label before storage; an invalid or
        empty request does not overwrite the previous address.
        """

        from .memory_schema import MemberAddressRow
        from .social import sanitize_address

        cleaned = sanitize_address(address)
        if not cleaned:
            return ""
        stamp = now or utc_now()
        with self.Session.begin() as session:
            row = session.get(MemberAddressRow, (account_id, group_id, member_id))
            if row is None:
                row = MemberAddressRow(
                    account_id=account_id,
                    group_id=group_id,
                    member_id=member_id,
                    address=cleaned,
                    origin_message_id=origin_message_id,
                    source_text=str(source_text or "")[:240],
                    created_at=stamp,
                    updated_at=stamp,
                )
                session.add(row)
            else:
                row.address = cleaned
                row.origin_message_id = origin_message_id or row.origin_message_id
                row.source_text = str(source_text or "")[:240] or row.source_text
                row.updated_at = stamp
        return cleaned

    def member_address(self, account_id: str, group_id: str, member_id: str) -> str:
        """The member-chosen address, or an empty string when none is stored."""

        from .memory_schema import MemberAddressRow
        with self.Session() as session:
            row = session.get(MemberAddressRow, (account_id, group_id, member_id))
        return row.address if row is not None else ""

    def user_profile(
        self,
        account_id: str,
        group_id: str,
        member_id: str,
    ) -> dict[str, object]:
        with self.Session() as session:
            row = session.scalar(
                select(UserProfileRow).where(
                    UserProfileRow.account_id == account_id,
                    UserProfileRow.group_id == group_id,
                    UserProfileRow.member_id == member_id,
                )
            )
        if row is None:
            return {
                "scope": {
                    "account_id": account_id,
                    "group_id": group_id,
                    "member_id": member_id,
                },
                "preferences": [],
                "interests": [],
                "tools": [],
                "expression_habits": [],
                "work_style": [],
                "constraints": [],
                "confirmed_info": [],
                "task_requirements": [],
                "corrections": [],
                "behavior": _empty_member_behavior(),
                "source_message_ids": [],
                "updated_at": None,
                "history": [],
            }
        return self._profile_dict(
            row.profile_json,
            scope={"account_id": row.account_id, "group_id": row.group_id, "member_id": row.member_id},
            source_message_ids=row.source_message_ids_json,
            updated_at=row.updated_at,
            history_json=row.history_json,
        )

    def upsert_user_profile(
        self,
        *,
        account_id: str,
        group_id: str,
        member_id: str,
        member_name: str = "",
        patch: dict[str, object],
        source_message_ids: Iterable[str] = (),
    ) -> dict[str, object]:
        if _is_unknown_member(member_id) or _is_self_member(account_id, member_id) or not patch:
            return self.user_profile(account_id, group_id, member_id)
        now = utc_now()
        ids = [str(value) for value in source_message_ids if str(value).strip()]
        with self.Session.begin() as session:
            row = session.scalar(
                select(UserProfileRow).where(
                    UserProfileRow.account_id == account_id,
                    UserProfileRow.group_id == group_id,
                    UserProfileRow.member_id == member_id,
                )
            )
            created = row is None
            if row is None:
                row = UserProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    member_id=member_id,
                    member_name=member_name,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            previous = _json_dict(row.profile_json)
            merged = _merge_profile(previous, patch)
            # Profile lists should represent the current active facts.  The
            # fact ledger retains superseded values for audit/history, so a
            # preference update does not leave the old value in the prompt.
            fact_field_types = {
                "preferences": "preference",
                "interests": "interest",
                "tools": "tool",
                "constraints": "constraint",
            }
            for field, memory_type in fact_field_types.items():
                if field not in patch:
                    continue
                active_rows = list(
                    session.scalars(
                        select(FactRow)
                        .where(
                            FactRow.account_id == account_id,
                            FactRow.group_id == group_id,
                            FactRow.member_id == member_id,
                            FactRow.memory_type == memory_type,
                            FactRow.status.in_(
                                ["candidate", "active", "confirmed", "inferred"]
                            ),
                            FactRow.valid_until.is_(None),
                        )
                        .order_by(FactRow.updated_at.asc(), FactRow.id.asc())
                    )
                )
                merged[field] = _bounded_unique(
                    [row.content for row in active_rows],
                    100,
                )
            history = _json_list(row.history_json)
            history.append(
                {
                    "updated_at": now.isoformat(),
                    "source_message_ids": ids[:20],
                    "changes": patch,
                    "previous": previous,
                }
            )
            row.profile_json = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
            row.history_json = json.dumps(history[-100:], ensure_ascii=False, separators=(",", ":"))
            row.source_message_ids_json = json.dumps(
                _bounded_unique(_json_list(row.source_message_ids_json) + ids, 200),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            row.member_name = member_name or row.member_name
            row.updated_at = now
            result = self._profile_dict(
                row.profile_json,
                scope={"account_id": account_id, "group_id": group_id, "member_id": member_id},
                source_message_ids=row.source_message_ids_json,
                updated_at=row.updated_at,
                history_json=row.history_json,
            )
        return result

    def ai_profile(self, account_id: str, group_id: str) -> dict[str, object]:
        with self.Session() as session:
            row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == account_id,
                    AIProfileRow.group_id == group_id,
                )
            )
        if row is None:
            return {
                "scope": {"account_id": account_id, "group_id": group_id},
                "common_topics": [],
                "common_vocabulary": [],
                "reply_length": "unknown",
                "formality": "unknown",
                "humor": "unknown",
                "technicality": "unknown",
                "interaction_patterns": [],
                "accepted_phrasing": [],
                "avoid_phrasing": [],
                "rules": [],
                "evidence_message_ids": [],
                "message_count": 0,
                "updated_at": None,
                "history": [],
            }
        return self._profile_dict(
            row.profile_json,
            scope={"account_id": row.account_id, "group_id": row.group_id},
            source_message_ids=row.source_message_ids_json,
            updated_at=row.updated_at,
            history_json=row.history_json,
        )

    def observe_ai_profile(
        self,
        event: MessageEvent,
        message_id: str | None,
    ) -> dict[str, object]:
        """Update one aggregate group profile; no member facts are written."""

        patch = extract_ai_profile_patch(
            event.text,
            message_id,
            ai_aliases=self.settings.wechat_ai_aliases,
        )
        if not patch:
            return self.ai_profile(event.account_id, event.group_id)
        now = utc_now()
        ids = [str(message_id or event.external_message_id or "").strip()]
        ids = [value for value in ids if value]
        with self.Session.begin() as session:
            row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == event.account_id,
                    AIProfileRow.group_id == event.group_id,
                )
            )
            created = row is None
            if row is None:
                row = AIProfileRow(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    group_name=event.group_name,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            previous = _json_dict(row.profile_json)
            profile = _merge_ai_profile(previous, patch)
            if isinstance(previous.get("synthesized"), dict) and previous["synthesized"]:
                profile["synthesized"] = previous["synthesized"]
                profile["synthesized_at"] = previous.get("synthesized_at")
            history = _json_list(row.history_json)
            # Keep an audit trail of *changes*, but not a full copy of the
            # previous profile on every message.  The old shape grew to tens of
            # thousands of characters and leaked straight into the prompt.
            history.append(
                {
                    "updated_at": now.isoformat(),
                    "source_message_ids": ids,
                    "changes": _compact_history_changes(patch),
                }
            )
            row.profile_json = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
            row.history_json = json.dumps(history[-100:], ensure_ascii=False, separators=(",", ":"))
            row.source_message_ids_json = json.dumps(
                _bounded_unique(_json_list(row.source_message_ids_json) + ids, 500),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            row.group_name = event.group_name or row.group_name
            row.updated_at = now
            result = self._profile_dict(
                row.profile_json,
                scope={"account_id": event.account_id, "group_id": event.group_id},
                source_message_ids=row.source_message_ids_json,
                updated_at=row.updated_at,
                history_json=row.history_json,
            )
        return result

    def task_profile(
        self,
        account_id: str,
        group_id: str,
        task_id: str,
        member_id: str = "",
    ) -> dict[str, object]:
        with self.Session() as session:
            row = session.scalar(
                select(TaskProfileRow).where(
                    TaskProfileRow.account_id == account_id,
                    TaskProfileRow.group_id == group_id,
                    TaskProfileRow.task_id == task_id,
                    TaskProfileRow.member_id == member_id,
                )
            )
        if row is None:
            return {
                "scope": {
                    "account_id": account_id,
                    "group_id": group_id,
                    "task_id": task_id,
                    **({"member_id": member_id} if member_id else {}),
                },
                "goal": "",
                "scope_text": "",
                "requirements": [],
                "decisions": [],
                "progress": [],
                "completed": [],
                "blockers": [],
                "people": [],
                "related_message_ids": [],
                "next_action": "",
                "updated_at": None,
                "history": [],
            }
        return self._profile_dict(
            row.profile_json,
            scope={
                "account_id": row.account_id,
                "group_id": row.group_id,
                "task_id": row.task_id,
                **({"member_id": row.member_id} if row.member_id else {}),
            },
            source_message_ids=row.source_message_ids_json,
            updated_at=row.updated_at,
            history_json=row.history_json,
        )

    def upsert_task_profile(
        self,
        *,
        account_id: str,
        group_id: str,
        task_id: str,
        patch: dict[str, object],
        source_message_ids: Iterable[str] = (),
        member_id: str = "",
    ) -> dict[str, object]:
        if not task_id.strip() or not patch:
            return self.task_profile(account_id, group_id, task_id, member_id)
        now = utc_now()
        ids = [str(value) for value in source_message_ids if str(value).strip()]
        with self.Session.begin() as session:
            row = session.scalar(
                select(TaskProfileRow).where(
                    TaskProfileRow.account_id == account_id,
                    TaskProfileRow.group_id == group_id,
                    TaskProfileRow.task_id == task_id,
                    TaskProfileRow.member_id == member_id,
                )
            )
            if row is None:
                row = TaskProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    task_id=task_id,
                    member_id=member_id,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            previous = _json_dict(row.profile_json)
            merged = _merge_task_profile(previous, patch)
            merged["related_message_ids"] = _bounded_unique(
                _json_list(previous.get("related_message_ids")) + ids,
                500,
            )
            history = _json_list(row.history_json)
            history.append(
                {
                    "updated_at": now.isoformat(),
                    "source_message_ids": ids[:20],
                    "changes": patch,
                    "previous": previous,
                }
            )
            row.profile_json = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
            row.history_json = json.dumps(history[-200:], ensure_ascii=False, separators=(",", ":"))
            row.source_message_ids_json = json.dumps(
                _bounded_unique(_json_list(row.source_message_ids_json) + ids, 500),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            row.updated_at = now
            result = self._profile_dict(
                row.profile_json,
                scope={
                    "account_id": account_id,
                    "group_id": group_id,
                    "task_id": task_id,
                    **({"member_id": member_id} if member_id else {}),
                },
                source_message_ids=row.source_message_ids_json,
                updated_at=row.updated_at,
                history_json=row.history_json,
            )
        return result

    def latest_task_id(self, account_id: str, group_id: str) -> str | None:
        with self.Session() as session:
            row = session.scalar(
                select(TaskProfileRow)
                .where(
                    TaskProfileRow.account_id == account_id,
                    TaskProfileRow.group_id == group_id,
                )
                .order_by(TaskProfileRow.updated_at.desc())
                .limit(1)
            )
            return row.task_id if row else None

    @staticmethod
    def _profile_dict(
        profile_json: str,
        *,
        scope: dict[str, str],
        source_message_ids: str,
        updated_at: datetime | None,
        history_json: str,
    ) -> dict[str, object]:
        profile = _json_dict(profile_json)
        profile["scope"] = scope
        profile["source_message_ids"] = _json_list(source_message_ids)
        profile["updated_at"] = _aware(updated_at).isoformat() if updated_at else None
        profile["history"] = _json_list(history_json)
        return profile

    def claim_processing(
        self,
        event: MessageEvent,
        question_hash: str,
        lease_seconds: int | None = None,
    ) -> ProcessingClaim:
        """Claim an inbound event or record why it must be skipped.

        ``event_key`` prevents the same visible UI row from being processed
        twice.  ``question_hash`` prevents a newly observed copy of a question
        from receiving another answer after a successful send.  Only ``sent``
        records block a question; skipped/failed records remain retryable.
        """
        event_key = self.hash_event(event)
        now = utc_now()
        lease = lease_seconds or getattr(self.settings, "wechat_processing_lease_seconds", 300)
        try:
            with self.Session.begin() as session:
                existing = session.scalar(
                    select(ReplyRecordRow).where(ReplyRecordRow.event_key == event_key)
                )
                if existing:
                    retryable = existing.status == "failed" or existing.reason in {
                        "sending_disabled",
                        "stop_file_present",
                        "circuit_breaker_rate",
                    }
                    stale = (
                        existing.status == "processing"
                        and (now - _aware(existing.updated_at)).total_seconds() > lease
                    )
                    if stale or retryable:
                        existing.status = "processing"
                        existing.reason = "processing_lease_expired" if stale else "retry_claimed"
                        existing.attempt_count += 1
                        existing.updated_at = now
                        return ProcessingClaim(
                            event_key,
                            question_hash,
                            True,
                            False,
                            "processing",
                            "processing_lease_expired" if stale else "retry_claimed",
                        )
                    return ProcessingClaim(
                        event_key,
                        question_hash,
                        False,
                        False,
                        existing.status,
                        "event_already_processed",
                    )

                answered = session.scalar(
                    select(ReplyRecordRow)
                    .where(
                        ReplyRecordRow.account_id == event.account_id,
                        ReplyRecordRow.group_id == event.group_id,
                        ReplyRecordRow.member_id == event.member_id,
                        ReplyRecordRow.question_hash == question_hash,
                        ReplyRecordRow.status == "sent",
                    )
                    .order_by(ReplyRecordRow.answered_at.desc())
                    .limit(1)
                )
                if answered:
                    session.add(
                        ReplyRecordRow(
                            event_key=event_key,
                            question_hash=question_hash,
                            account_id=event.account_id,
                            group_id=event.group_id,
                            group_name=event.group_name,
                            member_id=event.member_id,
                            member_name=event.member_name,
                            text=event.text,
                            status="skipped",
                            reason="question_already_answered",
                            reply_text=None,
                            source=event.source,
                            attempt_count=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    return ProcessingClaim(
                        event_key, question_hash, False, True, "skipped", "question_already_answered"
                    )

                session.add(
                    ReplyRecordRow(
                        event_key=event_key,
                        question_hash=question_hash,
                        account_id=event.account_id,
                        group_id=event.group_id,
                        group_name=event.group_name,
                        member_id=event.member_id,
                        member_name=event.member_name,
                        text=event.text,
                        status="processing",
                        reason="claimed",
                        reply_text=None,
                        source=event.source,
                        attempt_count=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return ProcessingClaim(event_key, question_hash, True, True, "processing", "claimed")
        except IntegrityError:
            # Another poller won the unique event_key race.  Treat that as a
            # normal duplicate and leave the winner's state untouched.
            with self.Session() as session:
                existing = session.scalar(
                    select(ReplyRecordRow).where(ReplyRecordRow.event_key == event_key)
                )
            if existing:
                return ProcessingClaim(
                    event_key, question_hash, False, False, existing.status, "event_already_processed"
                )
            raise

    def finish_processing(
        self,
        event_key: str,
        status: str,
        reason: str,
        reply_text: str | None = None,
        *,
        basis: str = "",
        rules: dict[str, object] | None = None,
        origin: str = "",
    ) -> None:
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(select(ReplyRecordRow).where(ReplyRecordRow.event_key == event_key))
            if not row:
                return
            row.status = status
            row.reason = reason
            row.reply_text = reply_text
            row.updated_at = now
            if basis:
                row.basis = basis
            if origin:
                row.origin = origin
            if rules is not None:
                row.rules_json = json.dumps(rules, ensure_ascii=False, separators=(",", ":"), default=str)
            if status == "sent":
                row.answered_at = now

    def processing_summary(self) -> dict[str, int]:
        with self.Session() as session:
            rows = session.execute(
                select(ReplyRecordRow.status, func.count(ReplyRecordRow.id)).group_by(ReplyRecordRow.status)
            )
            summary = {status: int(count) for status, count in rows}
            summary.setdefault("processing", 0)
            return summary

    def recover_stale_processing(self, lease_seconds: int | None = None, *, now: datetime | None = None) -> int:
        """Close claims left behind by a previous agent process.

        ``reply_records.processing`` is an idempotency claim, not a durable
        worker queue.  If the process exits between the claim and the terminal
        ``sent``/``skipped``/``failed`` write, the row would otherwise keep the
        overview's queue counter at 1 forever because the original message is
        not replayed.  Startup recovery runs before any new worker exists, so
        rows older than the processing lease are safe to close here.
        """

        moment = now or utc_now()
        lease = max(30, int(lease_seconds or getattr(self.settings, "wechat_processing_lease_seconds", 300)))
        cutoff = moment - timedelta(seconds=lease)
        with self.Session.begin() as session:
            result = session.execute(
                update(ReplyRecordRow)
                .where(
                    ReplyRecordRow.status == "processing",
                    ReplyRecordRow.updated_at < cutoff,
                )
                .values(status="failed", reason="processing_interrupted", updated_at=moment)
            )
            return int(result.rowcount or 0)

    def queue_details(self, *, limit: int = 50, lease_seconds: int | None = None) -> dict[str, object]:
        """Return only the data needed by the overview queue drill-down.

        The console deliberately exposes no account IDs, event hashes, or raw
        media here.  It shows what the operator needs to understand a stuck
        item: conversation, speaker, text preview, age, attempts, and reason.
        """

        from .reply_jobs import ReplyJobRow

        moment = utc_now()
        lease = max(30, int(lease_seconds or getattr(self.settings, "wechat_processing_lease_seconds", 300)))
        cap = max(1, min(int(limit), 100))
        with self.Session() as session:
            processing = list(session.scalars(
                select(ReplyRecordRow)
                .where(ReplyRecordRow.status == "processing")
                .order_by(ReplyRecordRow.updated_at.desc())
                .limit(cap)
            ))
            processing_items: list[dict[str, object]] = []
            for row in processing:
                age = max(0, int((moment - _aware(row.updated_at)).total_seconds()))
                processing_items.append({
                    "id": int(row.id),
                    "group_name": str(row.group_name or ""),
                    "member_name": str(row.member_name or ""),
                    "text": (str(row.text or "")[:500]),
                    "reason": str(row.reason or ""),
                    "source": str(row.source or ""),
                    "attempt_count": int(row.attempt_count or 0),
                    "created_at": _aware(row.created_at).isoformat(),
                    "updated_at": _aware(row.updated_at).isoformat(),
                    "age_seconds": age,
                    "stale": age > lease,
                })
            job_rows = session.execute(
                select(ReplyJobRow, MessageRow.group_name, MessageRow.member_name, MessageRow.text)
                .join(MessageRow, ReplyJobRow.message_id == MessageRow.id)
                .where(ReplyJobRow.state.in_(["pending", "running"]))
                .order_by(ReplyJobRow.next_retry_at, ReplyJobRow.id)
                .limit(cap)
            ).all()
            job_items: list[dict[str, object]] = []
            for job, group_name, member_name, text in job_rows:
                age = max(0, int((moment - _aware(job.updated_at)).total_seconds()))
                job_items.append({
                    "id": int(job.id),
                    "state": str(job.state or ""),
                    "group_name": str(group_name or ""),
                    "member_name": str(member_name or ""),
                    "text": (str(text or "")[:500]),
                    "basis": str(job.basis or ""),
                    "origin": str(job.origin or ""),
                    "attempts": int(job.attempts or 0),
                    "created_at": _aware(job.created_at).isoformat(),
                    "updated_at": _aware(job.updated_at).isoformat(),
                    "next_retry_at": _aware(job.next_retry_at).isoformat() if job.next_retry_at else None,
                    "last_error": str(job.last_error or "")[:300],
                    "age_seconds": age,
                })
            counts_rows = session.execute(
                select(ReplyJobRow.state, func.count(ReplyJobRow.id))
                .where(ReplyJobRow.state.in_(["pending", "running"]))
                .group_by(ReplyJobRow.state)
            )
            job_counts = {state: int(count) for state, count in counts_rows}
            interrupted = list(session.scalars(
                select(ReplyRecordRow)
                .where(
                    ReplyRecordRow.status == "failed",
                    ReplyRecordRow.reason.in_(["processing_interrupted", "processing_lease_expired"]),
                )
                .order_by(ReplyRecordRow.updated_at.desc())
                .limit(min(10, cap))
            ))
            interrupted_items = [{
                "id": int(row.id),
                "group_name": str(row.group_name or ""),
                "member_name": str(row.member_name or ""),
                "text": (str(row.text or "")[:300]),
                "reason": str(row.reason or ""),
                "created_at": _aware(row.created_at).isoformat(),
                "updated_at": _aware(row.updated_at).isoformat(),
                "attempt_count": int(row.attempt_count or 0),
            } for row in interrupted]
        return {
            "checked_at": moment.isoformat(),
            "lease_seconds": lease,
            "counts": {
                "processing": len(processing_items),
                "stale": sum(1 for item in processing_items if item["stale"]),
                "pending_jobs": job_counts.get("pending", 0),
                "running_jobs": job_counts.get("running", 0),
            },
            "processing": processing_items,
            "jobs": job_items,
            "recent_interrupted": interrupted_items,
        }

    def proactive_send_times(self, account_id: str, group_id: str, *, since: datetime) -> list[datetime]:
        """Count verified unsolicited replies in this group, including after restart."""

        with self.Session() as session:
            values = session.scalars(
                select(ReplyRecordRow.answered_at)
                .where(
                    ReplyRecordRow.account_id == account_id,
                    ReplyRecordRow.group_id == group_id,
                    ReplyRecordRow.status == "sent",
                    ReplyRecordRow.reason.startswith("proactive_", autoescape=True),
                    ReplyRecordRow.answered_at >= since,
                )
                .order_by(ReplyRecordRow.answered_at.desc())
            )
            return [_aware(value) for value in values]

    def recent_voice_clarification(self, account_id: str, group_id: str, *, since: datetime) -> bool:
        with self.Session() as session:
            return session.scalar(select(ReplyRecordRow.id).where(
                ReplyRecordRow.account_id == account_id, ReplyRecordRow.group_id == group_id,
                ReplyRecordRow.status == "sent", ReplyRecordRow.reason.in_(["voice_unintelligible", "voice_unavailable"]),
                ReplyRecordRow.answered_at >= since,
            ).limit(1)) is not None

    def recent_reply_records(self, limit: int = 50) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(ReplyRecordRow)
                    .order_by(ReplyRecordRow.updated_at.desc())
                    .limit(max(1, min(limit, 200)))
                )
            )
        return [
            {
                "event_key": row.event_key,
                "question_hash": row.question_hash,
                "group_name": row.group_name,
                "member_name": row.member_name,
                "text": row.text,
                "status": row.status,
                "reason": row.reason,
                "basis": row.basis,
                "origin": row.origin,
                "rules": _json_dict(row.rules_json),
                "reply_text": row.reply_text,
                "attempt_count": row.attempt_count,
                "created_at": _aware(row.created_at).isoformat(),
                "updated_at": _aware(row.updated_at).isoformat(),
                "answered_at": _aware(row.answered_at).isoformat() if row.answered_at else None,
            }
            for row in rows
        ]

    def matches_recent_sent_reply(
        self,
        *,
        account_id: str,
        group_id: str,
        text: str,
        limit: int = 200,
    ) -> bool:
        """Detect a bot echo when UIA cannot identify the local sender.

        The reply ledger survives listener restarts, unlike the in-memory
        outbound cache.  Restricting the lookup to the same account and group
        keeps a copied sentence from another conversation from becoming a
        global self-message signal.
        """

        wanted = _normalize_text(text)
        if not wanted:
            return False
        with self.Session() as session:
            rows = session.scalars(
                select(ReplyRecordRow.reply_text)
                .where(
                    ReplyRecordRow.account_id == account_id,
                    ReplyRecordRow.group_id == group_id,
                    ReplyRecordRow.status == "sent",
                    ReplyRecordRow.reply_text.is_not(None),
                )
                .order_by(ReplyRecordRow.updated_at.desc())
                .limit(max(1, min(int(limit), 500)))
            )
            return any(_normalize_text(value or "") == wanted for value in rows)

    def recent_messages(
        self,
        *,
        account_id: str,
        group_id: str,
        limit: int,
        max_chars: int,
        exclude_message_hash: str | None = None,
        query: str | None = None,
        thread_id: str | None = None,
        task_id: str | None = None,
        before_message_id: int | None = None,
        before_time: datetime | None = None,
    ) -> list[ConversationMessage]:
        """Return a bounded, oldest-first relevant window from one group.

        Raw message retention already supplies the durable conversation window.
        When a query or thread/task scope is supplied, unrelated rows are
        filtered before they enter the model context.  The method never
        crosses account or group boundaries and excludes the triggering row.
        """

        bounded_limit = max(0, min(int(limit), 50))
        bounded_chars = max(500, min(int(max_chars), 30_000))
        if bounded_limit == 0 or bounded_chars <= 0:
            return []
        with self.Session() as session:
            statement = (
                select(MessageRow)
                .where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.lifecycle_status == "active",
                )
            )
            statement = self._before_message(statement, before_message_id, before_time)
            statement = statement.order_by(MessageRow.observed_at.desc(), MessageRow.id.desc()).limit(
                max(bounded_limit + 1, 300 if str(query or "").strip() else bounded_limit + 1)
            )
            rows = list(session.scalars(statement))

        thread_value = str(thread_id or "").strip()
        task_value = str(task_id or "").strip()
        selected: list[ConversationMessage] = []
        used = 0
        # Fill the budget with the newest rows, then restore chronology. Filling
        # oldest-first could drop the message immediately preceding a summons.
        for row in rows:
            if exclude_message_hash and row.message_hash == exclude_message_hash:
                continue
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            text = self._row_text(row)
            if not text:
                continue
            if str(query or "").strip():
                same_thread = bool(thread_value and row.thread_id and row.thread_id == thread_value)
                same_task = bool(task_value and row.task_id and row.task_id == task_value)
                lexical_score = _context_similarity(query or "", text)
                has_image = bool(_json_list(getattr(row, "image_paths_json", "[]")))
                if not (same_thread or same_task or lexical_score >= _CONTEXT_SIMILARITY_THRESHOLD or has_image):
                    continue
            remaining = bounded_chars - used
            if remaining <= 0:
                break
            if len(text) > remaining:
                text = text[:remaining].rstrip()
            if not text:
                continue
            selected.append(
                ConversationMessage(
                    sender_name=row.member_name or "unknown",
                    text=text,
                    observed_at=_aware(row.observed_at),
                    sender_id=row.member_id or "",
                    message_id=row.external_message_id or str(row.id),
                    thread_id=row.thread_id or "",
                    task_id=row.task_id or "",
                    image_paths=_json_list(getattr(row, "image_paths_json", "[]")),
                    source_role="assistant" if row.is_self else row.source_role,
                    reply_to_sender=getattr(row, "reply_to_sender", "") or "",
                    reply_to_text=(getattr(row, "reply_to_text", "") or "")[:200],
                )
            )
            used += len(text)
            if len(selected) >= bounded_limit:
                break
        return list(reversed(selected))

    @staticmethod
    def _before_message(statement, before_message_id: int | None, before_time: datetime | None):
        if before_message_id:
            statement = statement.where(MessageRow.id < before_message_id)
        if before_time is not None:
            statement = statement.where(MessageRow.observed_at <= before_time)
        return statement

    def preceding_member_message(self, event: MessageEvent, *, within_seconds: int = 600) -> MessageEvent | None:
        """Last meaningful message by this sender before their standalone @.

        Scope, source time and ingestion bounds apply before the limit. Nothing
        here changes the trigger identity or pretends there was an explicit quote.
        """
        from .reply_context import is_mention_only

        if not event.member_id or event.member_id.casefold() in {"unknown", "未知成员"}:
            return None
        with self.Session() as session:
            statement = select(MessageRow).where(
                MessageRow.account_id == event.account_id,
                MessageRow.group_id == event.group_id,
                MessageRow.member_id == event.member_id,
                MessageRow.is_self.is_(False),
                MessageRow.source_role == "human",
                MessageRow.lifecycle_status == "active",
                MessageRow.text_kind != "system",
                MessageRow.message_hash != self.hash_event(event),
                MessageRow.observed_at >= event.observed_at - timedelta(seconds=within_seconds),
            )
            statement = self._before_message(statement, event.ingest_sequence, event.observed_at)
            rows = list(session.scalars(statement.order_by(MessageRow.observed_at.desc(), MessageRow.id.desc()).limit(20)))
        for row in rows:
            text = self._row_text(row)
            if not text or is_mention_only(text):
                continue
            metadata = _json_dict(row.media_metadata_json)
            if row.message_type == "voice" and metadata.get("voice_transcription", {}).get("status") != "ok":
                return None
            if row.message_type not in {"text", "image", "quote", "appmsg", "voice", "sticker"}:
                return None
            quote_data = _json_dict(row.reply_to_json)
            quote = None
            if quote_data:
                if quote_data.get("created_at"):
                    quote_data["created_at"] = datetime.fromisoformat(quote_data["created_at"])
                quote = QuotedMessage(**quote_data)
            return MessageEvent(
                account_id=row.account_id, group_id=row.group_id, group_name=row.group_name,
                member_id=row.member_id, member_name=row.member_name, text=text,
                observed_at=_aware(row.observed_at), external_message_id=row.external_message_id,
                ingest_sequence=row.id, source=row.source, source_role=row.source_role,
                source_created_at=_aware(row.source_created_at) if row.source_created_at else None,
                message_type=row.message_type, reply_to=quote,
                image_paths=_json_list(row.image_paths_json), media_metadata=metadata,
                thread_id=row.thread_id, task_id=row.task_id,
            )
        return None

    def proactive_speech_block(
        self,
        event: MessageEvent,
        *,
        gap_seconds: float = 60.0,
        max_messages: int = 3,
    ) -> list[ConversationMessage]:
        """Same-sender rows that precede a proactive candidate as one utterance.

        Only the trigger's own sender is considered, and only text-bearing rows:
        the model needs the sentence that introduced the fragment, not the
        member's last picture.  Assistant rows are read too, so the run cannot
        cross the character's own reply; they are dropped by the selector.  The
        trigger row itself is excluded because the caller passes it as the
        current question.
        """

        from .proactive_block import select_speech_block

        member_id = str(getattr(event, "member_id", "") or "").strip()
        if not member_id or member_id.casefold() in {"unknown", "未知成员"}:
            return []
        try:
            bounded_gap = max(1.0, min(float(gap_seconds), 600.0))
            bounded_max = max(1, min(int(max_messages), 5))
        except (TypeError, ValueError):
            return []
        if bounded_max <= 1:
            return []
        observed_at = getattr(event, "observed_at", None)
        if observed_at is None:
            return []
        horizon = observed_at - timedelta(seconds=bounded_gap * bounded_max)
        with self.Session() as session:
            # The member's own text rows form the block; a stored assistant row
            # is fetched only so the selector can stop the burst there.
            statement = select(MessageRow).where(
                MessageRow.account_id == event.account_id,
                MessageRow.group_id == event.group_id,
                MessageRow.lifecycle_status == "active",
                MessageRow.text_kind != "system",
                MessageRow.message_hash != self.hash_event(event),
                MessageRow.observed_at >= horizon,
                or_(
                    and_(
                        MessageRow.member_id == member_id,
                        MessageRow.message_type.in_(("text", "quote")),
                    ),
                    MessageRow.is_self.is_(True),
                ),
            )
            statement = self._before_message(statement, event.ingest_sequence, observed_at)
            rows = list(
                session.scalars(
                    statement.order_by(MessageRow.observed_at.desc(), MessageRow.id.desc()).limit(
                        bounded_max * 4
                    )
                )
            )
        messages: list[ConversationMessage] = []
        for row in rows:
            text = self._row_text(row).strip()
            if not text:
                continue
            messages.append(
                ConversationMessage(
                    sender_name=row.member_name or "unknown",
                    sender_id=row.member_id or "",
                    text=text,
                    observed_at=_aware(row.observed_at),
                    message_id=row.external_message_id or str(row.id),
                    thread_id=row.thread_id or "",
                    task_id=row.task_id or "",
                    image_paths=_json_list(getattr(row, "image_paths_json", "[]")),
                    source_role="assistant" if row.is_self else (row.source_role or "human"),
                    reply_to_sender=getattr(row, "reply_to_sender", "") or "",
                    reply_to_text=(getattr(row, "reply_to_text", "") or "")[:200],
                )
            )
        return select_speech_block(
            messages,
            trigger_at=_aware(observed_at),
            gap_seconds=bounded_gap,
            max_messages=bounded_max,
        )

    def related_messages(
        self,
        *,
        account_id: str,
        group_id: str,
        query: str,
        limit: int = 8,
        max_chars: int = 3500,
        exclude_message_hash: str | None = None,
        thread_id: str | None = None,
        task_id: str | None = None,
        before_message_id: int | None = None,
        before_time: datetime | None = None,
    ) -> list[ConversationMessage]:
        """Retrieve same-group messages related to the current question.

        This is intentionally lexical and bounded; the semantic memory path
        handles long-term facts while raw messages stay a short-lived source
        context.  Every result retains sender and timestamp.
        """

        bounded_limit = max(0, min(int(limit), 30))
        bounded_chars = int(max_chars)
        thread_value = str(thread_id or "").strip()
        task_value = str(task_id or "").strip()
        if bounded_limit == 0 or bounded_chars <= 0:
            return []
        if not _context_tokens(query) and not thread_value and not task_value:
            return []
        with self.Session() as session:
            statement = select(MessageRow).where(
                MessageRow.account_id == account_id, MessageRow.group_id == group_id,
                MessageRow.lifecycle_status == "active",
            )
            statement = self._before_message(statement, before_message_id, before_time)
            rows = list(session.scalars(statement.order_by(MessageRow.observed_at.desc(), MessageRow.id.desc()).limit(300)))
        scored: list[tuple[float, MessageRow]] = []
        for row in rows:
            if exclude_message_hash and row.message_hash == exclude_message_hash:
                continue
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            text = self._row_text(row)
            if not text:
                continue
            same_thread = bool(thread_value and row.thread_id and row.thread_id == thread_value)
            same_task = bool(task_value and row.task_id and row.task_id == task_value)
            if same_thread:
                scope_bonus = 0.35
            elif same_task:
                scope_bonus = 0.3
            else:
                scope_bonus = 0.0
            lexical_score = _context_similarity(query, text)
            has_image = bool(_json_list(getattr(row, "image_paths_json", "[]")))
            if not (same_thread or same_task) and lexical_score < _CONTEXT_SIMILARITY_THRESHOLD and not has_image:
                continue
            age_days = max(0.0, (utc_now() - _aware(row.observed_at)).total_seconds() / 86400)
            scored.append((lexical_score + scope_bonus + math.exp(-age_days / 14.0) * 0.15, row))
        scored.sort(key=lambda item: (item[0], _aware(item[1].observed_at)), reverse=True)
        selected: list[ConversationMessage] = []
        used = 0
        for _, row in scored[:bounded_limit]:
            text = self._row_text(row)
            remaining = max(0, bounded_chars - used)
            if not text or remaining <= 0:
                break
            text = text[:remaining].rstrip()
            if not text:
                continue
            selected.append(
                ConversationMessage(
                    sender_name=row.member_name or "unknown",
                    text=text,
                    observed_at=_aware(row.observed_at),
                    sender_id=row.member_id or "",
                    message_id=row.external_message_id or str(row.id),
                    thread_id=row.thread_id or "",
                    task_id=row.task_id or "",
                    image_paths=_json_list(getattr(row, "image_paths_json", "[]")),
                    source_role="assistant" if row.is_self else row.source_role,
                    reply_to_sender=getattr(row, "reply_to_sender", "") or "",
                    reply_to_text=(getattr(row, "reply_to_text", "") or "")[:200],
                )
            )
            used += len(text)
        return selected

    def reserve_platform_action(
        self,
        *,
        action_key: str,
        platform: str,
        account_id: str,
        action_type: str,
        target_id: str,
        parent_comment_id: str | None,
        text: str,
        target_url: str | None,
        dry_run: bool,
        metadata: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], bool]:
        now = utc_now()
        with self.Session.begin() as session:
            existing = session.scalar(
                select(PlatformActionRow).where(PlatformActionRow.action_key == action_key)
            )
            if existing:
                return self._platform_action_dict(existing), True
            row = PlatformActionRow(
                action_key=action_key,
                platform=platform,
                account_id=account_id,
                action_type=action_type,
                target_id=target_id,
                parent_comment_id=parent_comment_id,
                target_url=target_url,
                text=text,
                status="dry_run" if dry_run else "queued",
                reason="dry_run_plan" if dry_run else "queued_for_review",
                dry_run=dry_run,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._platform_action_dict(row), False

    def update_platform_action(
        self,
        action_key: str,
        *,
        status: str,
        reason: str,
        executed: bool = False,
    ) -> None:
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(
                select(PlatformActionRow).where(PlatformActionRow.action_key == action_key)
            )
            if not row:
                raise KeyError(f"platform_action_not_found:{action_key}")
            row.status = status
            row.reason = reason
            row.updated_at = now
            if executed:
                row.executed_at = now

    def platform_action(self, action_key: str) -> dict[str, object]:
        """Return one platform action for an explicit operator approval."""
        with self.Session() as session:
            row = session.scalar(
                select(PlatformActionRow).where(PlatformActionRow.action_key == action_key)
            )
            if not row:
                raise KeyError(f"platform_action_not_found:{action_key}")
            return self._platform_action_dict(row)

    def recent_platform_actions(self, limit: int = 50) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(PlatformActionRow)
                    .order_by(PlatformActionRow.updated_at.desc())
                    .limit(max(1, min(limit, 200)))
                )
            )
        return [self._platform_action_dict(row) for row in rows]

    @staticmethod
    def _platform_action_dict(row: PlatformActionRow) -> dict[str, object]:
        return {
            "id": row.id,
            "action_key": row.action_key,
            "platform": row.platform,
            "account_id": row.account_id,
            "action_type": row.action_type,
            "target_id": row.target_id,
            "parent_comment_id": row.parent_comment_id,
            "target_url": row.target_url,
            "text": row.text,
            "status": row.status,
            "reason": row.reason,
            "dry_run": row.dry_run,
            "metadata": json.loads(row.metadata_json or "{}"),
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "executed_at": _aware(row.executed_at).isoformat() if row.executed_at else None,
        }

    # -- Three-way platform identities and shared platform memory -------------

    def bind_platform_identity(
        self,
        *,
        account_id: str,
        member_id: str,
        member_name: str = "",
        platform: str,
        platform_user_id: str,
        platform_user_name: str = "",
        status: str = "active",
    ) -> dict[str, object]:
        """Bind one stable platform user ID to a WeChat member.

        The pair ``(account_id, platform, platform_user_id)`` is unique and a
        repeat bind is idempotent.  Binding a platform that a member already
        owns with a different stable ID replaces the old relation in place, so
        one member can never authorize two platform accounts at once.
        """

        account_id = str(account_id or "").strip()
        member_id = str(member_id or "").strip()
        platform = str(platform or "").strip()
        platform_user_id = str(platform_user_id or "").strip()
        if not account_id:
            raise ValueError("platform_identity_account_required")
        if not platform:
            raise ValueError("platform_identity_platform_required")
        if not platform_user_id:
            raise ValueError("platform_identity_user_id_required")
        if not member_id:
            raise ValueError("platform_identity_member_required")
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(
                select(PlatformIdentityRow).where(
                    PlatformIdentityRow.account_id == account_id,
                    PlatformIdentityRow.platform == platform,
                    PlatformIdentityRow.platform_user_id == platform_user_id,
                )
            )
            if row is None:
                # Replace the member's previous relation for this platform
                # instead of leaving two active platform IDs behind.
                previous = session.scalar(
                    select(PlatformIdentityRow)
                    .where(
                        PlatformIdentityRow.account_id == account_id,
                        PlatformIdentityRow.platform == platform,
                        PlatformIdentityRow.member_id == member_id,
                    )
                    .order_by(PlatformIdentityRow.updated_at.desc())
                    .limit(1)
                )
                if previous is not None:
                    row = previous
                    row.platform_user_id = platform_user_id
                else:
                    row = PlatformIdentityRow(
                        account_id=account_id,
                        member_id=member_id,
                        member_name=str(member_name or "").strip(),
                        platform=platform,
                        platform_user_id=platform_user_id,
                        platform_user_name=str(platform_user_name or "").strip(),
                        status=str(status or "active").strip() or "active",
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                    return self._platform_identity_dict(row)
            row.member_id = member_id
            row.member_name = str(member_name or "").strip()
            row.platform_user_name = str(platform_user_name or "").strip()
            row.status = str(status or "active").strip() or "active"
            row.updated_at = now
            # A member owns at most one stable ID per platform; drop any other
            # row this member still had for the same platform.
            stale = list(
                session.scalars(
                    select(PlatformIdentityRow).where(
                        PlatformIdentityRow.account_id == account_id,
                        PlatformIdentityRow.platform == platform,
                        PlatformIdentityRow.member_id == member_id,
                        PlatformIdentityRow.id != row.id,
                    )
                )
            )
            for other in stale:
                session.delete(other)
            session.flush()
            return self._platform_identity_dict(row)

    def list_platform_identities(
        self,
        account_id: str,
        *,
        platform: str | None = None,
    ) -> list[dict[str, object]]:
        with self.Session() as session:
            conditions = [PlatformIdentityRow.account_id == account_id]
            if platform:
                conditions.append(PlatformIdentityRow.platform == platform)
            rows = list(
                session.scalars(
                    select(PlatformIdentityRow)
                    .where(*conditions)
                    .order_by(
                        PlatformIdentityRow.platform.asc(),
                        PlatformIdentityRow.updated_at.desc(),
                        PlatformIdentityRow.id.asc(),
                    )
                )
            )
        return [self._platform_identity_dict(row) for row in rows]

    def platform_identity_user_ids(self, platform: str) -> list[str]:
        """All active stable platform IDs bound in this deployment."""

        with self.Session() as session:
            rows = session.scalars(
                select(PlatformIdentityRow.platform_user_id)
                .where(
                    PlatformIdentityRow.platform == platform,
                    PlatformIdentityRow.status == "active",
                )
                .order_by(PlatformIdentityRow.id.asc())
            )
            return [str(value) for value in rows if str(value or "").strip()]

    def platform_mention_names(self, platform: str) -> dict[str, str]:
        """Active stable platform ID -> display name, across the deployment."""

        with self.Session() as session:
            rows = session.execute(
                select(
                    PlatformIdentityRow.platform_user_id,
                    PlatformIdentityRow.platform_user_name,
                    PlatformIdentityRow.member_name,
                )
                .where(
                    PlatformIdentityRow.platform == platform,
                    PlatformIdentityRow.status == "active",
                )
                .order_by(PlatformIdentityRow.id.asc())
            ).all()
        names: dict[str, str] = {}
        for user_id, platform_user_name, member_name in rows:
            key = str(user_id or "").strip()
            if not key or key in names:
                continue
            names[key] = (
                str(platform_user_name or "").strip()
                or str(member_name or "").strip()
            )
        return names

    def delete_platform_identity(self, identity_id: int) -> bool:
        try:
            identity_id = int(identity_id)
        except (TypeError, ValueError):
            return False
        with self.Session.begin() as session:
            row = session.get(PlatformIdentityRow, identity_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def member_for_platform_user(
        self,
        account_id: str,
        platform: str,
        platform_user_id: str,
    ) -> dict[str, object] | None:
        with self.Session() as session:
            row = session.scalar(
                select(PlatformIdentityRow).where(
                    PlatformIdentityRow.account_id == account_id,
                    PlatformIdentityRow.platform == platform,
                    PlatformIdentityRow.platform_user_id == platform_user_id,
                )
            )
            if row is None or str(row.status or "active") != "active":
                return None
            return self._platform_identity_dict(row)

    def identities_for_member(
        self, account_id: str, member_id: str
    ) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(PlatformIdentityRow)
                    .where(
                        PlatformIdentityRow.account_id == account_id,
                        PlatformIdentityRow.member_id == member_id,
                        PlatformIdentityRow.status == "active",
                    )
                    .order_by(PlatformIdentityRow.platform.asc(), PlatformIdentityRow.id.asc())
                )
            )
        return [self._platform_identity_dict(row) for row in rows]

    @staticmethod
    def _platform_identity_dict(row: PlatformIdentityRow) -> dict[str, object]:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "member_id": row.member_id,
            "member_name": row.member_name,
            "platform": row.platform,
            "platform_user_id": row.platform_user_id,
            "platform_user_name": row.platform_user_name,
            "status": row.status,
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
        }

    def add_platform_memory(
        self,
        *,
        account_id: str,
        kind: str,
        member_id: str = "",
        platform: str = "",
        item_id: str = "",
        item_url: str = "",
        title: str = "",
        text: str = "",
        query: str = "",
        metadata: dict[str, object] | None = None,
        created_at: datetime | None = None,
    ) -> int:
        account_id = str(account_id or "").strip()
        kind = str(kind or "").strip()
        if not account_id:
            raise ValueError("platform_memory_account_required")
        if kind not in {"browse", "comment_seen", "comment_posted", "mention"}:
            raise ValueError(f"platform_memory_kind_invalid:{kind}")
        row = PlatformMemoryRow(
            account_id=account_id,
            member_id=str(member_id or "").strip(),
            platform=str(platform or "").strip(),
            kind=kind,
            item_id=str(item_id or "").strip(),
            item_url=str(item_url or "").strip(),
            title=str(title or "").strip()[:512],
            text=str(text or "").strip(),
            query=str(query or "").strip()[:512],
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            created_at=created_at or utc_now(),
        )
        with self.Session.begin() as session:
            session.add(row)
            session.flush()
            return row.id

    def recent_platform_memories(
        self,
        account_id: str,
        *,
        member_id: str | None = None,
        member_ids: Iterable[str] | None = None,
        platform: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        conditions = []
        if account_id:
            conditions.append(PlatformMemoryRow.account_id == account_id)
        if member_id:
            conditions.append(PlatformMemoryRow.member_id == member_id)
        if member_ids is not None:
            values = [str(value) for value in member_ids if str(value or "").strip()]
            if not values:
                return []
            conditions.append(PlatformMemoryRow.member_id.in_(values))
        if platform:
            conditions.append(PlatformMemoryRow.platform == platform)
        if kind:
            conditions.append(PlatformMemoryRow.kind == kind)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(PlatformMemoryRow)
                    .where(*conditions)
                    .order_by(PlatformMemoryRow.created_at.desc(), PlatformMemoryRow.id.desc())
                    .limit(max(1, min(int(limit), 200)))
                )
            )
        return [self._platform_memory_dict(row) for row in rows]

    @staticmethod
    def _platform_memory_dict(row: PlatformMemoryRow) -> dict[str, object]:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "member_id": row.member_id,
            "platform": row.platform,
            "kind": row.kind,
            "item_id": row.item_id,
            "item_url": row.item_url,
            "title": row.title,
            "text": row.text,
            "query": row.query,
            "metadata": json.loads(row.metadata_json or "{}"),
            "created_at": _aware(row.created_at).isoformat(),
        }

    def recent_interest_facts(self, account_id: str, limit: int = 20) -> list[dict[str, object]]:
        """Recent durable interest/preference facts used as proactive queries."""

        conditions = [
            FactRow.memory_type.in_(["interest", "preference"]),
            FactRow.status.in_(["active", "confirmed", "inferred", "candidate"]),
        ]
        if account_id:
            conditions.append(FactRow.account_id == account_id)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(FactRow)
                    .where(*conditions)
                    .order_by(FactRow.updated_at.desc(), FactRow.id.desc())
                    .limit(max(1, min(int(limit), 100)))
                )
            )
        return [
            {
                "id": row.id,
                "member_id": row.member_id,
                "group_id": row.group_id,
                "memory_type": row.memory_type,
                "content": row.content,
                "status": row.status,
                "updated_at": _aware(row.updated_at).isoformat(),
            }
            for row in rows
        ]

    def add_fact(self, fact: MemoryFact) -> int:
        """Legacy SQL fact insert kept for existing integrations/tests.

        New message ingestion uses :meth:`add_scoped_memory`, which adds the
        full memory contract, embeddings and conflict handling.  This method
        intentionally performs no network call so old offline callers remain
        deterministic.
        """

        now = fact.updated_at or utc_now()
        memory_id = fact.memory_id or self._memory_id(fact)
        row = FactRow(
            account_id=fact.account_id,
            group_id=fact.group_id,
            member_id=fact.member_id,
            memory_type=fact.memory_type,
            content=fact.content,
            confidence=max(0.0, min(1.0, fact.confidence)),
            status=fact.status,
            source_event_id=fact.source_event_id,
            observed_at=fact.observed_at,
            valid_from=fact.valid_from or fact.observed_at,
            valid_to=fact.valid_to,
            metadata_json=json.dumps(fact.metadata, ensure_ascii=False),
            memory_id=memory_id,
            scope_json=json.dumps(fact.scope, ensure_ascii=False, separators=(",", ":")),
            source_message_ids_json=json.dumps(fact.source_message_ids, ensure_ascii=False),
            task_id=fact.task_id,
            created_at=fact.observed_at,
            updated_at=now,
            valid_until=fact.valid_to,
            importance=max(0.0, min(1.0, fact.importance)),
            embedding_model=fact.embedding_model or "",
        )
        with self.Session.begin() as session:
            existing = session.scalar(select(FactRow).where(FactRow.memory_id == memory_id))
            if existing:
                return existing.id
            session.add(row)
            session.flush()
            return row.id

    def add_scoped_memory(self, fact: MemoryFact) -> int:
        """Insert one idempotent, scoped memory and index it semantically.

        Explicit corrections/preferences with the same ``memory_key`` mark the
        old active record ``superseded`` while preserving its row and source.
        Everything stays inside the SQL transaction; retrieval reads the same
        rows back, so there is no second copy to keep in sync.
        """

        now = fact.updated_at or utc_now()
        if _is_self_member(fact.account_id, fact.member_id):
            # The logged-in account is not a member; never build a profile,
            # fact or relation for it (its own words are model context only).
            return 0
        memory_id = fact.memory_id or self._memory_id(fact)
        source_ids = [str(value) for value in fact.source_message_ids if str(value).strip()]
        if not source_ids and fact.source_event_id:
            source_ids = [str(fact.source_event_id)]
        # Avoid a second provider call when a replayed event is already in the
        # durable ledger.  The transaction below repeats this check for the
        # concurrent insert race.
        with self.Session() as session:
            existing = session.scalar(select(FactRow).where(FactRow.memory_id == memory_id))
            if existing:
                return existing.id
        embedding: list[float] | None = None
        embedding_model = fact.embedding_model or ""
        if self.embedder.configured:
            try:
                embedding, embedding_model = self.embedder.embed_one(fact.content)
            except EmbeddingError as exc:
                log.warning("embedding unavailable memory_id=%s error=%s", memory_id, str(exc))
        row_id: int
        with self.Session.begin() as session:
            existing = session.scalar(select(FactRow).where(FactRow.memory_id == memory_id))
            if existing:
                return existing.id
            memory_key = str(fact.metadata.get("memory_key") or "").strip()
            if memory_key and fact.status in {"active", "confirmed", "inferred"}:
                candidates = list(
                    session.scalars(
                        select(FactRow).where(
                            FactRow.account_id == fact.account_id,
                            FactRow.group_id == fact.group_id,
                            FactRow.member_id == fact.member_id,
                            FactRow.memory_type == fact.memory_type,
                            FactRow.status.in_(
                                ["candidate", "active", "confirmed", "inferred"]
                            ),
                            FactRow.valid_until.is_(None),
                        )
                    )
                )
                for previous in candidates:
                    metadata = _json_dict(previous.metadata_json)
                    if str(metadata.get("memory_key") or "") != memory_key:
                        continue
                    previous.status = "superseded"
                    previous.valid_to = now
                    previous.valid_until = now
                    previous.updated_at = now
            row = FactRow(
                account_id=fact.account_id,
                group_id=fact.group_id,
                member_id=fact.member_id,
                memory_type=fact.memory_type,
                content=fact.content,
                confidence=max(0.0, min(1.0, fact.confidence)),
                status=fact.status or "candidate",
                source_event_id=fact.source_event_id,
                observed_at=fact.observed_at,
                valid_from=fact.valid_from or fact.observed_at,
                valid_to=fact.valid_to,
                metadata_json=json.dumps(fact.metadata, ensure_ascii=False, separators=(",", ":")),
                embedding_json=json.dumps(embedding, ensure_ascii=False, separators=(",", ":"))
                if embedding is not None
                else None,
                memory_id=memory_id,
                scope_json=json.dumps(fact.scope, ensure_ascii=False, separators=(",", ":")),
                source_message_ids_json=json.dumps(source_ids, ensure_ascii=False),
                task_id=fact.task_id,
                created_at=now,
                updated_at=now,
                valid_until=fact.valid_to,
                importance=max(0.0, min(1.0, fact.importance)),
                embedding_model=embedding_model,
            )
            session.add(row)
            session.flush()
            row_id = row.id
        return row_id

    @staticmethod
    def _memory_id(fact: MemoryFact) -> str:
        metadata_key = str(fact.metadata.get("memory_key") or "")
        source_ids = ",".join(sorted(str(value) for value in fact.source_message_ids if value))
        seed = "\x1f".join(
            [
                fact.account_id,
                fact.group_id,
                fact.member_id,
                fact.task_id or "",
                fact.memory_type,
                metadata_key or _normalize_text(fact.content),
                fact.source_event_id or "",
                source_ids,
            ]
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def supersede(self, fact_id: int, when: datetime | None = None) -> None:
        with self.Session.begin() as session:
            row = session.get(FactRow, fact_id)
            if row:
                row.status = "superseded"
                row.valid_to = when or utc_now()

    def search_facts(
        self,
        account_id: str,
        group_id: str,
        member_id: str,
        query: str,
        half_life_days: float,
        limit: int = 8,
    ) -> list[MemoryFact]:
        now = utc_now()
        tokens = _tokens(query)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(FactRow)
                    .where(
                        FactRow.account_id == account_id,
                        FactRow.group_id == group_id,
                        FactRow.member_id == member_id,
                        FactRow.memory_type != "chat",
                        FactRow.status.in_(["active", "confirmed", "inferred", "candidate"]),
                    )
                    .order_by(FactRow.observed_at.desc())
                    .limit(200)
                )
            )

        scored: list[tuple[float, FactRow]] = []
        for row in rows:
            words = _tokens(row.content)
            lexical = len(tokens & words) / max(1, len(tokens)) if tokens else 0.0
            age_days = max(0.0, (now - _aware(row.observed_at)).total_seconds() / 86400)
            recency = math.exp(
                -age_days
                / max(self._half_life_for_memory(row.memory_type, half_life_days), 0.01)
            )
            status_bonus = 0.15 if row.status == "confirmed" else 0.0
            if row.valid_until and _aware(row.valid_until) <= now:
                continue
            active_bonus = 0.10 if row.status == "active" else 0.0
            score = 0.50 * lexical + 0.25 * recency + 0.15 * row.confidence + status_bonus + active_bonus
            scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._fact_from_row(row) for _, row in scored[:limit]]

    def search_scoped_memories(
        self,
        *,
        account_id: str,
        group_id: str,
        member_id: str,
        query: str,
        half_life_days: float,
        task_id: str | None = None,
        member_ids: Iterable[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryFact]:
        """Hybrid lexical/vector retrieval with exact scope filtering."""

        now = utc_now()
        tokens = _tokens(query)
        query_vector: list[float] | None = None
        if self.embedder.configured:
            try:
                query_vector, _ = self.embedder.embed_one(query, is_query=True)
            except EmbeddingError as exc:
                log.warning("embedding query unavailable error=%s", str(exc))
        with self.Session() as session:
            conditions = [
                FactRow.account_id == account_id,
                FactRow.group_id == group_id,
                FactRow.memory_type != "chat",
                FactRow.status.in_(["active", "confirmed", "inferred", "candidate"]),
                FactRow.valid_until.is_(None),
            ]
            # Member-scoped rows are always exact.  Group/task memories use an
            # empty member id and may be included for the current task.
            requested_members = list(dict.fromkeys([member_id, *(str(value) for value in (member_ids or ()) if str(value))]))
            if task_id:
                conditions.append(
                    (FactRow.member_id.in_(requested_members))
                    | ((FactRow.member_id == "") & (FactRow.task_id == task_id))
                )
            else:
                conditions.append(FactRow.member_id.in_(requested_members))
            rows = list(
                session.scalars(select(FactRow).where(*conditions).order_by(FactRow.updated_at.desc()).limit(500))
            )

        scored: list[tuple[float, FactRow]] = []
        for row in rows:
            if row.valid_until and _aware(row.valid_until) <= now:
                continue
            lexical = 0.0
            words = _tokens(row.content)
            if tokens:
                lexical = len(tokens & words) / max(1, len(tokens))
            semantic = 0.0
            if query_vector and row.embedding_json:
                try:
                    semantic = _cosine(query_vector, json.loads(row.embedding_json))
                except (TypeError, ValueError, json.JSONDecodeError):
                    semantic = 0.0
            age_days = max(0.0, (now - _aware(row.observed_at)).total_seconds() / 86400)
            recency = math.exp(
                -age_days
                / max(self._half_life_for_memory(row.memory_type, half_life_days), 0.01)
            )
            status_bonus = 0.12 if row.status == "active" else 0.08 if row.status == "confirmed" else 0.0
            score = (
                0.42 * lexical
                + 0.36 * semantic
                + 0.12 * recency
                + 0.06 * row.confidence
                + 0.04 * row.importance
                + status_bonus
            )
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._fact_from_row(row) for _, row in scored[: max(1, limit)]]

    def backfill_normalized_text(self, *, batch_size: int = 500, limit: int | None = None) -> int:
        """Compute ``text_normalized`` for rows created before this feature."""

        updated = 0
        bounded_batch = max(1, min(int(batch_size), 5_000))
        version_marker = f'%"normalizer_version":{normalizer_version}%'
        while True:
            with self.Session.begin() as session:
                rows = list(
                    session.scalars(
                        select(MessageRow)
                        .where(
                            or_(
                                MessageRow.text_meta_json.is_(None),
                                MessageRow.text_meta_json == "{}",
                                ~MessageRow.text_meta_json.like(version_marker),
                            )
                        )
                        .order_by(MessageRow.id.asc())
                        .limit(bounded_batch)
                    )
                )
                if not rows:
                    break
                for row in rows:
                    normalized = normalize_chat_text(
                        row.text or "",
                        sender_id=row.member_id or "",
                        sender_name=row.member_name or "",
                        ai_aliases=self.settings.wechat_ai_aliases,
                    )
                    row.text_normalized = normalized.text
                    row.text_kind = normalized.kind
                    row.text_meta_json = json.dumps(
                        {
                            "prefix_sender": normalized.prefix_sender,
                            "addressed_as": normalized.addressed_as,
                            "quoted_text": normalized.quoted_text,
                            "sticker_count": normalized.sticker_count,
                            "image_count": normalized.image_count,
                            "truncated": normalized.truncated,
                            "normalizer_version": normalizer_version,
                            "backfilled": True,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    updated += 1
            if limit is not None and updated >= int(limit):
                break
            if len(rows) < bounded_batch:
                break
        return updated

    def archive_legacy_chat_facts(self) -> int:
        """Retire pre-normalization ``memory_type=chat`` rows.

        New code never creates them.  Old rows contain a raw ``wxid:\\n`` or
        XML payload and would otherwise keep resurfacing as "memory".
        """

        now = utc_now()
        with self.Session.begin() as session:
            result = session.execute(
                update(FactRow)
                .where(
                    FactRow.memory_type == "chat",
                    FactRow.status.in_(["active", "confirmed", "candidate", "inferred"]),
                    FactRow.valid_until.is_(None),
                )
                .values(status="archived", valid_until=now, updated_at=now)
            )
            return int(result.rowcount or 0)

    def group_scopes(self) -> list[tuple[str, str, str]]:
        with self.Session() as session:
            rows = list(
                session.execute(
                    select(
                        MessageRow.account_id,
                        MessageRow.group_id,
                        func.max(MessageRow.group_name),
                    ).group_by(MessageRow.account_id, MessageRow.group_id)
                )
            )
        return [(str(account), str(group), str(name or "")) for account, group, name in rows]

    def profile_window_messages(
        self,
        account_id: str,
        group_id: str,
        *,
        limit: int = 120,
    ) -> list[tuple[str, str]]:
        """Normalized ``(member_name, text)`` pairs for offline synthesis."""

        bounded = max(10, min(int(limit), 1_000))
        echoes = self._sent_reply_texts(account_id, group_id)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                    )
                    .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                    .limit(bounded * 2)
                )
            )
        rows.reverse()
        items: list[tuple[str, str]] = []
        for row in rows:
            if self._is_bot_row(row):
                continue
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            text = self._row_text(row)
            if not text or self._looks_like_echo(text, echoes) or self._looks_like_bot_output(text, row.member_id):
                continue
            items.append((row.member_name or "群友", text))
            if len(items) >= bounded:
                break
        return items

    def save_synthesized_group_profile(
        self,
        account_id: str,
        group_id: str,
        payload: dict[str, object],
        *,
        group_name: str = "",
    ) -> bool:
        if not payload:
            return False
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == account_id,
                    AIProfileRow.group_id == group_id,
                )
            )
            if row is None:
                row = AIProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    group_name=group_name or group_id,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            profile = _json_dict(row.profile_json)
            profile["synthesized"] = payload
            profile["synthesized_at"] = now.isoformat()
            row.profile_json = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
            row.group_name = group_name or row.group_name
            row.updated_at = now

            notes = payload.get("member_notes")
            if isinstance(notes, dict):
                members = list(
                    session.scalars(
                        select(UserProfileRow).where(
                            UserProfileRow.account_id == account_id,
                            UserProfileRow.group_id == group_id,
                        )
                    )
                )
                for member in members:
                    note = notes.get(member.member_name or "")
                    if not isinstance(note, str) or not note.strip():
                        continue
                    member_profile = _json_dict(member.profile_json)
                    member_profile["synthesized_note"] = note.strip()[:80]
                    member.profile_json = json.dumps(
                        member_profile, ensure_ascii=False, separators=(",", ":")
                    )
                    member.updated_at = now
        return True

    def rebuild_member_profiles(
        self,
        account_id: str,
        group_id: str,
        *,
        window_messages: int | None = None,
        force_remote: bool = False,
    ) -> int:
        """Rebuild member behavior counters from normalized window messages.

        Only the soft ``behavior`` block is recomputed; explicit facts
        (preferences/interests/tools/constraints) remain owned by the fact
        ledger and are left untouched.
        """

        window = max(50, min(int(window_messages or self.settings.group_style_window_messages), 5_000))
        echoes = self._sent_reply_texts(account_id, group_id)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                    )
                    .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                    .limit(window)
                )
            )
        rows.reverse()
        per_member: dict[str, dict[str, object]] = {}
        for row in rows:
            if self._is_bot_row(row):
                continue
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            if _is_unknown_member(row.member_id):
                continue
            text = self._row_text(row)
            if not text or self._looks_like_echo(text, echoes) or self._looks_like_bot_output(text, row.member_id):
                continue
            patch = extract_member_profile_patch(
                text,
                str(row.external_message_id or row.id),
                ai_aliases=self.settings.wechat_ai_aliases,
            )
            if not patch:
                continue
            bucket = per_member.setdefault(
                str(row.member_id),
                {"member_name": row.member_name or "", "behavior": {}, "evidence": []},
            )
            behavior_patch = patch.get("behavior") if isinstance(patch.get("behavior"), dict) else {}
            bucket["behavior"] = _merge_member_behavior(
                bucket["behavior"] if isinstance(bucket.get("behavior"), dict) else {},
                behavior_patch,
            )
            bucket["evidence"].extend(str(item) for item in behavior_patch.get("evidence_message_ids") or [])

        now = utc_now()
        for member_id, bucket in per_member.items():
            with self.Session.begin() as session:
                row = session.scalar(
                    select(UserProfileRow).where(
                        UserProfileRow.account_id == account_id,
                        UserProfileRow.group_id == group_id,
                        UserProfileRow.member_id == member_id,
                    )
                )
                created = row is None
                if row is None:
                    row = UserProfileRow(
                        account_id=account_id,
                        group_id=group_id,
                        member_id=member_id,
                        member_name=str(bucket.get("member_name") or ""),
                        profile_json="{}",
                        history_json="[]",
                        source_message_ids_json="[]",
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                profile = _json_dict(row.profile_json)
                behavior = bucket.get("behavior") if isinstance(bucket.get("behavior"), dict) else {}
                profile["behavior"] = behavior
                profile["expression_habits"] = list(behavior.get("expression_habits") or [])
                row.profile_json = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
                row.source_message_ids_json = json.dumps(
                    _bounded_unique([str(item) for item in bucket.get("evidence") or []], 500),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                row.member_name = str(bucket.get("member_name") or "") or row.member_name
                row.updated_at = now
                result = self._profile_dict(
                    row.profile_json,
                    scope={"account_id": account_id, "group_id": group_id, "member_id": member_id},
                    source_message_ids=row.source_message_ids_json,
                    updated_at=row.updated_at,
                    history_json=row.history_json,
                )
        return len(per_member)

    def rebuild_all_group_profiles(self, *, window_messages: int | None = None) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.execute(
                    select(
                        MessageRow.account_id,
                        MessageRow.group_id,
                        func.max(MessageRow.group_name),
                    )
                    .where(MessageRow.is_self.is_(False))
                    .group_by(MessageRow.account_id, MessageRow.group_id)
                )
            )
        rebuilt: list[dict[str, object]] = []
        for account_id, group_id, group_name in rows:
            result = self.rebuild_group_profiles(
                str(account_id),
                str(group_id),
                group_name=str(group_name or ""),
                window_messages=window_messages,
                force_remote=False,
            )
            result["members_rebuilt"] = self.rebuild_member_profiles(
                str(account_id),
                str(group_id),
                window_messages=window_messages,
                force_remote=False,
            )
            rebuilt.append(result)
        return rebuilt

    def rebuild_memory(self, *, window_messages: int | None = None) -> dict[str, object]:
        """One-shot maintenance: normalize history, retire legacy facts, rebuild profiles."""

        normalized = self.backfill_normalized_text()
        archived = self.archive_legacy_chat_facts()
        rebuilt = self.rebuild_all_group_profiles(window_messages=window_messages)
        return {
            "messages_normalized": normalized,
            "legacy_chat_facts_archived": archived,
            "groups_rebuilt": len(rebuilt),
            "member_profiles_rebuilt": sum(int(item.get("members_rebuilt") or 0) for item in rebuilt),
            "profiles": [
                {
                    "window_messages": item.get("window_messages"),
                    "members": item.get("members_rebuilt"),
                    "profile_chars": len(str(item.get("profile") or "")),
                }
                for item in rebuilt
            ],
        }

    def _half_life_for_memory(self, memory_type: str, fallback: float) -> float:
        """Use the configured decay family while preserving custom callers."""

        if memory_type in {"interest", "tool"}:
            return self.settings.half_life_for("interest")
        if memory_type in {"preference", "constraint", "fact", "confirmed_info"}:
            return self.settings.half_life_for("fact")
        return fallback

    def bind_platform(
        self,
        account_id: str,
        group_id: str,
        member_id: str,
        member_name: str,
        platform: str,
        platform_account_id: str,
    ) -> int:
        with self.Session.begin() as session:
            row = BindingRow(
                account_id=account_id,
                group_id=group_id,
                member_id=member_id,
                member_name=member_name,
                platform=platform,
                platform_account_id=platform_account_id,
                status="active",
                confirmed_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return row.id

    def audit_action(self, action: OutboundAction, allowed: bool, reason: str) -> None:
        with self.Session.begin() as session:
            session.add(
                ActionRow(
                    account_id=action.account_id,
                    group_id=action.group_id,
                    action_type=action.action_type,
                    target_member_id=action.target_member_id,
                    text=action.text,
                    at_names_json=json.dumps(action.at_names, ensure_ascii=False),
                    allowed=allowed,
                    reason=reason,
                    created_at=utc_now(),
                )
            )

    def recent_audits(self, limit: int = 50) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(ActionRow).order_by(ActionRow.created_at.desc()).limit(max(1, min(limit, 200)))
                )
            )
        return [
            {
                "id": row.id,
                "group_id": row.group_id,
                "action_type": row.action_type,
                "target_member_id": row.target_member_id,
                "text": row.text,
                "at_names": json.loads(row.at_names_json or "[]"),
                "allowed": row.allowed,
                "reason": row.reason,
                "created_at": _aware(row.created_at).isoformat(),
            }
            for row in rows
        ]

    def purge_expired_messages(self) -> int:
        from .memory_jobs import scope_transaction, stable_id
        from .memory_schema import AnalysisJobRow, EpisodeRow, LifecycleRow
        now = utc_now()
        with scope_transaction(self, "retention", "retention") as session:
            rows = list(session.scalars(select(MessageRow).where(MessageRow.expires_at < now, MessageRow.lifecycle_status == "active").limit(500)))
            for row in rows:
                object_id = "raw-" + stable_id(row.account_id, row.group_id, row.external_message_id or row.message_hash)
                row.lifecycle_status, row.object_version = "expired", row.object_version + 1
                row.text = row.text_normalized = row.reply_to_text = ""
                row.text_meta_json = row.media_metadata_json = row.reply_to_json = "{}"
                row.image_paths_json = "[]"
                lifecycle = session.get(LifecycleRow, object_id)
                if lifecycle:
                    lifecycle.status, lifecycle.version, lifecycle.updated_at = "expired", row.object_version, now
                for job in session.scalars(select(AnalysisJobRow).where(AnalysisJobRow.message_id == row.id, AnalysisJobRow.status.in_(["pending", "running"]))):
                    job.status, job.lease_until, job.lease_owner = "expired", None, ""
                for episode in session.scalars(select(EpisodeRow).where(EpisodeRow.account_id == row.account_id, EpisodeRow.group_id == row.group_id, EpisodeRow.primary_source_id == str(row.external_message_id or row.id))):
                    episode.content = episode.evidence_excerpt
                self._unindex_message(session, row.id)
                row.embedding, row.embedding_model = None, ""
            return len(rows)

    @staticmethod
    def _fact_from_row(row: FactRow) -> MemoryFact:
        metadata = _json_dict(row.metadata_json)
        source_ids = _json_list(row.source_message_ids_json)
        return MemoryFact(
            account_id=row.account_id,
            group_id=row.group_id,
            member_id=row.member_id,
            memory_type=row.memory_type,  # type: ignore[arg-type]
            content=row.content,
            confidence=row.confidence,
            status=row.status,  # type: ignore[arg-type]
            source_event_id=row.source_event_id,
            observed_at=_aware(row.observed_at),
            valid_from=_aware(row.valid_from) if row.valid_from else None,
            valid_to=_aware(row.valid_until or row.valid_to) if (row.valid_until or row.valid_to) else None,
            metadata=metadata,
            memory_id=row.memory_id,
            task_id=row.task_id,
            source_message_ids=[str(value) for value in source_ids],
            importance=float(row.importance or 0.5),
            embedding_model=row.embedding_model or "",
            updated_at=_aware(row.updated_at) if row.updated_at else None,
        )


def _compact_ai_history_entry(entry: object) -> dict[str, object]:
    if not isinstance(entry, dict):
        return {}
    changes = entry.get("changes") if isinstance(entry.get("changes"), dict) else {}
    return {
        "updated_at": entry.get("updated_at"),
        "source_message_ids": [str(item) for item in (entry.get("source_message_ids") or [])[:5]],
        "changes": _compact_history_changes(changes),
    }


def _compact_history_changes(patch: dict[str, object]) -> dict[str, object]:
    """Keep a small audit delta instead of a full previous-profile snapshot."""

    keep: dict[str, object] = {}
    scalar_fields = (
        "message_count",
        "char_count",
        "short_message_count",
        "question_count",
        "exclamation_count",
        "technical_count",
        "humor_count",
        "formal_count",
        "sticker_count",
        "reply_length",
        "formality",
        "humor",
        "technicality",
    )
    for field in scalar_fields:
        if field in patch:
            keep[field] = patch[field]
    for field in ("common_vocabulary", "accepted_phrasing", "rules", "interaction_patterns"):
        if field in patch:
            keep[field] = list(patch[field] or [])[:8]
    topic_counts = patch.get("topic_counts")
    if isinstance(topic_counts, dict):
        keep["topic_counts"] = dict(list(topic_counts.items())[:8])
    evidence = patch.get("evidence_message_ids")
    if isinstance(evidence, list):
        keep["evidence_message_ids"] = evidence[:5]
    return keep


def _json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _bounded_unique(values: Iterable[object], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result[-max(1, int(limit)) :]


def _is_unknown_member(value: object) -> bool:
    return str(value or "").strip().casefold() in {
        "",
        "unknown",
        "我",
        "自己",
        "me",
        "you",
        "you (me)",
    }


def _is_self_member(account_id: str, member_id: object) -> bool:
    """True when the row is the agent's own account, not a group member.

    The logged-in account posts through the same store (role ``assistant``) and
    its messages carry the account's own wxid.  The account is never a member:
    no profile, no fact, no relation, no affinity, and never in a timeline's
    active members or a member overview.  Matching is by stable id only; a
    display nickname is not proof of identity.
    """

    member = str(member_id or "").strip()
    return bool(member) and bool(account_id) and member == str(account_id).strip()


def _empty_member_behavior() -> dict[str, object]:
    """Return the stable shape exposed to the prompt and dashboard.

    The counters are deliberately descriptive rather than psychological.  A
    single message can contribute evidence, but inferred topics and habits are
    only surfaced after repeated observations in the same group/member scope.
    """

    return {
        "message_count": 0,
        "char_count": 0,
        "short_message_count": 0,
        "question_count": 0,
        "exclamation_count": 0,
        "emoji_count": 0,
        "sticker_count": 0,
        "image_count": 0,
        "linebreak_count": 0,
        "average_message_length": 0.0,
        "marker_counts": {},
        "ending_counts": {},
        "address_counts": {},
        "topic_counts": {},
        "vocabulary": {},
        "common_topics": [],
        "inferred_interests": [],
        "expression_habits": [],
        "evidence_message_ids": [],
    }


def _counter(value: object) -> Counter[str]:
    if not isinstance(value, dict):
        return Counter()
    result: Counter[str] = Counter()
    for key, raw in value.items():
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        if count > 0:
            result[str(key)] = count
    return result


def _merge_profile(previous: dict[str, Any], patch: dict[str, object]) -> dict[str, Any]:
    merged = dict(previous)
    list_fields = {
        "preferences",
        "interests",
        "tools",
        "expression_habits",
        "work_style",
        "constraints",
        "confirmed_info",
        "task_requirements",
        "corrections",
    }
    for key, value in patch.items():
        if key == "behavior" and isinstance(value, dict):
            merged["behavior"] = _merge_member_behavior(
                previous.get("behavior") if isinstance(previous.get("behavior"), dict) else {},
                value,
            )
            # Keep the existing top-level prompt field useful for clients that
            # predate the nested behavior contract.
            merged["expression_habits"] = list(merged["behavior"].get("expression_habits") or [])
            continue
        if key in list_fields:
            old = [str(item) for item in merged.get(key, []) if str(item).strip()]
            new_values = value if isinstance(value, (list, tuple, set)) else [value]
            merged[key] = _bounded_unique(old + [str(item) for item in new_values], 100)
        else:
            merged[key] = value
    return merged


def _merge_member_behavior(previous: dict[str, Any], patch: dict[str, object]) -> dict[str, object]:
    """Accumulate soft behavioral evidence for one group/member scope."""

    merged = _empty_member_behavior()
    numeric_fields = {
        "message_count",
        "char_count",
        "short_message_count",
        "question_count",
        "exclamation_count",
        "emoji_count",
        "sticker_count",
        "image_count",
        "linebreak_count",
    }
    for field in numeric_fields:
        try:
            merged[field] = int(previous.get(field) or 0) + int(patch.get(field) or 0)
        except (TypeError, ValueError):
            merged[field] = int(previous.get(field) or 0)

    for field in ("marker_counts", "ending_counts", "address_counts", "topic_counts", "vocabulary"):
        counts = _counter(previous.get(field))
        counts.update(_counter(patch.get(field)))
        merged[field] = dict(counts.most_common(80 if field == "vocabulary" else 30))

    evidence = _bounded_unique(
        _json_list(previous.get("evidence_message_ids"))
        + _json_list(patch.get("evidence_message_ids")),
        500,
    )
    merged["evidence_message_ids"] = evidence
    message_count = max(0, int(merged["message_count"]))
    char_count = max(0, int(merged["char_count"]))
    merged["average_message_length"] = round(char_count / max(1, message_count), 1)

    topic_counts = _counter(merged["topic_counts"])
    # Two observations are the minimum evidence for a durable-looking topic;
    # with a larger corpus, a 5% share is also required.  These are still soft
    # signals and are labelled as inferred interests in the prompt payload.
    min_topic_count = (
        1
        if message_count < 40
        else max(2, int(message_count * 0.05))
    )
    repeated_topics = [
        topic for topic, count in topic_counts.most_common(12) if count >= min_topic_count
    ]
    merged["common_topics"] = repeated_topics[:8]
    merged["inferred_interests"] = repeated_topics[:5]

    habits: list[str] = []
    short_ratio = int(merged["short_message_count"]) / max(1, message_count)
    question_ratio = int(merged["question_count"]) / max(1, message_count)
    exclamation_ratio = int(merged["exclamation_count"]) / max(1, message_count)
    emoji_ratio = int(merged["emoji_count"]) / max(1, message_count)
    if short_ratio >= 0.55:
        habits.append("short_turns")
    elif float(merged["average_message_length"]) >= 80:
        habits.append("detailed_turns")
    if question_ratio >= 0.35:
        habits.append("frequent_questions")
    if exclamation_ratio >= 0.35:
        habits.append("expressive_punctuation")
    if emoji_ratio >= 0.25:
        habits.append("emoji_frequent")
    marker_counts = _counter(merged["marker_counts"])
    ending_counts = _counter(merged["ending_counts"])
    address_counts = _counter(merged["address_counts"])
    for marker, count in marker_counts.most_common(4):
        if count >= min_topic_count:
            habits.append(f"uses_{marker}")
    for marker, count in ending_counts.most_common(3):
        if count >= min_topic_count:
            habits.append(f"ends_with_{marker}")
    for marker, count in address_counts.most_common(3):
        if count >= min_topic_count:
            habits.append(f"addresses_{marker}")
    merged["expression_habits"] = _bounded_unique(habits, 20)
    return merged


def _merge_ai_profile(previous: dict[str, Any], patch: dict[str, object]) -> dict[str, Any]:
    merged = dict(previous)
    message_count = int(previous.get("message_count") or 0) + int(patch.get("message_count") or 0)
    char_count = int(previous.get("char_count") or 0) + int(patch.get("char_count") or 0)
    short_count = int(previous.get("short_message_count") or 0) + int(patch.get("short_message_count") or 0)
    question_count = int(previous.get("question_count") or 0) + int(patch.get("question_count") or 0)
    exclamation_count = int(previous.get("exclamation_count") or 0) + int(patch.get("exclamation_count") or 0)
    technical_count = int(previous.get("technical_count") or 0) + int(patch.get("technical_count") or 0)
    humor_count = int(previous.get("humor_count") or 0) + int(patch.get("humor_count") or 0)
    formal_count = int(previous.get("formal_count") or 0) + int(patch.get("formal_count") or 0)
    sticker_count = int(previous.get("sticker_count") or 0) + int(patch.get("sticker_count") or 0)
    vocabulary = Counter(_json_dict(previous.get("vocabulary")))
    vocabulary.update(Counter(_json_dict(patch.get("vocabulary"))))
    topic_counts = Counter(_json_dict(previous.get("topic_counts")))
    topic_counts.update(Counter(_json_dict(patch.get("topic_counts"))))
    evidence = _bounded_unique(
        _json_list(previous.get("evidence_message_ids")) + _json_list(patch.get("evidence_message_ids")),
        500,
    )
    average_length = char_count / max(1, message_count)
    short_ratio = short_count / max(1, message_count)
    if short_ratio >= 0.6 or average_length <= 24:
        reply_length = "short"
    elif average_length >= 80:
        reply_length = "detailed"
    else:
        reply_length = "medium"
    formality_ratio = formal_count / max(1, message_count)
    humor_ratio = humor_count / max(1, message_count)
    technical_ratio = technical_count / max(1, message_count)
    common_topics = list(dict(topic_counts.most_common(10)).keys())
    interaction_patterns: list[str] = []
    if question_count / max(1, message_count) >= 0.4:
        interaction_patterns.append("frequent_questions")
    if exclamation_count / max(1, message_count) >= 0.35:
        interaction_patterns.append("expressive_punctuation")
    if short_ratio >= 0.6 or average_length <= 24:
        interaction_patterns.append("short_turns")
    elif average_length >= 80:
        interaction_patterns.append("detailed_turns")
    accepted_markers = [
        value
        for value in ("哈哈", "笑死", "狗头", "谢谢", "大佬", "兄弟", "家人们")
        if value in vocabulary
    ]
    merged.update(
        {
            "message_count": message_count,
            "char_count": char_count,
            "short_message_count": short_count,
            "question_count": question_count,
            "exclamation_count": exclamation_count,
            "technical_count": technical_count,
            "humor_count": humor_count,
            "formal_count": formal_count,
            "sticker_count": sticker_count,
            "vocabulary": dict(vocabulary.most_common(30)),
            "common_vocabulary": list(dict(vocabulary.most_common(10)).keys()),
            "topic_counts": dict(topic_counts.most_common(20)),
            "common_topics": common_topics,
            "interaction_patterns": interaction_patterns,
            "accepted_phrasing": accepted_markers,
            "avoid_phrasing": list(merged.get("avoid_phrasing") or []),
            "rules": list(merged.get("rules") or []),
            "reply_length": reply_length,
            "formality": "formal" if formality_ratio >= 0.35 else "casual" if formality_ratio <= 0.1 else "mixed",
            "humor": "frequent" if humor_ratio >= 0.25 else "occasional" if humor_ratio else "low",
            "technicality": "technical" if technical_ratio >= 0.35 else "general",
            "evidence_message_ids": evidence,
        }
    )
    return merged


def _merge_task_profile(previous: dict[str, Any], patch: dict[str, object]) -> dict[str, Any]:
    merged = dict(previous)
    mapping = {
        "scope": "scope_text",
        "next_action": "next_action",
        "goal": "goal",
    }
    list_fields = {"requirements", "decisions", "progress", "completed", "blockers", "people"}
    for key, value in patch.items():
        target = mapping.get(key, key)
        if target in list_fields:
            old = [str(item) for item in merged.get(target, []) if str(item).strip()]
            new_values = value if isinstance(value, (list, tuple, set)) else [value]
            merged[target] = _bounded_unique(old + [str(item) for item in new_values], 100)
        else:
            merged[target] = value
    return merged


def _cosine(left: list[float], right: object) -> float:
    if not isinstance(right, list) or len(left) != len(right) or not left:
        return 0.0
    try:
        values = [float(value) for value in right]
    except (TypeError, ValueError):
        return 0.0
    dot = sum(a * b for a, b in zip(left, values))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in values))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _naive_utc(value):
    """Rows store naive UTC; raw SQL bind parameters must match that shape."""

    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _sql_time(value) -> str | None:
    """SQLite keeps six fractional digits, so string comparison needs them."""

    value = _naive_utc(value)
    return value.strftime("%Y-%m-%d %H:%M:%S.%f") if value else None


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(normalized.split())


def _normalize_question(text: str, aliases: Iterable[str]) -> str:
    normalized = _normalize_text(text)
    for alias in sorted((item.strip() for item in aliases if item and item.strip()), key=len, reverse=True):
        if alias.isascii() and alias.replace("_", "").isalnum():
            pattern = rf"@?\s*\b{re.escape(alias.casefold())}\b"
        else:
            pattern = rf"@?\s*{re.escape(alias.casefold())}"
        normalized = re.sub(pattern, " ", normalized, flags=re.IGNORECASE)
    # Punctuation and spacing should not make an already answered question
    # appear new.  ``\w`` keeps Chinese characters and alphanumerics.
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized, flags=re.UNICODE)
    return compact or normalized


def _tokens(text: str) -> set[str]:
    """Small dependency-free tokenizer for Chinese and whitespace text."""
    normalized = text.strip().lower()
    words = {token for token in normalized.split() if token}
    cjk = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    words.update(cjk)
    words.update("".join(cjk[index : index + 2]) for index in range(max(0, len(cjk) - 1)))
    return words


_CONTEXT_SIMILARITY_THRESHOLD = 0.10


def _context_tokens(text: str) -> set[str]:
    """Extract meaningful context terms without treating single CJK chars as hits."""

    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_+#.-]+", normalized)
        if len(token) >= 2
    }
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(run) < 2:
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _context_similarity(query: str, text: str) -> float:
    """Return a bounded lexical similarity for short raw-message context."""

    query_terms = _context_tokens(query)
    message_terms = _context_tokens(text)
    if not query_terms or not message_terms:
        return 0.0
    overlap = len(query_terms & message_terms)
    if overlap == 0:
        return 0.0
    return overlap / math.sqrt(len(query_terms) * len(message_terms))
