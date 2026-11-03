"""Resolve a follow-up summons without changing the original channel event."""
from __future__ import annotations

import re

from .models import MessageEvent, QuotedMessage


def is_mention_only(text: str) -> bool:
    """A WeChat mention list, with no question/body after the mentions.

    Native mentions end in whitespace (usually U+2005). Do not consume words
    after that delimiter: ``@Away 这是什么意思`` still has its own question.
    """

    value = str(text or "").replace("＠", "@").strip()
    if not value.startswith("@"):
        return False
    remainder = re.sub(r"@[^\s@，。！？!?、,;；:：]+", "", value)
    return not remainder.strip(" \t\r\n\u2005\u200b，。！？!?、,;；:：~～")


def image_quote(quote: QuotedMessage | None) -> bool:
    return bool(quote and (quote.message_type == "image" or quote.text.strip() in {
        "[图片]", "[照片]", "[微信图片]", "[Image]",
    }))


def can_follow_up(event: MessageEvent, normalized_text: str) -> bool:
    return bool(
        not event.reply_to and not event.image_paths and not event.is_self
        and event.source_role == "human" and not event.proactive_window_id
        and is_mention_only(normalized_text)
    )
