"""Small deterministic guard for the whale-girl persona's output.

The persona prompt does most of the work, but a model can still slip into
"作为一个 AI" phrasing, overuse emoji, or escalate a playful tease into a
serious insult.  These checks are intentionally narrow and cheap: they only
touch self-identification, a few bubble/sound gimmicks, severe profanity, the
emoji count, and repetition of the persona's mutual-teasing words.  They do not
rewrite ordinary technical language.
"""

from __future__ import annotations

from collections import deque
import random
import re
from typing import Iterable

__all__ = [
    "TONE_LEXICON",
    "ToneWordRotator",
    "contains_severe_profanity",
    "sanitize_persona_reply",
]

# 互损词按场景分组。这些只是语气方向示例，不是固定台词；提示词会原样
# 投影同一份词库，输出过滤器再用它做去重轮转，两边不会漂移。
TONE_LEXICON: dict[str, tuple[str, ...]] = {
    "insulted": ("坏蛋", "大坏蛋", "讨厌", "欺负人", "过分"),
    "teased": ("笨笨", "傻傻的", "想得美", "就你话多", "皮痒了"),
    # 「哼」不作为被夸场景的示例词：它容易被模型当成万能开场，变成每条都哼。
    "praised": ("这还用你说", "少来这套……不过还行", "也不看看我是谁", "算你有眼光"),
    "indulgent": ("笨蛋",),
}

# Only mutual-teasing words take part in the last-five-replies rotation; the
# praise scene is expressed by whole sentences and is handled by the prompt.
_ROTATING_SCENES = ("insulted", "teased", "indulgent")
_SCENE_BY_WORD = {
    word: scene for scene in _ROTATING_SCENES for word in TONE_LEXICON[scene]
}
# 宠溺场景只有一个低频词；轮转时借用最接近的"逗"场景，避免原地重复。
_FALLBACK_SCENE = {"indulgent": "teased"}
# Longest first so "大坏蛋" is not detected as "坏蛋".
_ROTATING_WORD_RE = re.compile(
    "|".join(
        re.escape(word)
        for word in sorted(_SCENE_BY_WORD, key=len, reverse=True)
    )
)
# Used only by callers that have no per-agent rotator (welcome text, profile
# synthesis); the reply path always passes its agent's ToneWordRotator.
_DEFAULT_RNG = random.Random()

# Keep the first emoji cluster (including variation selectors/ZWJ sequences)
# and drop the rest.  The persona asks for at most one, not a fixed quota.
_EMOJI_RE = re.compile(
    "(?:"
    "[\U0001F1E0-\U0001F1FF]"
    "|[\U0001F300-\U0001F5FF]"
    "|[\U0001F600-\U0001F64F]"
    "|[\U0001F680-\U0001F6FF]"
    "|[\U0001F700-\U0001F77F]"
    "|[\U0001F780-\U0001F7FF]"
    "|[\U0001F800-\U0001F8FF]"
    "|[\U0001F900-\U0001F9FF]"
    "|[\U0001FA00-\U0001FAFF]"
    "|[\u2600-\u26FF]"
    "|[\u2700-\u27BF]"
    ")"
    "(?:\uFE0F|\u200D(?:[\U0001F300-\U0001FAFF]|[\u2600-\u27BF]))*"
)

_META_DISCLOSURE_REPLACEMENTS = (
    (
        re.compile(r"我(?:被)?禁止(?:了)?[^。！？!?\n]{0,16}(?:过激侮辱|脏话|骂人)"),
        "{self}不骂人",
    ),
    (
        re.compile(
            r"我(?:不会|不能|不可以|不被允许|被禁止|被限制)"
            r"[^。！？!?\n]{0,18}(?:吐泡泡|冒泡|说脏话|骂人|过激侮辱|侮辱别人)"
        ),
        "{self}不玩这个",
    ),
    (
        re.compile(r"我(?:被)?(?:系统|人设|规则|审核|过滤)(?:要求|限制|禁止|不允许)"),
        "{self}自己",
    ),
    (
        re.compile(
            r"我的(?:系统|人设|审核|过滤)?(?:提示词|规则|设定)"
            r"|系统提示|人设文件|审核规则|过滤词表"
        ),
        "{self}的习惯",
    ),
    (
        re.compile(
            r"(?:我的|本鱼的|系统)提示词|系统指令|人设文件|规则清单|审核机制|过滤词表|"
            r"system\s*prompt|提示词(?:里|中|要求|规定|写着|说了|告诉我)",
            re.IGNORECASE,
        ),
        "规矩",
    ),
    (re.compile(r"开发者(?:告诉|要求|规定)我"), "{self}自己"),
)

