"""Per-reply speaking style: the chat model picks a label, text rules fill gaps.

The configured ``TTS_RATE`` / ``TTS_INSTRUCTION`` stay the baseline.  A style
only scales the rate and appends one tone clause, so every provider keeps its
own persona voice while the delivery still follows the sentence's mood.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True, slots=True)
class VoiceStyle:
    label: str
    rate: float
    instruction: str


STYLES: dict[str, VoiceStyle] = {
    "平静": VoiceStyle("平静", 1.0, "语气自然平稳，句尾收住，不用刻意上扬。"),
    "开心": VoiceStyle("开心", 1.06, "语气轻快上扬，尾音带笑，节奏稍快。"),
    "撒娇": VoiceStyle("撒娇", 0.95, "声音放软带一点鼻音，尾音拖长，像在撒娇。"),
    "傲娇": VoiceStyle("傲娇", 1.03, "嘴硬心虚，重音放前半句，尾音微微上扬。"),
    "委屈": VoiceStyle("委屈", 0.88, "放慢，带一点气声，句尾下沉，像在忍着情绪。"),
    "生气": VoiceStyle("生气", 1.1, "咬字加重、句子短促，气息冲一点，但别喊。"),
    "吐槽": VoiceStyle("吐槽", 1.05, "语速偏快、语气平，尾音带一点笑意和无奈。"),
}
LABELS: tuple[str, ...] = tuple(STYLES)

_ANGRY = re.compile(r"滚|烦死|闭嘴|可恶|气死|讨厌|别烦|少来|再敢")
_SAD = re.compile(r"呜|难过|委屈|想哭|算了|对不起|没关系|难受")
_COY = re.compile(r"哼|才不|不要啦|人家|嘛|啦")
_LAUGH = re.compile(r"哈哈|嘿嘿|笑死|233|噗")
_SNARK = re.compile(r"就这|无语|离谱|服了|行吧|随便你")


def normalize(value: str) -> VoiceStyle | None:
    """Return the style for a model label, tolerating decoration around it."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().strip("「」\"'。 ")
    if not text:
        return None
    if text in STYLES:
        return STYLES[text]
    for label, style in STYLES.items():
        if label in text:
            return style
    return None


def infer(text: str) -> VoiceStyle:
    """Rule fallback when the model omits ``voice_style``."""

    value = unicodedata.normalize("NFKC", str(text or ""))
    exclaims = value.count("!") + value.count("！")
    if _ANGRY.search(value) and exclaims:
        return STYLES["生气"]
    if "……" in value or value.endswith("...") or _SAD.search(value):
        return STYLES["委屈"]
    if _COY.search(value):
        return STYLES["傲娇"]
    if _LAUGH.search(value) or exclaims >= 2:
        return STYLES["开心"]
    if _SNARK.search(value):
        return STYLES["吐槽"]
    if value.count("？") + value.count("?") >= 2:
        return STYLES["吐槽"]
    return STYLES["平静"]


def resolve(label: str, text: str) -> VoiceStyle:
    return normalize(label) or infer(text)


def merge_instruction(base: str, style: VoiceStyle) -> str:
    base = " ".join(str(base or "").split()).strip("；;。 ")
    clause = "这一次：" + style.instruction
    return (base + "；" + clause) if base else clause


def scaled_rate(base: float, style: VoiceStyle) -> float:
    try:
        value = float(base) * style.rate
    except (TypeError, ValueError):
        value = style.rate
    return round(min(2.0, max(0.5, value)), 3)


__all__ = ["VoiceStyle", "STYLES", "LABELS", "normalize", "infer", "resolve",
           "merge_instruction", "scaled_rate"]
