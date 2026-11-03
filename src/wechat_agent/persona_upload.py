"""Validate, stage and install operator-uploaded persona bundles.

The console lets an operator drop a Markdown persona (usually a whole folder
with a ``references/`` subdirectory) onto the settings page.  This module owns
every rule for that upload: name and path validation, size limits, the staging
directory and the merge into ``personas/<name>/``.  The HTTP handler in
``wechat_agent.desktop`` stays a thin adapter.

Nothing here talks to WeChat or the network.  ``apply=True`` only asks the
already existing ``DesktopController.save_config`` to write ``PERSONA_NAME`` /
``PERSONA_FILE`` to the local ``.env`` and reload the reader, exactly like the
settings form does.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .config import Settings

#: One upload may contain at most this many files.
MAX_FILES = 200
#: Decoded UTF-8 bytes accepted per upload (5 MB).
MAX_TOTAL_BYTES = 5 * 1024 * 1024
#: Maximum number of path components (directories + file name).
MAX_DEPTH = 6
#: Persona directory name length and allowed characters.
MAX_NAME_LENGTH = 64
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._\-\u4e00-\u9fff]{1,64}$")
#: Only plain text files may enter a persona folder.
TEXT_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".markdown",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".csv",
)
#: Preferred main file names, checked in order.
MAIN_FILE_CANDIDATES: tuple[str, ...] = ("SKILL.md",)
#: Loose style files uploaded at the bundle root are moved into the canonical
#: directory the runtime reads.  Missing files are never created: no style
#: file means no injected voice/appearance (never a bundled fallback).
STYLE_FILE_PATHS: dict[str, str] = {
    "speech-style.md": "references/speech-style.md",
    "image-style.md": "references/image-style.md",
}
DEFAULT_PERSONA_ROOT = Path("personas")
#: Files listed per persona by the catalog endpoint (keeps JSON bounded).
MAX_LISTED_FILES = 500

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_INVALID_NAME_CHARS = frozenset('<>:"|?*')


def _base_dir(base_dir: Path | None = None) -> Path:
    """Directory relative persona paths are resolved against."""

    if base_dir is not None:
        return Path(base_dir).expanduser().resolve()
    return Path.cwd().resolve()


def _resolve_relative(path: Path, base_dir: Path | None) -> Path:
    """Resolve a relative path against the working directory, then the repo."""

    if path.is_absolute():
        return path.resolve()
    base = _base_dir(base_dir)
    candidate = (base / path).resolve()
    if base_dir is None and not candidate.exists():
        # Mirrors ``wechat_agent.persona._resolve_path``: the console may run
        # from another directory but still owns the checkout's personas next
        # to ``src/``.
        repository_candidate = (_REPO_ROOT / path).resolve()
        if repository_candidate.exists():
            return repository_candidate
    return candidate


def _is_weak_root(path: Path) -> bool:
    """True when the derived folder is not a usable ``personas`` parent."""

    if path == Path(".") or path.name in {"", ".", ".."}:
        return True
    return path.parent == path  # filesystem root


def persona_root(settings: Settings, *, base_dir: Path | None = None) -> Path:
    """Derive the personas directory from ``settings.persona_file``.

    A configured ``.../personas/dafeiyu/SKILL.md`` yields ``.../personas``;
    ``SKILL.md`` or a malformed path falls back to ``personas/``.
    """

    configured = Path(str(settings.persona_file)).expanduser()
    root = configured.parent.parent
    if _is_weak_root(root):
        fallback = DEFAULT_PERSONA_ROOT
        if configured.is_absolute() and configured.parent.name:
            fallback = configured.parent / DEFAULT_PERSONA_ROOT
        root = fallback
    return _resolve_relative(root, base_dir)


def _display_path(path: Path, base: Path) -> str:
    """Render a path relative to the console checkout when possible."""

    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = base / expanded
    try:
        resolved = expanded.resolve()
    except OSError:
        resolved = expanded
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return str(resolved)


def _sort_key(path: str) -> tuple[str, str]:
    return (path.lower(), path)


def _pick_main_file(paths: list[str]) -> str:
    """Choose the persona prompt file: SKILL.md, then the first Markdown."""

    if not paths:
        return ""
    for candidate in MAIN_FILE_CANDIDATES:
        if candidate in paths:
            return candidate
    top_markdown = [
        path
        for path in paths
        if "/" not in path and PurePosixPath(path).suffix.lower() in {".md", ".markdown"}
    ]
    if top_markdown:
        return sorted(top_markdown, key=_sort_key)[0]
    return sorted(paths, key=_sort_key)[0]


def _updated_at(directory: Path) -> str:
    latest = 0.0
    try:
        latest = directory.stat().st_mtime
    except OSError:
        pass
    try:
        children = directory.rglob("*")
    except OSError:
        children = ()
    for child in children:
        try:
            if child.is_file():
                latest = max(latest, child.stat().st_mtime)
        except OSError:
            continue
    if not latest:
        latest = datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()


def _describe_persona(directory: Path) -> tuple[list[dict[str, object]], bool]:
    files: list[Path] = []
    truncated = False
    try:
        entries = sorted(
            (entry for entry in directory.rglob("*") if entry.is_file()),
            key=lambda entry: _sort_key(entry.relative_to(directory).as_posix()),
        )
    except OSError:
        entries = []
    for entry in entries:
        files.append(entry)
        if len(files) >= MAX_LISTED_FILES:
            truncated = True
            break
    relative_paths = [entry.relative_to(directory).as_posix() for entry in files]
    main_file = _pick_main_file(relative_paths)
    described: list[dict[str, object]] = []
    for entry, relative in zip(files, relative_paths):
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        described.append({"path": relative, "size": size, "main": relative == main_file})
    return described, truncated


def list_personas(
    settings: Settings,
    *,
    root: Path | None = None,
    base_dir: Path | None = None,
) -> dict[str, object]:
    """Return the persona catalog backing ``GET /api/personas``."""

    base = _base_dir(base_dir)
    personas_dir = _resolve_relative(root, base_dir) if root is not None else persona_root(settings, base_dir=base_dir)
    current = {
        "name": settings.persona_name,
        "file": _display_path(Path(str(settings.persona_file)), base),
        "enabled": bool(settings.persona_enabled),
    }
    personas: list[dict[str, object]] = []
    truncated = False
    if personas_dir.is_dir():
        try:
            children = sorted(personas_dir.iterdir(), key=lambda entry: _sort_key(entry.name))
        except OSError:
            children = []
        for child in children:
            if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
                continue
            files, child_truncated = _describe_persona(child)
            truncated = truncated or child_truncated
            main_file = next((str(item["path"]) for item in files if item["main"]), "")
            personas.append(
                {
                    "name": child.name,
                    "path": _display_path(child, base),
                    "files": files,
                    "mainFile": main_file,
                    "updatedAt": _updated_at(child),
                }
            )
    payload: dict[str, object] = {
        "root": _display_path(personas_dir, base),
        "current": current,
        "personas": personas,
    }
    if truncated:
        payload["truncated"] = True
    return payload


def _validate_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("请填写人设名称")
    name = value.strip()
    if not name:
        raise ValueError("请填写人设名称")
    if name in {".", ".."} or name.startswith("."):
        raise ValueError("人设名称不能以点开头，也不能是 . 或 ..")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"人设名称最多 {MAX_NAME_LENGTH} 个字符")
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("人设名称只能包含中文、字母、数字、点、下划线和短横线（1-64 个字符）")
    if name.upper().split(".")[0] in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"人设名称「{name}」与系统保留名冲突，请换一个名称")
    return name


def _validate_part(part: str, whole_path: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in part):
        raise ValueError(f"文件路径包含控制字符：{whole_path}")
    if any(character in _INVALID_NAME_CHARS for character in part):
        raise ValueError(f"文件路径包含非法字符：{whole_path}")
    if part != part.rstrip(" ."):
        raise ValueError(f"文件或文件夹名称不能以空格或点结尾：{whole_path}")


def _normalize_file_path(value: object, index: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"第 {index} 个文件缺少文件路径")
    path = value.strip().replace("\\", "/")
    if not path:
        raise ValueError(f"第 {index} 个文件路径为空")
    if "\x00" in path:
        raise ValueError(f"文件路径不能包含空字符：{path}")
    if path.startswith("/"):
        raise ValueError(f"文件路径必须是相对路径：{path}")
    if re.match(r"^[A-Za-z]:", path):
        raise ValueError(f"文件路径不能包含盘符：{path}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"文件路径不能包含上级目录（..）或空目录：{path}")
    if len(parts) > MAX_DEPTH:
        raise ValueError(f"文件夹层级过深（最多 {MAX_DEPTH} 层）：{path}")
    for part in parts:
        _validate_part(part, path)
    filename = parts[-1]
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        raise ValueError(
            f"不支持的文件类型：{filename}；只接受文本文件（{'、'.join(TEXT_EXTENSIONS)}）"
        )
    return "/".join(parts)


def _validate_files(value: object) -> list[tuple[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("请至少上传一个文件")
    if len(value) > MAX_FILES:
        raise ValueError(f"一次最多上传 {MAX_FILES} 个文件，当前 {len(value)} 个")
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个文件格式不正确")
        path = _normalize_file_path(item.get("path"), index)
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"文件「{path}」的内容必须是 UTF-8 文本")
        if "\x00" in content:
            raise ValueError(f"文件「{path}」包含二进制内容，无法保存")
        encoded_length = len(content.encode("utf-8"))
        total_bytes += encoded_length
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError(
                f"上传内容超过大小上限（最多 {MAX_TOTAL_BYTES // (1024 * 1024)} MB）"
            )
        if path in seen:
            raise ValueError(f"文件路径重复：{path}")
        seen.add(path)
        normalized.append((path, content))
    return normalized


def _canonicalize_style_paths(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Move loose root-level style files into ``references/``.

    The runtime only looks at ``references/speech-style.md`` and
    ``references/image-style.md``.  Accepting a loose ``image-style.md`` at
    the bundle root makes the existing upload UI forgiving without inventing
    a style when the user did not provide one.
    """

    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, content in files:
        parts = path.split("/")
        canonical = path
        if len(parts) == 1 and parts[0].casefold() in STYLE_FILE_PATHS:
            canonical = STYLE_FILE_PATHS[parts[0].casefold()]
        elif (
            len(parts) == 2
            and parts[0].casefold() == "references"
            and parts[1].casefold() in STYLE_FILE_PATHS
        ):
            canonical = STYLE_FILE_PATHS[parts[1].casefold()]
        if canonical in seen:
            raise ValueError(f"风格文件重复：{canonical}")
        seen.add(canonical)
        normalized.append((canonical, content))
    return normalized


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
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    raise ValueError(f"参数 {label} 必须是布尔值")


