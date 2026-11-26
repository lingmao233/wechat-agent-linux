"""Member alias resolution and learning.

A group member is identified by a stable ``member_id``; display names and the
names people actually type (real name, pinyin initials, homophones, old
nicknames) are surface forms.  This module turns those forms into member ids
**generically**: there is no hardcoded list of people, and a name is only ever
linked to one id when the evidence is unambiguous.

The same resolver is used by the retrieval planner, the event judge and the
console: if ``测试成员11``/``czx``/``测试成员21`` all point at one ``member_id``, asking
about any of them searches that member's messages and facts.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select

from . import homophones
from .identity import bot_names
from .memory_jobs import aware
from .storage import MessageRow

log = logging.getLogger(__name__)

_INTRO_PATTERNS = (
    re.compile(r"(?:我是|我叫|叫我|大家可以叫我|请叫我)\s*([\u4e00-\u9fffA-Za-z0-9_\-]{1,16})"),
    re.compile(r"我(?:的)?(?:名字|昵称)(?:是|叫)\s*([\u4e00-\u9fffA-Za-z0-9_\-]{1,16})"),
)
_ALIAS_CONFIG_ENTRY = re.compile(r"^\s*([^=＝]+)\s*[=＝]\s*(.+?)\s*$")


def parse_alias_config(raw: str) -> list[tuple[str, list[str]]]:
    """Parse ``成员=别名1|别名2;成员2=别名3`` into ordered entries.

    ``成员`` may be a stable member id or the current display name; the caller
    resolves display names against the live directory.  Bad entries are skipped,
    never guessed.
    """

    value = str(raw or "").strip()
    if not value:
        return []
    entries: list[tuple[str, list[str]]] = []
    for chunk in re.split(r"[;；\n]+", value):
        match = _ALIAS_CONFIG_ENTRY.match(chunk)
        if not match:
            continue
        key = match.group(1).strip()
        aliases = [
            item.strip()
            for item in re.split(r"[|｜、,，]+", match.group(2))
            if item.strip()
        ]
        if key and aliases:
            entries.append((key, aliases))
    return entries


def _clean_alias(value: object) -> str:
    text = " ".join(str(value or "").replace("@", " ").split()).strip(" ，,。.!！？?；;:：")
    return text[:32]


def _initials(form: str) -> str:
    """Pinyin initials of a Chinese form (``测试成员11`` -> ``czx``)."""

    if not form or not homophones.has_cjk(form):
        return ""
    key = homophones.pinyin_key(form)
    if not key:
        return ""
    parts = [part for part in key.split("'") if part]
    initials = "".join(part[0] for part in parts if part[:1].isascii() and part[:1].isalpha())
    return initials.casefold()


def _full_pinyin(form: str) -> str:
    key = homophones.pinyin_key(form)
    if not key:
        return ""
    return "".join(part for part in key.split("'") if part).casefold()


def _ascii_core(value: str) -> str:
    """Lowercase letters/digits only: ``测试成员21`` and ``ZXXX`` share ``zxxx``."""

    return re.sub(r"[^0-9a-z]", "", str(value or "").casefold())


def _ascii_token_present(text: str, token: str) -> bool:
    if not token or len(token) < 2:
        return False
    pattern = r"(?<![0-9a-z_])" + re.escape(token) + r"(?![0-9a-z_])"
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


class MemberAliasService:
    def __init__(self, runtime):
        self.runtime, self.storage, self.settings = runtime, runtime.storage, runtime.settings

    # ------------------------------------------------------------------ public
    def sync_config(self, account_id: str, group_id: str) -> int:
        """Apply operator-maintained aliases; display-name keys are resolved."""

        entries = parse_alias_config(getattr(self.settings, "wechat_member_aliases", ""))
        if not entries:
            return 0
        directory = self.storage.member_directory(account_id, group_id, limit=200)
        by_id = {item["id"]: item for item in directory}
        by_name = {item["name"].casefold(): item for item in directory}
        applied = 0
        for key, aliases in entries:
            member = by_id.get(key) or by_name.get(key.casefold())
            if member is None:
                log.debug("member alias config ignored account=%s group=%s unknown=%s", account_id, group_id, key)
                continue
            for alias in aliases:
                if self.storage.upsert_member_alias(
                    account_id=account_id,
                    group_id=group_id,
                    member_id=member["id"],
                    alias=alias,
                    alias_type="manual",
                    source="manual",
                    confidence=1.0,
                ):
                    applied += 1
        return applied

    def sync_nicknames(self, account_id: str, group_id: str, *, limit: int = 400) -> int:
        """Backfill display-name history from already stored messages."""

        with self.storage.Session() as session:
            rows = session.execute(
                select(MessageRow.member_id, MessageRow.member_name)
                .where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                    MessageRow.is_self.is_(False),
                    MessageRow.member_id != account_id,
                    MessageRow.member_name != "",
                )
                .order_by(MessageRow.id.desc())
                .limit(limit)
            ).all()
        applied = 0
        for member_id, member_name in rows:
            if self.storage.upsert_member_alias(
                account_id=account_id,
                group_id=group_id,
                member_id=str(member_id or ""),
                alias=str(member_name or ""),
                alias_type="nickname",
                source="nickname",
                confidence=0.95,
            ):
                applied += 1
        return applied

    def observe_self_intro(self, event) -> bool:
        """Record a self-introduction only when it names the author explicitly."""

        text = str(getattr(event, "text", "") or "")
        if not text or getattr(event, "is_self", False) or event.member_id == event.account_id:
            return False
        bot_keys = {name.casefold() for name in bot_names(self.settings)}
        member_name = str(getattr(event, "member_name", "") or "")
        for pattern in _INTRO_PATTERNS:
            for match in pattern.finditer(text):
                # Ignore the still-common "我是说…/我是觉得…" false positives.
                alias = _clean_alias(match.group(1))
                if not alias or alias.casefold() in bot_keys:
                    continue
                if alias.casefold() == member_name.casefold():
                    continue
                if not any(char in alias for char in ("觉得", "说", "想", "在", "不")):
                    return self.storage.upsert_member_alias(
                        account_id=event.account_id,
                        group_id=event.group_id,
                        member_id=event.member_id,
                        alias=alias,
                        alias_type="real_name",
                        source="self_intro",
                        confidence=0.7,
                        evidence_message_id=str(event.external_message_id or event.ingest_sequence or ""),
                    )
        return False

    def aliases(self, account_id: str, group_id: str) -> dict[str, list[dict]]:
        return self.storage.member_aliases(account_id, group_id)

    def directory(self, account_id: str, group_id: str) -> list[dict]:
        return self.storage.member_directory(account_id, group_id)

    def resolve(self, text: str, account_id: str, group_id: str) -> list[dict]:
        """Return every member mentioned by ``text`` through any alias form.

        The result is ordered by method confidence.  ``ambiguous`` means the
        same surface form (or equally strong forms) points at more than one
        member; callers must not hard-pick then.
        """

        value = str(text or "")
        if not value.strip():
            return []
        directory = self.directory(account_id, group_id)
        if not directory:
            return []
        alias_map = self.aliases(account_id, group_id)
        bot_keys = {name.casefold() for name in bot_names(self.settings)}
        folded = value.casefold()
        matches: list[dict] = []
        for member in directory:
            member = {
                **member,
                "aliases": [
                    entry
                    for entry in alias_map.get(member["id"], [])
                    if str(entry.get("alias") or "").casefold() not in bot_keys
                ],
            }
            forms = self._forms(member)
            best: dict | None = None
            for form, rank, method in forms:
                found, matched_form = self._match_form(value, folded, form)
                if not found:
                    continue
                candidate = {
                    "member_id": member["id"],
                    "member_name": member["name"],
                    "matched": matched_form,
                    "method": method,
                    "rank": rank,
                    "confidence": max(0.0, min(1.0, 0.95 - rank * 0.05)),
                    "ambiguous": False,
                }
                if best is None or candidate["rank"] < best["rank"]:
                    best = candidate
            if best is not None:
                matches.append(best)
        if not matches:
            return []
        # A form that points at several members must not be resolved by rank
        # alone; mark all of them ambiguous when the best rank is shared.
        best_rank = min(item["rank"] for item in matches)
        strong = [item for item in matches if item["rank"] == best_rank]
        if len(strong) > 1:
            for item in strong:
                item["ambiguous"] = True
        matches.sort(key=lambda item: (item["rank"], -item["confidence"]))
        return matches

    def resolve_unique(self, text: str, account_id: str, group_id: str) -> list[dict]:
        """Members that are safe to use as a search scope (no ambiguity)."""

        matches = self.resolve(text, account_id, group_id)
        return [item for item in matches if not item["ambiguous"]]

    # --------------------------------------------------------------- discovery
    def discover(self, account_id: str, group_id: str, *, limit: int = 80) -> dict:
        """Ask the model for aliases from recent chat; every item needs evidence."""

        if not (self.runtime.llm and getattr(self.settings, "llm_api_key", "")):
            return {"aliases": 0, "candidates": 0}
        directory = self.directory(account_id, group_id)
        if len(directory) < 2:
            return {"aliases": 0, "candidates": 0}
        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                        MessageRow.lifecycle_status == "active",
                        MessageRow.is_self.is_(False),
                        MessageRow.source_role == "human",
                        MessageRow.text_kind.notin_(["system", "emoji"]),
                    )
                    .order_by(MessageRow.id.desc())
                    .limit(max(20, min(int(limit), 120)))
                )
            )
        rows.reverse()
        messages = []
        by_id: dict[str, str] = {}
        for row in rows:
            text = self.storage._row_text(row).strip()
            if not text or text.startswith("["):
                continue
            message_id = str(row.external_message_id or row.id)
            by_id[message_id] = text
            messages.append(
                {
                    "message_id": message_id,
                    "member_id": row.member_id,
                    "member_name": row.member_name,
                    "text": text[:200],
                    "at": aware(row.source_created_at or row.observed_at).isoformat(),
                }
            )
        if len(messages) < 3:
            return {"aliases": 0, "candidates": 0}
        payload = {
            "members": directory[:50],
            "messages": messages[-40:],
        }
        system = (
            "你在为群聊机器人整理成员别名，只返回 JSON："
            '{"aliases":[{"member_id":"成员ID","alias":"称呼","evidence_message_id":"消息ID","confidence":0.0}]}'
            "。alias 必须是消息里真实出现过的称呼（昵称、真实姓名、拼音首字母、同音字都算）；"
            "只能指向 members 里的 member_id；evidence_message_id 必须是 messages 里真实存在的消息，且 alias 能在该消息正文中找到；"
            "同一个人被不同写法称呼（例如真名、缩写、同音字、旧昵称）时可以输出多条；"
            "不同成员共用的称呼不要输出；不确定就返回空数组，宁可输出空，不编造。"
        )
        try:
            raw = self.runtime.llm.complete(
                system,
                json.dumps(payload, ensure_ascii=False),
                timeout_seconds=min(60.0, float(getattr(self.settings, "llm_timeout_seconds", 60.0) or 60.0)),
            )
        except Exception as exc:  # a failed curation call must not break maintenance
            log.warning("alias discovery model call failed error=%s", type(exc).__name__)
            return {"aliases": 0, "candidates": 0}
        value = _parse_object(raw)
        if value is None or not isinstance(value.get("aliases"), list):
            return {"aliases": 0, "candidates": 0}
        allowed = {item["id"]: item for item in directory}
        existing = self.storage.member_aliases(account_id, group_id)
        active = candidates = 0
        for item in value["aliases"][:40]:
            if not isinstance(item, dict):
                continue
            member_id = str(item.get("member_id") or "").strip()
            alias = _clean_alias(item.get("alias"))
            evidence_id = str(item.get("evidence_message_id") or "").strip()
            confidence = item.get("confidence")
            if member_id not in allowed or not alias or evidence_id not in by_id:
                continue
            if alias.casefold() in {name.casefold() for name in bot_names(self.settings)}:
                continue
            if alias not in by_id[evidence_id]:
                continue
            try:
                score = float(confidence)
            except (TypeError, ValueError):
                continue
            if not 0.0 <= score <= 1.0:
                continue
            # Never overwrite a name that already belongs to another member.
            owners = {
                other_id
                for other_id, items in existing.items()
                for entry in items
                if entry["alias"].casefold() == alias.casefold()
            }
            if owners and member_id not in owners:
                continue
            stored = self.storage.upsert_member_alias(
                account_id=account_id,
                group_id=group_id,
                member_id=member_id,
                alias=alias,
                alias_type="learned",
                source="model",
                confidence=score,
                evidence_message_id=evidence_id,
            )
            if stored:
                if score >= 0.8:
                    active += 1
                else:
                    candidates += 1
        return {"aliases": active, "candidates": candidates}

    # ---------------------------------------------------------------- matching
    def _forms(self, member: dict) -> list[tuple[str, int, str]]:
        """(surface form, rank, method); lower rank is stronger evidence."""

        forms: list[tuple[str, int, str]] = []
        name = str(member.get("name") or "").strip()
        if name:
            forms.append((name, 1, "display_name"))
            initials = _initials(name)
            if initials:
                forms.append((initials, 3, "initials"))
            full = _full_pinyin(name)
            if full:
                forms.append((full, 4, "pinyin"))
        for entry in member.get("aliases") or []:
            alias = str(entry.get("alias") if isinstance(entry, dict) else entry).strip()
            if not alias:
                continue
            source = str(entry.get("source") if isinstance(entry, dict) else "")
            confidence = float(entry.get("confidence") or 0.0) if isinstance(entry, dict) else 0.5
            if source not in {"manual", "nickname", "self_intro"} and confidence < 0.8:
                # A low-confidence model guess is stored for review but must
                # not steer retrieval by itself.
                continue
            if source == "manual":
                rank, method = 0, "manual"
            elif source == "nickname":
                rank, method = 1, "nickname"
            elif source == "self_intro":
                rank, method = 2, "self_intro"
            elif confidence >= 0.8:
                rank, method = 2, "learned"
            else:
                rank, method = 5, "candidate"
            forms.append((alias, rank, method))
            initials = _initials(alias)
            if initials:
                forms.append((initials, rank + 1, "initials"))
        # Deduplicate by casefold, keeping the strongest rank.
        best: dict[str, tuple[str, int, str]] = {}
        for form, rank, method in forms:
            if not form:
                continue
            key = form.casefold()
            if key not in best or rank < best[key][1]:
                best[key] = (form, rank, method)
        return list(best.values())

    @staticmethod
    def _match_form(value: str, folded: str, form: str) -> tuple[bool, str]:
        if not form:
            return False, ""
        if homophones.has_cjk(form):
            if form.casefold() in folded:
                return True, form
            for start, end in homophones.find_homophone_spans(value, form):
                # Member aliases are names inside a sentence ("问测试成员12…"),
                # so unlike a bot vocative they are not required to stand alone.
                return True, value[start:end]
            return False, ""
        token = _ascii_core(form)
        if len(token) >= 2 and _ascii_token_present(folded, token):
            return True, token
        return False, ""


def _parse_object(raw) -> dict | None:
    if not isinstance(raw, str):
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    if not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def alias_service(runtime) -> MemberAliasService:
    cached = getattr(runtime, "_alias_service", None)
    if cached is None:
        cached = MemberAliasService(runtime)
        runtime._alias_service = cached
    return cached
