"""Pinyin-aware matching for phone-typed group chat.

Group members type on phones with pinyin input, so a nickname or trigger word
often arrives as a homophone typo: ``肥鱼`` becomes ``飞鱼``, ``小鱼`` becomes
``小宇``.  Rules that only compare characters miss those messages and the model
then treats a mis-typed name as a different person or an unrelated word.

``pypinyin`` is an optional dependency: when it is not installed every helper
degrades to exact text so the agent still starts and behaves predictably.
"""

from __future__ import annotations

import re
from functools import lru_cache

try:  # pragma: no cover - import shape varies across versions
    from pypinyin import lazy_pinyin
except Exception:  # pragma: no cover - optional dependency
    lazy_pinyin = None

_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_WORD_CHAR = re.compile(r"[0-9A-Za-z_\u3400-\u4dbf\u4e00-\u9fff]")


def available() -> bool:
    """True when tone-insensitive pinyin comparison is possible."""

    return lazy_pinyin is not None


def has_cjk(text: object) -> bool:
    return bool(_CJK_CHAR.search(str(text or "")))


@lru_cache(maxsize=4096)
def pinyin_key(text: str) -> str:
    """Tone-insensitive pinyin for Chinese characters, verbatim for the rest.

    Comparing whole strings keeps the syllable boundary explicit, so a
    two-character name can never match a longer phrase that merely contains the
    same syllables in a different split.
    """

    value = str(text or "")
    if not value:
        return ""
    if lazy_pinyin is None:
        return value.casefold()
    parts: list[str] = []
    for char in value:
        if _CJK_CHAR.match(char):
            syllables = lazy_pinyin(char)
            parts.append(syllables[0] if syllables else char)
        else:
            parts.append(char.casefold())
    return "'".join(parts)


def homophone_equals(left: object, right: object) -> bool:
    """True when two different spellings read the same in pinyin."""

    value = str(left or "")
    other = str(right or "")
    if not value or not other or value == other:
        return False
    if lazy_pinyin is None or not (has_cjk(value) or has_cjk(other)):
        return False
    return pinyin_key(value) == pinyin_key(other)


def find_homophone_spans(text: str, target: str) -> list[tuple[int, int]]:
    """Return span offsets in ``text`` that read like ``target`` but differ.

    Only same-length Chinese windows are considered.  Exact occurrences are the
    caller's existing job and are skipped here.
    """

    value = str(text or "")
    name = str(target or "")
    if lazy_pinyin is None or len(name) < 2 or len(value) < len(name):
        return []
    if not all(_CJK_CHAR.match(char) for char in name):
        return []
    wanted = pinyin_key(name)
    if not wanted:
        return []
    spans: list[tuple[int, int]] = []
    for start in range(len(value) - len(name) + 1):
        window = value[start:start + len(name)]
        if window == name:
            continue
        if not all(_CJK_CHAR.match(char) for char in window):
            continue
        if pinyin_key(window) == wanted:
            spans.append((start, start + len(name)))
    return spans


def is_standalone(text: str, start: int, end: int) -> bool:
    """True when the span is the whole message or sits between separators.

    A homophone match inside ordinary prose (``我叫小宇``) is mostly a real
    person's name, so callers use this guard to require a genuine summons.
    """

    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return (not before or not _WORD_CHAR.match(before)) and (
        not after or not _WORD_CHAR.match(after)
    )
