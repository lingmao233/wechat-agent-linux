"""Durable inbound reply jobs.

The listener used to acknowledge the Weixin cursor and then park the message in
an in-memory deque.  A restart (or an overflowing deque) therefore lost the
message forever: the cursor had already moved past it.  A reply job is written
in the same transaction as the raw message, so the cursor is only acknowledged
after the intent to answer is durable.

States::

    pending ──claim──▶ running ──send ok──▶ done
       │                 │
       │                 ├─lease expired, no intent ─▶ pending (safe retry)
       │                 └─lease expired, has intent ─▶ uncertain (human)
       └─older than 10 min ─▶ expired (never answer stale chat)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json

from sqlalchemy import DateTime, Integer, String, Text, and_, exists, func, or_, select
from sqlalchemy.orm import Mapped, aliased, mapped_column

from .memory_jobs import aware, encode, scope_transaction
from .memory_schema import EpisodeRow  # noqa: F401  (registers shared tables)
from .models import MessageEvent, utc_now
from .storage import Base

PENDING_TTL = timedelta(minutes=10)
DEFAULT_LEASE_SECONDS = 300

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_EXPIRED = "expired"
STATE_UNCERTAIN = "uncertain"


class ReplyJobRow(Base):
    __tablename__ = "reply_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    message_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    state: Mapped[str] = mapped_column(String(24), default=STATE_PENDING, index=True)
    basis: Mapped[str] = mapped_column(String(32), default="")
    origin: Mapped[str] = mapped_column(String(32), default="human")
    rules_json: Mapped[str] = mapped_column(Text, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_owner: Mapped[str] = mapped_column(String(64), default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    intent_json: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


@dataclass(slots=True)
class ReplyJob:
    """One claimed job plus the message it refers to."""

    job_id: int
    message_id: int
    account_id: str
    group_id: str
    basis: str
    origin: str
    rules: dict
    attempts: int
    event: MessageEvent
    # Set once the send intent is durable: a crash after this point is not
    # safe to retry, because the message may already be visible in WeChat.
    intent_sent: bool = False


def enqueue(session, event: MessageEvent, message_id: int, *, basis: str = "", origin: str = "human", rules: dict | None = None, now: datetime | None = None) -> bool:
    """Create the job inside the caller's message transaction."""

    existing = session.scalar(select(ReplyJobRow.id).where(ReplyJobRow.message_id == message_id))
    if existing is not None:
        return False
    # Freshness follows the message, not the insert: a backlog replayed from an
    # old cursor position must expire rather than answer yesterday's question.
    moment = now or event.source_created_at or event.observed_at or utc_now()
    session.add(ReplyJobRow(
        account_id=event.account_id,
        group_id=event.group_id,
        message_id=message_id,
        state=STATE_PENDING,
        basis=basis,
        origin=origin,
        rules_json=encode(rules or {}),
        attempts=0,
        next_retry_at=moment,
        created_at=moment,
        updated_at=moment,
    ))
    return True


def _recover_scope(session, account_id: str, group_id: str, now: datetime) -> dict[str, int]:
    """Apply the three restart rules to one group; idempotent and cheap."""

    counts = {"expired": 0, "retry": 0, "uncertain": 0}
    stale = list(session.scalars(select(ReplyJobRow).where(
        ReplyJobRow.account_id == account_id,
        ReplyJobRow.group_id == group_id,
        ReplyJobRow.state == STATE_PENDING,
        ReplyJobRow.created_at < now - PENDING_TTL,
    )))
    for row in stale:
        row.state, row.last_error, row.updated_at = STATE_EXPIRED, "pending_ttl_exceeded", now
        counts["expired"] += 1
    orphaned = list(session.scalars(select(ReplyJobRow).where(
        ReplyJobRow.account_id == account_id,
        ReplyJobRow.group_id == group_id,
        ReplyJobRow.state == STATE_RUNNING,
        or_(ReplyJobRow.lease_until.is_(None), ReplyJobRow.lease_until <= now),
    )))
    for row in orphaned:
        row.lease_owner, row.lease_until, row.updated_at = "", None, now
        if str(row.intent_json or "").strip():
            # A send may have happened; re-sending risks a duplicate message.
            row.state, row.last_error = STATE_UNCERTAIN, "lease_expired_with_send_intent"
            counts["uncertain"] += 1
        else:
            row.state, row.next_retry_at = STATE_PENDING, now
            counts["retry"] += 1
    return counts


