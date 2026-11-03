"""OpenAI-compatible speech synthesis provider.

Covers OpenAI itself as well as gateways and local servers that implement
``POST /v1/audio/speech`` with the usual ``model``/``input``/``voice`` body.
Preset voices only: the public OpenAI speech API does not expose voice cloning,
so the UI must hide the upload card for this provider.
"""
from __future__ import annotations

import httpx

from .base import TTSProvider, TTSCapabilities, VoiceProviderError
from .audio import ensure_wav

PRESET_VOICES = (
    ("alloy", "Alloy"),
    ("echo", "Echo"),
    ("fable", "Fable"),
    ("onyx", "Onyx"),
    ("nova", "Nova"),
    ("shimmer", "Shimmer"),
    ("coral", "Coral"),
    ("sage", "Sage"),
    ("ash", "Ash"),
    ("ballad", "Ballad"),
    ("verse", "Verse"),
)


class OpenAICompatibleTTSProvider(TTSProvider):
    provider_id = "openai_compatible"
    label = "OpenAI 兼容 TTS"
    capabilities = TTSCapabilities(
        provider=provider_id,
        label=label,
        clone_mode="none",
        presets=True,
        preview=True,
        singing=False,
        max_sample_seconds=0.0,
        sample_notice="当前供应商只提供预置音色，不会上传参考录音。",
        docs_url="https://platform.openai.com/docs/guides/text-to-speech",
    )

    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    @property
    def _base(self) -> str:
        return str(getattr(self.settings, "tts_base_url", "") or "").strip().rstrip("/") or "https://api.openai.com/v1"

    @property
    def _key(self) -> str:
        return str(getattr(self.settings, "tts_api_key", "") or "").strip()

    @property
    def configured(self) -> bool:
        if not str(getattr(self.settings, "tts_model", "") or "").strip():
            return False
        if not str(getattr(self.settings, "tts_voice", "") or "").strip():
            return False
        # Local gateways commonly accept no key; the public OpenAI endpoint
        # needs one.  Do not silently send an empty Authorization header.
        return bool(self._key or "api.openai.com" not in self._base)

    def model_supported(self) -> bool:
        return bool(str(getattr(self.settings, "tts_model", "") or "").strip())

    def _endpoint(self) -> str:
        base = self._base
        if base.endswith("/audio/speech"):
            return base
        if base.endswith("/v1"):
            return base + "/audio/speech"
        return base + "/v1/audio/speech"

    def _client(self):
        options = {"proxy": self.settings.media_http_proxy_url.strip()} if self.settings.media_http_proxy_url.strip() else {}
        return httpx.Client(timeout=60, follow_redirects=False, transport=self.transport, **options)

    def cache_material(self) -> dict[str, object]:
        return {
            "provider": self.provider_id,
            "base_url": self._base,
            "model": str(getattr(self.settings, "tts_model", "") or ""),
            "voice": str(getattr(self.settings, "tts_voice", "") or ""),
            "rate": getattr(self.settings, "tts_rate", 1.0),
            "instruction": str(getattr(self.settings, "tts_instruction", "") or ""),
        }

    def synthesize(self, text: str, *, mode: str = "speech", voice: str | None = None) -> bytes:
        payload = {
            "model": str(getattr(self.settings, "tts_model", "") or ""),
            "input": text,
            "voice": voice or str(getattr(self.settings, "tts_voice", "") or ""),
            "response_format": "wav",
        }
        rate = float(getattr(self.settings, "tts_rate", 1.0) or 1.0)
        if rate != 1.0:
            payload["speed"] = rate
        instruction = str(getattr(self.settings, "tts_instruction", "") or "").strip()
        if instruction and "gpt-4o" in payload["model"].casefold():
            payload["instructions"] = instruction
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = "Bearer " + self._key
        try:
            with self._client() as client:
                response = client.post(self._endpoint(), headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise VoiceProviderError("tts_provider_timeout") from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError("tts_provider_unavailable") from exc
        if response.status_code != 200:
            raise VoiceProviderError(_status_code(response.status_code))
        content = response.content
        if not content:
            raise VoiceProviderError("tts_provider_audio_missing")
        try:
            return ensure_wav(content)
        except VoiceProviderError:
            raise
        except Exception as exc:
            raise VoiceProviderError("tts_provider_audio_invalid") from exc

    def list_presets(self) -> list[dict[str, str]]:
        return [{"id": voice_id, "name": name} for voice_id, name in PRESET_VOICES]


def _status_code(status: int) -> str:
    if status in {401, 403}:
        return "tts_provider_auth"
    if status == 429:
        return "tts_provider_quota"
    if status in {404, 405}:
        return "tts_provider_config"
    if status == 400:
        return "tts_provider_rejected"
    if status >= 500:
        return "tts_provider_unavailable"
    return "tts_provider_error"
