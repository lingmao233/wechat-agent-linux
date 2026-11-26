"""The bot's own names, shared by every module that must not confuse itself
with a group member.

``WECHAT_AI_ALIASES`` started as the addressing list for the reply trigger and
later got reused (in different ad-hoc combinations) for quote identity, the
addressee judge and the event judge.  That left one gap: the event judge was
never told which names belong to the bot, so a sentence such as
``测试成员4你能不能滚`` could be read as an insult aimed at the bot.  This module is
the single answer to "what is my name"; ``WECHAT_BOT_NAME`` is the explicit,
operator-settable part of it.
"""

from __future__ import annotations

import re

_NAME_SPLIT = re.compile(r"[,，、;；\n]+")


def _split_names(value: object) -> list[str]:
    return [part.strip() for part in _NAME_SPLIT.split(str(value or "")) if part.strip()]


def split_names(value: object) -> list[str]:
    """Split one configured name field (comma/、/semicolon separated)."""

    return _split_names(value)


def bot_names(settings) -> list[str]:
    """Every display name that means "the bot", deduplicated by case.

    Explicit ``wechat_bot_name`` comes first, then the persona and WeChat
    nickname, then the addressing aliases.  A comma-separated value is accepted
    in each field so one setting can carry a primary name and a common variant.
    """

    raw = [
        getattr(settings, "wechat_bot_name", ""),
        getattr(settings, "persona_name", ""),
        getattr(settings, "wechat_self_name", ""),
        *(getattr(settings, "wechat_ai_aliases", None) or []),
    ]
    names: list[str] = []
    seen: set[str] = set()
    for value in raw:
        for name in _split_names(value):
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                names.append(name)
    return names


def human_member_names(settings, names, *, limit: int = 50) -> list[str]:
    """Drop names that are really the bot and keep at most ``limit`` people."""

    bot_keys = {name.casefold() for name in bot_names(settings)}
    output: list[str] = []
    seen: set[str] = set()
    for raw in names:
        # A member's display name is a single label; only the configured bot
        # names may carry commas as separators.
        name = str(raw or "").strip()
        key = name.casefold()
        if not name or key in bot_keys or key in seen:
            continue
        seen.add(key)
        output.append(name)
        if len(output) >= limit:
            break
    return output