def recover_all(storage, *, now: datetime | None = None) -> dict[str, int]:
    """Startup recovery across every scope."""

    moment = now or utc_now()
    with scope_transaction(storage, "reply", "recover") as session:
        scopes = list(session.execute(
            select(ReplyJobRow.account_id, ReplyJobRow.group_id).distinct()
        ))
    totals = {"expired": 0, "retry": 0, "uncertain": 0}
    for account_id, group_id in scopes:
        totals = {key: totals[key] + value for key, value in _recover_scope_one(storage, account_id, group_id, moment).items()}
    return totals


def _recover_scope_one(storage, account_id: str, group_id: str, now: datetime) -> dict[str, int]:
    with scope_transaction(storage, account_id, group_id) as session:
        return _recover_scope(session, account_id, group_id, now)


def claim_next(storage, *, now: datetime | None = None, lease_seconds: int = DEFAULT_LEASE_SECONDS, owner: str = "") -> ReplyJob | None:
    """Claim the oldest due job, never two at once inside one group."""

    from .memory_jobs import event_from_row
    from .storage import MessageRow

    moment = now or utc_now()
    with scope_transaction(storage, "reply", "claim") as session:
        scopes = list(session.execute(select(ReplyJobRow.account_id, ReplyJobRow.group_id).where(ReplyJobRow.state.in_([STATE_PENDING, STATE_RUNNING])).distinct()))
        for account_id, group_id in scopes:
            _recover_scope(session, account_id, group_id, moment)
        other = aliased(ReplyJobRow)
        group_is_busy = exists().where(and_(
            other.account_id == ReplyJobRow.account_id,
            other.group_id == ReplyJobRow.group_id,
            other.state == STATE_RUNNING,
            other.lease_until > moment,
        ))
        row = session.scalar(
            select(ReplyJobRow)
            .where(
                ReplyJobRow.state == STATE_PENDING,
                ReplyJobRow.next_retry_at <= moment,
                ~group_is_busy,
            )
            .order_by(ReplyJobRow.next_retry_at, ReplyJobRow.id)
            .limit(1)
        )
        if row is None:
            return None
        row.state = STATE_RUNNING
        row.lease_owner = owner
        row.lease_until = moment + timedelta(seconds=lease_seconds)
        row.attempts += 1
        row.updated_at = moment
        message = session.get(MessageRow, row.message_id)
        if message is None:
            row.state, row.last_error = STATE_FAILED, "message_row_missing"
            return None
        event = event_from_row(message)
        event.ingest_sequence = message.id
        return ReplyJob(
            job_id=row.id,
            message_id=message.id,
            account_id=row.account_id,
            group_id=row.group_id,
            basis=row.basis,
            origin=row.origin,
            rules=_loads(row.rules_json),
            attempts=row.attempts,
            event=event,
        )


