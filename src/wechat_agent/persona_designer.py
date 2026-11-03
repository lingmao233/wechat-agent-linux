"""Generate a persona bundle from a natural-language description.

The console's persona designer asks the configured chat model for exactly the
small file set the runtime understands::

    SKILL.md
    references/speech-style.md   (optional)
    references/image-style.md    (optional)

The generation prompt is intentionally independent from the active persona.
A fresh custom bundle must not silently inherit the bundled dafeiyu voice or
appearance; the caller only gets files that were explicitly requested.
"""

from __future__ import annotations

import json
import re
from typing import Any

SKILL_PATH = "SKILL.md"
SPEECH_PATH = "references/speech-style.md"
IMAGE_PATH = "references/image-style.md"

MAX_DESCRIPTION_CHARS = 4_000
MAX_NAME_CHARS = 64
MAX_FILE_CHARS = 16_000
MAX_TOTAL_CHARS = 32_000

_CANONICAL_PATHS = {
    "skill.md": SKILL_PATH,
    "skill.markdown": SKILL_PATH,
    "speech-style.md": SPEECH_PATH,
    "references/speech-style.md": SPEECH_PATH,
    "image-style.md": IMAGE_PATH,
    "references/image-style.md": IMAGE_PATH,
}

_DESIGN_SYSTEM = """你是 DSH Attune 微信插件的人设设计师。根据用户的自然语言描述，生成一个可直接上传的人设包。

只输出一个 JSON 对象，不要 Markdown 代码围栏，不要解释，不要输出 JSON 以外的文字。
JSON 格式必须是：
{
  "name": "角色名，2-16 个字符，适合作为人设目录名",
  "slug": "可选目录名，中文、字母、数字、点、下划线或短横线",
  "files": [
    {"path": "SKILL.md", "content": "Markdown 正文"}
  ]
}

文件规则：
1. 必须包含 path 为 SKILL.md 的文件。它是角色主设定：身份、背景、性格、关系、行为边界、回复原则。用第二人称“你是……”写，直接可执行，不写固定画风。
2. 仅当“需要说话风格”为 true 时，额外包含一条 path 为 references/speech-style.md 的文件。它只写说话方式：语气、称呼、句子长度、口头禅、emoji/颜文字、禁止项；不要写外貌。
3. 仅当“需要生图外观”为 true 时，额外包含一条 path 为 references/image-style.md 的文件。它只写稳定、可复用的角色外观和画风：主体、年龄感、发型、服装、配色、画风、背景/构图要求，用于生成单幅角色表情图；不要写具体动作、剧情或当次对话。
4. 某个文件不需要时，直接省略，不要生成空文件，不要写“无”“未提供”“待补充”等占位内容。系统不会注入缺失的风格文件，也不会用任何内置角色代替。
5. 不要沿用或复制任何已有内置角色的人设；如果用户没有明确指定，创建一个与当前插件无关的新角色。
6. content 是 Markdown 字符串；JSON 中的换行写成 \\n，确保整个输出是合法 JSON。
7. 不要输出 SKILL.md、references/speech-style.md、references/image-style.md 以外的文件。
"""


def _clean_text(value: object, label: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f"{label}必须是文本")
    if required and not text:
        raise ValueError(f"请填写{label}")
    if len(text) > limit:
        raise ValueError(f"{label}最多 {limit} 个字符")
    return text