_BUBBLE_PATTERNS = (
    re.compile(r"[（(]\s*吐(?:个)?泡泡\s*[)）]"),
    re.compile(r"吐(?:个)?泡泡"),
    re.compile(r"咕噜(?:咕噜)?"),
    re.compile(r"啵啵"),
    re.compile("\U0001FAE7"),  # 🫧
)

_SEVERE_INSULT_RE = re.compile(
    r"傻\s*逼|傻\s*B|煞\s*笔|沙\s*比", re.IGNORECASE
)

# 傲娇角色被夸时不能退回谦虚客套。提示词已经禁止，但人设示例和模型惯性仍会
# 偶尔把「不好意思」写出来，这里做一次确定性的兜底改写（只作用于内置大肥鱼）。
# 只在「害羞地接受夸奖」的语境替换；「不好意思，我记错了」这类道歉含义不动。
_SHY_ACCEPT_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"怪让人不好意思的?|让人不好意思的?|有点不好意思的?|都不好意思(?:了|的)?"
        ),
        "心里还挺受用的",
    ),
    (re.compile(r"害羞|羞涩|腼腆"), "嘴硬"),
)

# “哼”不能变成口头禅：连续回复里不重复，单条回复最多一个，连写的“哼哼哼”
# 合并成一个。哼唱 / 哼歌 / 哼着 这类实词不属于语气词，保持原样。
_HENG_RE = re.compile(r"(?<!哼)哼(?![唱歌曲着调首])")
_HENG_RUN_RE = re.compile(r"哼{2,}")

# ``None`` marks a slot that is picked from the insulted scene at runtime
# instead of hard-coded to one word.  ``笨蛋`` used to be that word and became
# the persona's only comeback; 坏蛋/大坏蛋/讨厌 rotate instead.
_SEVERE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (_SEVERE_INSULT_RE, None),
    (re.compile(r"(?<!妈)妈的"), "真是的"),
    (re.compile(r"操你|日你|草你"), "揍你"),
    (re.compile(r"去死|死全家"), "别闹了"),
    (re.compile(r"贱人|婊子|狗娘养"), "家伙"),
    (re.compile(r"滚你妈"), "别闹了"),
)

_SELF_AI_PATTERNS = (
    (
        re.compile(
            r"(?:我|本鱼|人家|大肥鱼我)\s*(?:只|就|可)?\s*(?:是|作为)\s*"
            r"(?:一个|一名)?\s*(?:人工智能|语言模型|大模型|机器人|助手|AI|ai)"
            r"(?:助手|机器人|语言模型|大模型)?",
            re.IGNORECASE,
        ),
        "{self}是{identity}",
    ),
    (
        re.compile(
            r"(?:作为|身为)\s*(?:一个|一名)?\s*"
            r"(?:人工智能|语言模型|大模型|机器人|助手|AI|ai)"
            r"(?:助手|机器人|语言模型|大模型)?",
            re.IGNORECASE,
        ),
        "作为{identity}",
    ),
)


# The plan's soft replacements for a filtered slur are these three words.
# The wider insulted scene is only a fallback when all three are still hot in
# the five-reply window, so the output never lands on 笨蛋 by default.
_SEVERE_SOFT_WORDS = ("坏蛋", "大坏蛋", "讨厌")


def _tone_matches(text: str) -> list[tuple[int, int, str]]:
    return [
        (match.start(), match.end(), match.group(0))
        for match in _ROTATING_WORD_RE.finditer(text)
    ]


