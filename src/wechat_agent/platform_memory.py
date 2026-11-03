"""Shared platform memory: browsing, seen comments and planned/posted comments.

Every row belongs to the local WeChat account and (when known) a member, but
not to a group: the same person can appear in several groups and the console
can read the memory across all of them.  The memory is deliberately separate
from ``memory_facts``: it keeps a public page/author as evidence, never treats
a platform author as a group member, and is only appended to the prompt as the
``platform`` memory family when ``PLATFORM_MEMORY_ENABLED`` is on.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from .config import Settings
from .models import MemoryFact, utc_now
from .platform_identity import PLATFORM_LABELS
from .storage import Storage

log = logging.getLogger(__name__)

PLATFORM_MEMORY_KINDS = ("browse", "comment_seen", "comment_posted", "mention")
_KIND_LABELS = {
    "browse": "浏览",
    "comment_seen": "看到评论",
    "comment_posted": "计划评论",
    "mention": "计划@",
}


def platform_memory_prefix(platform: str) -> str:
    label = PLATFORM_LABELS.get(str(platform or "").strip(), "平台")
    return f"【{label}】"


def _value(source: object, key: str, default: Any = "") -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _clean(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


class PlatformMemoryService:
    """Read/write ``platform_memories`` and render them for the reply prompt."""

    def __init__(self, storage: Storage, settings: Settings | None = None):
        self.storage = storage
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return True if self.settings is None else bool(self.settings.platform_memory_enabled)

    # -- writes ---------------------------------------------------------------

    def record_browse(
        self,
        *,
        account_id: str,
        platform: str = "",
        item_id: str = "",
        item_url: str = "",
        title: str = "",
        text: str = "",
        query: str = "",
        member_id: str = "",
        metadata: dict[str, object] | None = None,
        created_at: datetime | None = None,
    ) -> int:
        return self.storage.add_platform_memory(
            account_id=account_id,
            kind="browse",
            member_id=member_id,
            platform=str(platform or ""),
            item_id=str(item_id or ""),
            item_url=str(item_url or ""),
            title=_clean(title, 300),
            text=_clean(text, 1200),
            query=_clean(query, 200),
            metadata=metadata,
            created_at=created_at,
        )

    def record_browse_items(
        self,
        *,
        account_id: str,
        platform: str,
        items: Iterable[object],
        query: str = "",
        member_id: str = "",
        limit: int = 10,
    ) -> int:
        stored = 0
        seen: set[str] = set()
        for item in items:
            item_id = str(_value(item, "item_id") or "").strip()
            item_url = str(_value(item, "url", _value(item, "item_url")) or "").strip()
            if not item_id and not item_url:
                continue
            key = item_id or item_url
            if key in seen:
                continue
            seen.add(key)
            self.record_browse(
                account_id=account_id,
                platform=platform,
                item_id=item_id,
                item_url=item_url,
                title=str(_value(item, "title") or ""),
                text=str(_value(item, "text") or _value(item, "snippet") or ""),
                query=query,
                member_id=member_id,
                metadata={
                    "author_id": str(_value(item, "author_id") or ""),
                    "author_name": str(_value(item, "author_name") or ""),
                },
            )
            stored += 1
            if stored >= max(1, int(limit)):
                break
        return stored

    def record_comment_seen(
        self,
        *,
        account_id: str,
        platform: str = "",
        item_id: str = "",
        item_url: str = "",
        title: str = "",
        text: str = "",
        query: str = "",
        member_id: str = "",
        metadata: dict[str, object] | None = None,
    ) -> int:
        return self.storage.add_platform_memory(
            account_id=account_id,
            kind="comment_seen",
            member_id=member_id,
            platform=str(platform or ""),
            item_id=str(item_id or ""),
            item_url=str(item_url or ""),
            title=_clean(title, 300),
            text=_clean(text, 1600),
            query=_clean(query, 200),
            metadata=metadata,
        )

    def record_comment_plan(
        self,
        *,
        account_id: str,
        platform: str,
        item_id: str = "",
        item_url: str = "",
        title: str = "",
        text: str = "",
        query: str = "",
        member_id: str = "",
        action_key: str = "",
        dry_run: bool = True,
        mention_ids: Iterable[str] = (),
        executed: bool = False,
        metadata: dict[str, object] | None = None,
    ) -> int:
        mention_targets = [str(value).strip() for value in mention_ids if str(value).strip()]
        merged = {
            "action_key": action_key,
            "dry_run": bool(dry_run),
            "executed": bool(executed),
            "mention_target_ids": mention_targets,
            **(metadata or {}),
        }
        return self.storage.add_platform_memory(
            account_id=account_id,
            kind="mention" if mention_targets else "comment_posted",
            member_id=member_id,
            platform=str(platform or ""),
            item_id=str(item_id or ""),
            item_url=str(item_url or ""),
            title=_clean(title, 300),
            text=_clean(text, 1000),
            query=_clean(query, 200),
            metadata=merged,
        )

    def record_sources(
        self,
        *,
        account_id: str,
        sources: Iterable[object],
        member_id: str = "",
        limit: int = 10,
    ) -> dict[str, int]:
        """Store browse and comment evidence carried by research sources.

        ``WebSource`` objects from the platform research path carry
        ``metadata.platform``/``item_id``/``item_url``; a ``*_comments`` source
        type is a read comment page, everything else is a browsed item.
        """

        browse = 0
        comments = 0
        seen: set[tuple[str, str, str]] = set()
        for source in sources or ():
            metadata = _value(source, "metadata", {}) or {}
            platform = str(_value(metadata, "platform") or "").strip()
            source_type = str(_value(source, "source_type") or "")
            item_id = str(_value(metadata, "item_id") or "").strip()
            item_url = str(_value(source, "url") or _value(metadata, "item_url") or "").strip()
            if not platform:
                if source_type in {"douyin", "xiaohongshu"}:
                    platform = source_type
                elif source_type.endswith("_comments"):
                    platform = source_type.removesuffix("_comments")
            if not platform:
                continue
            key = (platform, item_id or item_url, "comment" if source_type.endswith("_comments") else "item")
            if key in seen:
                continue
            seen.add(key)
            title = str(_value(source, "title") or "")
            if source_type.endswith("_comments"):
                self.record_comment_seen(
                    account_id=account_id,
                    platform=platform,
                    item_id=item_id,
                    item_url=item_url,
                    title=title,
                    text=str(_value(source, "snippet") or _value(source, "content") or ""),
                    member_id=member_id,
                    metadata={"author_name": str(_value(metadata, "author_name") or "")},
                )
                comments += 1
            else:
                self.record_browse(
                    account_id=account_id,
                    platform=platform,
                    item_id=item_id,
                    item_url=item_url,
                    title=title,
                    text=str(_value(source, "snippet") or _value(source, "content") or ""),
                    member_id=member_id,
                    metadata={
                        "author_id": str(_value(metadata, "author_id") or _value(metadata, "author") or ""),
                        "author_name": str(_value(metadata, "author_name") or ""),
                    },
                )
                browse += 1
            if browse + comments >= max(1, int(limit)):
                break
        return {"browse": browse, "comments": comments}

    # -- reads ----------------------------------------------------------------

    def recent(
        self,
        account_id: str,
        *,
        member_id: str | None = None,
        platform: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        return self.storage.recent_platform_memories(
            account_id,
            member_id=member_id,
            platform=platform,
            kind=kind,
            limit=limit,
        )

    def memories_as_facts(
        self,
        account_id: str,
        group_id: str,
        member_id: str,
        *,
        limit: int = 5,
    ) -> list[MemoryFact]:
        """Render recent platform memory as inferred prompt facts."""

        if not self.enabled:
            return []
        if not str(member_id or "").strip():
            return []
        try:
            rows = self.storage.recent_platform_memories(
                account_id,
                member_id=member_id,
                limit=max(1, min(int(limit), 20)),
            )
        except Exception as exc:
            log.warning("platform memory read failed error=%s", type(exc).__name__)
            return []
        facts: list[MemoryFact] = []
        for row in rows:
            text = _clean(row.get("text") or row.get("title") or "", 1000)
            if not text:
                continue
            platform = str(row.get("platform") or "")
            kind_label = _KIND_LABELS.get(str(row.get("kind") or ""), "平台")
            title = _clean(row.get("title") or "", 120)
            body = f"{title}：{text}" if title and title != text else text
            content = f"{platform_memory_prefix(platform)}{kind_label}：{body}"[:240]
            observed = _parse_time(row.get("created_at"))
            facts.append(
                MemoryFact(
                    account_id=account_id,
                    group_id=group_id,
                    member_id=str(member_id),
                    memory_type="platform",
                    content=content,
                    confidence=0.8,
                    status="inferred",
                    observed_at=observed,
                    metadata={
                        "platform": platform,
                        "kind": str(row.get("kind") or ""),
                        "item_id": str(row.get("item_id") or ""),
                        "item_url": str(row.get("item_url") or ""),
                        "platform_memory_id": row.get("id"),
                    },
                )
            )
        return facts

    def summary_text(
        self,
        account_id: str,
        member_id: str,
        *,
        limit: int = 6,
    ) -> str:
        """Compact Chinese summary for the console or a prompt fallback."""

        facts = self.memories_as_facts(
            account_id,
            group_id="",
            member_id=member_id,
            limit=limit,
        )
        return "\n".join(fact.content for fact in facts)


def _parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return utc_now()
