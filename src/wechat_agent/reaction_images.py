"""Reusable, single-meaning pictures planned by the conversation model."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import unicodedata
from uuid import uuid4

from PIL import Image

from .config import Settings
from .models import MessageEvent


log = logging.getLogger(__name__)

# These describe the visual reference, not any text printed in the attachments.
# The model supplies the actual emotion, pose, scene and optional short caption.
CHARACTER_STYLE = (
    "主体必须且只能是同一位蓝色鲸鱼娘大肥鱼：可爱的二至三头身Q版女孩，"
    "蓬松的深蓝到亮蓝渐变长发、大大的蓝眼睛、鲸鱼鳍状耳朵和蓝色鲸鱼尾巴，"
    "深蓝白色荷叶边女仆裙、白围裙和蓝色蝴蝶结，海洋与鲸鱼小配饰。"
    "柔软圆润的可爱日系表情包插画，干净线条、柔和上色、清晰夸张的情绪，"
    "简洁浅色背景或透明背景。是一位鲸鱼娘，不是单独的鲸鱼动物。"
    "只画一张独立的单幅表情图片，只表达一种意思；"
    "禁止九宫格、拼图、多格漫画、动作图集、重复角色和多种表情合集。"
)
_STYLE_ID = hashlib.sha256(CHARACTER_STYLE.encode()).hexdigest()[:16]
_ID = re.compile(r"[0-9a-f]{64}\Z")
_ADDRESSEE = re.compile(r"用户|客户|顾客|群主|管理员|亲爱的|老铁|家人们|wxid_|@|\busers?\b|\bcustomers?\b", re.I)


def _clean(value: str) -> str:
    return " ".join("".join(c for c in value if unicodedata.category(c) not in {"Cf", "Cc"}).split())


@dataclass(frozen=True, slots=True)
class ReactionImagePlan:
    emotion: str = ""
    action: str = ""
    prompt: str = ""
    caption: str = ""
    reuse_id: str = ""
    # A cache asset is reusable only inside a matching conversation scene.
    scene: str = ""
    topic_tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> ReactionImagePlan | None:
        if not isinstance(value, dict):
            return None
        limits = {"emotion": 40, "action": 100, "prompt": 1800, "caption": 20, "reuse_id": 64, "scene": 160}
        fields = {}
        for name, limit in limits.items():
            raw = value.get(name, "")
            if not isinstance(raw, str) or len(raw) > limit:
                return None
            fields[name] = _clean(raw)
        raw_tags = value.get("topic_tags", ())
        if isinstance(raw_tags, str):
            raw_tags = [item.strip() for item in raw_tags.split(",") if item.strip()]
        if not isinstance(raw_tags, (list, tuple)) or len(raw_tags) > 8:
            return None
        tags = []
        for raw in raw_tags:
            if not isinstance(raw, str) or len(raw) > 32:
                return None
            cleaned = _clean(raw)
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
        fields["topic_tags"] = tuple(tags)
        plan = cls(**fields)
        if plan.reuse_id:
            return plan if _ID.fullmatch(plan.reuse_id) else None
        return plan if plan.emotion and plan.action and plan.prompt else None


@dataclass(frozen=True, slots=True)
class ReactionImage:
    path: str
    image_id: str
    emotion: str
    caption: str
    reused: bool
    scene: str = ""
    topic_tags: tuple[str, ...] = ()


class ReactionImages:
    """Keep character-only assets on disk; never cache conversation identities."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, settings: Settings, image_client):
        self.settings, self.image_client = settings, image_client

    @property
    def root(self) -> Path:
        return Path(self.settings.reaction_image_cache_dir).resolve() / _STYLE_ID

    def _lock(self) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(str(self.root), threading.Lock())

    def should_offer(self, event: MessageEvent) -> bool:
        if not self.settings.wechat_allow_send or self.settings.stop_file.exists():
            return False
        if not self.settings.reaction_images_enabled or not self.settings.image_model_enabled:
            return False
        probability = self.settings.reaction_image_probability
        if probability <= 0:
            return False
        available = bool(
            self.settings.image_model_base_url.strip()
            and self.settings.image_model_api_key.strip()
            and self.settings.image_model.strip()
        )
        if not available and not self.catalog():
            return False
        seed = json.dumps(["reaction", event.account_id, event.group_id, event.member_id,
                           event.external_message_id, event.proactive_window_id, event.text], ensure_ascii=False)
        roll = int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16) / 2**64
        return roll < probability

    @staticmethod
    def _generic(plan: ReactionImagePlan, names: list[str]) -> bool:
        text = unicodedata.normalize("NFKC", " ".join((plan.emotion, plan.action, plan.prompt, plan.caption, plan.scene, *plan.topic_tags)))
        if _ADDRESSEE.search(text):
            return False
        # Prompts and captions are reusable visual descriptions, not quotations
        # of somebody's name or the current group. Short aliases can be normal
        # words (e.g. 鱼); the fixed character description stays outside this test.
        return not any(len(name.strip()) >= 2 and unicodedata.normalize("NFKC", _clean(name)).casefold() in text.casefold()
                       for name in names if name)

    def _entry(self, image_id: str) -> dict | None:
        if not _ID.fullmatch(image_id):
            return None
        try:
            item = json.loads((self.root / f"{image_id}.json").read_text(encoding="utf-8"))
            if not isinstance(item, dict) or item.get("style_id") != _STYLE_ID or item.get("image_id") != image_id:
                return None
            plan = ReactionImagePlan.from_dict(item)
            # Legacy entries without an explicit scene are intentionally
            # invisible to both catalog and reuse; otherwise a stale food
            # reaction could be selected for a new unrelated topic.
            if not plan or not plan.scene or not self._generic(plan, []):
                return None
            path = self.root / f"{image_id}.png"
            if not path.is_file() or path.stat().st_size > self.settings.image_upload_max_bytes:
                return None
            if hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
                return None
            with Image.open(path) as picture:
                picture.verify()
            return item
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    def catalog(self, limit: int = 16) -> list[dict]:
        if not self.root.is_dir():
            return []
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for file in files:
            item = self._entry(file.stem)
            if item:
                # Expose the bounded scene metadata to the conversation model
                # so it can reject an otherwise tempting but unrelated image
                # before sending a reuse_id.  The sidecar remains the source
                # of truth and is checked again in ``prepare``.
                result.append({key: item[key] for key in
                               ("image_id", "emotion", "action", "caption", "scene", "topic_tags")})
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def prompt(plan: ReactionImagePlan) -> str:
        caption = (
            "画面中的唯一文字为这句简短文案：" + json.dumps(plan.caption, ensure_ascii=False) + "。逐字准确绘制，不添加其他文字。"
            if plan.caption else "画面不出现任何文字、字母、字幕或对话文字。"
        )
        return (CHARACTER_STYLE + "\n本张情绪：" + plan.emotion + "。动作与含义：" + plan.action
                + "。\n由对话模型设计的画面：" + plan.prompt + "\n" + caption
                + "不出现用户等身份称呼，不出现昵称、姓名、账号、@、落款、水印和收件对象。"
                + "只用通用情绪、动作和道具表达，适合下次复用；保持单幅、单一意思。")

    def prepare(self, plan: ReactionImagePlan, *, names: list[str] | None = None, scene_hint: str = "") -> ReactionImage | None:
        names = names or []
        plan = ReactionImagePlan.from_dict(asdict(plan))
        if not plan:
            return None
        if not self._generic(plan, names):
            log.info("reaction image declined: contains an addressee")
            return None
        with self._lock():
            if plan.reuse_id:
                item = self._entry(plan.reuse_id)
                cached_plan = ReactionImagePlan.from_dict(item)
                if not item or not cached_plan or not self._generic(cached_plan, names):
                    return None
                if not self._scene_compatible(plan, cached_plan):
                    log.info("reaction image reuse rejected: scene mismatch requested=%s cached=%s", plan.scene, cached_plan.scene)
                    return None
                return self._result(item, reused=True)
            # Meaning, action and the exact printed caption determine reuse,
            # rather than a varying prose prompt or a friend's identity.
            scene = plan.scene or _clean(f"{plan.emotion} {plan.action}")
            if not scene:
                return None
            if not plan.scene:
                plan = ReactionImagePlan(plan.emotion, plan.action, plan.prompt, plan.caption, plan.reuse_id, scene, plan.topic_tags)
            key = json.dumps([_STYLE_ID, plan.emotion.casefold(), plan.action.casefold(), plan.caption,
                              plan.scene.casefold(), tuple(tag.casefold() for tag in plan.topic_tags)], ensure_ascii=False)
            image_id = hashlib.sha256(key.encode()).hexdigest()
            item = self._entry(image_id)
            if item:
                return self._result(item, reused=True)
            generated = None
            temporary = []
            try:
                generated = self.image_client.generate(self.prompt(plan))
                if not generated.local_paths:
                    return None
                source = Path(generated.local_paths[0])
                if source.stat().st_size > self.settings.image_upload_max_bytes:
                    raise ValueError("reaction_image_too_large")
                self.root.mkdir(parents=True, exist_ok=True)
                path = self.root / f"{image_id}.png"
                temp_image = self.root / f".{uuid4().hex}.tmp"
                temporary.append(temp_image)
                with Image.open(source) as picture:
                    picture.convert("RGBA").save(temp_image, format="PNG")
                if temp_image.stat().st_size > self.settings.image_upload_max_bytes:
                    raise ValueError("reaction_image_too_large")
                item = {"image_id": image_id, "style_id": _STYLE_ID, "emotion": plan.emotion,
                        "action": plan.action, "prompt": plan.prompt, "caption": plan.caption,
                        "scene": plan.scene, "topic_tags": list(plan.topic_tags),
                        "sha256": hashlib.sha256(temp_image.read_bytes()).hexdigest()}
                temp_meta = self.root / f".{uuid4().hex}.tmp"
                temporary.append(temp_meta)
                temp_meta.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
                os.replace(temp_image, path)
                os.replace(temp_meta, self.root / f"{image_id}.json")
                return self._result(item, reused=False)
            finally:
                for path in temporary:
                    path.unlink(missing_ok=True)
                if generated and generated.local_paths:
                    self.image_client.cleanup_local(generated.local_paths)

    def _result(self, item: dict, *, reused: bool) -> ReactionImage:
        return ReactionImage(str(self.root / f"{item['image_id']}.png"), item["image_id"],
                             item["emotion"], item["caption"], reused,
                             str(item.get("scene") or ""), tuple(item.get("topic_tags") or ()))

    @staticmethod
    def _scene_compatible(requested: ReactionImagePlan, cached: ReactionImagePlan) -> bool:
        """Require an explicit topic overlap before honoring ``reuse_id``."""

        # A reuse id without a scene is ambiguous.  Reject it even if the
        # image exists; only a fresh model plan that describes the current
        # topic may reuse an asset.
        if not requested.scene or not cached.scene:
            return False
        requested_tags = {tag.casefold() for tag in requested.topic_tags if tag}
        cached_tags = {tag.casefold() for tag in cached.topic_tags if tag}
        if requested_tags and cached_tags:
            return bool(requested_tags & cached_tags)
        # Tags are optional for old generated assets, so use conservative
        # lexical overlap as a fallback.  Remove generic conversational words
        # and require two useful CJK bigrams (or one non-CJK token) to avoid
        # carrying a food-choice picture into an unrelated drink discussion.
        stop = {"今天", "昨天", "晚上", "现在", "刚刚", "这个", "那个", "关于", "讨论", "话题", "聊天", "一下", "然后", "还是", "就是"}

        def terms(value: str) -> set[str]:
            value = value.casefold()
            result = set(re.findall(r"[a-z0-9_]{3,}", value))
            chars = "".join(re.findall(r"[\u4e00-\u9fff]", value))
            result.update(chars[index:index + 2] for index in range(len(chars) - 1))
            return {item for item in result if item not in stop}

        left, right = terms(requested.scene), terms(cached.scene)
        overlap = left & right
        return len(overlap) >= 2 or bool(overlap and any(len(item) >= 3 and item.isascii() for item in overlap))
