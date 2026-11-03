"""Read-only data access for the server-rendered console.

The console only reads.  Writes keep going through the guarded
``DesktopController`` / ``MemoryConsole`` HTTP APIs.  Importing the ORM row
classes directly keeps page rendering free of ad-hoc SQL strings and of the
legacy single-page HTML.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import and_, func, inspect, or_, select

from ..memory_runtime import MemoryRuntime
from ..memory_schema import EpisodeRow, RelationshipStateRow, RetrievalTraceRow
from ..storage import (
    AIProfileRow,
    FactRow,
    MessageRow,
    ReplyRecordRow,
    UserProfileRow,
)
from ..timefmt import stamp

PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("preferences", "偏好"),
    ("interests", "兴趣"),
    ("tools", "常用工具"),
    ("expression_habits", "表达习惯"),
    ("work_style", "工作方式"),
    ("constraints", "限制条件"),
    ("confirmed_info", "确认信息"),
    ("task_requirements", "任务要求"),
    ("corrections", "更正"),
)

SEMANTIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("interests", "兴趣与话题"),
    ("expression_style", "表达风格"),
    ("atmosphere", "氛围"),
    ("interaction_patterns", "互动习惯"),
    ("memes", "群梗"),
    ("participation_guidance", "接话建议"),
    ("observations", "暂定观察"),
    ("explicit_rules", "明确群约定"),
)

EVENT_TYPES: tuple[tuple[str, str], ...] = (
    ("insult", "辱骂"),
    ("praise", "夸奖"),
    ("comfort", "安慰"),
    ("delight", "戳中"),
    ("apology", "道歉"),
    ("correction", "更正"),
    ("unanswered_bid", "邀请未回应"),
)
EVENT_TYPE_LABELS = dict(EVENT_TYPES)
LIFECYCLE_LABELS = {
    "active": "有效",
    "candidate": "待确认",
    "reverted": "已撤销",
    "superseded": "已更新",
    "expired": "已过期",
}
MODE_LABELS = {"warm": "亲近", "normal": "平常", "playful_cool": "短暂冷淡"}
REPLY_STATUS_LABELS = {
    "sent": "已发送",
    "skipped": "已跳过",
    "failed": "发送失败",
    "processing": "处理中",
    "blocked": "已拦截",
}
# Reasons that mean a human should look at the send ledger.
ATTENTION_REASONS = {
    "sending_disabled",
    "stop_file_present",
    "circuit_breaker_rate",
    "group_not_allowlisted",
    "private_not_allowlisted",
    "member_not_allowlisted",
}
PROCESSING_STALE_AFTER = timedelta(minutes=10)


def _dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _list(value: object) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "description", "label"):
            if value.get(key):
                return str(value[key]).strip()
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (list, tuple)):
        return "；".join(_text(item) for item in value if _text(item))
    return str(value).strip()


def _bounded_entries(value: object, *, limit: int = 30) -> list[str]:
    """Flatten profile values (strings, dicts or lists) into display lines."""

    raw = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in raw:
        rendered = _text(item)
        if rendered:
            output.append(rendered[:600])
        if len(output) >= limit:
            break
    return output


def member_url(member_id: str, account_id: str, group_id: str) -> str:
    return "/memory?" + urlencode(
        {"view": "members", "account_id": account_id, "group_id": group_id, "focus_member": member_id}
    )


def group_url(group_id: str, account_id: str) -> str:
    return "/memory?" + urlencode({"view": "groups", "account_id": account_id, "group_id": group_id})


class ConsoleData:
    """Dashboard-safe projections of the local database.

    Every method opens a short session and returns plain dictionaries so the
    templates never touch SQLAlchemy rows.
    """

    def __init__(self, storage, settings):
        self.storage = storage
        self.settings = settings

    # ------------------------------------------------------------------ time
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def time_text(self, value: object) -> str:
        parsed = _parse_time(value)
        if parsed is None:
            return ""
        return stamp(parsed, timezone_name=self.settings.group_timezone)

    def today_start_utc(self) -> datetime:
        from ..timefmt import resolve_zone

        zone = resolve_zone(self.settings.group_timezone)
        local_now = datetime.now(zone)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_local.astimezone(timezone.utc)

    # ------------------------------------------------------------- helpers
    def _activity_map(self) -> dict[tuple[str, str], tuple[datetime | None, int]]:
        with self.storage.Session() as session:
            rows = session.execute(
                select(
                    MessageRow.account_id,
                    MessageRow.group_id,
                    func.max(MessageRow.observed_at),
                    func.count(MessageRow.id),
                )
                .where(MessageRow.lifecycle_status == "active")
                .group_by(MessageRow.account_id, MessageRow.group_id)
            ).all()
        return {(str(account), str(group)): (observed, int(count)) for account, group, observed, count in rows}

    def member_names(self, account_id: str, group_id: str, *, limit: int = 2000) -> dict[str, str]:
        with self.storage.Session() as session:
            rows = session.execute(
                select(MessageRow.member_id, MessageRow.member_name)
                .where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.member_id != "",
                    MessageRow.is_self.is_(False),
                )
                .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                .limit(limit)
            ).all()
        names: dict[str, str] = {}
        for member_id, member_name in rows:
            member_id, member_name = str(member_id), str(member_name or "").strip()
            if member_id and member_id not in names and member_name:
                names[member_id] = member_name
        return names

    def _event_view(self, payload: dict) -> dict:
        event_type = str(payload.get("event_type") or "")
        status = str(payload.get("lifecycle_status") or "")
        confidence = float(payload.get("confidence") or 0)
        return {
            "memory_id": str(payload.get("memory_id") or ""),
            "object_version": int(payload.get("object_version") or 1),
            "event_type": event_type,
            "type_label": EVENT_TYPE_LABELS.get(event_type, event_type or "事件"),
            "actor_id": str(payload.get("actor_member_id") or ""),
            "actor_name": str(payload.get("actor_name") or payload.get("actor_member_id") or "未知成员"),
            "content": str(payload.get("content") or ""),
            "evidence_excerpt": str(payload.get("evidence_excerpt") or ""),
            "sources": [str(item) for item in (payload.get("source_message_ids") or [])][:8],
            "status": status,
            "status_label": LIFECYCLE_LABELS.get(status, status or "未知"),
            "confidence_pct": round(max(0.0, min(1.0, confidence)) * 100),
            "at_text": self.time_text(payload.get("occurred_at")),
            "at_iso": str(payload.get("occurred_at") or ""),
        }

    def _events_by_member(self, account_id: str, group_id: str, *, limit: int = 400) -> dict[str, list[dict]]:
        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    select(EpisodeRow)
                    .where(EpisodeRow.account_id == account_id, EpisodeRow.group_id == group_id)
                    .order_by(EpisodeRow.occurred_at.desc())
                    .limit(limit)
                )
            )
        output: dict[str, list[dict]] = {}
        for row in rows:
            payload = MemoryRuntime.episode_payload(row)
            output.setdefault(str(row.actor_id or ""), []).append(self._event_view(payload))
        return output

    def event_views(self, payloads: list[dict]) -> list[dict]:
        return [self._event_view(payload) for payload in payloads if isinstance(payload, dict)]

    def _profile_view(self, row: UserProfileRow | None) -> dict:
        data = _dict(row.profile_json) if row is not None else {}
        fields = []
        for key, label in PROFILE_FIELDS:
            items = _bounded_entries(data.get(key)) if key in data else []
            if items:
                fields.append({"key": key, "label": label, "items": items})
        return {
            "fields": fields,
            "count": sum(len(field["items"]) for field in fields),
            "updated_at": self.time_text(row.updated_at) if row is not None else "",
            "raw": data,
        }

    def _profile_preview(self, profile: dict, *, limit: int = 3) -> list[str]:
        preview: list[str] = []
        for field in profile["fields"]:
            for item in field["items"]:
                preview.append(f"{field['label']}：{item}")
                if len(preview) >= limit:
                    return preview
        return preview

    def _snapshot(self, runtime, account_id: str, group_id: str, member_id: str) -> dict:
        try:
            return runtime.emotion.snapshot(account_id, group_id, member_id, now=self.now())
        except Exception:  # a broken emotion row must not blank the page
            return {}

    def _relationship_view(self, snapshot: dict, *, member_id: str = "", member_name: str = "") -> dict:
        affinity = snapshot.get("affinity")
        try:
            affinity_value = float(affinity) if affinity is not None else 50.0
        except (TypeError, ValueError):
            affinity_value = 50.0
        mode = str(snapshot.get("mode") or "normal")
        return {
            "member_id": str(snapshot.get("member_id") or member_id),
            "member_name": member_name or str(snapshot.get("member_id") or member_id),
            "affinity_value": affinity_value,
            "affinity": round(affinity_value, 1),
            "base_affinity": round(float(snapshot.get("base_affinity") or 50.0), 1),
            "positive_credit": round(float(snapshot.get("positive_credit") or 0.0), 1),
            "penalty": round(float(snapshot.get("penalty") or 0.0), 1),
            "mode": mode,
            "mode_label": MODE_LABELS.get(mode, mode),
            "labels": snapshot.get("labels") or {},
            "top_labels": snapshot.get("top_labels") or [],
            "penalties": snapshot.get("penalties") or [],
            "evaluated_at": self.time_text(snapshot.get("evaluated_at")),
        }

    # --------------------------------------------------------------- scopes
    def _conversation_type(self, account_id: str, group_id: str, group_name: str) -> str:
        if group_id.endswith("@chatroom"):
            return "group"
        private_names = {
            str(value).strip().casefold()
            for value in (getattr(self.settings, "wechat_private_chat_names", None) or [])
            if str(value).strip()
        }
        if group_id.casefold() in private_names or group_name.casefold() in private_names:
            return "private"
        with self.storage.Session() as session:
            rows = session.scalars(
                select(MessageRow.media_metadata_json)
                .where(MessageRow.account_id == account_id, MessageRow.group_id == group_id)
                .order_by(MessageRow.id.desc())
                .limit(20)
            ).all()
        for raw in rows:
            kind = str(_dict(raw).get("conversation_type") or "").casefold()
            if kind in {"group", "private"}:
                return kind
        return "group"

    def scopes(self, memory_console) -> list[dict]:
        try:
            rows = list(memory_console.groups())
        except Exception:
            rows = []
        activity = self._activity_map()
        output: list[dict] = []
        for row in rows:
            account_id = str(row.get("account_id") or "")
            group_id = str(row.get("group_id") or "")
            if not account_id or not group_id:
                continue
            group_name = str(row.get("group_name") or group_id)
            last_active, counted = activity.get((account_id, group_id), (None, 0))
            output.append(
                {
                    "account_id": account_id,
                    "group_id": group_id,
                    "group_name": group_name,
                    "conversation_type": self._conversation_type(account_id, group_id, group_name),
                    "message_count": int(row.get("message_count") or counted or 0),
                    "analysis": dict(row.get("analysis") or {}),
                    "features": dict(row.get("features") or {}),
                    "last_active_at": last_active,
                    "last_active_text": self.time_text(last_active),
                    "last_active_iso": last_active.isoformat() if last_active else "",
                    "url": group_url(group_id, account_id),
                }
            )
        output.sort(key=lambda scope: scope["last_active_iso"] or "", reverse=True)
        return output

    def select_scope(self, scopes: list[dict], account_id: str = "", group_id: str = "") -> dict | None:
        if not scopes:
            return None
        for scope in scopes:
            if group_id and scope["group_id"] != group_id:
                continue
            if account_id and scope["account_id"] != account_id:
                continue
            return scope
        return None

    # --------------------------------------------------------------- members
    def members(self, runtime, scopes: list[dict]) -> list[dict]:
        output: list[dict] = []
        for scope in scopes:
            account_id, group_id = scope["account_id"], scope["group_id"]
            with self.storage.Session() as session:
                relationships = {
                    str(row.member_id): row
                    for row in session.scalars(
                        select(RelationshipStateRow).where(
                            RelationshipStateRow.account_id == account_id,
                            RelationshipStateRow.group_id == group_id,
                        )
                    )
                }
                profiles = {
                    str(row.member_id): row
                    for row in session.scalars(
                        select(UserProfileRow).where(
                            UserProfileRow.account_id == account_id,
                            UserProfileRow.group_id == group_id,
                        )
                    )
                }
            names = self.member_names(account_id, group_id)
            events = self._events_by_member(account_id, group_id)
            member_ids = sorted(
                member_id
                for member_id in (set(relationships) | set(profiles) | set(names))
                if not self.storage.is_self_member(account_id, member_id)
            )
            rows: list[dict] = []
            for member_id in member_ids:
                member_name = (
                    str(getattr(profiles.get(member_id), "member_name", "") or "").strip()
                    or names.get(member_id, "")
                    or member_id
                )
                snapshot = self._snapshot(runtime, account_id, group_id, member_id)
                relationship = self._relationship_view(snapshot, member_id=member_id, member_name=member_name)
                profile = self._profile_view(profiles.get(member_id))
                member_events = events.get(member_id, [])[:3]
                rows.append(
                    {
                        "member_id": member_id,
                        "member_name": member_name,
                        "affinity": relationship["affinity"],
                        "affinity_value": relationship["affinity_value"],
                        "mode_label": relationship["mode_label"],
                        "profile": profile,
                        "profile_preview": self._profile_preview(profile),
                        "recent_events": member_events,
                        "last_event_text": member_events[0]["at_text"] if member_events else "",
                        "url": member_url(member_id, account_id, group_id),
                    }
                )
            rows.sort(key=lambda item: (-item["affinity_value"], item["member_name"]))
            output.append(
                {
                    **scope,
                    "members": rows,
                    "member_count": len(rows),
                    "attention_count": sum(1 for row in rows if row["affinity_value"] < 40),
                }
            )
        return output

    def _member_in_scope(self, account_id: str, group_id: str, member_id: str) -> bool:
        with self.storage.Session() as session:
            if session.scalar(
                select(UserProfileRow.id)
                .where(
                    UserProfileRow.account_id == account_id,
                    UserProfileRow.group_id == group_id,
                    UserProfileRow.member_id == member_id,
                )
                .limit(1)
            ):
                return True
            if session.scalar(
                select(RelationshipStateRow.member_id)
                .where(
                    RelationshipStateRow.account_id == account_id,
                    RelationshipStateRow.group_id == group_id,
                    RelationshipStateRow.member_id == member_id,
                )
                .limit(1)
            ):
                return True
            return (
                session.scalar(
                    select(MessageRow.id)
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                        MessageRow.member_id == member_id,
                        MessageRow.lifecycle_status == "active",
                    )
                    .limit(1)
                )
                is not None
            )

    def resolve_member_scope(
        self, scopes: list[dict], member_id: str, account_id: str = "", group_id: str = ""
    ) -> dict | None:
        preferred = [scope for scope in scopes if scope["account_id"] == account_id or not account_id]
        preferred = [scope for scope in preferred if scope["group_id"] == group_id or not group_id]
        for scope in preferred + [scope for scope in scopes if scope not in preferred]:
            if self._member_in_scope(scope["account_id"], scope["group_id"], member_id):
                return scope
        return None

    def member_detail(
        self,
        runtime,
        scopes: list[dict],
        member_id: str,
        account_id: str = "",
        group_id: str = "",
    ) -> dict | None:
        scope = self.resolve_member_scope(scopes, member_id, account_id, group_id)
        if scope is None:
            return None
        account, group = scope["account_id"], scope["group_id"]
        with self.storage.Session() as session:
            profile_row = session.scalar(
                select(UserProfileRow).where(
                    UserProfileRow.account_id == account,
                    UserProfileRow.group_id == group,
                    UserProfileRow.member_id == member_id,
                )
            )
        names = self.member_names(account, group)
        member_name = (
            str(getattr(profile_row, "member_name", "") or "").strip()
            or names.get(member_id, "")
            or member_id
        )
        snapshot = self._snapshot(runtime, account, group, member_id)
        relationship = self._relationship_view(snapshot, member_id=member_id, member_name=member_name)
        profile = self._profile_view(profile_row)
        events = self.event_views(
            runtime.recent_events(
                account,
                group,
                member_id=member_id,
                include_inactive=True,
                limit=30,
            )
        )
        messages = self.recent_messages(account, group, member_id=member_id, limit=30)
        return {
            "scope": scope,
            "member_id": member_id,
            "member_name": member_name,
            "relationship": relationship,
            "profile": profile,
            "profile_history": self._profile_history(profile_row),
            "events": events,
            "messages": messages,
            "message_count": len(messages),
        }

    def _profile_history(self, row: UserProfileRow | None) -> list[dict]:
        if row is None:
            return []
        entries = []
        for item in _list(row.history_json)[-20:]:
            if not isinstance(item, dict):
                continue
            changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
            labels = [label for key, label in PROFILE_FIELDS if key in changes]
            entries.append(
                {
                    "at_text": self.time_text(item.get("updated_at")),
                    "changes": "、".join(labels) or "画像更新",
                    "sources": [str(value) for value in (item.get("source_message_ids") or [])][:6],
                }
            )
        return list(reversed(entries))

    # ---------------------------------------------------------------- groups
    def semantic(self, memory_console, account_id: str, group_id: str) -> dict:
        try:
            snapshot = memory_console.profiles.snapshot(account_id, group_id)
        except Exception:
            return {"version": 0, "refresh_required": False, "sections": {}, "coverage": {}, "error": True}
        data = snapshot.get("semantic_v2") or {}
        sections: dict[str, dict] = {}
        for key, label in SEMANTIC_FIELDS:
            entries = data.get(key) or []
            sections[key] = {
                "label": label,
                "entries": [self._semantic_entry(item) for item in entries if isinstance(item, dict)][:12],
            }
        return {
            "version": int(snapshot.get("semantic_version") or 0),
            "refresh_required": bool(snapshot.get("refresh_required")),
            "sections": sections,
            "interests": sections["interests"]["entries"],
            "atmosphere": sections["atmosphere"]["entries"],
            "memes": sections["memes"]["entries"],
            "coverage": self._semantic_coverage(data.get("coverage") or {}),
            "error": False,
        }

    def _semantic_entry(self, item: dict) -> dict:
        classification = str(item.get("classification") or "")
        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        sources = [
            str(value)
            for value in (item.get("evidence_message_ids") or [])
            if str(value).strip()
        ][:8]
        return {
            "text": _text(item.get("description") or item.get("topic") or item.get("phrase")),
            "topic": str(item.get("topic") or ""),
            "phrase": str(item.get("phrase") or ""),
            "confidence_pct": round(max(0.0, min(1.0, confidence)) * 100),
            "classification": classification,
            "classification_label": {"stable": "稳定兴趣", "recent": "近期话题"}.get(
                classification, ""
            ),
            "overused": bool(item.get("overused")),
            "participants": item.get("participants"),
            "days": item.get("days"),
            "discussions": item.get("independent_discussions"),
            "sources": sources,
            "at_text": self.time_text(item.get("last_seen_at")),
        }

    def _semantic_coverage(self, coverage: dict) -> dict:
        if not isinstance(coverage, dict) or not coverage:
            return {}
        return {
            "messages": coverage.get("messages") or 0,
            "sampled": coverage.get("sampled_messages") or 0,
            "participants": coverage.get("participants") or 0,
            "updated_at": self.time_text(coverage.get("updated_at")),
            "insufficient": bool(coverage.get("insufficient")),
        }

    def _ai_counters(self, account_id: str, group_id: str) -> dict:
        with self.storage.Session() as session:
            row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == account_id, AIProfileRow.group_id == group_id
                )
            )
        data = _dict(row.profile_json) if row is not None else {}
        words = data.get("vocabulary") or {}
        topics = data.get("topic_counts") or {}

        def ranked(values: dict) -> list[dict]:
            pairs = []
            for key, value in values.items() if isinstance(values, dict) else []:
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                pairs.append({"name": str(key), "count": count})
            return sorted(pairs, key=lambda item: (-item["count"], item["name"]))[:10]

        return {
            "message_count": int(data.get("message_count") or 0),
            "words": ranked(words),
            "topics": ranked(topics),
            "humor": int(data.get("humor_count") or 0),
            "formal": int(data.get("formal_count") or 0),
            "technical": int(data.get("technical_count") or 0),
            "questions": int(data.get("question_count") or 0),
            "short": int(data.get("short_message_count") or 0),
            "updated_at": self.time_text(row.updated_at) if row is not None else "",
        }

    def timeline(self, account_id: str, group_id: str) -> tuple[list[dict], bool]:
        """Read the narrative layer when it exists; never create its table.

        The table belongs to the narrative worker (P8).  This page only reads
        it, and falls back to an empty list plus a clear hint while the table
        or rows are absent.
        """

        try:
            available = bool(inspect(self.storage.engine).has_table("group_timeline"))
        except Exception:
            available = False
        if not available:
            return [], False
        try:
            rows = self.storage.recent_timeline(account_id, group_id, days=31)
        except Exception:
            return [], True
        output = []
        for row in rows:

            def names(values) -> str:
                labels = []
                for item in values or []:
                    if isinstance(item, dict):
                        labels.append(str(item.get("name") or item.get("member_name") or item.get("member_id") or ""))
                    else:
                        labels.append(str(item))
                return "、".join(label for label in labels if label)

            output.append(
                {
                    "date": row.get("day") or "",
                    "summary": row.get("summary") or "",
                    "topics": "、".join(str(value) for value in (row.get("topics") or [])),
                    "members": names(row.get("active_members")),
                    "message_count": int(row.get("message_count") or 0),
                }
            )
        return output, True

    def _group_payload(self, memory_console, scope: dict) -> dict:
        account_id, group_id = scope["account_id"], scope["group_id"]
        semantic = self.semantic(memory_console, account_id, group_id)
        try:
            style = self.storage.group_style_snapshot(account_id, group_id)
        except Exception:
            style = {"enabled": False, "ready": False, "profile": "", "observed_count": 0}
        timeline, timeline_available = self.timeline(account_id, group_id)
        return {
            **scope,
            "semantic": semantic,
            "style": style,
            "ai": self._ai_counters(account_id, group_id),
            "timeline": timeline,
            "timeline_available": timeline_available,
            "messages": self.recent_messages(account_id, group_id, limit=20),
            "url": group_url(group_id, account_id),
        }

    def groups(self, memory_console, scopes: list[dict]) -> list[dict]:
        return [self._group_payload(memory_console, scope) for scope in scopes]

    def group(self, memory_console, scopes: list[dict], group_id: str, account_id: str = "") -> dict | None:
        scope = self.select_scope(scopes, account_id=account_id, group_id=group_id)
        if scope is None:
            return None
        return self._group_payload(memory_console, scope)

    # ----------------------------------------------------------------- facts
    def facts(self, scope: dict, member_id: str = "", memory_type: str = "") -> dict:
        account_id, group_id = scope["account_id"], scope["group_id"]
        statement = select(FactRow).where(
            FactRow.account_id == account_id,
            FactRow.group_id == group_id,
            FactRow.status != "archived",
        )
        if member_id:
            statement = statement.where(FactRow.member_id == member_id)
        if memory_type:
            statement = statement.where(FactRow.memory_type == memory_type)
        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    statement.order_by(
                        func.coalesce(FactRow.updated_at, FactRow.observed_at).desc(),
                        FactRow.id.desc(),
                    ).limit(200)
                )
            )
            types = sorted(
                str(value)
                for value in session.scalars(
                    select(FactRow.memory_type)
                    .where(FactRow.account_id == account_id, FactRow.group_id == group_id)
                    .distinct()
                )
                if value
            )
            member_rows = session.execute(
                select(FactRow.member_id, func.count(FactRow.id))
                .where(FactRow.account_id == account_id, FactRow.group_id == group_id)
                .group_by(FactRow.member_id)
            ).all()
        names = self.member_names(account_id, group_id)
        items = []
        for row in rows:
            sources = [str(value) for value in _list(row.source_message_ids_json) if str(value).strip()]
            items.append(
                {
                    "id": row.memory_id or str(row.id),
                    "member_id": str(row.member_id or ""),
                    "member_name": names.get(str(row.member_id or ""), str(row.member_id or "未知")),
                    "memory_type": str(row.memory_type or ""),
                    "content": str(row.content or ""),
                    "confidence_pct": round(max(0.0, min(1.0, float(row.confidence or 0))) * 100),
                    "status": str(row.status or ""),
                    "status_label": {
                        "active": "有效",
                        "confirmed": "已确认",
                        "candidate": "待确认",
                        "inferred": "推断",
                        "superseded": "已更新",
                        "expired": "已过期",
                    }.get(str(row.status or ""), str(row.status or "")),
                    "sources": sources[:6],
                    "at_text": self.time_text(row.updated_at or row.observed_at),
                    "observed_text": self.time_text(row.observed_at),
                }
            )
        members = [
            {
                "id": str(member_id_value or ""),
                "name": names.get(str(member_id_value or ""), str(member_id_value or "未知")),
                "count": int(count),
            }
            for member_id_value, count in member_rows
            if member_id_value
        ]
        members.sort(key=lambda item: (-item["count"], item["name"]))
        return {
            "rows": items,
            "types": types,
            "members": members,
            "member_id": member_id,
            "memory_type": memory_type,
            "total": len(items),
        }

    def source(self, scope: dict, identifier: str) -> dict | None:
        account_id, group_id = scope["account_id"], scope["group_id"]
        identifier = str(identifier or "").strip()
        if not identifier:
            return None
        local_ids = [int(identifier)] if identifier.isdigit() and len(identifier) < 19 else []
        with self.storage.Session() as session:
            row = session.scalar(
                select(MessageRow).where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    or_(
                        MessageRow.external_message_id == identifier,
                        MessageRow.id.in_(local_ids),
                    ),
                )
            )
        if row is None:
            return None
        if row.lifecycle_status == "active":
            content = row.text
        elif row.lifecycle_status == "expired":
            content = "原文已过期"
        else:
            content = "来源已删除或被更正"
        return {
            "identifier": identifier,
            "message_id": str(row.external_message_id or row.id),
            "actor_name": str(row.member_name or row.member_id or "未知成员"),
            "actor_id": str(row.member_id or ""),
            "content": content,
            "lifecycle_status": str(row.lifecycle_status or ""),
            "at_text": self.time_text(row.source_created_at or row.observed_at),
        }

    # --------------------------------------------------------------- emotion
    def emotion(self, runtime, account_id: str, group_id: str) -> dict:
        try:
            state = runtime.emotion.snapshot(account_id, group_id, now=self.now())
        except Exception:
            state = {}
        with self.storage.Session() as session:
            member_ids = [
                str(value)
                for value in session.scalars(
                    select(RelationshipStateRow.member_id).where(
                        RelationshipStateRow.account_id == account_id,
                        RelationshipStateRow.group_id == group_id,
                    )
                )
                if value
            ]
        names = self.member_names(account_id, group_id)
        members = []
        for member_id in member_ids:
            snapshot = self._snapshot(runtime, account_id, group_id, member_id)
            if not snapshot:
                continue
            view = self._relationship_view(
                snapshot, member_id=member_id, member_name=names.get(member_id, member_id)
            )
            members.append(view)
        members.sort(key=lambda item: (-item["affinity_value"], item["member_name"]))
        return {
            "mood": state.get("mood"),
            "p": state.get("p"),
            "a": state.get("a"),
            "d": state.get("d"),
            "labels": state.get("labels") or {},
            "top_labels": state.get("top_labels") or [],
            "mode": state.get("mode") or "normal",
            "mode_label": MODE_LABELS.get(str(state.get("mode") or "normal"), str(state.get("mode") or "")),
            "evaluated_at": self.time_text(state.get("evaluated_at")),
            "members": members,
        }

    # ----------------------------------------------------------- recent data
    def recent_messages(self, account_id: str, group_id: str, *, member_id: str = "", limit: int = 30) -> list[dict]:
        statement = select(MessageRow).where(
            MessageRow.account_id == account_id,
            MessageRow.group_id == group_id,
            MessageRow.lifecycle_status == "active",
        )
        if member_id:
            statement = statement.where(MessageRow.member_id == member_id)
        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    statement.order_by(MessageRow.observed_at.desc(), MessageRow.id.desc()).limit(limit)
                )
            )
        output = []
        for row in rows:
            output.append(
                {
                    "id": str(row.external_message_id or row.id),
                    "member_id": str(row.member_id or ""),
                    "member_name": str(row.member_name or row.member_id or "未知成员"),
                    "text": str(row.text or ""),
                    "at_text": self.time_text(row.source_created_at or row.observed_at),
                    "is_self": bool(row.is_self),
                    "source_role": str(row.source_role or ""),
                }
            )
        return output

    def recent_traces(self, scope: dict) -> list[dict]:
        account_id, group_id = scope["account_id"], scope["group_id"]
        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    select(RetrievalTraceRow)
                    .where(
                        RetrievalTraceRow.account_id == account_id,
                        RetrievalTraceRow.group_id == group_id,
                    )
                    .order_by(RetrievalTraceRow.created_at.desc())
                    .limit(50)
                )
            )
        return [
            {
                "request_id": str(row.request_id),
                "status": str(row.status or ""),
                "at_text": self.time_text(row.created_at),
            }
            for row in rows
        ]

    def trace(self, runtime, scope: dict, request_id: str) -> dict | None:
        request_id = str(request_id or "").strip()
        if not request_id:
            return None
        try:
            result = runtime.trace(request_id, account_id=scope["account_id"])
        except (KeyError, ValueError):
            return None
        snapshot = result.get("snapshot") if isinstance(result, dict) else None
        if not isinstance(snapshot, dict) or str(snapshot.get("group_id") or "") != scope["group_id"]:
            return None
        return result

    # --------------------------------------------------------- overview/logs
    def overview(self) -> dict:
        start = self.today_start_utc()
        stale_before = self.now() - PROCESSING_STALE_AFTER
        blocked = or_(
            ReplyRecordRow.status.in_(["failed", "blocked"]),
            and_(
                ReplyRecordRow.status == "skipped",
                ReplyRecordRow.reason.in_(sorted(ATTENTION_REASONS)),
            ),
            and_(
                ReplyRecordRow.status == "processing",
                ReplyRecordRow.updated_at <= stale_before,
            ),
        )
        with self.storage.Session() as session:
            today_replies = int(
                session.scalar(
                    select(func.count(ReplyRecordRow.id)).where(
                        ReplyRecordRow.status == "sent",
                        ReplyRecordRow.answered_at >= start,
                    )
                )
                or 0
            )
            total_sent = int(
                session.scalar(
                    select(func.count(ReplyRecordRow.id)).where(ReplyRecordRow.status == "sent")
                )
                or 0
            )
            statuses = {
                str(status): int(count)
                for status, count in session.execute(
                    select(ReplyRecordRow.status, func.count(ReplyRecordRow.id)).group_by(
                        ReplyRecordRow.status
                    )
                )
            }
            attention_rows = list(
                session.scalars(
                    select(ReplyRecordRow)
                    .where(blocked)
                    .order_by(ReplyRecordRow.updated_at.desc())
                    .limit(10)
                )
            )
            attention_total = int(
                session.scalar(select(func.count(ReplyRecordRow.id)).where(blocked)) or 0
            )
        attention = [
            {
                "event_key": row.event_key,
                "group_name": row.group_name,
                "member_name": row.member_name,
                "text": row.text,
                "reply_text": row.reply_text or "",
                "status": row.status,
                "status_label": REPLY_STATUS_LABELS.get(row.status, row.status),
                "reason": row.reason,
                "at_text": self.time_text(row.updated_at),
            }
            for row in attention_rows
        ]
        return {
            "today_replies": today_replies,
            "total_sent": total_sent,
            "statuses": statuses,
            "attention": attention,
            "attention_total": attention_total,
        }

    def reply_ledger(self, limit: int = 100) -> list[dict]:
        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    select(ReplyRecordRow)
                    .order_by(ReplyRecordRow.updated_at.desc())
                    .limit(max(1, min(limit, 200)))
                )
            )
        return [
            {
                "member_name": row.member_name,
                "group_name": row.group_name,
                "text": row.text,
                "reply_text": row.reply_text or "",
                "status": row.status,
                "status_label": REPLY_STATUS_LABELS.get(row.status, row.status),
                "reason": row.reason,
                "basis": row.basis,
                "attempts": row.attempt_count,
                "at_text": self.time_text(row.updated_at),
            }
            for row in rows
        ]

    def audit_ledger(self, limit: int = 100) -> list[dict]:
        from ..storage import ActionRow

        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    select(ActionRow).order_by(ActionRow.created_at.desc()).limit(max(1, min(limit, 200)))
                )
            )
        return [
            {
                "action_type": row.action_type,
                "group_id": row.group_id,
                "member_id": row.target_member_id or "",
                "text": row.text,
                "allowed": bool(row.allowed),
                "result_label": "允许" if row.allowed else "拦截",
                "reason": row.reason,
                "at_text": self.time_text(row.created_at),
            }
            for row in rows
        ]