def _loads(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def mark_intent(storage, job_id: int, intent: dict, *, now: datetime | None = None) -> None:
    """Record the send intent before the UI action, enabling uncertain recovery."""

    moment = now or utc_now()
    with storage.Session.begin() as session:
        row = session.get(ReplyJobRow, job_id)
        if row is None:
            return
        row.intent_json = encode(intent)
        row.updated_at = moment


def finish(storage, job_id: int, state: str, result: dict | None = None, *, error: str = "", now: datetime | None = None) -> None:
    moment = now or utc_now()
    with storage.Session.begin() as session:
        row = session.get(ReplyJobRow, job_id)
        if row is None:
            return
        row.state = state
        row.result_json = encode(result or {})
        row.last_error = error[:500]
        row.lease_owner, row.lease_until = "", None
        row.updated_at = moment


def finish_if_running(storage, job_id: int, state: str, result: dict | None = None, *, error: str = "", now: datetime | None = None) -> bool:
    """Terminal transition that never overwrites an ``uncertain`` verdict."""

    from sqlalchemy import update
    moment = now or utc_now()
    with storage.Session.begin() as session:
        changed = session.execute(update(ReplyJobRow).where(
            ReplyJobRow.id == job_id,
            ReplyJobRow.state == STATE_RUNNING,
        ).values(
            state=state,
            result_json=encode(result or {}),
            last_error=error[:500],
            lease_owner="",
            lease_until=None,
            updated_at=moment,
        ))
        return bool(changed.rowcount)


def describe(storage, job_id: int, *, basis: str = "", origin: str = "", rules: dict | None = None, now: datetime | None = None) -> None:
    """Attach the authoritative policy verdict to a claimed job."""

    moment = now or utc_now()
    with storage.Session.begin() as session:
        row = session.get(ReplyJobRow, job_id)
        if row is None:
            return
        if basis:
            row.basis = basis
        if origin:
            row.origin = origin
        if rules is not None:
            row.rules_json = encode(rules)
        row.updated_at = moment


def any_active(storage) -> bool:
    with storage.Session() as session:
        return session.scalar(select(ReplyJobRow.id).where(
            ReplyJobRow.state.in_([STATE_PENDING, STATE_RUNNING])
        ).limit(1)) is not None


def release(storage, job_id: int, *, error: str, retry_at: datetime | None = None, now: datetime | None = None) -> None:
    """Return a job to the queue for a later attempt (no send was attempted)."""

    moment = now or utc_now()
    with storage.Session.begin() as session:
        row = session.get(ReplyJobRow, job_id)
        if row is None:
            return
        if row.attempts >= 5:
            row.state, row.last_error = STATE_FAILED, error[:500]
        else:
            row.state, row.last_error = STATE_PENDING, error[:500]
            row.next_retry_at = retry_at or moment + timedelta(seconds=min(300, 5 * 2 ** max(0, row.attempts - 1)))
        row.lease_owner, row.lease_until = "", None
        row.updated_at = moment


def pending_events(storage, account_id: str, group_id: str, *, limit: int = 50) -> list[MessageEvent]:
    """Pending jobs of one group as events; used by the proactive guard."""

    from .memory_jobs import event_from_row
    from .storage import MessageRow

    with storage.Session() as session:
        rows = list(session.scalars(
            select(MessageRow)
            .join(ReplyJobRow, ReplyJobRow.message_id == MessageRow.id)
            .where(
                ReplyJobRow.account_id == account_id,
                ReplyJobRow.group_id == group_id,
                ReplyJobRow.state == STATE_PENDING,
            )
            .order_by(ReplyJobRow.id)
            .limit(max(1, min(limit, 200)))
        ))
        return [event_from_row(row) for row in rows]


def has_pending(storage, account_id: str, group_id: str) -> bool:
    with storage.Session() as session:
        return session.scalar(select(ReplyJobRow.id).where(
            ReplyJobRow.account_id == account_id,
            ReplyJobRow.group_id == group_id,
            ReplyJobRow.state.in_([STATE_PENDING, STATE_RUNNING]),
        ).limit(1)) is not None


def pending_depth(storage, account_id: str, group_id: str) -> int:
    with storage.Session() as session:
        return int(session.scalar(select(func.count(ReplyJobRow.id)).where(
            ReplyJobRow.account_id == account_id,
            ReplyJobRow.group_id == group_id,
            ReplyJobRow.state.in_([STATE_PENDING, STATE_RUNNING]),
        )) or 0)


def counts(storage, *, now: datetime | None = None) -> dict[str, int]:
    del now
    with storage.Session() as session:
        rows = session.execute(select(ReplyJobRow.state, func.count(ReplyJobRow.id)).group_by(ReplyJobRow.state))
        return {state: int(total) for state, total in rows}


def recent(storage, *, limit: int = 20, states: tuple[str, ...] = (STATE_UNCERTAIN, STATE_FAILED)) -> list[dict]:
    with storage.Session() as session:
        rows = list(session.scalars(
            select(ReplyJobRow).where(ReplyJobRow.state.in_(states)).order_by(ReplyJobRow.updated_at.desc()).limit(max(1, min(limit, 100)))
        ))
        return [{
            "job_id": row.id,
            "account_id": row.account_id,
            "group_id": row.group_id,
            "message_id": row.message_id,
            "state": row.state,
            "basis": row.basis,
            "origin": row.origin,
            "attempts": row.attempts,
            "error": row.last_error,
            "intent": _loads(row.intent_json),
            "result": _loads(row.result_json),
            "updated_at": aware(row.updated_at).isoformat(),
        } for row in rows]


def prune(storage, *, older_than_days: int = 30, now: datetime | None = None) -> int:
    """Drop finished jobs from the working set; the ledger keeps answering."""

    from sqlalchemy import delete
    moment = now or utc_now()
    cutoff = moment - timedelta(days=older_than_days)
    with storage.Session.begin() as session:
        result = session.execute(delete(ReplyJobRow).where(
            ReplyJobRow.state.in_([STATE_DONE, STATE_EXPIRED]),
            ReplyJobRow.updated_at < cutoff,
        ))
        return int(result.rowcount or 0)
