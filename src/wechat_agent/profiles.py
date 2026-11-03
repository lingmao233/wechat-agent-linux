from __future__ import annotations

"""Deterministic, privacy-bounded behavioural profile extraction.

The extractor only counts observable behaviour of one message: length,
punctuation, emoji, repeated vocabulary and topic words.  It does not assert
personal facts; what a sentence *means* is decided in ``judge.py``.  Long-term
values live in the fact ledger, not in this file.
"""

from collections import Counter
import re
import unicodedata
from typing import Any

from .group_style import ADDRESS_MARKERS, ENDING_MARKERS, STYLE_MARKERS
from .models import MessageEvent
from .textnorm import normalize_chat_text


def _clean(value: str, limit: int = 240) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = " ".join(normalized.split()).strip(" ，,。.!！？?；;:：")
    return normalized[:limit].strip()


def _profile_text(value: str, limit: int = 1200) -> str:
    """Normalize a WeChat row before extracting behavioral signals.

    The database adapter can expose sender prefixes and XML wrapper payloads
    for quoted messages.  Those transport fields must not become a member's
    supposed vocabulary or interest.
    """

    normalized = normalize_chat_text(value, max_chars=limit)
    return _clean(normalized.visible_text, limit)


def extract_member_profile_patch(
    text: str,
    message_id: str | None,
    *,
    ai_aliases: tuple[str, ...] | list[str] = (),
) -> dict[str, object]:
    """Extract cumulative behavioral signals from one member message.

    This is deliberately broader than explicit preference extraction: repeated
    topics, wording, message length, punctuation and interaction habits become
    soft profile signals.  It does not assert sensitive traits or turn a
    one-off sentence into a confirmed personal fact.
    """

    normalized = normalize_chat_text(text, ai_aliases=ai_aliases)
    if normalized.is_system:
        return {}
    value = _clean(normalized.visible_text.replace("@AI", " "), 1200)
    if not value and not normalized.sticker_count:
        return {}
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,30}|[\u4e00-\u9fff]{2,8}", value)
    vocabulary = Counter(token.casefold() for token in tokens if len(token) >= 2)
    folded = value.casefold()
    topic_markers = {
        "软件开发": ("代码", "bug", "api", "接口", "数据库", "服务"),
        "模型与 AI": ("模型", "ai", "提示词", "embedding", "向量"),
        "部署与运维": ("部署", "配置", "服务器", "上线", "日志"),
        "测试与验证": ("测试", "验证", "回归", "验收"),
        "设计与内容": ("设计", "界面", "图片", "视频", "文案"),
        "学习与考试": ("学习", "考试", "单词", "四级", "课程"),
        "娱乐与生活": ("电影", "音乐", "游戏", "旅行", "吃饭", "天气"),
    }
    topic_counts = {
        topic: sum(folded.count(marker.casefold()) for marker in markers)
        for topic, markers in topic_markers.items()
    }
    topic_counts = {topic: count for topic, count in topic_counts.items() if count}
    marker_counts = {
        marker: folded.count(marker.casefold())
        for marker in STYLE_MARKERS
        if marker.casefold() in folded
    }
    ending_counts = {
        marker: 1
        for marker in ENDING_MARKERS
        if value.rstrip().endswith(marker)
    }
    address_counts = {
        marker: value.count(marker)
        for marker in ADDRESS_MARKERS
        if marker in value
    }
    emoji_count = sum(1 for char in value if _is_emoji(char)) + normalized.sticker_count
    return {
        "behavior": {
            "message_count": 1,
            "char_count": len(value),
            "short_message_count": int(len(value) <= 16),
            "question_count": value.count("?") + value.count("？"),
            "exclamation_count": value.count("!") + value.count("！"),
            "emoji_count": emoji_count,
            "sticker_count": normalized.sticker_count,
            "image_count": normalized.image_count,
            "linebreak_count": int("\n" in value or "\r" in value),
            "marker_counts": marker_counts,
            "ending_counts": ending_counts,
            "address_counts": address_counts,
            "topic_counts": topic_counts,
            "vocabulary": dict(vocabulary.most_common(30)),
            "evidence_message_ids": [str(message_id)] if message_id else [],
        }
    }


def _is_emoji(char: str) -> bool:
    codepoint = ord(char)
    if codepoint in {0xFE0E, 0xFE0F}:
        return False
    if 0x1F000 <= codepoint <= 0x1FAFF:
        return True
    return 0x2600 <= codepoint <= 0x27BF and unicodedata.category(char) == "So"


