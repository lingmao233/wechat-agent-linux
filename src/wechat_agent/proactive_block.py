"""Merge a member's consecutive short messages into one proactive turn.

The proactive scheduler picks one stored row at a time.  In real group chat a
member often sends two or three short messages in a row, and only the last row
becomes the trigger.  The earlier rows are still in the context window, but the
model tends to answer the literal last fragment ("还有俩包苹果干") without
connecting it to the sentence that introduced it ("奖励是可以追zls").

This module selects the same-sender run that belongs to the trigger turn.  The
caller can then hand the model a *speech block* — the whole burst plus the
trigger — as one utterance instead of a single fragment.  Selection is pure and
bounded: no database access, no guessing beyond the given gap.
"""

from __future__ import annotations

from datetime import datetime

from .models import ConversationMessage

#: Hard ceiling so a long monologue can never dominate the prompt.
MAX_BLOCK_MESSAGES = 5
#: One row may not carry more than this into the prompt block.
MAX_BLOCK_CHARS = 400


def select_speech_block(
    messages: list[ConversationMessage],
    *,
    trigger_at: datetime | None,
    gap_seconds: float = 60.0,
    max_messages: int = 3,
) -> list[ConversationMessage]:
    """Return the oldest-first same-sender run that precedes ``trigger_at``.

    ``messages`` must be newest-first and already scoped to one sender in one
    group by the caller.  A row joins the block while the gap to the next newer
    row (the trigger for the first step) is within ``gap_seconds``.  An assistant
    row ends the run: the burst was interrupted by the character's own reply and
    must not be merged across that turn.  Media placeholders without text are
    dropped by the caller, not here.
    """

    if trigger_at is None or max_messages <= 1:
        return []
    try:
        bounded_gap = max(0.0, min(float(gap_seconds), 600.0))
        prior_limit = max(0, min(int(max_messages), MAX_BLOCK_MESSAGES) - 1)
    except (TypeError, ValueError):
        return []
    if prior_limit == 0:
        return []

    block: list[ConversationMessage] = []
    newer_at = trigger_at
    for item in messages:
        if len(block) >= prior_limit:
            break
        if item.source_role == "assistant":
            break
        observed = item.observed_at
        if observed is None:
            break
        gap = (newer_at - observed).total_seconds()
        if gap < 0 or gap > bounded_gap:
            break
        text = str(item.text or "").strip()
        if not text:
            break
        if len(text) > MAX_BLOCK_CHARS:
            text = text[:MAX_BLOCK_CHARS].rstrip()
        if text != item.text:
            item = ConversationMessage(
                sender_name=item.sender_name,
                sender_id=item.sender_id,
                text=text,
                observed_at=item.observed_at,
                message_id=item.message_id,
                thread_id=item.thread_id,
                task_id=item.task_id,
                image_paths=list(item.image_paths),
                source_role=item.source_role,
                reply_to_sender=item.reply_to_sender,
                reply_to_text=item.reply_to_text,
            )
        block.append(item)
        newer_at = observed
    return list(reversed(block))


def block_payload(block: list[ConversationMessage], *, timezone_name: str = "", stamp=None) -> list[dict[str, object]]:
    """Project a speech block for the prompt as bounded JSON-friendly rows."""

    payload: list[dict[str, object]] = []
    for item in block or []:
        row: dict[str, object] = {
            "senderName": item.sender_name or "unknown",
            "messageId": item.message_id or "",
            "content": str(item.text or "")[:MAX_BLOCK_CHARS],
        }
        if callable(stamp) and item.observed_at is not None:
            row["sentAt"] = stamp(item.observed_at, timezone_name=timezone_name)
        payload.append(row)
    return payload
