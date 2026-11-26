from __future__ import annotations

"""Compact, human-readable projection of the stored profiles.

The audit profile in SQL keeps history, evidence ids and raw counters for
debugging.  None of that belongs in a per-message prompt: it is large, mostly
irrelevant to the current turn and dilutes the model's attention.  This module
turns the three scopes into a small natural-language block with a hard budget.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .timefmt import short_moment_text

__all__ = ["build_profile_summary"]

_REPLY_SHAPE = {
    "short": "1~2 句、约 5~25 字",
    "medium": "2~4 句、约 25~60 字",
    "detailed": "必要时分点、约 60~150 字",
}
_FACT_TYPES = {
    "preferences": ("preference",),
    "interests": ("interest",),
    "tools": ("tool",),
    "constraints": ("constraint",),
    "confirmed_info": ("relation", "experience"),
    "expression_habits": ("habit",),
    "corrections": ("correction",),
}


def build_profile_summary(
    user: dict[str, Any] | None,
    ai: dict[str, Any] | None,
    task: dict[str, Any] | None,
    *,
    max_chars: int = 4000,
) -> str:
    lines: list[str] = []
    user = user or {}
    ai = ai or {}
    task = task or {}
    synthesized = ai.get("synthesized") if isinstance(ai.get("synthesized"), dict) else {}

    name = str(user.get("member_name") or "").strip()
    user_lines: list[str] = []
    note = str(user.get("synthesized_note") or "").strip()
    if note:
        user_lines.append(f"群友印象：{_short(note, 60)}")
    nicknames = synthesized.get("nicknames")
    if isinstance(nicknames, dict) and name:
        nickname = str(nicknames.get(name) or "").strip()
        if nickname:
            user_lines.append(f"群友常称呼：{_short(nickname, 30)}")
    aliases = user.get("aliases")
    if isinstance(aliases, list):
        values = [str(item).strip() for item in aliases[:6] if str(item).strip()]
        if values:
            user_lines.append("群友别名：" + "、".join(values))
    summary = str(user.get("member_summary") or "").strip()
    if summary:
        user_lines.append(f"当前画像：{_short(summary, 220)}")
    _add_list(user_lines, "偏好", _profile_values(user, "preferences"), 6)
    _add_list(user_lines, "兴趣", _profile_values(user, "interests"), 6)
    _add_list(user_lines, "常用工具", _profile_values(user, "tools"), 4)
    _add_list(user_lines, "长期要求", _profile_values(user, "constraints"), 4)
    _add_list(user_lines, "最近变化", user.get("recent_changes"), 3)
    _add_list(user_lines, "群友提到（未确认）", user.get("reported_facts"), 3)

    behavior = user.get("behavior") if isinstance(user.get("behavior"), dict) else {}
    behavior_bits: list[str] = []
    average = _float(behavior.get("average_message_length"))
    if average > 0:
        behavior_bits.append(f"平均 {average:.0f} 字")
    habits = [str(item) for item in behavior.get("expression_habits") or []]
    if "short_turns" in habits:
        behavior_bits.append("多短句")
    if "frequent_questions" in habits:
        behavior_bits.append("常提问")
    if "emoji_frequent" in habits:
        behavior_bits.append("常用表情")
    if behavior_bits:
        user_lines.append("表达：" + "、".join(behavior_bits))
    _add_list(user_lines, "常聊话题", behavior.get("common_topics"), 6)
    _add_list(user_lines, "口头禅", _top_keys(behavior.get("marker_counts"), 4), 4)

    if user_lines:
        lines.append(f"群友{'（' + name + '）' if name else ''}：")
        lines.extend(f"- {line}" for line in user_lines)

    ai_lines: list[str] = []
    reply_length = str(ai.get("reply_length") or synthesized.get("reply_shape") or "").strip()
    if reply_length in _REPLY_SHAPE:
        ai_lines.append(f"回复形态：{reply_length}（建议 {_REPLY_SHAPE[reply_length]}）")
    _add_list(ai_lines, "群氛围", [synthesized.get("culture")], 1)
    tone = []
    if ai.get("formality") and ai.get("formality") != "unknown":
        tone.append(str(ai["formality"]))
    if ai.get("humor") and ai.get("humor") != "unknown":
        tone.append(f"幽默{ai['humor']}")
    if ai.get("technicality") and ai.get("technicality") != "unknown":
        tone.append(str(ai["technicality"]))
    if tone:
        ai_lines.append("语气：" + "、".join(tone))
    _add_list(ai_lines, "常聊话题", ai.get("common_topics"), 8)
    _add_list(ai_lines, "常用词", ai.get("common_vocabulary"), 8)
    _add_list(ai_lines, "可接受说法", ai.get("accepted_phrasing"), 6)
    _add_list(ai_lines, "应避免", ai.get("avoid_phrasing"), 6)
    _add_list(ai_lines, "回复示例", synthesized.get("reply_examples"), 5)
    _add_list(ai_lines, "内部梗", synthesized.get("jokes"), 5)
    _add_list(ai_lines, "整活套路", synthesized.get("bit_routines"), 3)
    _add_list(ai_lines, "互损/钓鱼", synthesized.get("tease_patterns"), 3)
    _add_list(ai_lines, "安慰话术", synthesized.get("comfort_patterns"), 3)
    _add_list(ai_lines, "接梗触发", synthesized.get("meme_triggers"), 3)
    message_shape = str(synthesized.get("message_shape") or "").strip()
    if message_shape:
        ai_lines.append("发消息习惯：" + _short(message_shape, 60))
    _add_list(ai_lines, "群内规矩", synthesized.get("rules") or ai.get("rules"), 6)
    _add_list(ai_lines, "避免话题", synthesized.get("taboos"), 5)
    if ai_lines:
        lines.append("本群 AI 画像：")
        lines.extend(f"- {line}" for line in ai_lines)

    task_lines: list[str] = []
    for key, label in (
        ("goal", "目标"),
        ("next_action", "下一步"),
        ("blockers", "阻塞"),
        ("decisions", "已决定"),
    ):
        value = task.get(key)
        if isinstance(value, (list, tuple)):
            value = "；".join(str(item) for item in value if str(item).strip())
        text = str(value or "").strip()
        if text:
            task_lines.append(f"{label}：{_short(text, 120)}")
    progress = task.get("progress")
    if isinstance(progress, (list, tuple)) and progress:
        task_lines.append("进度：" + _short("；".join(str(item) for item in progress[-2:]), 120))
    if task_lines:
        lines.append("当前任务：")
        lines.extend(f"- {line}" for line in task_lines)

    return _bounded("\n".join(lines), max_chars)


def _profile_values(user: dict[str, Any], key: str) -> Any:
    """Prefer fact entries with their own timestamps over the cached strings."""

    facts = user.get("facts")
    types = _FACT_TYPES.get(key)
    if isinstance(facts, list) and facts and types:
        items: list[str] = []
        for fact in facts:
            if not isinstance(fact, dict) or fact.get("memory_type") not in types:
                continue
            content = str(fact.get("content") or "").strip()
            if not content:
                continue
            when = _fact_time(fact.get("observed_at"))
            items.append(f"{content}（{when}）" if when else content)
            if len(items) >= 6:
                break
        if items:
            return items
    values = user.get(key)
    if isinstance(values, list):
        # Legacy profiles were stored oldest-first; the prompt wants newest.
        return values[-6:][::-1]
    return values


def _fact_time(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return short_moment_text(datetime.fromisoformat(value))
    except ValueError:
        return ""


def _add_list(target: list[str], label: str, values: Any, limit: int) -> None:
    items = _clean_items(values, limit)
    if items:
        target.append(f"{label}：" + "、".join(items))


def _clean_items(values: Any, limit: int) -> list[str]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, dict)):
        return []
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            content = str(value.get("content") or "").strip()
            when = _fact_time(value.get("valid_to") or value.get("observed_at"))
            text = f"{content}（{when}）" if content and when else content
        else:
            text = str(value).strip()
        if not text or text in result:
            continue
        result.append(_short(text, 60))
        if len(result) >= limit:
            break
    return result


def _top_keys(values: Any, limit: int) -> list[str]:
    if isinstance(values, dict):
        items = sorted(
            ((str(key), _float(value)) for key, value in values.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        return [key for key, count in items[:limit] if count > 0]
    return _clean_items(values, limit)


def _short(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _bounded(text: str, limit: int) -> str:
    limit = max(200, int(limit))
    if len(text) <= limit:
        return text
    cut = text[:limit]
    newline = cut.rfind("\n")
    if newline > limit * 0.6:
        cut = cut[:newline]
    return cut.rstrip() + "\n…（画像已截断）"
