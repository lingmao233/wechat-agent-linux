from __future__ import annotations

"""Bound one judgement to storage-safe event candidates.

The reading of the sentence now happens in ``judge.py``; this module only
applies the checks that must stay in code: the excerpt has to appear verbatim,
an @-mention of somebody else can never be an event about the bot, and a
correction must resolve its old source inside the supplied conversation
window.  No keyword table lives here any more.
"""

import re

from . import homophones
from .identity import bot_names
from .judge import Judge, Judgement
from .models import MessageEvent
from .social import EVENT_TYPES, SocialEventCandidate

__all__ = ["EVENT_TYPES", "analyze", "analyze_events", "bound", "bound_target"]

_TRAILING_PUNCTUATION = " \t\r\n，,。！？!?；;：:、…~～"


def _exact_prefix(value: str, names) -> str:
    folded = value.casefold()
    return max(
        (name for name in names if folded.startswith(name.casefold())),
        key=len,
        default="",
    )


def _homophone_prefix(value: str, names) -> str:
    """The typed text when a name is an opening homophone (``小宇`` for ``小鱼``)."""

    best = ""
    for name in names:
        if len(name) < 2 or not homophones.has_cjk(name):
            continue
        for start, end in homophones.find_homophone_spans(value, name):
            if start == 0 and end > len(best):
                best = value[:end]
    return best


def _exact_suffix(value: str, names) -> str:
    folded = value.casefold()
    return max(
        (name for name in names if folded.endswith(name.casefold())),
        key=len,
        default="",
    )


def _homophone_suffix(value: str, names) -> str:
    """The typed text when a name closes the sentence as a homophone vocative."""

    best = ""
    for name in names:
        if len(name) < 2 or not homophones.has_cjk(name):
            continue
        for start, end in homophones.find_homophone_spans(value, name):
            if end != len(value) or start == 0:
                continue
            if value[start - 1] not in "，,。！？!?；;：:、 \u2005":
                continue
            if end - start > len(best):
                best = value[start:end]
    return best


def _exact_suffix_person(value: str, names) -> str:
    """An exact member suffix only counts when it follows punctuation."""

    best = ""
    for name in names:
        if not value.casefold().endswith(name.casefold()):
            continue
        prefix = value[: -len(name)].rstrip()
        if prefix and prefix[-1] in "，,。！？!?；;：:、 \u2005" and len(name) > len(best):
            best = name
    return best


def _named_person(
    exact_bot: str,
    exact_person: str,
    homo_bot: str,
    homo_person: str,
) -> str:
    """Pick the longest vocative; exact text beats a homophone and the bot wins ties."""

    length, exact, is_bot, name = max(
        (len(exact_bot), 2, 1, exact_bot),
        (len(exact_person), 2, 0, exact_person),
        (len(homo_bot), 1, 1, homo_bot),
        (len(homo_person), 1, 0, homo_person),
        key=lambda item: (item[0], item[1], item[2]),
    )
    if not name or is_bot:
        return ""
    return name


def _addresses_member(text: str, member_names, aliases) -> str:
    """Return the member name this sentence opens (or closes) by calling.

    WeChat users often omit ``@`` and the comma: ``测试成员4你能不能滚`` is a
    vocative, not prose, and pinyin input turns the bot's own name into a
    homophone (``小鱼`` → ``小宇``, ``肥鱼`` → ``飞鱼``).  Matching is
    longest-name-first; exact text beats a homophone, a real member's exact
    name beats the bot's homophone, and the bot wins ties.  That keeps
    ``小鱼 我说的是测试成员4`` a message to the bot while ``小宇`` is not silently
    treated as a different person.
    """

    value = str(text or "").strip().lstrip("@\u2005 ").strip()
    if not value:
        return ""
    bot_keys = {str(name).strip() for name in aliases if str(name).strip()}
    people = {
        str(name).strip()
        for name in member_names
        if len(str(name).strip()) >= 2
    }
    opening = _named_person(
        _exact_prefix(value, bot_keys),
        _exact_prefix(value, people),
        _homophone_prefix(value, bot_keys),
        _homophone_prefix(value, people),
    )
    if opening:
        return opening

    # ``你能不能滚，测试成员4`` is the same vocative at the other end.  A member
    # name needs punctuation before it so ``我最喜欢测试成员4`` stays prose; the bot
    # name only suppresses the downgrade.
    tail = value.rstrip(_TRAILING_PUNCTUATION)
    closing = _named_person(
        _exact_suffix(tail, bot_keys),
        _exact_suffix_person(tail, people),
        _homophone_suffix(tail, bot_keys),
        _homophone_suffix(tail, people),
    )
    return closing


def bound_target(
    candidate: SocialEventCandidate,
    event: MessageEvent,
    aliases,
    member_names=(),
) -> SocialEventCandidate:
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
    named_member = _addresses_member(event.text, member_names, aliases)
    if candidate.target_type == "ai" and (
        other_mentions or named_member or (other_quote and not addresses_ai)
    ):
        candidate.confidence = min(candidate.confidence, 0.65)
        candidate.target_type = "member" if named_member else "unknown"
        candidate.target_key = "unknown"
        candidate.metadata = {
            **candidate.metadata,
            "target_downgraded": "named_member" if named_member else "ambiguous_or_other_target",
        }
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


def bound(event: MessageEvent, judgement: Judgement, settings, *, context=(), member_names=()) -> Judgement:
    """Apply the checks that must stay in code to an already-read message."""

    aliases = bot_names(settings)
    window = list(context)[-6:]
    kept: list[SocialEventCandidate] = []
    for candidate in judgement.events:
        if candidate.evidence_excerpt and candidate.evidence_excerpt not in event.text:
            continue
        candidate = bound_target(candidate, event, aliases, member_names=member_names)
        if candidate.event_type == "correction":
            correction = _correction(candidate, event, window)
            if correction is None:
                continue
            candidate.metadata = {**candidate.metadata, "correction": correction}
        kept.append(candidate)
    judgement.events = kept
    return judgement


def analyze(
    event: MessageEvent | None,
    *,
    context=(),
    settings,
    judge: Judge | None = None,
    member_names=(),
) -> Judgement:
    """Run one judgement and keep only what storage may safely use."""

    if event is None or event.is_self or event.source_role != "human":
        return Judgement(reason="not_human")
    if judge is None:
        return Judgement(reason="no_judge")
    if not settings.memory_event_extraction_enabled:
        return Judgement(reason="extraction_disabled")
    judgement = judge.analyze(event, context, members=member_names)
    return bound(event, judgement, settings, context=context, member_names=member_names)


def analyze_events(
    event: MessageEvent,
    judgement: Judgement,
    settings,
    *,
    context=(),
    member_names=(),
) -> list[SocialEventCandidate]:
    """Bound a judgement that came from a batched call."""

    return bound(event, judgement, settings, context=context, member_names=member_names).events

