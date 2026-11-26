"""Memory curation for facts that are already stored (PLAN P7).

Extraction belongs to the ingestion path; nothing here discovers new facts by
reading the chat.  Three jobs run over the durable ``memory_facts`` ledger:

* :meth:`Digest.merge_similar_facts` collapses synonymous facts of one member
  and one ``memory_type`` into a single newer row, marks the originals
  ``superseded`` (rows are never deleted) and carries their ``message_id``
  provenance onto the merged row.
* :meth:`Digest.build_digests` compresses one finished local day and the
  trailing seven local days into ``daily_digest`` / ``weekly_digest`` facts, so
  retrieval can answer "最近群里怎么样" from a summary instead of raw rows.
* :meth:`Digest.archive_stale` marks decayed, never-retrieved, low-confidence
  facts ``archived`` without deleting them.

Everything is idempotent and safe to re-run: merged rows and digests use stable
``memory_id`` values derived from their scope, so a crash, a restart or a second
run on the same day cannot duplicate a summary, and a failed model call only
skips that round.  The model is never an authority: it only sees ids it must
choose from, it may return nothing at all, and its output is validated against
the exact ids supplied before a single row is written.  "宁可输出空，不编造"
is part of every prompt.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .memory_jobs import aware, encode, stable_id
from .memory_schema import RetrievalTraceRow
from .models import MemoryFact
from .storage import FactRow, MessageRow

log = logging.getLogger(__name__)

MERGE_SYSTEM = (
    "你在整理同一个成员、同一类记忆事实里重复的条目，只返回 JSON："
    '{"merged":[{"content":"合并后的一句话","keep_memory_ids":["事实ID","事实ID"]}]}'
    "。只合并意思重复、能用一句话说清的条目；keep_memory_ids 只能取给定列表里的 ID，且至少两个；"
    "content 必须有这些条目本身的支撑，不补充原话里没有的信息，不推测动机，不编造证据。"
    "没有重复就返回空数组。宁可输出空，不编造。"
)
DAILY_SYSTEM = (
    "你在为一个微信群写当天的事实摘要，只返回 JSON："
    '{"summary":"不超过300字的中文摘要"}'
    "。只写消息里出现过的事，不推测动机，不评价人品；消息不足以支撑摘要时返回空字符串。"
    "宁可输出空，不编造。"
)
WEEKLY_SYSTEM = (
    "你在为一个微信群写这一周的事实摘要，只返回 JSON："
    '{"summary":"不超过300字的中文摘要"}'
    "。只写消息里出现过的事，不推测动机，不评价人品；消息不足以支撑摘要时返回空字符串。"
    "宁可输出空，不编造。"
)
RECONCILE_SYSTEM = (
    "你在整理一个群成员的记忆，只返回 JSON："
    '{"summary":"不超过180字的当前画像总结，只写仍有效的信息",'
    '"current":[{"kind":"preference|interest|habit|experience|relation",'
    '"content":"合并后的一句话","keep_memory_ids":["事实ID","事实ID"]}],'
    '"supersede":[{"memory_id":"被取代的事实ID","reason":"为什么被取代","confidence":0.9}]}'
    "。规则：只整理给定事实，不补充事实里没有的信息，不推测动机；"
    "同一主题的新说法可以取代旧说法（supersede 旧 id）；语义重复的条目用 keep_memory_ids 合并成一条；"
    "不同主题、可以同时成立的说法不要合并，也不要标记取代；"
    "所有 id 只能取自给定列表；不确定就返回空数组。宁可输出空，不编造。"
)

# Summaries are derived from many messages and carry no single member; merging
# them into each other would destroy the provenance this module exists to keep.
_DERIVED_TYPES = ("daily_digest", "weekly_digest", "member_profile_summary")
_UNMERGEABLE_TYPES = (*_DERIVED_TYPES, "chat")
_DIGEST_CONFIDENCE = 0.7
_MERGE_KEYS = frozenset({"content", "keep_memory_ids"})
_REFERENCE_KEYS = frozenset({"evidence_id", "object_id", "memory_id"})


def _local_day_bounds(settings, day: str) -> tuple[datetime, datetime]:
    """Local midnight in ``group_timezone`` as the naive UTC ``observed_at`` stores.

    Kept local to this module (the same arithmetic as ``narrative``) so memory
    curation has no dependency on another feature layer.
    """

    zone = ZoneInfo(settings.group_timezone)
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _parse_object(raw) -> dict | None:
    """Parse one model reply as a JSON object; anything else is a skip."""

    if not isinstance(raw, str):
        log.warning("curation output was not text type=%s", type(raw).__name__)
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    if not text:
        log.warning("curation output was empty")
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:  # JSONDecodeError included
        log.warning("curation output was not valid json error=%s", type(exc).__name__)
        return None
    return value if isinstance(value, dict) else None


def _row_source_ids(row) -> list[str]:
    """Message ids a stored fact came from, including the legacy event id."""

    try:
        values = json.loads(row.source_message_ids_json or "[]")
    except (TypeError, ValueError):
        values = []
    output = [str(value).strip()[:128] for value in values if str(value).strip()] if isinstance(values, list) else []
    if row.source_event_id:
        output.append(str(row.source_event_id).strip()[:128])
    return list(dict.fromkeys(item for item in output if item))[:200]


def _sample(rows, *, max_chars: int = 8000, max_rows: int = 60) -> list:
    """Even chronological sample that always keeps the first and last row."""

    if len(rows) <= max_rows:
        return list(rows)
    window = max(1, len(rows) // max_rows)
    picked = [rows[index] for index in range(0, len(rows), window)][:max_rows]
    if rows[-1] not in picked:
        picked[-1] = rows[-1]
    total, kept = 0, []
    for row in picked:
        cost = len(row.text_normalized or row.text or "")
        if total + cost > max_chars and kept:
            break
        kept.append(row)
        total += cost
    return kept


def _payload(rows) -> list[dict]:
    return [
        {
            "id": row.external_message_id or str(row.id),
            "member_id": row.member_id,
            "member_name": row.member_name,
            "at": aware(row.observed_at).isoformat(),
            "text": (row.text_normalized or row.text or "")[:280],
        }
        for row in rows
    ]


class Digest:
    def __init__(self, runtime):
        self.runtime, self.storage, self.settings = runtime, runtime.storage, runtime.settings
        # Instance-only, advisory last-run marker for :meth:`refresh`; the
        # durable once-per-day guard belongs to the caller, not to a new table.
        self._last_run: dict[tuple[str, str, str], str] = {}

    # ------------------------------------------------------------------ plumbing
    def _model_ready(self) -> bool:
        return bool(getattr(self.runtime, "llm", None) and getattr(self.settings, "llm_api_key", ""))

    def _now(self, now: datetime | None = None) -> datetime:
        return aware(now or self.runtime.clock())

    def _complete(self, system: str, payload: dict, *, max_tokens: int = 0) -> str | None:
        timeout = float(getattr(self.settings, "llm_timeout_seconds", 60.0) or 60.0)
        try:
            return self.runtime.llm.complete(
                system, encode(payload), max_tokens=max_tokens, timeout_seconds=min(60.0, timeout)
            )
        except Exception as exc:  # a failed model call must never break maintenance
            log.warning("curation model call failed error=%s", type(exc).__name__)
            return None

    def _local_today(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.settings.group_timezone)).date().isoformat()

    # --------------------------------------------------------------- merge facts
    def merge_similar_facts(self, account_id: str, group_id: str, *, member_id: str | None = None, limit: int = 40) -> int:
        """Merge synonymous facts of one member/type; return merged groups applied.

        Without ``member_id`` the most recent member/type bucket that has more
        than one active fact is curated, because facts are only comparable
        inside one member and one ``memory_type``.  One model call is made for
        that bucket; a re-run finds only the merged row and is a no-op.
        """

        if not self._model_ready():
            return 0
        limit = max(2, min(int(limit or 40), 200))
        now = self._now()
        with self.storage.Session() as session:
            statement = select(FactRow).where(
                FactRow.account_id == account_id,
                FactRow.group_id == group_id,
                FactRow.status.in_(["active", "confirmed"]),
                FactRow.memory_type.notin_(list(_UNMERGEABLE_TYPES)),
            )
            if member_id is not None:
                statement = statement.where(FactRow.member_id == member_id)
            rows = list(
                session.scalars(
                    statement.order_by(FactRow.observed_at.desc(), FactRow.id.desc()).limit(limit)
                )
            )
        if len(rows) < 2:
            return 0
        bucket = self._bucket(rows)
        if len(bucket) < 2:
            return 0
        supplied = {row.memory_id or f"fact-{row.id}": row for row in bucket}
        payload = {
            "member_id": bucket[0].member_id,
            "memory_type": bucket[0].memory_type,
            "facts": [
                {
                    "memory_id": row.memory_id or f"fact-{row.id}",
                    "content": row.content[:400],
                    "confidence": round(float(row.confidence or 0.0), 3),
                    "observed_at": aware(row.observed_at).isoformat(),
                }
                for row in bucket
            ],
        }
        raw = self._complete(MERGE_SYSTEM, payload)
        if raw is None:
            return 0
        value = _parse_object(raw)
        if value is None:
            return 0
        items = value.get("merged")
        if not isinstance(items, list):
            log.warning("merge output without a merged list group=%s", group_id)
            return 0
        applied = 0
        claimed: set[str] = set()
        for item in items[:20]:
            merged = self._validate_merge(item, supplied)
            if merged is None:
                continue
            content, kept = merged
            identity = {row.memory_id or f"fact-{row.id}" for row in kept}
            if identity & claimed:  # one model reply must not merge a row twice
                continue
            source_ids = list(dict.fromkeys(source for row in kept for source in _row_source_ids(row)))[:200]
            if not source_ids:
                # No message to trace back to means the entry cannot be stored.
                log.warning("merge dropped without provenance group=%s member=%s", group_id, kept[0].member_id)
                continue
            keep_ids = sorted(identity)
            fact = MemoryFact(
                account_id=account_id,
                group_id=group_id,
                member_id=kept[0].member_id,
                memory_type=kept[0].memory_type,
                content=content,
                confidence=max(float(row.confidence or 0.0) for row in kept),
                status="active",
                source_event_id=None,
                observed_at=max(aware(row.observed_at) for row in kept),
                valid_from=min(aware(row.observed_at) for row in kept),
                metadata={"curation": "merge", "merged_from": keep_ids},
                memory_id=stable_id(account_id, group_id, kept[0].member_id, kept[0].memory_type, "merge", *keep_ids),
                task_id=None,
                source_message_ids=source_ids,
                updated_at=now,
            )
            try:
                merged_id = self.storage.add_scoped_memory(fact)
            except Exception as exc:
                log.warning("merge write failed group=%s error=%s", group_id, type(exc).__name__)
                continue
            if merged_id:
                claimed |= identity
                self._supersede(kept, merged_id, now)
                # The ledger changed; the cached prompt/console profile must be
                # rebuilt or it keeps showing the two old strings.
                self.storage.rebuild_member_profile(account_id, group_id, kept[0].member_id, now=now)
                applied += 1
        return applied

    def _bucket(self, rows) -> list:
        """The most recently active member/type group that actually has duplicates."""

        buckets: dict[tuple[str, str], list] = defaultdict(list)
        for row in rows:
            buckets[(row.member_id, row.memory_type)].append(row)
        mergeable = {name: values for name, values in buckets.items() if len(values) > 1}
        if not mergeable:
            return []
        key = max(
            mergeable,
            key=lambda name: (max(aware(row.observed_at) for row in mergeable[name]), len(mergeable[name]), name),
        )
        return sorted(mergeable[key], key=lambda row: (aware(row.observed_at), row.id), reverse=True)

    def _validate_merge(self, item, supplied: dict) -> tuple[str, list] | None:
        if not isinstance(item, dict) or not set(item) <= _MERGE_KEYS:
            return None
        if not isinstance(item.get("content"), str):
            return None
        content = item["content"].strip()[:300]
        raw_ids = item.get("keep_memory_ids")
        if not content or not isinstance(raw_ids, list):
            return None
        keep_ids = [str(identifier) for identifier in raw_ids if isinstance(identifier, str)]
        if len(keep_ids) != len(raw_ids):
            return None
        keep_ids = list(dict.fromkeys(keep_ids))
        if len(keep_ids) < 2 or any(identifier not in supplied for identifier in keep_ids):
            return None
        return content, [supplied[identifier] for identifier in keep_ids]

    def _supersede(self, kept, merged_id: int, now: datetime) -> None:
        """Close replaced facts without ever deleting a row."""

        with self.storage.Session.begin() as session:
            merged = session.get(FactRow, merged_id)
            merged_memory_id = merged.memory_id if merged is not None else ""
            for row in kept:
                current = session.get(FactRow, row.id)
                if current is None or current.id == merged_id:
                    continue
                if current.status not in {"active", "confirmed"}:
                    continue
                current.status = "superseded"
                current.valid_to = now
                current.valid_until = now
                current.updated_at = now
                if merged_memory_id:
                    current.superseded_by = merged_memory_id

    # ------------------------------------------------------------------ digests
    def build_digests(self, account_id: str, group_id: str, *, now: datetime | None = None) -> dict:
        """Write at most one daily and one weekly digest for finished periods."""

        result = {"daily": 0, "weekly": 0}
        if not self._model_ready():
            return result
        now = self._now(now)
        yesterday = datetime.fromisoformat(self._local_today(now)).date() - timedelta(days=1)
        if self._daily_digest(account_id, group_id, yesterday, now):
            result["daily"] = 1
        if self._weekly_digest(account_id, group_id, yesterday, now):
            result["weekly"] = 1
        return result

    def _daily_digest(self, account_id: str, group_id: str, day, now: datetime) -> bool:
        identifier = stable_id(account_id, group_id, "daily_digest", day.isoformat())
        if self._digest_exists(identifier):
            return False
        start, end = _local_day_bounds(self.settings, day.isoformat())
        rows = self._human_messages(account_id, group_id, start, end, limit=2000)
        if len(rows) < 5:
            return False
        sample = _sample(rows)
        raw = self._complete(DAILY_SYSTEM, {"day": day.isoformat(), "messages": _payload(sample)})
        summary = self._summary(raw)
        if not summary:
            return False
        return self._store_digest(
            account_id, group_id, "daily_digest", identifier, summary,
            {"day": day.isoformat(), "window": "day"}, rows, sample, now,
        )

    def _weekly_digest(self, account_id: str, group_id: str, yesterday, now: datetime) -> bool:
        # The rolling window is the seven finished local days ending
        # ``yesterday``; the id is keyed by the ISO week of its last day so a
        # daily maintenance loop writes one weekly summary per week, not one
        # per run.
        first_day = yesterday - timedelta(days=6)
        week = yesterday.isocalendar()
        week_key = f"{week.year}-W{week.week:02d}"
        identifier = stable_id(account_id, group_id, "weekly_digest", week_key)
        if self._digest_exists(identifier):
            return False
        start, _ = _local_day_bounds(self.settings, first_day.isoformat())
        _, end = _local_day_bounds(self.settings, yesterday.isoformat())
        rows = self._human_messages(account_id, group_id, start, end, limit=3000)
        if len(rows) < 5:
            return False
        sample = _sample(rows, max_chars=6000)
        raw = self._complete(
            WEEKLY_SYSTEM,
            {"week": week_key, "from": first_day.isoformat(), "to": yesterday.isoformat(), "messages": _payload(sample)},
        )
        summary = self._summary(raw)
        if not summary:
            return False
        return self._store_digest(
            account_id, group_id, "weekly_digest", identifier, summary,
            {"week": week_key, "from": first_day.isoformat(), "to": yesterday.isoformat(), "window": "week"},
            rows, sample, now,
        )

    def _summary(self, raw) -> str:
        value = _parse_object(raw)
        if value is None:
            return ""
        summary = value.get("summary")
        return summary.strip()[:400] if isinstance(summary, str) else ""

    def _digest_exists(self, identifier: str) -> bool:
        with self.storage.Session() as session:
            return session.scalar(select(FactRow.id).where(FactRow.memory_id == identifier).limit(1)) is not None

    def _human_messages(self, account_id: str, group_id: str, start: datetime, end: datetime, *, limit: int) -> list:
        statement = select(MessageRow).where(
            MessageRow.account_id == account_id,
            MessageRow.group_id == group_id,
            MessageRow.lifecycle_status == "active",
            MessageRow.is_self.is_(False),
            MessageRow.source_role == "human",
            MessageRow.text_kind.notin_(["system", "emoji"]),
            MessageRow.observed_at >= start,
            MessageRow.observed_at < end,
        )
        with self.storage.Session() as session:
            rows = list(session.scalars(statement.order_by(MessageRow.id.desc()).limit(limit)))
        rows.reverse()
        return rows

    def _store_digest(
        self, account_id, group_id, memory_type, identifier, summary, metadata, rows, sample, now,
    ) -> bool:
        source_ids = list(dict.fromkeys(row.external_message_id or str(row.id) for row in sample))[:200]
        if not source_ids:
            log.warning("digest dropped without source messages group=%s type=%s", group_id, memory_type)
            return False
        fact = MemoryFact(
            account_id=account_id,
            group_id=group_id,
            member_id="",
            memory_type=memory_type,
            content=summary[:400],
            confidence=_DIGEST_CONFIDENCE,
            status="active",
            observed_at=now,
            metadata={**metadata, "message_count": len(rows), "sampled_messages": len(sample),
                      "model": str(getattr(self.settings, "llm_model", "") or "")},
            memory_id=identifier,
            task_id=None,
            source_message_ids=source_ids,
            updated_at=now,
        )
        try:
            self.storage.add_scoped_memory(fact)
        except Exception as exc:
            log.warning("digest write failed group=%s type=%s error=%s", group_id, memory_type, type(exc).__name__)
            return False
        return True

    # ------------------------------------------------------------------ reconcile
    def reconcile_members(
        self,
        account_id: str,
        group_id: str,
        *,
        limit_members: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Per-member curation: merge, supersede contradictions, write a summary.

        The model may only choose ids it was given.  Every superseded row stays
        in the ledger with ``valid_to`` so the console/answer can show the
        timeline; only the current projection changes.
        """

        result = {"members": 0, "merged": 0, "superseded": 0, "summaries": 0}
        if not self._model_ready() or not getattr(self.settings, "member_reconcile_enabled", True):
            return result
        now = self._now(now)
        limit = max(1, min(int(limit_members or getattr(self.settings, "member_reconcile_max_members", 5) or 5), 50))
        today = self._local_today(now)
        with self.storage.Session() as session:
            rows = session.execute(
                select(FactRow.member_id, func.max(FactRow.updated_at))
                .where(
                    FactRow.account_id == account_id,
                    FactRow.group_id == group_id,
                    FactRow.member_id != "",
                    FactRow.status.in_(["active", "confirmed"]),
                    FactRow.valid_until.is_(None),
                    FactRow.attribution != "reported",
                    FactRow.memory_type.notin_(list(_UNMERGEABLE_TYPES)),
                )
                .group_by(FactRow.member_id)
                .order_by(func.max(FactRow.updated_at).desc())
                .limit(limit * 3)
            ).all()
        members = [str(member_id) for member_id, _ in rows if member_id]
        for member_id in members:
            if result["members"] >= limit:
                break
            identifier = stable_id(account_id, group_id, "member_summary", member_id, today)
            if self._digest_exists(identifier):
                continue
            with self.storage.Session() as session:
                facts = list(
                    session.scalars(
                        select(FactRow)
                        .where(
                            FactRow.account_id == account_id,
                            FactRow.group_id == group_id,
                            FactRow.member_id == member_id,
                            FactRow.status.in_(["active", "confirmed"]),
                            FactRow.valid_until.is_(None),
                            FactRow.attribution != "reported",
                            FactRow.memory_type.notin_(list(_UNMERGEABLE_TYPES)),
                        )
                        .order_by(FactRow.observed_at.desc(), FactRow.id.desc())
                        .limit(40)
                    )
                )
            if len(facts) < 2:
                continue
            supplied = {fact.memory_id or f"fact-{fact.id}": fact for fact in facts}
            payload = {
                "member_id": member_id,
                "facts": [
                    {
                        "memory_id": fact.memory_id or f"fact-{fact.id}",
                        "kind": fact.memory_type,
                        "content": fact.content[:240],
                        "observed_at": aware(fact.observed_at).isoformat(),
                    }
                    for fact in facts
                ],
            }
            raw = self._complete(RECONCILE_SYSTEM, payload)
            value = _parse_object(raw)
            if value is None:
                continue
            summary = self._summary_text(value.get("summary"))
            current = self._valid_current(value.get("current"), supplied)
            supersede = self._valid_supersede(value.get("supersede"), supplied)
            keep_ids = {identifier for _, _, rows in current for identifier in rows}
            supersede = [item for item in supersede if item[0] not in keep_ids]
            if not summary and not current and not supersede:
                continue
            merged = self._apply_current(account_id, group_id, member_id, current, supplied, now)
            superseded = self._apply_supersede(supersede, now)
            source_ids = self._fact_source_ids(facts)
            traceable_summary = summary if source_ids else ""
            if traceable_summary:
                summary_fact = MemoryFact(
                    account_id=account_id,
                    group_id=group_id,
                    member_id=member_id,
                    memory_type="member_profile_summary",
                    content=traceable_summary,
                    confidence=0.8,
                    status="active",
                    observed_at=now,
                    metadata={
                        "curation": "reconcile",
                        "source_memory_ids": list(supplied.keys())[:40],
                        "model": str(getattr(self.settings, "llm_model", "") or ""),
                    },
                    memory_id=identifier,
                    task_id=None,
                    source_message_ids=source_ids[:40],
                    updated_at=now,
                    asserted_at=now,
                    attribution="self",
                )
                try:
                    self.storage.add_scoped_memory(summary_fact)
                    result["summaries"] += 1
                except Exception as exc:
                    log.warning("member summary write failed member=%s error=%s", member_id, type(exc).__name__)
            self.storage.rebuild_member_profile(
                account_id,
                group_id,
                member_id,
                now=now,
                summary=traceable_summary or None,
                summary_source_ids=source_ids[:40],
            )
            result["members"] += 1
            result["merged"] += merged
            result["superseded"] += superseded
        return result

    def _summary_text(self, value) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:240]

    def _valid_current(self, items, supplied: dict) -> list[tuple[str, str, list[str]]]:
        if not isinstance(items, list):
            return []
        output: list[tuple[str, str, list[str]]] = []
        claimed: set[str] = set()
        for item in items[:8]:
            if not isinstance(item, dict) or not set(item) <= {"kind", "content", "keep_memory_ids"}:
                continue
            kind = str(item.get("kind") or "").strip()
            content = self._summary_text(item.get("content"))
            raw_ids = item.get("keep_memory_ids")
            if kind not in {"preference", "interest", "habit", "experience", "relation"} or not content:
                continue
            if not isinstance(raw_ids, list) or len(raw_ids) < 1:
                continue
            ids = [str(value) for value in raw_ids if isinstance(value, str)]
            if len(ids) != len(raw_ids):
                continue
            ids = list(dict.fromkeys(ids))
            if any(identifier not in supplied for identifier in ids):
                continue
            if claimed & set(ids):
                continue
            claimed |= set(ids)
            output.append((kind, content, ids))
        return output

    def _valid_supersede(self, items, supplied: dict) -> list[tuple[str, str]]:
        if not isinstance(items, list):
            return []
        output: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in items[:20]:
            if not isinstance(item, dict) or not set(item) <= {"memory_id", "reason", "confidence"}:
                continue
            identifier = str(item.get("memory_id") or "").strip()
            reason = self._summary_text(item.get("reason"))
            confidence = item.get("confidence")
            if identifier not in supplied or identifier in seen or not reason:
                continue
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                continue
            if float(confidence) < 0.7:
                continue
            seen.add(identifier)
            output.append((identifier, reason))
        return output

    def _apply_current(self, account_id, group_id, member_id, current, supplied, now: datetime) -> int:
        """Merge groups of >=2 kept facts into one row; singletons stay as-is."""

        applied = 0
        for kind, content, ids in current:
            rows = [supplied[identifier] for identifier in ids]
            if len(rows) < 2:
                continue
            source_ids = self._fact_source_ids(rows)
            if not source_ids:
                continue
            memory_id = stable_id(account_id, group_id, member_id, kind, "reconcile_merge", *sorted(ids))
            fact = MemoryFact(
                account_id=account_id,
                group_id=group_id,
                member_id=member_id,
                memory_type=kind,
                content=content,
                confidence=max(float(row.confidence or 0.0) for row in rows),
                status="active",
                observed_at=max(aware(row.observed_at) for row in rows),
                valid_from=min(aware(row.observed_at) for row in rows),
                metadata={"curation": "reconcile", "merged_from": ids},
                memory_id=memory_id,
                task_id=None,
                source_message_ids=source_ids,
                updated_at=now,
                asserted_at=max(aware(row.observed_at) for row in rows),
                attribution="self",
            )
            try:
                merged_id = self.storage.add_scoped_memory(fact)
            except Exception as exc:
                log.warning("reconcile merge failed member=%s error=%s", member_id, type(exc).__name__)
                continue
            if not merged_id:
                continue
            self._mark_superseded([row.id for row in rows], merged_id, now)
            applied += 1
        return applied

    def _apply_supersede(self, items: list[tuple[str, str]], now: datetime) -> int:
        if not items:
            return 0
        ids = set()
        with self.storage.Session() as session:
            for identifier, _reason in items:
                row = session.scalar(select(FactRow).where(FactRow.memory_id == identifier).limit(1))
                if row is None or row.status not in {"active", "confirmed"}:
                    continue
                row.status = "superseded"
                row.valid_to = now
                row.valid_until = now
                row.updated_at = now
                ids.add(row.id)
            session.commit()
        return len(ids)

    def _mark_superseded(self, ids: list[int], merged_id: int, now: datetime) -> None:
        with self.storage.Session.begin() as session:
            merged = session.get(FactRow, merged_id)
            for row_id in ids:
                row = session.get(FactRow, row_id)
                if row is None or row.id == merged_id or row.status not in {"active", "confirmed"}:
                    continue
                row.status = "superseded"
                row.valid_to = now
                row.valid_until = now
                row.updated_at = now
                if merged is not None and merged.memory_id:
                    row.superseded_by = merged.memory_id

    @staticmethod
    def _fact_source_ids(rows) -> list[str]:
        output: list[str] = []
        for row in rows:
            for source in _row_source_ids(row):
                if source not in output:
                    output.append(source)
        return output[:200]

    # ------------------------------------------------------------------ archive
    def archive_stale(self, account_id: str, group_id: str, *, now: datetime | None = None, days: int = 120) -> int:
        """Archive decayed facts that no retrieval ever referenced."""

        if not self._model_ready():
            return 0
        now = self._now(now)
        days = max(1, int(days or 120))
        cutoff = now - timedelta(days=days)
        with self.storage.Session.begin() as session:
            rows = list(
                session.scalars(
                    select(FactRow).where(
                        FactRow.account_id == account_id,
                        FactRow.group_id == group_id,
                        FactRow.status.in_(["active", "candidate"]),
                        FactRow.confidence < 0.5,
                        FactRow.observed_at < cutoff,
                    )
                )
            )
            if not rows:
                return 0
            referenced = self._referenced_ids(session, account_id, group_id)
            archived = 0
            touched: set[str] = set()
            for row in rows:
                if (row.memory_id or f"fact-{row.id}") in referenced:
                    continue
                row.status = "archived"
                row.updated_at = now
                touched.add(row.member_id)
                archived += 1
        for member_id in touched:
            if member_id:
                self.storage.rebuild_member_profile(account_id, group_id, member_id, now=now)
        return archived

    def _referenced_ids(self, session, account_id: str, group_id: str) -> set[str]:
        """Every memory id any retrieval trace mentioned, accepted or dropped."""

        referenced: set[str] = set()

        def collect(value, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in _REFERENCE_KEYS and isinstance(item, str):
                        referenced.add(item)
                    elif key == "used_evidence_ids" and isinstance(item, list):
                        referenced.update(str(entry) for entry in item if isinstance(entry, str))
                    else:
                        collect(item, depth + 1)
            elif isinstance(value, list):
                for item in value[:2000]:
                    collect(item, depth + 1)

        traces = session.scalars(
            select(RetrievalTraceRow.trace_json)
            .where(RetrievalTraceRow.account_id == account_id, RetrievalTraceRow.group_id == group_id)
            .order_by(RetrievalTraceRow.created_at.desc())
            .limit(2000)
        )
        for trace_json in traces:
            try:
                collect(json.loads(trace_json))
            except (TypeError, ValueError):
                continue
        return referenced

    # ------------------------------------------------------------------ refresh
    def refresh(self, account_id: str, group_id: str, *, now: datetime | None = None) -> dict:
        """Run merge, digests and archive in order, once per local day per instance."""

        summary = {
            "merged": 0,
            "reconciled": 0,
            "superseded": 0,
            "summaries": 0,
            "daily": 0,
            "weekly": 0,
            "archived": 0,
        }
        if not self._model_ready():
            return summary
        now = self._now(now)
        day = self._local_today(now)
        if self._should_run(account_id, group_id, "merge", day):
            summary["merged"] = self.merge_similar_facts(account_id, group_id)
            self._mark_run(account_id, group_id, "merge", day)
        if self._should_run(account_id, group_id, "reconcile", day):
            reconciled = self.reconcile_members(account_id, group_id, now=now)
            summary["reconciled"] = reconciled["members"]
            summary["superseded"] = reconciled["superseded"]
            summary["summaries"] = reconciled["summaries"]
            self._mark_run(account_id, group_id, "reconcile", day)
        if self._should_run(account_id, group_id, "digests", day):
            summary.update(self.build_digests(account_id, group_id, now=now))
            self._mark_run(account_id, group_id, "digests", day)
        if self._should_run(account_id, group_id, "archive", day):
            summary["archived"] = self.archive_stale(account_id, group_id, now=now)
            self._mark_run(account_id, group_id, "archive", day)
        return summary

    def _should_run(self, account_id: str, group_id: str, kind: str, day: str) -> bool:
        return self._last_run.get((account_id, group_id, kind)) != day

    def _mark_run(self, account_id: str, group_id: str, kind: str, day: str) -> None:
        # Only a completed step is recorded, so a raised error (not a failed
        # model call, which is a skip) is retried on the next maintenance tick.
        self._last_run[(account_id, group_id, kind)] = day