def _unique_words(replies: Iterable[Iterable[str]]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for reply in replies:
        for word in reply:
            seen.setdefault(word, None)
    return tuple(seen)


class ToneWordRotator:
    """Keep the persona's mutual-teasing words from becoming catchphrases.

    The rotator remembers the tone words used in the last five replies.  When
    one of them shows up again it is swapped for another word from the same
    scene instead of being deleted.  The selector is shuffled with a private
    ``random.Random`` so ordinary runs vary while tests can seed it.
    """

    def __init__(self, *, history_size: int = 5, seed: int | None = None) -> None:
        self.history_size = max(1, int(history_size))
        self._window: deque[tuple[str, ...]] = deque(maxlen=self.history_size)
        # 独立记录最近两条回复是否用了语气词「哼」。它不计入互损词轮转，
        # 只用来阻止「哼」变成连续多条的固定开场。
        self._heng_window: deque[bool] = deque(maxlen=2)
        self._rng = random.Random(seed)

    def heng_used_recently(self) -> bool:
        """Whether the previous two replies used the ``哼`` interjection."""

        return any(self._heng_window)

    def note_heng(self, used: bool) -> None:
        """Record whether the reply that was just rendered kept a ``哼``."""

        self._heng_window.append(bool(used))

    def recent_words(self) -> tuple[str, ...]:
        """Words used in the last replies, oldest first, without duplicates."""

        return _unique_words(self._window)

    def surviving_words(self) -> tuple[str, ...]:
        """Words still inside the window after the next reply is appended."""

        replies = list(self._window)
        if len(replies) >= self.history_size:
            replies = replies[1:]
        return _unique_words(replies)

    @staticmethod
    def detect(text: str) -> list[str]:
        """Return the mutual-teasing words present in ``text``, in order."""

        return [word for _, _, word in _tone_matches(str(text or ""))]

    def reset(self) -> None:
        self._window.clear()
        self._heng_window.clear()

    def choose(
        self,
        scene: str,
        *,
        avoid: Iterable[str] = (),
        exclude: Iterable[str] = (),
        words: Iterable[str] | None = None,
        never: Iterable[str] = (),
    ) -> str:
        """Pick a word from ``scene`` that is not in ``avoid`` when possible."""

        pool = (
            tuple(words)
            if words is not None
            else self._pool(scene, excluded=set(exclude))
        )
        avoid_set = set(avoid)
        available = [word for word in pool if word not in avoid_set]
        if available:
            return self._rng.choice(available)
        return self._least_recent(pool, avoid_set, set(never))

    @staticmethod
    def _pool(scene: str, *, excluded: set[str]) -> tuple[str, ...]:
        words = tuple(
            word for word in TONE_LEXICON.get(scene, ()) if word not in excluded
        )
        if words:
            return words
        fallback = _FALLBACK_SCENE.get(scene, "")
        if fallback:
            return tuple(
                word for word in TONE_LEXICON[fallback] if word not in excluded
            )
        return TONE_LEXICON.get(scene, ())

    def _least_recent(
        self, pool: tuple[str, ...], avoid: set[str], never: set[str]
    ) -> str:
        # Every candidate is currently "hot".  Reusing the one that appeared
        # longest ago is the only choice that keeps the five-reply window free
        # of duplicates once this reply is appended.  Words already present in
        # the current reply are excluded outright so one reply never repeats
        # the same tease twice.
        positions: dict[str, int] = {}
        for index, reply in enumerate(self._window):
            for word in reply:
                positions[word] = index
        current = len(self._window)
        candidates = [word for word in pool if word not in never]
        if not candidates:
            candidates = list(pool)
        if not candidates:
            return ""
        self._rng.shuffle(candidates)
        return min(
            candidates,
            key=lambda word: positions.get(word, current if word in avoid else -1),
        )

    def apply(self, text: str) -> str:
        """Swap repeated tone words, then record this reply in the window."""

        value = str(text or "")
        if not value:
            self._window.append(())
            return value
        matches = _tone_matches(value)
        # Words that stay in the five-reply window once this reply is added.
        # A word used only by the reply that is about to age out may be reused
        # without ever showing up twice in the same five-reply span.
        surviving = set(self.surviving_words())
        # Natural tone words that are already in this reply must not be
        # produced again by a substitution, even when they appear later in
        # the sentence than the word being replaced.
        present = {word for _, _, word in matches}
        used_now: set[str] = set()
        replacements: list[tuple[int, int, str]] = []
        for start, end, word in matches:
            if word in surviving or word in used_now:
                replacement = self.choose(
                    _SCENE_BY_WORD[word],
                    avoid=surviving | present,
                    exclude=(word,),
                    never=present,
                )
                if replacement and replacement != word:
                    replacements.append((start, end, replacement))
                    used_now.add(replacement)
                    present.add(replacement)
                    continue
            used_now.add(word)
        for start, end, replacement in reversed(replacements):
            value = value[:start] + replacement + value[end:]
        self._window.append(tuple(self.detect(value)))
        return value


def _pick_insult_word(
    tone: ToneWordRotator | None, avoid: set[str]
) -> str:
    """Choose a non-``笨蛋`` comeback for a filtered severe insult."""

    if tone is not None:
        blocked = set(tone.surviving_words()) | avoid
        preferred = [word for word in _SEVERE_SOFT_WORDS if word not in blocked]
        if preferred:
            return tone.choose("insulted", words=preferred)
        return tone.choose("insulted", avoid=blocked, never=avoid)
    pool = [word for word in _SEVERE_SOFT_WORDS if word not in avoid]
    if not pool:
        pool = [word for word in TONE_LEXICON["insulted"] if word not in avoid]
    return _DEFAULT_RNG.choice(pool or list(_SEVERE_SOFT_WORDS))


def contains_severe_profanity(text: str) -> bool:
    return any(pattern.search(str(text or "")) for pattern, _ in _SEVERE_REPLACEMENTS)


def _limit_heng(value: str, *, allow: bool) -> str:
    """Collapse ``哼哼哼`` and stop ``哼`` from repeating across replies.

    ``allow=False`` drops the interjection outright, which is used when one of
    the previous two replies already had it; otherwise only the first one is
    kept.  A removed ``哼`` also takes one adjacent comma with it so the result
    never starts with a dangling punctuation mark.
    """

    if "哼" not in value:
        return value
    value = _HENG_RUN_RE.sub("哼", value)
    spans = [match.span() for match in _HENG_RE.finditer(value)]
    if not spans:
        return value
    remove = spans if not allow else spans[1:]
    for start, end in reversed(remove):
        if start > 0 and value[start - 1] in "，,、":
            start -= 1
        elif end < len(value) and value[end] in "，,、":
            end += 1
        value = value[:start] + value[end:]
    value = re.sub(r"[，,、]{2,}", "，", value)
    value = re.sub(r"^[，,、\s]+", "", value).strip()
    return value


def sanitize_persona_reply(
    text: str,
    *,
    max_emoji: int = 1,
    tone: ToneWordRotator | None = None,
    self_reference: str = "本鱼",
    identity: str = "鲸鱼娘",
    bubble_gags: bool = True,
) -> str:
    """Apply the persona's small, deterministic output constraints.

    ``tone`` is the per-agent voice bookkeeper.  When supplied, severe
    insults rotate through the insulted scene and mutual-teasing words used in
    the last five replies are swapped out instead of repeating.

    ``self_reference``, ``identity`` and ``bubble_gags`` are the legacy
    dafeiyu voice defaults.  A custom persona can pass its own name and set
    ``bubble_gags=False`` so the output filter does not insert whale-girl
    phrasing into an unrelated character.
    """

    value = str(text or "").strip()
    if not value:
        return value
    labels = {"self": self_reference or "我", "identity": identity or "这个角色"}
    for pattern, replacement in _META_DISCLOSURE_REPLACEMENTS:
        value = pattern.sub(replacement.format(**labels), value)
    if bubble_gags:
        for pattern in _BUBBLE_PATTERNS:
            value = pattern.sub("", value)
        # "冒个泡" is the same bubble gimmick in a different wording; keep the
        # sentence natural instead of deleting it outright.
        value = re.sub(r"冒个泡|冒泡", "说一声", value)
        # 被夸时退回「不好意思/害羞」是客服腔，不是傲娇；只改害羞语境，
        # 保留「不好意思，我记错了」这类道歉用法。
        for pattern, replacement in _SHY_ACCEPT_REPLACEMENTS:
            value = pattern.sub(replacement, value)
    used_insult: set[str] = {
        word for _, _, word in _tone_matches(value)
    }

    def soften_severe_insult(match: re.Match[str]) -> str:
        word = _pick_insult_word(tone, used_insult)
        used_insult.add(word)
        return word

    for pattern, replacement in _SEVERE_REPLACEMENTS:
        if replacement is None:
            value = pattern.sub(soften_severe_insult, value)
        else:
            value = pattern.sub(replacement, value)
    for pattern, replacement in _SELF_AI_PATTERNS:
        value = pattern.sub(replacement.format(**labels), value)

    limit = max(0, int(max_emoji))
    seen = 0

    def keep_limited(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= limit else ""

    value = _EMOJI_RE.sub(keep_limited, value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    value = value or f"{labels['self']}在。"
    if tone is not None:
        value = _limit_heng(value, allow=not tone.heng_used_recently())
        value = tone.apply(value)
        tone.note_heng(bool(_HENG_RE.search(value)))
    return value