def _as_bool(value: object, default: bool, label: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{label}必须是布尔值")


def _slug(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", str(value or ""))
    text = text.strip("-._")[:32]
    return text or "persona"


def _parse_request(payload: object) -> tuple[str, str, bool, bool]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    description = _clean_text(
        payload.get("description"),
        "角色描述",
        MAX_DESCRIPTION_CHARS,
        required=True,
    )
    name = _clean_text(payload.get("name"), "人设名", MAX_NAME_CHARS).strip()
    include_speech = _as_bool(payload.get("include_speech"), True, "需要说话风格")
    include_image = _as_bool(payload.get("include_image"), True, "需要生图外观")
    return description, name, include_speech, include_image


def build_persona_design_prompt(
    description: str,
    *,
    name: str = "",
    include_speech: bool = True,
    include_image: bool = True,
) -> tuple[str, str]:
    """Return the system and user prompts for one persona-design request."""

    user = "\n".join(
        [
            "用户想设计的角色：",
            description,
            "",
            f"希望的人设名（可选）：{name or '（未指定，请你起名）'}",
            f"需要说话风格（references/speech-style.md）：{'是' if include_speech else '否'}",
            f"需要生图外观（references/image-style.md）：{'是' if include_image else '否'}",
            "",
            "请严格按系统规则输出 JSON。",
        ]
    )
    return _DESIGN_SYSTEM, user


def _extract_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("文字模型没有返回内容")
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回合法的人设 JSON")
    try:
        value = json.loads(text[start : end + 1])
    except (TypeError, ValueError) as exc:
        raise ValueError("模型返回的人设 JSON 无法解析，请重试") from exc
    if not isinstance(value, dict):
        raise ValueError("模型返回的人设 JSON 必须是对象")
    return value


def _raw_files(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [
            {"path": path, "content": content}
            for path, content in value.items()
        ]
    if not isinstance(value, list):
        raise ValueError("人设 JSON 缺少 files 列表")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("files 里的每一项都必须是对象")
        result.append(item)
    return result


def parse_persona_design(
    raw: str,
    *,
    fallback_name: str = "",
    include_speech: bool = True,
    include_image: bool = True,
) -> dict[str, object]:
    """Validate model output and return the canonical bundle.

    Unrequested/unknown files are dropped with a warning. Missing requested
    files are errors so the UI can ask the model to retry.
    """

    parsed = _extract_object(raw)
    raw_name = _clean_text(parsed.get("name"), "模型生成的角色名", 40)
    suggested_slug = _clean_text(parsed.get("slug"), "模型生成的目录名", 64)
    name = raw_name or fallback_name.strip() or "新角色"

    found: dict[str, str] = {}
    warnings: list[str] = []
    for item in _raw_files(parsed.get("files")):
        path_value = item.get("path")
        path = str(path_value or "").strip().replace("\\", "/").removeprefix("./")
        canonical = _CANONICAL_PATHS.get(path.casefold())
        if canonical is None:
            warnings.append(f"已忽略未知文件：{path or '（空路径）'}")
            continue
        if canonical == SPEECH_PATH and not include_speech:
            warnings.append("未勾选说话风格，已忽略模型生成的 speech-style.md")
            continue
        if canonical == IMAGE_PATH and not include_image:
            warnings.append("未勾选生图外观，已忽略模型生成的 image-style.md")
            continue
        content_value = item.get("content")
        if not isinstance(content_value, str) or not content_value.strip():
            raise ValueError(f"文件 {canonical} 的内容为空")
        content = content_value.strip()
        if "\x00" in content:
            raise ValueError(f"文件 {canonical} 包含二进制内容")
        if len(content) > MAX_FILE_CHARS:
            raise ValueError(f"文件 {canonical} 内容过长，请重试")
        if canonical in found:
            raise ValueError(f"模型重复生成了文件：{canonical}")
        found[canonical] = content

    if SKILL_PATH not in found:
        raise ValueError("模型没有生成必需的 SKILL.md，请重试")
    if include_speech and SPEECH_PATH not in found:
        raise ValueError("模型没有生成 references/speech-style.md，请重试")
    if include_image and IMAGE_PATH not in found:
        raise ValueError("模型没有生成 references/image-style.md，请重试")

    ordered_paths = [SKILL_PATH]
    if include_speech:
        ordered_paths.append(SPEECH_PATH)
    if include_image:
        ordered_paths.append(IMAGE_PATH)
    files = [{"path": path, "content": found[path]} for path in ordered_paths]
    total = sum(len(item["content"].encode("utf-8")) for item in files)
    if total > MAX_TOTAL_CHARS:
        raise ValueError("生成的人设总长度超过上限，请缩短描述后重试")

    return {
        "ok": True,
        "name": name,
        "slug": _slug(suggested_slug or name),
        "files": files,
        "warnings": warnings,
        "format": {
            "main": SKILL_PATH,
            "speech": SPEECH_PATH if include_speech else None,
            "image": IMAGE_PATH if include_image else None,
        },
    }


def generate_persona_design(llm: Any, payload: object) -> dict[str, object]:
    """Ask the configured chat model for a validated persona bundle."""

    description, name, include_speech, include_image = _parse_request(payload)
    system, user = build_persona_design_prompt(
        description,
        name=name,
        include_speech=include_speech,
        include_image=include_image,
    )
    raw = llm.complete(system, user, temperature=0.65, max_tokens=6_000)
    return parse_persona_design(
        raw,
        fallback_name=name,
        include_speech=include_speech,
        include_image=include_image,
    )
