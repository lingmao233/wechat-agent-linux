"""Group-chat intent detection and guarded platform automation.

The chain is intentionally one-way and conservative:

1. ``detect_intent`` recognises a Chinese (or English) holiday/food/guide
   intent in a WeChat message and extracts a small search query;
2. ``WebResearchClient`` uses that query to browse the configured platforms;
3. after the group reply is actually sent, :class:`PlatformAutomation` may
   create a short comment plan through :class:`PlatformService`;
4. nothing is executed unless the platform write switch **and** the automatic
   comment switch are on, the hourly ledger budget is available, and the
   visible browser confirms the write.  A ``dry_run`` plan is never upgraded.

``PLATFORM_PROACTIVE_ENABLED`` adds a separate low-frequency worker that
browses configured keywords during non-quiet hours and creates a plan; it is
dry-run by default for the same reason.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import Settings
from .models import MessageEvent, utc_now
from .platform_identity import PlatformIdentityService, platform_label
from .platform_memory import PlatformMemoryService
from .platforms.service import PlatformService
from .storage import Storage

log = logging.getLogger(__name__)

SUPPORTED_PLATFORMS: tuple[str, ...] = ("douyin", "xiaohongshu")

# Strong Chinese intent cues: these are the phrases that make extra platform
# browsing useful.  A bare "有没有" is deliberately not enough.
_INTENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"想看|想去|想要去|打算去|准备去|想旅游|想旅行|想玩|想吃|想喝|想买|种草"), "想去/想要"),
    (re.compile(r"攻略|指南|路线|行程|怎么玩|怎么去|自由行|穷游|打卡"), "攻略/路线"),
    (re.compile(r"求推荐|推荐一下|推荐个|安利|有没有[^。！？!?]{0,12}?(推荐|攻略|好玩|好吃|地方|景点)"
                r"|有什么[^。！？!?]{0,12}?(推荐|攻略|好玩|好吃|地方|景点)"), "求推荐"),
    (re.compile(r"哪里好玩|哪里好吃|哪家好|哪个好|值得去|值得买|值得吃|避雷|踩雷"), "评分/值不值得"),
)
_INTENT_PLATFORM_PATTERN = re.compile(r"抖音|douyin|小红书|xiaohongshu|xhslink|\bxhs\b", re.IGNORECASE)
_EN_INTENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(
        r"\b(recommend(?:ation)?|guide|travel|itinerary|trip|tourist|attractions?|"
        r"must[- ]visit|things to do|where to (?:go|eat)|restaurants?|recipes?|worth visiting)\b",
        re.IGNORECASE,
    ), "english-intent"),
)

_MENTION_RE = re.compile(r"@([^\s@,，。！？!?；;：:#（）()\[\]{}]+)")
_PUNCT_RE = re.compile(r"[，。！？!?；;：:、,.~～…\s]+")
_LEADING_COMMAND = re.compile(
    r"^(?:你好|您好|哈喽|嗨|在吗|请问|想问一下|想问|问一下|问下|帮我|麻烦你|麻烦|给我|"
    r"我想去|我想|想要去|想去|打算去|准备去|想看|想要|求推荐|求|这个|那个|这家|那家|这条|那条)+"
)
_GLOBAL_FILLERS = (
    "有没有", "有木有", "有什么", "有哪些", "有啥", "有推荐吗", "有推荐", "求推荐",
    "帮我找", "帮我", "麻烦你", "麻烦", "请问", "想问一下", "想问", "问一下", "问下",
    "请大家", "大家", "谁能", "可不可以", "能不能", "可以", "值不值得", "值得去",
    "值不值得去", "值得", "一下", "推荐吗",
)
_PARTICLES = re.compile(r"[的了吗呢吧啊呀嘛哦嗯]")
# Common intent words that Chinese compounds append to the actual topic, e.g.
# "大理攻略" -> "大理 攻略".  Splitting them keeps the browse query readable
# without a full segmenter.
_TOPIC_SUFFIXES = ("攻略", "推荐", "好玩", "好吃", "好喝", "值得", "避雷")
_EN_LEADING = re.compile(
    r"^(?:can you|could you|would you|please|i want to|i'd like to|i want|help me|recommend|"
    r"recommendation for|guide for)\s+",
    re.IGNORECASE,
)
_QUERY_MAX_CHARS = 80
_QUERY_MAX_TOKENS = 6


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _extract_query(text: str) -> str:
    """Strip greetings/@/particles while keeping the topic and intent words."""

    value = _MENTION_RE.sub(" ", str(text or ""))
    value = _PUNCT_RE.sub(" ", value).strip()
    value = _LEADING_COMMAND.sub(" ", value).strip()
    for filler in _GLOBAL_FILLERS:
        value = value.replace(filler, " ")
    value = _PARTICLES.sub(" ", value)
    tokens = [token.strip() for token in value.split() if token.strip()]
    cleaned: list[str] = []
    for token in tokens:
        token = re.sub(r"^[求帮请给找]+", "", token).strip()
        if not token or len(token) > 30:
            continue
        for suffix in _TOPIC_SUFFIXES:
            if len(token) > len(suffix) and token.endswith(suffix):
                head = token[: -len(suffix)].strip()
                if head:
                    cleaned.extend([head, suffix])
                    token = ""
                break
        if token and token not in cleaned:
            cleaned.append(token)
    # Chinese overlap: "看海" already carries "海", so keep the longer phrase.
    kept: list[str] = []
    for token in cleaned:
        if any(token != other and token in other for other in cleaned):
            continue
        kept.append(token)
    query = " ".join(kept[:_QUERY_MAX_TOKENS])[:_QUERY_MAX_CHARS].strip()
    if query:
        return query
    fallback = _MENTION_RE.sub(" ", str(text or ""))
    fallback = _PUNCT_RE.sub(" ", fallback).strip()
    return fallback[:_QUERY_MAX_CHARS]


def _extract_english_query(text: str) -> str:
    value = _MENTION_RE.sub(" ", str(text or ""))
    value = _PUNCT_RE.sub(" ", value).strip()
    previous = None
    while previous != value:
        previous = value
        value = _EN_LEADING.sub("", value).strip()
    value = re.sub(r"^(?:a|an|the)\s+", "", value, flags=re.IGNORECASE)
    return (value or _clean_text(text))[:_QUERY_MAX_CHARS].strip()


def detect_intent(
    text: object,
    *,
    available_platforms: Iterable[str] = SUPPORTED_PLATFORMS,
) -> dict[str, object]:
    """Return ``{matched, query, platforms, reason}`` for one incoming message."""

    raw = str(text or "")
    platforms_available = [str(value) for value in available_platforms if str(value)]
    explicit: list[str] = []
    lowered = raw.casefold()
    if "抖音" in raw or "douyin" in lowered:
        explicit.append("douyin")
    if "小红书" in raw or "xiaohongshu" in lowered or "xhs" in lowered:
        explicit.append("xiaohongshu")

    keywords: list[str] = []
    for pattern, label in _INTENT_PATTERNS:
        if pattern.search(raw):
            keywords.append(label)
    english = False
    for pattern, label in _EN_INTENT_PATTERNS:
        if pattern.search(raw):
            keywords.append(label)
            english = True
    matched = bool(keywords)
    if not matched:
        return {
            "matched": False,
            "query": "",
            "platforms": [],
            "reason": "no_intent_keyword",
            "keywords": [],
            "explicit_platforms": explicit,
        }

    query = _extract_english_query(raw) if english and not any(
        pattern.search(raw) for pattern, _ in _INTENT_PATTERNS
    ) else _extract_query(raw)
    if explicit:
        selected = [name for name in explicit if not platforms_available or name in platforms_available]
        if not selected:
            selected = explicit
    else:
        selected = list(platforms_available)
    reason = "命中意图:" + ",".join(keywords)
    return {
        "matched": True,
        "query": query,
        "platforms": selected,
        "reason": reason,
        "keywords": keywords,
        "explicit_platforms": explicit,
    }


def _mention_name(value: object, fallback: object = "") -> str:
    name = re.sub(r"[\s@,，。！？!?；;：:#（）()\[\]{}]+", "", str(value or ""))
    if name:
        return name
    return re.sub(r"[\s@,，。！？!?；;：:#（）()\[\]{}]+", "", str(fallback or "")) or "朋友"


def _parse_iso(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class PlatformAutomation:
    """Turn a sent group reply and proactive schedules into guarded plans."""

    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        service: PlatformService,
        *,
        llm: Any | None = None,
        account_id: str | None = None,
        identity: PlatformIdentityService | None = None,
        memory: PlatformMemoryService | None = None,
    ):
        self.storage = storage
        self.settings = settings
        self.service = service
        self.llm = llm
        self.identity = identity or PlatformIdentityService(storage, settings)
        self.memory = memory or PlatformMemoryService(storage, settings)
        self.account_id = str(account_id or "").strip() or self.identity.default_account_id
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stop_worker = threading.Event()
        self._last_run_at: datetime | None = None
        self._last_result: dict[str, object] | None = None
        self._last_error: str | None = None
        self._recent_results: list[dict[str, object]] = []
        self._run_count = 0

    # -- configuration helpers ------------------------------------------------

    @property
    def auto_write_enabled(self) -> bool:
        """Automatic execution needs both the write gate and the auto switch."""

        return bool(
            self.settings.platform_write_enabled
            and self.settings.platform_auto_comment_after_reply
        )

    def _enabled_platforms(self) -> list[str]:
        adapters = getattr(self.service, "adapters", None)
        if isinstance(adapters, dict) and adapters:
            return [str(name) for name in adapters if str(name) in SUPPORTED_PLATFORMS]
        return list(SUPPORTED_PLATFORMS)

    def _hourly_action_count(self) -> int:
        """Count platform writes already queued/sent in the previous hour."""

        try:
            rows = self.storage.recent_platform_actions(200)
        except Exception:
            return 0
        cutoff = utc_now() - timedelta(hours=1)
        count = 0
        for row in rows:
            if bool(row.get("dry_run")):
                continue
            if str(row.get("status") or "") not in {"queued", "approved", "executing", "sent"}:
                continue
            moment = _parse_iso(row.get("updated_at")) or _parse_iso(row.get("created_at"))
            if moment is None:
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            if moment >= cutoff:
                count += 1
        return count

    def _rate_budget_available(self) -> bool:
        return self._hourly_action_count() < max(1, int(self.settings.platform_max_actions_per_hour))

    def _executor_available(self, platform: str) -> bool:
        """True only when the platform adapter can really perform a write.

        A dry-run adapter has no ``execute_action``; auto mode must therefore
        stay in dry-run rather than queueing a write that can never run.
        """

        try:
            adapter = self.service.adapter(platform)  # type: ignore[arg-type]
        except Exception:
            return False
        return callable(getattr(adapter, "execute_action", None))

    @staticmethod
    def _as_aware(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment

    def _quiet_window(self) -> tuple[time, time]:
        try:
            start, end = str(self.settings.platform_quiet_hours or "").split("-")
            return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())
        except ValueError:
            return time(23, 0), time(8, 0)

    def _local_moment(self, moment: datetime | None = None) -> datetime:
        current = moment or utc_now()
        if current.tzinfo is None:
            return current
        zone = None
        try:
            zone = ZoneInfo(str(getattr(self.settings, "group_timezone", "") or "Asia/Shanghai"))
        except Exception:
            zone = None
        return current.astimezone(zone) if zone is not None else current.astimezone()

    def in_quiet_hours(self, moment: datetime | None = None) -> bool:
        local = self._local_moment(moment)
        start, end = self._quiet_window()
        current = local.timetz().replace(tzinfo=None)
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    # -- comment generation ---------------------------------------------------

    def _compose_comment(
        self,
        platform: str,
        identity: dict[str, object],
        item: dict[str, object],
        *,
        reply_text: str = "",
    ) -> tuple[str, str]:
        """Return ``(text, source)`` where source is ``llm`` or ``template``."""

        label = platform_label(platform)
        name = _mention_name(identity.get("platform_user_name"), identity.get("member_name") or identity.get("platform_user_id"))
        title = _clean_text(item.get("title"))[:60]
        snippet = _clean_text(item.get("text"))[:120]
        max_chars = max(20, int(self.settings.platform_comment_max_chars))
        text = ""
        complete = getattr(self.llm, "complete", None)
        if callable(complete):
            try:
                raw = complete(
                    "你在小红书/抖音上给一条公开内容写一条自然、短、不夸张的中文评论，"
                    f"开头必须先 @{name}，不要链接，不要暴露隐私，只输出评论正文。",
                    json.dumps(
                        {
                            "platform": label,
                            "mention_name": name,
                            "title": title,
                            "content": snippet,
                            "group_reply": _clean_text(reply_text)[:120],
                            "max_chars": max_chars,
                        },
                        ensure_ascii=False,
                    ),
                    temperature=0.7,
                    max_tokens=200,
                    timeout_seconds=min(
                        20.0,
                        float(getattr(self.settings, "llm_timeout_seconds", 60) or 60),
                    ),
                )
                text = _clean_text(raw).strip("\"'“”‘’")
                if len(text) > max_chars:
                    text = text[:max_chars]
            except Exception as exc:
                log.info("platform comment generation failed error=%s; using template", type(exc).__name__)
                text = ""
        source = "llm" if text else "template"
        if not text:
            topic = title or _clean_text(reply_text)[:40] or "这个分享"
            templates = (
                f"@{name} 这个「{topic}」看着不错，先码住～",
                f"@{name} {label}上这条{('「' + topic + '」') if title else '内容'}挺有意思的，收藏啦",
                f"@{name} 感谢分享，这个{topic}值得试试～",
            )
            text = templates[len(name) % len(templates)]
        text = self._ensure_mention(text, name, max_chars)
        return text, source

    @staticmethod
    def _ensure_mention(text: str, name: str, max_chars: int) -> str:
        value = _clean_text(text)
        mention = f"@{name}"
        if mention not in value:
            value = f"{mention} {value}".strip()
        if len(value) > max_chars:
            value = value[:max_chars].rstrip()
            if mention not in value:
                value = mention
        return value or mention

    # -- reply hook -----------------------------------------------------------

    def after_reply(
        self,
        event: MessageEvent | None,
        reply_text: str = "",
        platform_sources: Iterable[object] | None = None,
    ) -> dict[str, object]:
        """Record platform evidence and optionally queue one comment plan."""

        result: dict[str, object] = {
            "ok": True,
            "member_id": "",
            "platform": "",
            "item_id": "",
            "item_url": "",
            "action_key": "",
            "dry_run": True,
            "duplicate": False,
            "executed": False,
            "scheduled": False,
            "mention_ids": [],
            "comment_text": "",
            "comment_source": "",
            "memories": {"browse": 0, "comments": 0},
            "reason": "",
        }
        try:
            sources = list(platform_sources or ())
            if event is None:
                result["reason"] = "missing_event"
                return result
            account_id = str(getattr(event, "account_id", "") or "").strip() or self.account_id
            member_id = str(getattr(event, "member_id", "") or "").strip()
            result["member_id"] = member_id
            if not sources:
                result["reason"] = "no_platform_sources"
                return result
            if self.settings.platform_memory_enabled:
                try:
                    result["memories"] = self.memory.record_sources(
                        account_id=account_id,
                        sources=sources,
                        member_id=member_id,
                    )
                except Exception as exc:
                    log.warning("platform memory write failed error=%s", type(exc).__name__)
            identities = self.identity.identities_for_member(member_id, account_id=account_id)
            if not identities:
                result["reason"] = "member_not_bound"
                return result
            by_platform: dict[str, list[dict[str, object]]] = {}
            for source in sources:
                metadata = getattr(source, "metadata", {}) or {}
                if isinstance(source, dict):
                    metadata = source.get("metadata") or {}
                platform = str(metadata.get("platform") or "").strip()
                if not platform:
                    source_type = str(getattr(source, "source_type", "") or "")
                    if source_type in SUPPORTED_PLATFORMS:
                        platform = source_type
                    elif source_type.endswith("_comments"):
                        platform = source_type.removesuffix("_comments")
                if platform not in self._enabled_platforms():
                    continue
                item_id = str(metadata.get("item_id") or "").strip()
                item_url = str(metadata.get("item_url") or getattr(source, "url", "") or "").strip()
                if not item_id and not item_url:
                    continue
                by_platform.setdefault(platform, []).append(
                    {
                        "item_id": item_id,
                        "item_url": item_url,
                        "title": str(getattr(source, "title", "") or ""),
                        "text": str(getattr(source, "snippet", "") or getattr(source, "content", "") or ""),
                        "source_type": str(getattr(source, "source_type", "") or ""),
                    }
                )
            chosen_identity = next(
                (item for item in identities if str(item.get("platform") or "") in by_platform),
                None,
            )
            if chosen_identity is None:
                result["reason"] = "no_source_for_bound_platform"
                return result
            platform = str(chosen_identity.get("platform") or "")
            candidates = by_platform.get(platform) or []
            item = next((entry for entry in candidates if entry.get("item_id")), None)
            result["platform"] = platform
            if item is None:
                result["reason"] = "item_id_missing"
                return result
            item_id = str(item.get("item_id") or "")
            item_url = str(item.get("item_url") or "")
            result["item_id"] = item_id
            result["item_url"] = item_url
            mention_id = str(chosen_identity.get("platform_user_id") or "").strip()
            comment_text, comment_source = self._compose_comment(
                platform, chosen_identity, item, reply_text=reply_text
            )
            result["comment_text"] = comment_text
            result["comment_source"] = comment_source
            result["mention_ids"] = [mention_id] if mention_id else []
            auto = self.auto_write_enabled
            rate_ok = self._rate_budget_available()
            can_execute = self._executor_available(platform)
            dry_run = not (auto and rate_ok and can_execute)
            if auto and not can_execute:
                result["reason"] = "executor_unavailable_dry_run"
            elif auto and not rate_ok:
                result["reason"] = "rate_limited_dry_run"
            receipt = self.service.plan_comment(
                platform,
                item_id,
                comment_text,
                mention_ids=[mention_id] if mention_id else (),
                dry_run=dry_run,
            )
            result.update(
                {
                    "action_key": receipt.action_key,
                    "dry_run": bool(receipt.dry_run),
                    "duplicate": bool(receipt.duplicate),
                }
            )
            if self.settings.platform_memory_enabled:
                try:
                    self.memory.record_comment_plan(
                        account_id=account_id,
                        platform=platform,
                        item_id=item_id,
                        item_url=item_url,
                        title=str(item.get("title") or ""),
                        text=comment_text,
                        query=str(getattr(event, "text", "") or "")[:200],
                        member_id=member_id,
                        action_key=receipt.action_key,
                        dry_run=bool(receipt.dry_run),
                        mention_ids=result["mention_ids"],
                    )
                except Exception as exc:
                    log.warning("platform plan memory failed error=%s", type(exc).__name__)
            if not receipt.dry_run and receipt.status in {"queued", "approved"} and not receipt.duplicate:
                self._schedule_auto_execute(
                    receipt.action_key,
                    account_id=account_id,
                    member_id=member_id,
                    platform=platform,
                    item_id=item_id,
                    item_url=item_url,
                    title=str(item.get("title") or ""),
                    text=comment_text,
                    query=str(getattr(event, "text", "") or "")[:200],
                    mention_ids=list(result["mention_ids"]),
                )
                result["scheduled"] = True
                result["reason"] = result["reason"] or "queued_for_auto_execute"
            elif receipt.duplicate:
                result["reason"] = result["reason"] or "duplicate_plan"
            elif not result["reason"]:
                result["reason"] = "dry_run_planned"
            return result
        except Exception as exc:
            log.warning("platform after_reply failed error=%s", type(exc).__name__)
            result.update({"ok": False, "reason": "error", "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
            return result

    def _schedule_auto_execute(
        self,
        action_key: str,
        *,
        account_id: str,
        member_id: str,
        platform: str,
        item_id: str,
        item_url: str,
        title: str,
        text: str,
        query: str,
        mention_ids: list[str],
    ) -> None:
        def run() -> None:
            try:
                self.service.execute(action_key, confirm=True)
            except Exception as exc:
                log.warning(
                    "platform auto execute failed action=%s error=%s",
                    action_key,
                    f"{type(exc).__name__}:{str(exc)[:200]}",
                )
                return
            if not self.settings.platform_memory_enabled:
                return
            try:
                self.memory.record_comment_plan(
                    account_id=account_id,
                    platform=platform,
                    item_id=item_id,
                    item_url=item_url,
                    title=title,
                    text=text,
                    query=query,
                    member_id=member_id,
                    action_key=action_key,
                    dry_run=False,
                    mention_ids=mention_ids,
                    executed=True,
                    metadata={"status": "sent", "auto": True},
                )
            except Exception as exc:
                log.debug("platform executed memory write failed error=%s", type(exc).__name__)

        thread = threading.Thread(
            target=run,
            name=f"platform-auto-{platform}",
            daemon=True,
        )
        thread.start()

    # -- proactive browsing ---------------------------------------------------

    def _proactive_keywords(self) -> list[str]:
        configured = [str(value).strip() for value in (self.settings.platform_proactive_keywords or []) if str(value).strip()]
        if configured:
            return configured
        keywords: list[str] = []
        try:
            rows = self.storage.recent_interest_facts(self.account_id, limit=20)
            if not rows and self.account_id:
                rows = self.storage.recent_interest_facts("", limit=20)
        except Exception:
            rows = []
        for row in rows:
            content = _clean_text(row.get("content"))
            if not content:
                continue
            keyword = content[:40]
            if keyword not in keywords:
                keywords.append(keyword)
        return keywords[:20]

    def _match_proactive_identity(
        self,
        platform: str,
        item: dict[str, object],
        keyword: str,
    ) -> dict[str, object] | None:
        text = f"{item.get('title') or ''} {item.get('text') or ''}"
        for identity in self.identity.list(self.account_id):
            if str(identity.get("platform") or "") != platform:
                continue
            display = str(identity.get("platform_user_name") or "").strip()
            member = str(identity.get("member_name") or "").strip()
            if display and display in text:
                return identity
            if member and member in text:
                return identity
            if keyword and (keyword in display or keyword in member):
                return identity
        return None

    def run_once(
        self,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Run at most one proactive browse/plan cycle.

        ``force`` skips the interval and quiet-hours checks for an explicit
        operator click; the enable switch, write gates and dry-run default are
        never bypassed.
        """

        moment = now or utc_now()
        result: dict[str, object] = {
            "ok": True,
            "ran": False,
            "reason": "",
            "platform": "",
            "query": "",
            "items": 0,
            "action_key": "",
            "dry_run": True,
            "executed": False,
            "scheduled": False,
            "started_at": moment.isoformat(),
        }
        if not self.settings.platform_proactive_enabled:
            result["reason"] = "disabled"
            return self._remember_run(result, moment)
        if not force and self.in_quiet_hours(moment):
            result["reason"] = "quiet_hours"
            return self._remember_run(result, moment)
        if not force and self._last_run_at is not None:
            elapsed = self._as_aware(moment) - self._as_aware(self._last_run_at)
            interval = timedelta(minutes=max(30, int(self.settings.platform_proactive_interval_minutes)))
            if elapsed < interval:
                result["reason"] = "interval_not_elapsed"
                result["next_run_at"] = (self._last_run_at + interval).isoformat()
                return self._remember_run(result, moment)
        keywords = self._proactive_keywords()
        if not keywords:
            result["reason"] = "no_keywords"
            return self._remember_run(result, moment)
        platforms = self._enabled_platforms()
        if not platforms:
            result["reason"] = "no_platform_adapter"
            return self._remember_run(result, moment)
        platform = platforms[self._run_count % len(platforms)]
        keyword = keywords[self._run_count % len(keywords)]
        self._run_count += 1
        result.update({"ran": True, "platform": platform, "query": keyword})
        try:
            items = list(
                self.service.browse(
                    platform,
                    query=keyword,
                    limit=min(10, max(5, int(getattr(self.settings, "web_search_max_results", 5)) + 5)),
                )
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
            self._last_error = error
            result.update({"ok": False, "reason": "browse_failed", "error": error})
            try:
                self.storage.add_platform_memory(
                    account_id=self.account_id,
                    kind="browse",
                    platform=platform,
                    query=keyword,
                    text="浏览失败",
                    metadata={"error": error},
                )
            except Exception:
                pass
            return self._remember_run(result, moment)
        self._last_error = None
        result["items"] = len(items)
        if self.settings.platform_memory_enabled and items:
            try:
                self.memory.record_browse_items(
                    account_id=self.account_id,
                    platform=platform,
                    items=items,
                    query=keyword,
                )
            except Exception as exc:
                log.debug("proactive browse memory failed error=%s", type(exc).__name__)
        if not items:
            result["reason"] = "no_items"
            return self._remember_run(result, moment)
        target = next((item for item in items if str(getattr(item, "item_id", "") or "").strip()), None)
        if target is None:
            result["reason"] = "item_id_missing"
            return self._remember_run(result, moment)
        item = {
            "item_id": str(getattr(target, "item_id", "") or ""),
            "item_url": str(getattr(target, "url", "") or ""),
            "title": str(getattr(target, "title", "") or ""),
            "text": str(getattr(target, "text", "") or ""),
        }
        identity = self._match_proactive_identity(platform, item, keyword)
        if identity is None:
            name = "朋友"
            mention_ids: list[str] = []
            platform_user = ""
        else:
            name = _mention_name(identity.get("platform_user_name"), identity.get("member_name") or identity.get("platform_user_id"))
            mention_ids = [str(identity.get("platform_user_id") or "").strip()]
            platform_user = str(identity.get("platform_user_id") or "")
        comment_text, comment_source = self._compose_comment(
            platform, {"platform_user_name": name, "member_name": name}, item
        )
        if not mention_ids:
            # A proactive comment without a binding is a plain public comment:
            # drop the @ placeholder so the ledger text matches what would be
            # typed, and skip the mention validation entirely.
            comment_text = _clean_text(comment_text.replace(f"@{name}", " ")) or _clean_text(item["title"])[:40] or "这个分享不错～"
        auto = self.auto_write_enabled
        rate_ok = self._rate_budget_available()
        can_execute = self._executor_available(platform)
        dry_run = not (auto and rate_ok and can_execute)
        if auto and not can_execute:
            result["reason"] = "executor_unavailable_dry_run"
        elif auto and not rate_ok:
            result["reason"] = "rate_limited_dry_run"
        try:
            receipt = self.service.plan_comment(
                platform,
                item["item_id"],
                comment_text,
                mention_ids=mention_ids,
                dry_run=dry_run,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
            self._last_error = error
            result.update({"ok": False, "reason": "plan_failed", "error": error})
            return self._remember_run(result, moment)
        result.update(
            {
                "action_key": receipt.action_key,
                "dry_run": bool(receipt.dry_run),
                "comment_text": comment_text,
                "comment_source": comment_source,
                "mention_ids": mention_ids,
            }
        )
        if self.settings.platform_memory_enabled:
            try:
                self.memory.record_comment_plan(
                    account_id=self.account_id,
                    platform=platform,
                    item_id=item["item_id"],
                    item_url=item["item_url"],
                    title=item["title"],
                    text=comment_text,
                    query=keyword,
                    member_id=str(identity.get("member_id") or "") if identity else "",
                    action_key=receipt.action_key,
                    dry_run=bool(receipt.dry_run),
                    mention_ids=mention_ids,
                    metadata={"proactive": True, "platform_user": platform_user},
                )
            except Exception as exc:
                log.debug("proactive plan memory failed error=%s", type(exc).__name__)
        if not receipt.dry_run and receipt.status in {"queued", "approved"}:
            self._schedule_auto_execute(
                receipt.action_key,
                account_id=self.account_id,
                member_id=str(identity.get("member_id") or "") if identity else "",
                platform=platform,
                item_id=item["item_id"],
                item_url=item["item_url"],
                title=item["title"],
                text=comment_text,
                query=keyword,
                mention_ids=mention_ids,
            )
            result["scheduled"] = True
            result["reason"] = "queued_for_auto_execute"
        elif not result["reason"]:
            result["reason"] = "dry_run_planned"
        return self._remember_run(result, moment)

    def _remember_run(self, result: dict[str, object], moment: datetime) -> dict[str, object]:
        with self._lock:
            if result.get("platform"):
                self._last_run_at = moment
            self._last_result = dict(result)
            if result.get("error"):
                self._last_error = str(result.get("error"))
            self._recent_results.append(
                {
                    key: result.get(key)
                    for key in ("ran", "reason", "platform", "query", "items", "action_key", "dry_run", "started_at")
                }
            )
            self._recent_results = self._recent_results[-5:]
        log.info(
            "platform proactive cycle ran=%s reason=%s platform=%s items=%s dry_run=%s",
            result.get("ran"),
            result.get("reason"),
            result.get("platform"),
            result.get("items"),
            result.get("dry_run"),
        )
        return result

    # -- worker lifecycle -----------------------------------------------------

    def start_worker(self) -> bool:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            self._stop_worker.clear()
            interval_minutes = max(30, min(int(self.settings.platform_proactive_interval_minutes), 1440))
            self._worker = threading.Thread(
                target=self._worker_loop,
                args=(interval_minutes * 60,),
                name="platform-proactive",
                daemon=True,
            )
            self._worker.start()
            return True

    def _worker_loop(self, interval_seconds: float) -> None:
        # The first unattended run happens after one interval: opening a
        # browser or browsing at process start must always be operator-driven.
        while not self._stop_worker.wait(interval_seconds):
            try:
                self.run_once()
            except Exception as exc:  # background safety net
                self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                log.warning("platform proactive worker failed error=%s", type(exc).__name__)

    def stop_worker(self, timeout: float = 3.0) -> None:
        self._stop_worker.set()
        worker = self._worker
        self._worker = None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, timeout))

    def worker_alive(self) -> bool:
        worker = self._worker
        return bool(worker is not None and worker.is_alive())

    def status(self) -> dict[str, object]:
        now = utc_now()
        interval = timedelta(minutes=max(30, int(self.settings.platform_proactive_interval_minutes)))
        last_run_at = self._last_run_at.isoformat() if self._last_run_at else None
        next_run_at = (self._last_run_at + interval).isoformat() if self._last_run_at else None
        last = self._last_result or {}
        summary = ""
        if last:
            summary = (
                f"{last.get('reason') or 'unknown'}"
                f" platform={last.get('platform') or '-'}"
                f" items={last.get('items') or 0}"
                f" dry_run={last.get('dry_run')}"
            )
        return {
            "enabled": bool(self.settings.platform_proactive_enabled),
            "interval_minutes": int(self.settings.platform_proactive_interval_minutes),
            "keywords": list(self.settings.platform_proactive_keywords or []),
            "effective_keywords": self._proactive_keywords(),
            "quiet_hours": str(self.settings.platform_quiet_hours),
            "quiet_now": self.in_quiet_hours(now),
            "last_run_at": last_run_at,
            "next_run_at": next_run_at,
            "last_result": summary,
            "last_result_detail": dict(last) if last else None,
            "recent_results": list(self._recent_results),
            "last_error": self._last_error,
            "worker_alive": self.worker_alive(),
            "write_enabled": bool(self.settings.platform_write_enabled),
            "auto_write_enabled": self.auto_write_enabled,
            "dry_run_default": True,
            "platforms": self._enabled_platforms(),
        }