def has_task_signal(text: str) -> bool:
    return bool(
        re.search(
            r"任务|目标|需求|进度|阻塞|完成|下一步|里程碑|截止|task|todo|blocked|progress",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )


def infer_task_id(event: MessageEvent, fallback: str = "") -> str | None:
    explicit = _clean(event.task_id or "", 80)
    if explicit:
        return explicit
    match = re.search(
        r"(?:任务|task)\s*(?:id|编号|号|#)?\s*[:：#-]?\s*([A-Za-z0-9][A-Za-z0-9_.-]{1,63})",
        event.text or "",
        flags=re.IGNORECASE,
    )
    if match:
        return _clean(match.group(1), 80)
    return _clean(fallback, 80) or None


def extract_task_patch(text: str) -> dict[str, Any]:
    """Extract labelled task state without inventing missing fields."""

    value = _clean(text, 1200)
    patch: dict[str, Any] = {}
    patterns = {
        "goal": r"(?:目标|任务目标|要做的是|需要完成)(?:是|为|：|:)?(?P<value>[^。！？!?，,；;\n]{1,180})",
        "scope": r"(?:范围|涉及)(?:是|为|：|:)?(?P<value>[^。！？!?，,；;\n]{1,180})",
        "requirements": r"(?:需求|要求|必须)(?:是|为|：|:)?(?P<value>[^。！？!?，,；;\n]{1,180})",
        "decisions": r"(?:决定|已确定|确定)(?:是|为|：|:)?(?P<value>[^。！？!?，,；;\n]{1,180})",
        "progress": r"(?:进度|目前|当前进展)(?:是|为|：|:)?(?P<value>[^。！？!?，,；;\n]{1,180})",
        "completed": r"(?:已完成|完成了|已经完成)(?:是|为|：|:)?(?P<value>[^。！？!?，,；;\n]{1,180})",
        "blockers": r"(?:阻塞|卡在|问题是|风险是)(?:在|是|为|：|:)?(?P<value>[^。！？!?，,；;\n]{1,180})",
        "next_action": r"(?:下一步|接下来|后续)(?:是|为|：|:)?(?P<value>[^。！？!?，,；;\n]{1,180})",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            cleaned = _clean(match.group("value"), 220)
            if cleaned:
                patch[key] = cleaned
    people = re.findall(r"@([\w\u4e00-\u9fff-]{1,40})", value)
    if people:
        patch["people"] = sorted(set(people))[:20]
    if not patch and has_task_signal(value):
        # Keep an explicit, bounded progress note when a message is task
        # related but has no labelled field.
        patch["progress"] = value[:220]
    return patch


def extract_ai_profile_patch(
    text: str,
    message_id: str | None,
    *,
    ai_aliases: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Return aggregate counters and evidence ids for a group-level profile."""

    normalized = normalize_chat_text(text, ai_aliases=ai_aliases)
    if normalized.is_system:
        return {}
    value = _clean(normalized.visible_text.replace("@AI", " "), 1000)
    if not value:
        if not normalized.sticker_count:
            return {}
        return {
            "message_count": 1,
            "char_count": 0,
            "short_message_count": 1,
            "question_count": 0,
            "exclamation_count": 0,
            "technical_count": 0,
            "humor_count": 0,
            "formal_count": 0,
            "sticker_count": 1,
            "vocabulary": {},
            "topic_counts": {},
            "evidence_message_ids": [str(message_id)] if message_id else [],
        }
    compact = re.sub(r"\s+", "", value)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,30}|[\u4e00-\u9fff]{2,8}", value)
    noise = {"ai", "away", "http", "https", "www"}
    words = Counter(token.casefold() for token in tokens if len(token) >= 2 and token.casefold() not in noise)
    technical = sum(
        1
        for marker in ("api", "代码", "部署", "接口", "数据库", "模型", "测试", "配置", "bug", "服务")
        if marker.casefold() in value.casefold()
    )
    humorous = sum(1 for marker in ("哈哈", "笑死", "😂", "🤣", "233", "狗头", "哈哈哈") if marker in value)
    formal = sum(1 for marker in ("请", "谢谢", "因此", "结论", "麻烦", "您好") if marker in value)
    topic_markers = {
        "软件开发": ("代码", "bug", "api", "接口", "数据库", "服务"),
        "模型与 AI": ("模型", "AI", "提示词", "embedding", "向量"),
        "部署与运维": ("部署", "配置", "服务器", "上线", "日志"),
        "测试与验证": ("测试", "验证", "回归", "验收"),
        "设计与内容": ("设计", "界面", "图片", "视频", "文案"),
    }
    topic_counts = {
        topic: sum(value.casefold().count(marker.casefold()) for marker in markers)
        for topic, markers in topic_markers.items()
    }
    topic_counts = {topic: count for topic, count in topic_counts.items() if count}
    return {
        "message_count": 1,
        "char_count": len(compact),
        "short_message_count": int(len(compact) <= 16),
        "question_count": value.count("?") + value.count("？"),
        "exclamation_count": value.count("!") + value.count("！"),
        "technical_count": technical,
        "humor_count": humorous,
        "formal_count": formal,
        "sticker_count": normalized.sticker_count,
        "vocabulary": dict(words.most_common(20)),
        "topic_counts": topic_counts,
        "evidence_message_ids": [str(message_id)] if message_id else [],
    }

