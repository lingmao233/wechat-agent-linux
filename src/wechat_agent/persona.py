from __future__ import annotations

"""Load operator-provided speaking style instructions for the text model."""

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Persona:
    name: str
    text: str
    source: str = ""
    # Optional, persona-owned prompt fragments.  They are deliberately kept
    # out of the main Markdown block so an operator can replace the character
    # without the agent retaining another character's voice or drawing style.
    speech_style: str = ""
    image_style: str = ""


def load_persona(settings: Settings) -> Persona | None:
    """Load a bounded Markdown persona and its adjacent reference files.

    The persona is operator configuration, so it is read locally at agent
    construction time.  It is placed in a clearly delimited prompt block and
    can only shape tone; the LLM client adds the hard rule that it cannot
    change safety, memory isolation, or factuality requirements.

    Two optional reference files let a persona own the parts that used to be
    hard-coded for the bundled dafeiyu character:

    ``references/speech-style.md``
        Character-specific speaking style.  When absent, the generic
        human-like prompt rules still apply but no prior character's voice is
        injected (including the shared playful tone examples).

    ``references/image-style.md``
        Character/appearance description used for optional reaction pictures.
        When absent, automatic reaction pictures are disabled for this persona
        instead of silently drawing the previous character.
    """

    if not settings.persona_enabled:
        return None
    path = _resolve_path(settings.persona_file)
    if not path.is_file():
        log.warning("persona file does not exist: %s", path)
        return None

    parts: list[str] = []
    references = path.parent / "references"
    try:
        parts.append(path.read_text(encoding="utf-8"))
        if references.is_dir():
            for reference in (references / "lines.md", references / "lore.md"):
                if reference.is_file():
                    parts.append(reference.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("cannot read persona file %s: %s", path, exc)
        return None

    text = "\n\n".join(part.strip() for part in parts if part.strip())
    text = text[: settings.persona_max_chars].strip()
    if not text:
        return None
    return Persona(
        name=settings.persona_name.strip() or path.stem,
        text=text,
        source=settings.persona_source_url.strip() or str(path),
        speech_style=_read_style_file(references / "speech-style.md", settings.persona_max_chars),
        image_style=_read_style_file(references / "image-style.md", settings.persona_max_chars),
    )


def _read_style_file(path: Path, limit: int) -> str:
    """Read an optional persona-owned prompt fragment without leaking errors."""

    try:
        if not path.is_file():
            return ""
        value = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("cannot read persona style file %s: %s", path, exc)
        return ""
    return value.strip()[:limit]


def _resolve_path(value: Path) -> Path:
    path = value.expanduser()
    if path.is_absolute():
        return path
    cwd_path = (Path.cwd() / path).resolve()
    if cwd_path.is_file():
        return cwd_path
    # Running from a different working directory should still find the
    # checked-out persona beside the repository's src/ directory.
    repo_path = (Path(__file__).resolve().parents[2] / path).resolve()
    return repo_path
