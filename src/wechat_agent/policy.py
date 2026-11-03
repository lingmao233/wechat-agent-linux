from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
from threading import Lock
import unicodedata
from zoneinfo import ZoneInfo

from .config import Settings
from .models import MessageEvent, OutboundAction
from .social import proactive_affinity_weight
from .storage import Storage
from .textnorm import normalize_chat_text

REPLY_BASES = ("addressed", "keyword", "random", "schedule", "idle", "manual")


@dataclass(frozen=True, slots=True)
class ReplyDecision:
    """Why a message was answered, recorded next to the reply ledger."""

    reply: bool
    reason: str
    basis: str
    origin: str
    rules: dict[str, object] = field(default_factory=dict)


def reply_basis(reason: str) -> str:
    """Map a policy reason onto the fixed basis vocabulary."""

    if reason in {"ai_mentioned", "private_message"} or reason.startswith("voice_"):
        return "addressed"
    if reason == "wechat_keyword":
        return "keyword"
    if reason == "proactive_scheduled":
        return "schedule"
    if reason == "proactive_idle":
        return "idle"
    if reason.startswith("proactive_"):
        return "random"
    return "manual"


class ActionPolicy:
    """Guard every outbound action, including a reply to a group message."""

    def __init__(self, settings: Settings, storage: Storage, *, clock=None, emotion=None):
        self.settings = settings
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Optional relationship snapshot source.  When present, a member the
        # character likes makes an unaddressed message more likely to be joined;
        # without it (unit tests, emotion layer off) the weight is a flat 1.0.
        self.emotion = emotion
        self._lock = Lock()
        # Keep a bounded in-process send history for the minute circuit,
        # operator-configured cooldown, and optional hourly cap.  It is reset
        # when the listener restarts; the durable reply ledger still handles
        # question idempotency across restarts.
        self._sent_times: deque[datetime] = deque()
        self._last_sent_at: datetime | None = None
        self._circuit_threshold = 20  # per rolling minute; not a daily quota

    def is_stopped(self) -> bool:
        return self.settings.stop_file.exists()

    def is_allowed_group(self, group_name: str) -> bool:
        configured = getattr(self.settings, "wechat_group_names", None) or [
            self.settings.wechat_group_name
        ]
        wanted = {str(value).strip().casefold() for value in configured if str(value).strip()}
        return bool(wanted) and str(group_name or "").strip().casefold() in wanted

    @staticmethod
    def _conversation_type(value) -> str:
        if isinstance(value, MessageEvent):
            kind = getattr(value, "conversation_type", "") or value.media_metadata.get("conversation_type")
            return str(kind or ("group" if value.group_id.endswith("@chatroom") else "private")).casefold()
        kind = getattr(value, "conversation_type", "") or getattr(value, "media_metadata", {}).get("conversation_type")
        group_id = str(getattr(value, "group_id", "") or "")
        return str(kind or ("group" if group_id.endswith("@chatroom") else "private")).casefold()

    def is_allowed_private(self, event_or_name) -> bool:
        if not getattr(self.settings, "wechat_private_reply_enabled", False):
            return False
        configured = getattr(self.settings, "wechat_private_chat_names", None) or []
        if not configured:
            return False
        wanted = {str(item).strip().casefold() for item in configured if str(item).strip()}
        if isinstance(event_or_name, str):
            values = {event_or_name}
        else:
            values = {
                getattr(event_or_name, "group_name", ""),
                getattr(event_or_name, "group_id", ""),
            }
        return any(str(value or "").strip().casefold() in wanted for value in values if str(value or "").strip())

    def is_allowed_chat(self, event_or_name) -> bool:
        if self._conversation_type(event_or_name) == "private":
            return self.is_allowed_private(event_or_name)
        value = event_or_name if isinstance(event_or_name, str) else getattr(event_or_name, "group_name", "")
        return self.is_allowed_group(value)

    def should_reply_to(self, event: MessageEvent) -> tuple[bool, str]:
        if not self.is_allowed_chat(event):
            return False, "private_not_allowlisted" if self._conversation_type(event) == "private" else "group_not_allowlisted"
        if event.is_self:
            return False, "self_message"
        if event.historical:
            return False, "historical_message"
        if (self._conversation_type(event) != "private"
                and self.settings.wechat_allowed_members
                and not self._member_is_allowed(event)):
            return False, "member_not_allowlisted"
        normalized = normalize_chat_text(
            event.text,
            sender_id=event.member_id,
            sender_name=event.member_name,
            ai_aliases=self.settings.wechat_ai_aliases,
        )
        if normalized.is_system:
            return False, "system_message"
        if event.message_type == "voice":
            status = event.media_metadata.get("voice_transcription", {}).get("status")
            if not self.settings.voice_enabled:
                return False, "voice_disabled"
            if status != "ok" and event.proactive_window_id:
                return False, "voice_not_transcribed"
            if status in {"unintelligible", "unavailable"}:
                guard = self._frequency_reason() or self._voice_clarification_guard(event)
                return (False, guard) if guard else (True, "voice_" + status)
            if status != "ok":
                return False, "voice_not_transcribed"
        # One-to-one chats are already addressed by the conversation itself;
        # they do not need an @ mention or a group proactive roll.
        if self._conversation_type(event) == "private":
            frequency_reason = self._frequency_reason()
            if frequency_reason:
                return False, frequency_reason
            return True, "private_message"
        trigger_reason = self._trigger_reason(normalized.text, addressed_as=normalized.addressed_as)
        if event.proactive_window_id:
            if self._conversation_type(event) == "private":
                return False, "private_proactive_disabled"
            # A scheduled replay must never turn an invitation into an
            # unsolicited reply, even when a candidate survived an old lease.
            if trigger_reason:
                return False, "direct_message_not_proactive"
            if "@" in self._normalize_trigger_text(normalized.text):
                return False, "addressed_other_member"
            if not self.settings.proactive_schedule_enabled:
                return False, "proactive_schedule_disabled"
            from .proactive_scheduler import authorize_window
            action = OutboundAction(account_id=event.account_id, group_id=event.group_id, group_name=event.group_name, text="", proactive_window_id=event.proactive_window_id, proactive_lease_owner=str(event.media_metadata.get("proactive_lease_owner") or ""))
            frequency = self._frequency_reason()
            if frequency:
                return False, frequency
            guard = self._proactive_guard_reason(event)
            if guard:
                return False, guard
            allowed, reason = authorize_window(self.storage, action, now=self.clock())
            kind = "proactive_idle" if event.source == "proactive_scheduler" and event.media_metadata.get("proactive_mode") == "idle" else "proactive_scheduled"
            return (True, kind) if allowed else (False, reason)
        if trigger_reason:
            frequency_reason = self._frequency_reason()
            if frequency_reason:
                return False, frequency_reason
            return True, trigger_reason
        proactive_reason = self._proactive_reason(normalized.text) if self._conversation_type(event) != "private" else None
        if proactive_reason and self.settings.wechat_proactive_reply:
            frequency_reason = self._frequency_reason()
            if frequency_reason:
                return False, frequency_reason
            guard = self._proactive_guard_reason(event)
            if guard:
                return False, guard
            if not self._proactive_roll(event):
                return False, "proactive_probability"
            return True, f"proactive_{proactive_reason}"
        # Default addressed-only behavior: when proactive speaking is off, a
        # plain question is still a group message and must not invoke the model.
        return False, "ai_not_mentioned"

    def _member_is_allowed(self, event: MessageEvent) -> bool:
        """Check the Weixin unique ID; display names are not authorization keys."""

        allowed = {
            str(value).strip()
            for value in self.settings.wechat_allowed_members
            if str(value).strip()
        }
        return bool(event.member_id.strip()) and event.member_id.strip() in allowed

    def _mentions_ai(self, text: str) -> bool:
        return self._trigger_reason(text) is not None

    def direct_trigger_reason(self, event: MessageEvent) -> str | None:
        """Use the same normalized addressing rules for direct and scheduled work."""

        normalized = normalize_chat_text(
            event.text,
            sender_id=event.member_id,
            sender_name=event.member_name,
            ai_aliases=self.settings.wechat_ai_aliases,
        )
        return self._trigger_reason(normalized.text, addressed_as=normalized.addressed_as)

    def _trigger_reason(self, text: str, *, addressed_as: str = "") -> str | None:
        """Return the trigger class for a message.

        ``wechat_ai_aliases`` are addressing names.  They match an explicit
        ``@name`` anywhere in the message or a name at the beginning.  They
        deliberately do not match a name in the middle of ordinary prose, so
        the bot's own sentence such as ``我是 AI`` cannot re-enter the loop.
        ``wechat_keywords`` are the separate free-form keyword trigger and may
        occur anywhere, matching the behaviour of the previously working bot.
        """

        kind, _matched = self._trigger_match(text, addressed_as=addressed_as)
        return kind or None

    def _trigger_match(self, text: str, *, addressed_as: str = "") -> tuple[str, str]:
        """Return ``(kind, matched alias or keyword)`` for one message."""

        value = self._normalize_trigger_text(text)
        if addressed_as:
            # Text normalization uses @AI as a canonical placeholder. Restore
            # the actual matched alias before applying the addressing rules;
            # a deployment does not have to call its persona "AI".
            value = value.replace("@AI", "@" + addressed_as)
        if not value:
            return "", ""
        folded = value.casefold()
        aliases = [*self.settings.wechat_ai_aliases]
        # The logged-in nickname is also an address by default, matching the
        # previously working bot's automatic self-name registration.
        if self.settings.wechat_self_name.strip():
            aliases.append(self.settings.wechat_self_name)
        for raw_alias in aliases:
            alias = self._normalize_trigger_text(raw_alias)
            if not alias:
                continue
            escaped = re.escape(alias)
            # WeChat may insert a special-width space after the mention.  It
            # is normalized above, and punctuation/end-of-message closes the
            # name so @AI助手 does not accidentally count as @AI.
            mention_pattern = (
                rf"(?<![\w@])@\s*{escaped}"
                rf"(?=$|[\s,，。！？!?：:；;、()（）\[\]【】<>《》])"
            )
            if re.search(mention_pattern, value, flags=re.IGNORECASE):
                return "ai_mentioned", alias
            # A name without @ is accepted only as the message prefix.  This
            # preserves the old bot's ``AI 你好`` form while rejecting
            # ``我是 AI`` in an assistant response.
            if folded.startswith(alias.casefold()):
                return "ai_mentioned", alias

        for raw_keyword in self.settings.wechat_keywords:
            keyword = self._normalize_trigger_text(raw_keyword)
            if keyword and keyword.casefold() in folded:
                return "wechat_keyword", keyword
        return "", ""

    def decision(self, event: MessageEvent) -> ReplyDecision:
        """One call that yields the reply verdict and its recorded basis."""

        reply, reason = self.should_reply_to(event)
        normalized = normalize_chat_text(
            event.text,
            sender_id=event.member_id,
            sender_name=event.member_name,
            ai_aliases=self.settings.wechat_ai_aliases,
        )
        kind, matched = self._trigger_match(normalized.text, addressed_as=normalized.addressed_as)
        basis = reply_basis(reason) if reply else ""
        origin = "rule" if reason in {"proactive_scheduled", "proactive_idle"} else "human"
        rules: dict[str, object] = {
            "reason": reason,
            "trigger": kind,
            "matched": matched,
            "proactive": bool(event.proactive_window_id),
            "messageType": event.message_type,
            "conversation": self._conversation_type(event),
        }
        return ReplyDecision(reply, reason, basis, origin, rules)

    @staticmethod
    def _normalize_trigger_text(text: object) -> str:
        value = unicodedata.normalize("NFKC", str(text or ""))
        # Remove zero-width formatting characters, then collapse all Unicode
        # whitespace.  This covers the U+2005 spacing emitted by some WeChat
        # UIA builds after an @ mention.
        value = "".join(char for char in value if unicodedata.category(char) not in {"Cf"})
        return " ".join(value.split()).strip()

    def authorize(self, action: OutboundAction) -> tuple[bool, str]:
        if self.is_stopped():
            reason = "stop_file_present"
        elif not self.settings.wechat_allow_send:
            reason = "sending_disabled"
        elif getattr(action, "conversation_type", "group") == "private" and not self.is_allowed_private(action):
            reason = "private_not_allowlisted"
        elif getattr(action, "conversation_type", "group") != "private" and not self.is_allowed_group(action.group_name):
            reason = "group_not_allowlisted"
        elif (getattr(action, "conversation_type", "group") != "private"
              and self.settings.wechat_allowed_members
              and action.target_member_id not in self.settings.wechat_allowed_members):
            reason = "member_not_allowlisted"
        elif (frequency_reason := self._frequency_reason()):
            reason = frequency_reason
        elif action.action_type == "voice_clarification" and (voice_guard := self._voice_clarification_guard(action)):
            reason = voice_guard
        else:
            reason = self._circuit_reason()
            if reason == "allowed" and (action.action_type == "proactive_reply" or action.proactive_window_id):
                reason = self._proactive_guard_reason(action) or reason
            if reason == "allowed" and action.proactive_window_id:
                if not self.settings.proactive_schedule_enabled:
                    reason = "proactive_schedule_disabled"
                else:
                    from .proactive_scheduler import authorize_window
                    _, reason = authorize_window(self.storage, action, now=self.clock())

        allowed = reason in {"allowed", "target_member_policy_checked"}
        self.storage.audit_action(action, allowed=allowed, reason=reason)
        return allowed, reason

    def mark_sent(self) -> None:
        now = self.clock()
        with self._lock:
            self._prune_sent_times(now)
            self._sent_times.append(now)
            self._last_sent_at = now

    def _frequency_reason(self) -> str | None:
        """Return a configured frequency guard reason, if one is active."""

        cooldown = max(0.0, float(self.settings.wechat_reply_cooldown_seconds))
        hourly_limit = max(0, int(self.settings.wechat_max_replies_per_hour))
        if cooldown <= 0 and hourly_limit <= 0:
            return None
        now = self.clock()
        with self._lock:
            self._prune_sent_times(now)
            if cooldown > 0 and self._last_sent_at is not None:
                elapsed = (now - self._last_sent_at).total_seconds()
                if elapsed < cooldown:
                    return "reply_cooldown_active"
            if hourly_limit > 0 and len(self._sent_times) >= hourly_limit:
                return "reply_hourly_limit"
        return None

    def _proactive_reason(self, text: str) -> str | None:
        """Let ordinary unaddressed conversation enter the random gate too."""

        value = self._normalize_trigger_text(text)
        if not value or value.startswith("[") or value.endswith("]"):
            return None
        # Do not barge into a message with any explicit @ target.  If it was
        # addressed to the bot, the trigger branch above already handled it.
        if "@" in value:
            return None
        if any(
            cue in value
            for cue in (
                "好累",
                "累死",
                "难受",
                "烦死",
                "不开心",
                "崩溃",
                "焦虑",
                "压力大",
                "委屈",
                "失眠",
                "想哭",
            )
        ):
            return "comfort"
        if any(cue in value for cue in ("骗你的", "逗你的", "上钩", "钓鱼", "不告诉你", "假的吧")):
            return "prank"
        if any(cue in value for cue in ("哈哈", "笑死", "绷不住", "离谱", "整活", "梗", "乐死")):
            return "joke"
        if value.endswith(("?", "？")) or value.startswith(
            ("有没有", "谁能", "求推荐", "怎么", "为什么", "为啥")
        ):
            return "question"
        return "random" if any(char.isalnum() for char in value) else None

    def proactive_quiet(self, now: datetime) -> bool:
        from datetime import time as clock_time
        start, end = (clock_time.fromisoformat(value)
                      for value in self.settings.proactive_quiet_hours.split("-"))
        current = now.astimezone(ZoneInfo(self.settings.group_timezone)).time().replace(tzinfo=None)
        if start == end:
            return False
        return start <= current < end if start < end else current >= start or current < end

    def _voice_clarification_guard(self, event: MessageEvent | OutboundAction) -> str | None:
        interval = self.settings.voice_clarification_min_interval_seconds
        if interval and self.storage.recent_voice_clarification(
            event.account_id, event.group_id, since=self.clock() - timedelta(seconds=interval),
        ):
            return "voice_clarification_cooldown"
        return None

    def _proactive_guard_reason(self, event: MessageEvent | OutboundAction) -> str | None:
        """Apply proactive-only cooldown and hourly cap."""

        now = self.clock()
        if self.proactive_quiet(now):
            return "proactive_quiet_hours"
        interval = max(0.0, float(self.settings.wechat_proactive_min_interval_seconds)) if self.settings.wechat_proactive_reply else 0.0
        hourly_limit = max(0, int(self.settings.wechat_proactive_max_per_hour)) if self.settings.wechat_proactive_reply else 0
        if self.settings.proactive_schedule_enabled:
            scheduled_limit = self.settings.proactive_max_per_group_hour
            hourly_limit = min(hourly_limit, scheduled_limit) if hourly_limit else scheduled_limit
            if hourly_limit == 0:
                return "proactive_hourly_limit"
        if interval <= 0 and hourly_limit <= 0:
            return None
        sent = self.storage.proactive_send_times(
            event.account_id, event.group_id,
            since=now - timedelta(seconds=max(3600, interval)),
        )
        if interval > 0 and sent and (now - sent[0]).total_seconds() < interval:
            return "proactive_cooldown"
        if hourly_limit > 0 and sum(value > now - timedelta(hours=1) for value in sent) >= hourly_limit:
            return "proactive_hourly_limit"
        return None

    def _affinity_weight(self, event: MessageEvent) -> float:
        """Affinity-driven willingness to join this member's ordinary chat."""
        if self.emotion is None or not self.settings.emotion_layer_enabled:
            return 1.0
        try:
            snapshot = self.emotion.snapshot(
                event.account_id, event.group_id, event.member_id, now=self.clock(),
            )
        except Exception:  # a broken snapshot must not change the policy decision
            return 1.0
        return proactive_affinity_weight(
            snapshot.get("affinity", 50.0), snapshot.get("mood", 60.0),
        )

    def _proactive_roll(self, event: MessageEvent) -> bool:
        """Stable per-message random gate; retries do not change the outcome."""

        probability = max(0.0, min(1.0, float(self.settings.wechat_proactive_probability)))
        # 好感高时更愿意接话：概率乘以 0.75～1.5，但仍在 0～1 内，且不跳过冷却与频控。
        probability = max(0.0, min(1.0, probability * self._affinity_weight(event)))
        if probability >= 1.0:
            return True
        if probability <= 0.0:
            return False
        seed = ":".join(
            (
                event.account_id,
                event.group_id,
                event.member_id,
                event.external_message_id or "",
                event.text,
            )
        )
        roll = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
        return roll < probability

    def _prune_sent_times(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=1)
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()

    def _circuit_reason(self) -> str:
        now = self.clock()
        cutoff = now - timedelta(minutes=1)
        with self._lock:
            self._prune_sent_times(now)
            recent_count = sum(item >= cutoff for item in self._sent_times)
            if recent_count >= self._circuit_threshold:
                return "circuit_breaker_rate"
        return "allowed"
