"""Narrative layer: per-day group timeline and per-week member relations.

Two things live here, and nothing else reads raw messages to build them:

* ``refresh_timeline`` compresses one local day of a group into a short
  summary with topics, active members and the day's emotional shift.  The
  summary keeps the ids of the messages it was built from, so every sentence
  stays auditable back to source.
* ``refresh_relations`` aggregates a week of directed member-to-member
  relations ("常一起打游戏", "互相调侃").  Relations are stored per direction
  and only surfaced for members the current question actually involves.

Both run on the existing maintenance thread and are idempotent per day/week,
so a restart cannot double-write or skip a period.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .memory_jobs import aware, encode
from .storage import MessageRow

log = logging.getLogger(__name__)

TIMELINE_SYSTEM = (
    "你在为一个微信群做当天的事实摘要，只返回 JSON："
    '{"summary":"不超过120字的中文摘要","topics":["话题"],'
    '"emotion":{"start":"开头情绪","end":"结尾情绪","shift":"一句话变化"},'
    '"active_members":[{"member_id":"成员ID","member_name":"名字","note":"一句他今天做了什么"}]}'
    "。只写消息里出现过的事，不推测动机，不评价人品；消息不足以支撑摘要时返回空字符串与空数组。"
)
RELATION_SYSTEM = (
    "你在整理微信群成员之间的关系，只返回 JSON："
    '{"relations":[{"member_id":"A","other_member_id":"B","relation":"不超过16字的描述","strength":0.0}]}'
    "。只写消息里能直接看出的互动（常一起聊什么、互相调侃、互相帮忙、有分歧），"
    "strength 取 0 到 1；没有可依据的互动就不要输出这一对。不要写性格评价，不要写没有证据的关系。"
)


def _local_day_bounds(settings, day: str) -> tuple[datetime, datetime]:
    """Local midnight in ``group_timezone``, returned as the naive UTC the
    ``messages.observed_at`` column actually stores."""

    zone = ZoneInfo(settings.group_timezone)
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


class Narrative:
    def __init__(self, runtime):
        self.runtime, self.storage, self.settings = runtime, runtime.storage, runtime.settings

    # ---------------------------------------------------------------- timeline
    def refresh_timeline(self, account_id: str, group_id: str, *, day: str | None = None) -> dict | None:
        """Summarize one day.  Returns the stored row, or None when skipped."""

        now = self.runtime.clock()
        day = day or now.astimezone(ZoneInfo(self.settings.group_timezone)).date().isoformat()
        start, end = _local_day_bounds(self.settings, day)
        if end > now.astimezone(timezone.utc).replace(tzinfo=None):
            return None
        with self.storage.Session() as session:
            exists = session.scalar(
                select(MessageRow.id).where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.observed_at >= start,
                    MessageRow.observed_at < end,
                ).limit(1)
            )
            if exists is None:
                return None
            rows = list(session.scalars(
                select(MessageRow).where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.lifecycle_status == "active",
                    MessageRow.is_self.is_(False),
                    MessageRow.source_role == "human",
                    MessageRow.text_kind.notin_(["system", "emoji"]),
                    MessageRow.observed_at >= start,
                    MessageRow.observed_at < end,
                ).order_by(MessageRow.id)
            ))
        if len(rows) < 3:
            return None
        sample = self._sample(rows)
        payload = [
            {"id": row.external_message_id or str(row.id), "member_id": row.member_id,
             "member_name": row.member_name, "at": aware(row.observed_at).isoformat(),
             "text": (row.text_normalized or row.text)[:280]}
            for row in sample
        ]
        summary, topics, emotion, members = "", [], {}, []
        if self.runtime.llm and self.settings.llm_api_key:
            try:
                raw = self.runtime.llm.complete(TIMELINE_SYSTEM, encode({"day": day, "messages": payload}),
                                               max_tokens=800, timeout_seconds=min(60.0, self.settings.llm_timeout_seconds))
                value = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip()) or "{}")
                summary = str(value.get("summary") or "")[:400]
                topics = [str(item)[:40] for item in value.get("topics") or []][:12]
                emotion = value.get("emotion") if isinstance(value.get("emotion"), dict) else {}
                members = [
                    {"member_id": str(item.get("member_id") or ""), "member_name": str(item.get("member_name") or ""),
                     "note": str(item.get("note") or "")[:80]}
                    for item in value.get("active_members") or [] if isinstance(item, dict)
                ][:20]
                model = getattr(self.settings, "llm_model", "")
            except Exception as exc:  # model failure must not break maintenance
                log.warning("timeline summary failed group=%s day=%s error=%s", group_id, day, type(exc).__name__)
                return None
        else:
            model = ""
        if not summary:
            return None
        known = {row.member_id for row in rows}
        members = [item for item in members if item["member_id"] in known]
        self.storage.upsert_group_timeline(
            account_id=account_id, group_id=group_id, day=day, summary=summary, topics=topics,
            active_members=members, emotion=emotion,
            source_message_ids=[row.external_message_id or str(row.id) for row in rows][:200],
            message_count=len(rows), model=model,
        )
        return {"day": day, "summary": summary, "topics": topics, "emotion": emotion, "active_members": members}

    def _sample(self, rows, *, max_chars: int = 8000) -> list:
        """Even sample across the day, keeping the first and last message."""

        if not rows:
            return []
        if len(rows) <= 60:
            return rows
        window = max(1, len(rows) // 60)
        picked = [rows[index] for index in range(0, len(rows), window)][:60]
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

    # --------------------------------------------------------------- relations
    def refresh_relations(self, account_id: str, group_id: str, *, days: int = 7) -> int:
        """Aggregate the last ``days`` of interaction into directed relations."""

        now = self.runtime.clock()
        start = (now - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)
        until = now.astimezone(timezone.utc).replace(tzinfo=None)
        with self.storage.Session() as session:
            rows = list(session.scalars(
                select(MessageRow).where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.lifecycle_status == "active",
                    MessageRow.is_self.is_(False),
                    MessageRow.source_role == "human",
                    MessageRow.observed_at >= start,
                    MessageRow.observed_at <= until,
                ).order_by(MessageRow.id.desc()).limit(1500)
            ))
        if len(rows) < 20 or not (self.runtime.llm and self.settings.llm_api_key):
            return 0
        rows.reverse()
        names = {}
        for row in rows:
            names.setdefault(row.member_id, row.member_name)
        payload = [
            {"id": row.external_message_id or str(row.id), "member_id": row.member_id,
             "member_name": row.member_name, "text": (row.text_normalized or row.text)[:200]}
            for row in self._sample(rows, max_chars=6000)
        ]
        try:
            raw = self.runtime.llm.complete(
                RELATION_SYSTEM,
                encode({"members": [{"member_id": key, "member_name": value} for key, value in names.items()],
                        "messages": payload}),
                max_tokens=1200, timeout_seconds=min(60.0, self.settings.llm_timeout_seconds),
            )
            value = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip()) or "{}")
        except Exception as exc:
            log.warning("relation aggregation failed group=%s error=%s", group_id, type(exc).__name__)
            return 0
        stored = 0
        for item in value.get("relations") or []:
            if not isinstance(item, dict):
                continue
            member_id, other = str(item.get("member_id") or ""), str(item.get("other_member_id") or "")
            if member_id not in names or other not in names or member_id == other:
                continue
            try:
                strength = max(0.0, min(1.0, float(item.get("strength") or 0)))
            except (TypeError, ValueError):
                continue
            relation = str(item.get("relation") or "").strip()[:64]
            if not relation:
                continue
            self.storage.upsert_member_relation(
                account_id=account_id, group_id=group_id, member_id=member_id, other_member_id=other,
                relation=relation, strength=strength, evidence=[],
            )
            stored += 1
        return stored
