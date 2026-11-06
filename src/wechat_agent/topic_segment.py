"""Cut the group conversation into the current topic segment for one reply.

The flat "last N messages" window mixes the current topic with older, unrelated
chatter: a video XML row could eat most of the budget, and a topic that started
ten minutes earlier could be pushed out by messages from a previous topic.  The
dsh-superagent prototype solves this with source-provided thread identities
(first messages of a thread plus the trigger's neighbourhood); WeChat has no
thread metadata, so the boundary is inferred here from time gaps plus the
existing scan limit.

Rules:

* walk back from the trigger; stop when the gap to the newer message exceeds
  ``gap_seconds`` (the group went quiet and a new topic started);
* stop when the trigger is older than ``max_minutes`` (a long quiet period is a
  new topic, not context for this one);
* never scan more than ``scan_limit`` stored rows; hitting the scan limit is
  reported as ``truncated_prefix`` so the prompt can say older text was cut;
* a segment shorter than ``min_messages`` borrows one or two older rows even
  across the gap, because a one-line context is no context;
* a topic longer than ``max_messages`` keeps its opening ``head_messages``
  lines plus the newest lines and marks the omitted middle instead of silently
  dropping the beginning.

The module is pure: no database, no clock, no settings import.  Callers pass
already-fetched ``ConversationMessage`` rows and a trigger time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import ConversationMessage

DEFAULT_GAP_SECONDS = 300.0
DEFAULT_MAX_MINUTES = 30.0
DEFAULT_MAX_MESSAGES = 12
DEFAULT_HEAD_MESSAGES = 3
DEFAULT_SCAN_MESSAGES = 40
DEFAULT_MIN_MESSAGES = 2
DEFAULT_TIMEZONE = "Asia/Shanghai"

#: Raw WeChat media payloads that occasionally leak into stored text.
_MEDIA_XML = re.compile(
    r"<(?:msg|appmsg|emoji|sysmsg|img|voicemsg|voipmsg|videomsg|location|refermsg)\b",
    re.IGNORECASE,
)

#: Text the adapter already normalizes media rows to.
_MEDIA_PLACEHOLDERS = {
    "[图片]": "image",
    "[照片]": "image",
    "[微信图片]": "image",
    "[Image]": "image",
    "[语音]": "voice",
    "[视频]": "video",
    "[表情]": "emoji",
    "[链接]": "link",
    "[文件]": "file",
    "[位置]": "location",
}

_XML_PLACEHOLDER_ORDER = (
    ("<videomsg", "[视频]"),
    ("<voicemsg", "[语音]"),
    ("<emoji", "[表情]"),
    ("<location", "[位置]"),
    ("<img", "[图片]"),
    ("<appmsg", "[链接]"),
    ("<refermsg", "[引用]"),
)


def _epoch(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _clock(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(ZoneInfo(timezone_name or DEFAULT_TIMEZONE)).strftime("%H:%M:%S")
    except Exception:
        return value.strftime("%H:%M:%S")


def human_duration(seconds: float | int | None) -> str:
    """Render a bounded, human-readable duration for the prompt header."""

    total = max(0, int(seconds or 0))
    if total < 60:
        return f"{total}秒"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{remainder}秒" if remainder else f"{minutes}分钟"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"


def media_placeholder(text: str) -> str | None:
    """Return the placeholder for a media row, or ``None`` for ordinary text."""

    value = str(text or "").strip()
    if not value:
        return None
    if value in _MEDIA_PLACEHOLDERS:
        return value
    head = value[:120]
    if not _MEDIA_XML.search(head):
        return None
    lowered = value.lower()
    for marker, placeholder in _XML_PLACEHOLDER_ORDER:
        if marker in lowered:
            return placeholder
    return "[媒体]"


def prepare_text(text: str, max_chars: int = 1200) -> tuple[str, bool]:
    """Normalize one message for the prompt and report whether it was cut."""

    placeholder = media_placeholder(text)
    if placeholder is not None:
        return placeholder, False
    value = str(text or "")
    limit = int(max_chars or 0)
    if limit <= 0 or len(value) <= limit:
        return value, False
    return value[:limit].rstrip() + "…", True


@dataclass(slots=True)
class TopicSegment:
    """One contiguous stretch of conversation that belongs to the current turn."""

    messages: list[ConversationMessage] = field(default_factory=list)
    start_message_id: str = ""
    start_at: datetime | None = None
    start_sender: str = ""
    start_preview: str = ""
    duration_seconds: int = 0
    omitted_middle: int = 0
    omitted_span_seconds: int = 0
    truncated_prefix: bool = False
    #: How many of ``messages`` belong to the segment's opening block.  Rows
    #: after this index are the newest tail; the prompt inserts the omitted
    #: middle marker at this split point.
    head_count: int = 0
    #: ``empty`` / ``quiet`` / ``gap`` / ``minutes`` / ``scan_limit`` /
    #: ``messages`` / ``history`` / ``unknown``.
    boundary: str = "empty"

    def to_dict(
        self,
        *,
        timezone_name: str = DEFAULT_TIMEZONE,
        include_ids: bool = False,
    ) -> dict[str, object]:
        """JSON-safe summary used by the prompt and the retrieval trace."""

        payload: dict[str, object] = {
            "startSender": self.start_sender,
            "startText": self.start_preview,
            "startedAt": _clock(self.start_at, timezone_name),
            "durationSeconds": self.duration_seconds,
            "durationText": human_duration(self.duration_seconds),
            "messageCount": len(self.messages) + max(0, self.omitted_middle),
            "omittedMiddle": max(0, self.omitted_middle),
            "headCount": max(0, self.head_count),
            "truncatedPrefix": bool(self.truncated_prefix),
            "boundary": self.boundary,
        }
        if self.omitted_middle:
            payload["omittedSpanSeconds"] = self.omitted_span_seconds
            payload["omittedSpanText"] = human_duration(self.omitted_span_seconds)
        if include_ids:
            payload["messageIds"] = [
                message.message_id for message in self.messages if message.message_id
            ]
        return payload

    def trim_to_chars(self, max_chars: int) -> "TopicSegment":
        """Drop the oldest rows when the rendered segment exceeds the budget.

        The prompt keeps the segment's opening lines, so this is only a last
        resort for a topic whose every message is huge; the dropped prefix is
        reported instead of silently shrinking the window.
        """

        limit = int(max_chars or 0)
        if limit <= 0 or not self.messages:
            return self
        kept = list(self.messages)
        total = sum(len(message.text or "") for message in kept)
        dropped = 0
        while len(kept) > 1 and total > limit:
            removed = kept.pop(0)
            total -= len(removed.text or "")
            dropped += 1
        if not dropped:
            return self
        first = kept[0]
        moved = max(0, int(_epoch(first.observed_at) - _epoch(self.start_at)))
        return TopicSegment(
            messages=kept,
            start_message_id=str(first.message_id or ""),
            start_at=first.observed_at,
            start_sender=str(first.sender_name or ""),
            start_preview=_preview(media_placeholder(first.text) or first.text),
            duration_seconds=max(0, self.duration_seconds - moved),
            omitted_middle=self.omitted_middle,
            omitted_span_seconds=self.omitted_span_seconds,
            truncated_prefix=True,
            boundary=self.boundary,
            # The opening block may have been cut; the prompt must not insert
            # the omitted-middle marker at a now-wrong index.
            head_count=0,
        )


def _preview(text: str, limit: int = 40) -> str:
    value = " ".join(str(text or "").replace("\n", " ").split())
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def build_topic_segment(
    messages: list[ConversationMessage],
    *,
    trigger_at: datetime | None = None,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    max_minutes: float = DEFAULT_MAX_MINUTES,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    scan_limit: int = DEFAULT_SCAN_MESSAGES,
    min_messages: int = DEFAULT_MIN_MESSAGES,
    head_messages: int = DEFAULT_HEAD_MESSAGES,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> TopicSegment:
    """Return the segment that ends at the trigger, with its start recorded."""

    ordered = [message for message in (messages or []) if message.observed_at is not None]
    ordered.sort(key=lambda message: (_epoch(message.observed_at), str(message.message_id or "")))
    if not ordered:
        return TopicSegment(boundary="empty")

    trigger = trigger_at if trigger_at is not None else ordered[-1].observed_at
    trigger_ts = _epoch(trigger)
    scan_size = max(1, int(scan_limit or DEFAULT_SCAN_MESSAGES))
    scan = ordered[-scan_size:]
    scan_truncated = len(scan) < len(ordered)
    boundary_gap = max(0.0, float(gap_seconds or 0))
    boundary_max = max(60.0, float(max_minutes or 0) * 60.0)

    start_index = 0
    boundary = "scan_limit" if scan_truncated else "unknown"
    for index in range(len(scan) - 1, -1, -1):
        message = scan[index]
        if index + 1 < len(scan):
            newer = scan[index + 1]
            if _epoch(newer.observed_at) - _epoch(message.observed_at) > boundary_gap:
                boundary = "gap"
                start_index = index + 1
                break
        if trigger_ts - _epoch(message.observed_at) > boundary_max:
            # The newest row itself is older than the limit: the group was quiet,
            # this is a new topic and no old chatter belongs to it.  With newer
            # rows present this is a long continuous topic and only its recent
            # part is kept.
            boundary = "quiet" if index == len(scan) - 1 else "minutes"
            start_index = index + 1
            break
        if index == 0:
            boundary = "scan_limit" if scan_truncated else "history"
            start_index = 0

    selected = scan[start_index:]
    minimum = max(0, int(min_messages or 0))
    if 0 < len(selected) < minimum and start_index > 0:
        selected = scan[max(0, start_index - (minimum - len(selected))):]

    truncated_prefix = boundary in {"minutes", "scan_limit"}

    kept = list(selected)
    omitted_middle = 0
    omitted_span_seconds = 0
    head_count = len(kept)
    if len(selected) > max(1, int(max_messages or 1)):
        keep = max(1, int(max_messages))
        head = max(1, min(int(head_messages or 1), keep - 1)) if keep > 1 else keep
        tail = keep - head
        kept = selected[:head] + (selected[-tail:] if tail > 0 else [])
        omitted_middle = len(selected) - len(kept)
        head_count = head if omitted_middle > 0 else len(kept)
        if omitted_middle > 0 and tail > 0:
            omitted_span_seconds = int(
                max(0.0, _epoch(selected[-tail].observed_at) - _epoch(selected[head - 1].observed_at))
            )
        boundary = "messages"

    if not kept:
        return TopicSegment(boundary=boundary or "quiet", truncated_prefix=truncated_prefix)

    first = kept[0]
    return TopicSegment(
        messages=kept,
        start_message_id=str(first.message_id or ""),
        start_at=first.observed_at,
        start_sender=str(first.sender_name or ""),
        start_preview=_preview(media_placeholder(first.text) or first.text),
        duration_seconds=int(max(0.0, trigger_ts - _epoch(first.observed_at))),
        omitted_middle=max(0, omitted_middle),
        omitted_span_seconds=omitted_span_seconds,
        truncated_prefix=truncated_prefix,
        boundary=boundary or "unknown",
        head_count=max(0, head_count),
    )
