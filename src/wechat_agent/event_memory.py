from __future__ import annotations

"""Bound one judgement to storage-safe event candidates.

The reading of the sentence now happens in ``judge.py``; this module only
applies the checks that must stay in code: the excerpt has to appear verbatim,
an @-mention of somebody else can never be an event about the bot, and a
correction must resolve its old source inside the supplied conversation
window.  No keyword table lives here any more.
"""

import re

from .judge import Judge, Judgement
from .models import MessageEvent
from .social import EVENT_TYPES, SocialEventCandidate

__all__ = ["EVENT_TYPES", "analyze", "analyze_events", "bound", "bound_target"]


def bound_target(candidate: SocialEventCandidate, event: MessageEvent, aliases) -> SocialEventCandidate:
    """Downgrade an AI target when the message clearly points at someone else."""

    known = {alias.strip().casefold() for alias in aliases if alias and alias.strip()}
    mentions = re.findall(r"@([^\s@，,。!?！？]+)", event.text)
    other_mentions = any(name.casefold() not in known for name in mentions)
    quote = event.reply_to
    other_quote = bool(
        quote
        and (
            (quote.sender_id and quote.sender_id != event.account_id)
            or (not quote.sender_id and quote.sender_name and quote.sender_name.casefold() not in known)
        )
    )
    addresses_ai = any(alias and alias in event.text for alias in aliases)
    if candidate.target_type == "ai" and (other_mentions or (other_quote and not addresses_ai)):
        candidate.confidence = min(candidate.confidence, 0.65)
        candidate.target_type, candidate.target_key = "unknown", "unknown"
        candidate.metadata = {**candidate.metadata, "target_downgraded": "ambiguous_or_other_target"}
    if not event.member_id:
        candidate.confidence = min(candidate.confidence, 0.7)
    return candidate


def _correction(item, event: MessageEvent, context) -> dict | None:
    change = (item.metadata or {}).get("correction")
    if not isinstance(change, dict) or re.search(r"(?:是不是|是否|有人说|他说|她说|听说)|[？?]", event.text):
        return None
    fields = ("subject", "field", "old_value", "new_value", "old_source_id")
    if any(not isinstance(change.get(key), str) or not change[key].strip() or len(change[key]) > 200 for key in fields):
        return None
    change = {key: change[key].strip() for key in fields}
    if change["old_value"] == change["new_value"]:
        return None
    if any(change[key] not in event.text for key in ("subject", "field", "new_value")):
        return None
    # A model-provided source id is only a proposal. Resolve it to a human
    # source from the supplied, already scoped conversation window.
    current_id = str(event.external_message_id or event.ingest_sequence or "")
    source = next(
        (
            row
            for row in context
            if row.message_id == change["old_source_id"]
            and row.message_id != current_id
            and row.source_role == "human"
            and change["old_value"] in row.text
        ),
        None,
    )
    if source is None or (change["subject"] == "我" and (not event.member_id or source.sender_id != event.member_id)):
        return None
    return change


def bound(event: MessageEvent, judgement: Judgement, settings, *, context=()) -> Judgement:
    """Apply the checks that must stay in code to an already-read message."""

    aliases = [*settings.wechat_ai_aliases, settings.wechat_self_name, settings.persona_name]
    window = list(context)[-6:]
    kept: list[SocialEventCandidate] = []
    for candidate in judgement.events:
        if candidate.evidence_excerpt and candidate.evidence_excerpt not in event.text:
            continue
        candidate = bound_target(candidate, event, aliases)
        if candidate.event_type == "correction":
            correction = _correction(candidate, event, window)
            if correction is None:
                continue
            candidate.metadata = {**candidate.metadata, "correction": correction}
        kept.append(candidate)
    judgement.events = kept
    return judgement


def analyze(event: MessageEvent | None, *, context=(), settings, judge: Judge | None = None) -> Judgement:
    """Run one judgement and keep only what storage may safely use."""

    if event is None or event.is_self or event.source_role != "human":
        return Judgement(reason="not_human")
    if judge is None:
        return Judgement(reason="no_judge")
    if not settings.memory_event_extraction_enabled:
        return Judgement(reason="extraction_disabled")
    judgement = judge.analyze(event, context)
    return bound(event, judgement, settings, context=context)


def analyze_events(event: MessageEvent, judgement: Judgement, settings, *, context=()) -> list[SocialEventCandidate]:
    """Bound a judgement that came from a batched call."""

    return bound(event, judgement, settings, context=context).events

