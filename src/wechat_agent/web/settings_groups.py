"""Settings visibility and compact page groupings for the console."""

from __future__ import annotations

VISIBLE_SETTINGS: dict[str, tuple[str, ...]] = {
    # The text model is the first thing an operator configures, so its group
    # leads the "常用" section (and stays linked from "模型与服务").
    "模型": ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"),
    "微信": (
        "WECHAT_GROUP_NAMES",
        "WECHAT_AI_ALIASES",
        "WECHAT_KEYWORDS",
        "WECHAT_ALLOW_SEND",
        "WECHAT_PRIVATE_REPLY_ENABLED",
        "WECHAT_PRIVATE_CHAT_NAMES",
    ),
    "回复偏好": (
        "WECHAT_PROACTIVE_REPLY",
        "WECHAT_PROACTIVE_PROBABILITY",
        "REACTION_IMAGES_ENABLED",
        "REACTION_IMAGE_PROBABILITY",
        "VOICE_REPLY_ENABLED",
        "VOICE_REPLY_PROBABILITY",
    ),
    "记忆": ("EMBED_BASE", "EMBED_MODEL"),
    "人设": ("PERSONA_NAME", "PERSONA_FILE"),
    "语音": ("VOICE_ENABLED", "TTS_VOICE"),
    "绘图 / OSS": ("IMAGE_MODEL_ENABLED", "IMAGE_MODEL"),
    "平台": ("PLATFORM_BROWSER_ENABLED",),
    "训练": ("TRAINING_EXPORT_ENABLED",),
}

SETTING_SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("general", "常用", ("模型", "微信", "回复偏好")),
    ("services", "模型与服务", ("模型", "记忆", "语音", "绘图 / OSS")),
    ("workspace", "角色与数据", ("人设", "平台", "训练")),
)

VISIBLE_GROUP_ORDER: tuple[str, ...] = tuple(VISIBLE_SETTINGS)
VISIBLE_ENV_TO_GROUP: dict[str, str] = {
    env: group for group, envs in VISIBLE_SETTINGS.items() for env in envs
}


def mark_schema(fields: list[dict[str, object]]) -> list[dict[str, object]]:
    for field in fields:
        env = str(field.get("env") or "")
        group = VISIBLE_ENV_TO_GROUP.get(env)
        field["visible"] = group is not None
        field["advanced"] = group is None and not field.get("hidden")
        field["group"] = group or str(field.get("category") or "其他")
    return fields


def visible_count(fields: list[dict[str, object]]) -> int:
    return sum(1 for field in fields if field.get("visible"))
