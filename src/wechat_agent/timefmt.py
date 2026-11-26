"""Time helpers for prompts.

Everything the model sees must be Beijing time expressed the way a person reads
a clock.  The database keeps aware UTC datetimes; converting them here is the
only place that knows about the group timezone.

Two rules matter for memory:

* an absolute date is always shown, even for yesterday, so a stored fact can
  never be read as "yesterday" several days later;
* relative dates written in a message ("昨天", "上周三") are resolved against
  that message's timestamp before they become a durable fact.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WEEKDAY_NUMBERS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}
_ZONE_LABELS = {
    "Asia/Shanghai": "北京时间",
    "Asia/Chongqing": "北京时间",
    "Asia/Hong_Kong": "香港时间",
    "Asia/Taipei": "台北时间",
    "Asia/Tokyo": "日本时间",
}


def resolve_zone(name: str | None) -> ZoneInfo:
    """Return the configured zone, falling back to Beijing time."""

    for candidate in (name, DEFAULT_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo(DEFAULT_TIMEZONE)


def zone_label(name: str | None) -> str:
    """Human label for the configured zone, falling back with the zone itself."""

    resolved = str(resolve_zone(name))
    return _ZONE_LABELS.get(resolved, resolved)


def to_local(value: datetime | None, *, timezone_name: str | None = None) -> datetime | None:
    """Treat a naive value as UTC (that is how rows are stored) and convert."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(resolve_zone(timezone_name))


def clock_text(now: datetime | None = None, *, timezone_name: str | None = None) -> str:
    """``2026-11-02 19:23（周一，北京时间）``"""

    current = to_local(now or datetime.now(timezone.utc), timezone_name=timezone_name)
    assert current is not None
    return (
        f"{current:%Y-%m-%d %H:%M}"
        f"（{_WEEKDAYS[current.weekday()]}，{zone_label(timezone_name)}）"
    )


def moment_text(value: datetime | None, *, timezone_name: str | None = None) -> str:
    """``2026-11-02 17:35``; empty string for a missing value.

    This is the format used next to a stored fact: it must never be reduced to
    a bare "昨天", because a fact outlives the day it was said.
    """

    local = to_local(value, timezone_name=timezone_name)
    return f"{local:%Y-%m-%d %H:%M}" if local is not None else ""


def short_moment_text(value: datetime | None, *, timezone_name: str | None = None) -> str:
    """``11-02 17:35`` for tight prompt/profile projections."""

    local = to_local(value, timezone_name=timezone_name)
    return f"{local:%m-%d %H:%M}" if local is not None else ""


def _split(delta: timedelta) -> tuple[int, int, int]:
    total = int(max(0.0, delta.total_seconds()))
    return total // 86400, total % 86400 // 3600, total % 3600 // 60


def relative_text(delta: timedelta) -> str:
    """Human phrasing for how long ago something happened."""

    day_count, hours, minutes = _split(delta)
    if day_count == 0:
        if hours == 0 and minutes == 0:
            return "刚刚"
        if hours == 0:
            return f"{minutes} 分钟前"
        if minutes == 0:
            return f"{hours} 小时前"
        return f"{hours} 小时 {minutes} 分钟前"
    if day_count == 1:
        return "昨天"
    if day_count < 7:
        return f"{day_count} 天前"
    if day_count < 30:
        return f"{day_count // 7} 周前"
    if day_count < 365:
        return f"{day_count // 30} 个月前"
    return f"{day_count // 365} 年前"


