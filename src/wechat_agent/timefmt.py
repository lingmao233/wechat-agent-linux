"""Time helpers for prompts.

Everything the model sees must be Beijing time expressed the way a person reads
a clock.  The database keeps aware UTC datetimes; converting them here is the
only place that knows about the group timezone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
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
    """``2026-11-02 17:35（3 小时前）``; empty string for missing values."""

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
        return f"昨天 {local:%H:%M}"
    if day_count < 7:
        return f"{local:%Y-%m-%d %H:%M}（{relative_text(delta)}）"
    return f"{local:%Y-%m-%d %H:%M}"
