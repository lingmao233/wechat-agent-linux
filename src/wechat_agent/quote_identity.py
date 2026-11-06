"""Decide whether a quoted WeChat message was sent by this bot itself.

WeChat's ``<refermsg>`` block does not reliably identify the sender of a quoted
message when that message came from the bot.  Production case
``messages.id=4339`` (2026-11-05 21:01 Beijing) quoted the bot's own line but
carried ``fromusr=47371124839@chatroom`` (the group id) and
``displayname=Away``.  The prompt then treated "Away" as an ordinary member and
the bot answered "那句又不是我发的".

Resolution order:
1. the refermsg ``fromusr`` equals the bot wxid (definitive);
2. the quoted server id matches a stored message in this account/group, whose
   ``is_self`` flag is the local truth;
3. the quoted display name equals a configured self identity (``wechat_self_name``,
   ``persona_name`` or one of ``wechat_ai_aliases``) -- this also repairs
   historical rows whose quoted message has already been purged.
"""

from __future__ import annotations

import unicodedata

from sqlalchemy import select

from .storage import MessageRow


def normalize_identity(value: object) -> str:
    """Normalize a display name for identity comparison (full-width safe)."""

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def self_identity_names(settings) -> set[str]:
    """The account nicknames that identify the bot as a message sender.

    ``wechat_ai_aliases`` is intentionally not included: those are addressing
    words used by members ("AI"/"机器人"), and a human whose nickname happens to
    be one of them must not be mistaken for the bot.
    """

    names = {
        str(getattr(settings, "wechat_self_name", "") or ""),
        str(getattr(settings, "persona_name", "") or ""),
    }
    return {normalized for name in names if (normalized := normalize_identity(name))}


def quoted_message_is_self(storage, account_id: str, group_id: str, quote) -> bool:
    """Return True when ``quote`` is this bot's own earlier message."""

    if quote is None:
        return False
    sender_id = str(getattr(quote, "sender_id", "") or "").strip()
    if sender_id and sender_id == account_id:
        return True
    server_id = str(getattr(quote, "message_id", "") or "").strip()
    if server_id:
        with storage.Session() as session:
            row = session.scalar(
                select(MessageRow)
                .where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.external_message_id.like(f"%:{server_id}"),
                )
                .order_by(MessageRow.id.desc())
                .limit(1)
            )
            if row is not None:
                return bool(row.is_self)
    return normalize_identity(getattr(quote, "sender_name", "")) in self_identity_names(storage.settings)