def _merge_tree(source: Path, target: Path) -> None:
    """Move every staged file into an existing persona directory."""

    for item in source.rglob("*"):
        if item.is_dir():
            continue
        destination = target / item.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        item.replace(destination)


def _write_staging(personas_dir: Path, files: list[tuple[str, str]]) -> Path:
    try:
        personas_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建人设目录：{exc}") from exc
    if not personas_dir.is_dir():
        raise ValueError("人设根目录被同名文件占用，无法保存")
    try:
        staging = Path(tempfile.mkdtemp(prefix=".upload-", dir=str(personas_dir)))
    except OSError as exc:
        raise ValueError(f"无法创建上传临时目录：{exc}") from exc
    try:
        for relative, content in files:
            destination = staging.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content.encode("utf-8"))
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(f"保存人设文件失败：{exc}") from exc
    return staging


def _install_staging(staging: Path, target: Path, *, overwrite: bool) -> bool:
    """Merge or move a finished staging tree; return True when created."""

    created = not target.exists()
    try:
        if not created:
            if not overwrite:
                raise ValueError(
                    f"人设「{target.name}」已存在；如需覆盖或补充文件，请勾选“覆盖同名文件”"
                )
            if target.is_symlink() or not target.is_dir():
                raise ValueError(f"人设「{target.name}」的目标路径已被文件占用，无法写入")
            _merge_tree(staging, target)
            shutil.rmtree(staging, ignore_errors=True)
            return False
        try:
            os.replace(staging, target)
        except OSError:
            # Different devices or a Windows handle: fall back to a merge into
            # a freshly created directory, then drop the staging tree.
            target.mkdir(parents=True, exist_ok=True)
            _merge_tree(staging, target)
            shutil.rmtree(staging, ignore_errors=True)
        return True
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(f"保存人设文件失败：{exc}") from exc


