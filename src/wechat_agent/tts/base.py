"""Provider contract shared by speech replies and the voice DIY studio.

The public Web UI must never assume one TTS vendor.  Each provider declares its
capabilities here; the API surfaces those capabilities to the plugin so the UI
can hide upload/clone actions when a provider only offers preset voices.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class VoiceProviderError(RuntimeError):
    """A safe provider error code.

    Provider response bodies, API keys and signed URLs must never be copied
    into this exception.  Callers translate the code into a user-facing message.
    """

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


CloneMode = Literal["none", "instant", "async"]


@dataclass(frozen=True)
class TTSCapabilities:
    provider: str
    label: str
    clone_mode: CloneMode
    presets: bool
    preview: bool
    singing: bool
    max_sample_seconds: float
    sample_notice: str
    docs_url: str = ""

    @property
    def can_clone(self) -> bool:
        return self.clone_mode != "none"

    def public(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "label": self.label,
            "clone_mode": self.clone_mode,
            "can_clone": self.can_clone,
            "presets": self.presets,
            "preview": self.preview,
            "singing": self.singing,
            "max_sample_seconds": self.max_sample_seconds,
            "sample_notice": self.sample_notice,
            "docs_url": self.docs_url,
        }


class TTSProvider:
    """One synthesis backend, optionally with a cloning API.

    ``settings`` is the live pydantic Settings object.  Providers must read
    fields lazily so config reloads are reflected without rebuilding the
    object graph unnecessarily (the console does rebuild services on reload,
    but tests and the controller also keep references).
    """

    provider_id = ""
    label = ""
    capabilities: TTSCapabilities

    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    def model_supported(self) -> bool:
        return True

    def cache_material(self) -> dict[str, object]:
        raise NotImplementedError

    def synthesize(self, text: str, *, mode: str = "speech", voice: str | None = None) -> bytes:
        raise NotImplementedError

    def clone(self, audio: bytes, *, name: str, language: str) -> dict[str, object]:
        raise VoiceProviderError("tts_clone_unsupported")

    def query_status(self, voice_id: str) -> str:
        """Return ready, deploying, failed or unknown; never raw provider JSON."""
        return "ready" if voice_id else "failed"

    def list_presets(self) -> list[dict[str, str]]:
        return []