def stamp(
    value: datetime | None,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> str:
    """``2026-11-02 17:35（3 小时前）``; empty string for missing values.

    Yesterday is rendered as ``2026-11-02 17:35（昨天）`` rather than a bare
    ``昨天 17:35``: the relative word is a convenience, never the only anchor.
    """

    if value is None:
        return ""
    current = to_local(now or datetime.now(timezone.utc), timezone_name=timezone_name)
    local = to_local(value, timezone_name=timezone_name)
    assert current is not None and local is not None
    delta = current - local
    if delta < timedelta(0):
        # Clock skew or a scheduled row ahead of us: never claim "in 3 hours".
        return f"{local:%Y-%m-%d %H:%M}"
    day_count, _hours, _minutes = _split(delta)
    if day_count == 0:
        return f"{local:%Y-%m-%d %H:%M}（{relative_text(delta)}）"
    if day_count == 1:
        return f"{local:%Y-%m-%d %H:%M}（昨天）"
    if day_count < 7:
        return f"{local:%Y-%m-%d %H:%M}（{relative_text(delta)}）"
    return f"{local:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# Relative-date anchoring for durable facts
# ---------------------------------------------------------------------------

_RELATIVE_DAY_WORDS = (
    ("大前天", -3),
    ("前天", -2),
    ("昨天", -1),
    ("昨日", -1),
    ("今天", 0),
    ("今日", 0),
    ("明天", 1),
    ("后天", 2),
    ("大后天", 3),
)
_RELATIVE_DAY_RE = re.compile("|".join(cue for cue, _ in _RELATIVE_DAY_WORDS))
_WEEKDAY_RE = re.compile(r"(上周|本周|这周|周|星期)([一二三四五六日天])")
_DAYS_AGO_RE = re.compile(r"(\d{1,2})\s*天前")


def _date_label(target: date, anchor: date) -> str:
    if target.year == anchor.year:
        return f"{target.month}月{target.day}日"
    return f"{target.year}年{target.month}月{target.day}日"


def _weekday_date(anchor: date, week_prefix: str, weekday: int) -> date | None:
    monday = anchor - timedelta(days=anchor.weekday())
    if week_prefix == "上周":
        return monday - timedelta(days=7) + timedelta(days=weekday)
    if week_prefix in {"本周", "这周"}:
        return monday + timedelta(days=weekday)
    # Bare "周三"/"星期三": the most recent occurrence on or before the anchor.
    candidate = monday + timedelta(days=weekday)
    if candidate > anchor:
        candidate -= timedelta(days=7)
    return candidate


def resolve_relative_dates(
    text: str,
    anchor: datetime | None,
    *,
    timezone_name: str | None = None,
) -> tuple[str, datetime | None]:
    """Replace relative date words in ``text`` with absolute dates.

    ``anchor`` is the message timestamp (the moment the sentence was said).
    Returns the rewritten text and the first resolved local midnight (as an
    aware UTC datetime).  When the anchor is missing or nothing is found, the
    text is returned unchanged and the timestamp is ``None``.

    The rewrite only changes the date phrase; the rest of the sentence is kept
    verbatim so the original claim remains auditable.
    """

    value = str(text or "")
    local_anchor = to_local(anchor, timezone_name=timezone_name)
    if not value or local_anchor is None:
        return value, None
    zone = resolve_zone(timezone_name)
    anchor_date = local_anchor.date()
    resolved: list[date] = []

    def day_word(match: re.Match[str]) -> str:
        target = anchor_date + timedelta(days=dict(_RELATIVE_DAY_WORDS)[match.group(0)])
        resolved.append(target)
        return _date_label(target, anchor_date)

    value = _RELATIVE_DAY_RE.sub(day_word, value)

    def weekday_word(match: re.Match[str]) -> str:
        target = _weekday_date(anchor_date, match.group(1), _WEEKDAY_NUMBERS[match.group(2)])
        if target is None:
            return match.group(0)
        resolved.append(target)
        return _date_label(target, anchor_date)

    value = _WEEKDAY_RE.sub(weekday_word, value)

    def days_ago(match: re.Match[str]) -> str:
        count = int(match.group(1))
        if count <= 0 or count > 60:
            return match.group(0)
        target = anchor_date - timedelta(days=count)
        resolved.append(target)
        return _date_label(target, anchor_date)

    value = _DAYS_AGO_RE.sub(days_ago, value)
    if not resolved:
        return value, None
    first = min(resolved)
    event_time = datetime.combine(first, time.min, tzinfo=zone).astimezone(timezone.utc)
    return value, event_time