def upload_persona(
    settings: Settings,
    payload: object,
    *,
    controller: Any | None = None,
    root: Path | None = None,
    base_dir: Path | None = None,
) -> dict[str, object]:
    """Validate an upload, merge it into ``personas/<name>/`` and optionally apply.

    Raises ``ValueError`` with a readable Chinese message for every client
    error; the HTTP handler turns those into ``400 {"error": ...}``.
    """

    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    name = _validate_name(payload.get("name"))
    files = _canonicalize_style_paths(_validate_files(payload.get("files")))
    overwrite = _as_bool(payload.get("overwrite"), False, "overwrite")
    apply = _as_bool(payload.get("apply"), True, "apply")
    if apply and controller is None:
        raise ValueError("无法应用人设：控制台配置服务不可用，请改用 apply=false")

    base = _base_dir(base_dir)
    personas_dir = _resolve_relative(root, base_dir) if root is not None else persona_root(settings, base_dir=base_dir)
    target = personas_dir / name
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError(f"人设「{name}」的目标路径已被文件占用，无法写入")
    if target.is_dir() and not overwrite:
        raise ValueError(f"人设「{name}」已存在；如需覆盖或补充文件，请勾选“覆盖同名文件”")

    staging = _write_staging(personas_dir, files)
    try:
        created = _install_staging(staging, target, overwrite=overwrite)
    except ValueError:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    main_relative = _pick_main_file([relative for relative, _ in files])
    main_absolute = target.joinpath(*main_relative.split("/"))
    response: dict[str, object] = {
        "ok": True,
        "name": name,
        "created": created,
        "root": _display_path(personas_dir, base),
        "path": _display_path(target, base),
        "mainFile": main_relative,
        "files": [
            {
                "path": relative,
                "size": len(content.encode("utf-8")),
                "main": relative == main_relative,
            }
            for relative, content in files
        ],
        "applied": False,
        "restarted": False,
    }
    if not apply:
        return response
    persona_file = _display_path(main_absolute, base)
    try:
        saved = controller.save_config(
            {"values": {"PERSONA_NAME": name, "PERSONA_FILE": persona_file}}
        )
    except ValueError as exc:
        raise ValueError(f"人设文件已保存，但写入配置失败：{exc}") from exc
    response["applied"] = True
    response["restarted"] = bool(saved.get("restarted"))
    if isinstance(saved, dict) and isinstance(saved.get("config"), dict):
        response["config"] = saved["config"]
    return response
