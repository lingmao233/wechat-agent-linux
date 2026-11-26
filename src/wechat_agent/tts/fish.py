"""Fish Audio speech synthesis provider.

Fish Audio exposes a compact HTTP API: ``POST /v1/tts`` streams audio bytes for
one voice model.  Community and cloned voice models are addressed by
``reference_id`` while the generation model travels in the ``model`` header, so
a voice published by somebody else works without any local training.
"""
from __future__ import annotations

import httpx

from .base import TTSProvider, TTSCapabilities, VoiceProviderError
from .audio import ensure_wav

# Community voice models that fit the whale-girl persona: Chinese loli, cute or
# tsundere character voices.  The ids are stable Fish Audio model ids; the names
# match their public listing so the console can show them as presets.
PRESET_VOICES = (
    ("f82e3885ac22468eb6c773b96f2c5752", "萝莉萌妹"),
    ("77b54218098b43e78db04c64f2da6b07", "小小萝莉"),
    ("13f717759feb423c8db53e04335d2bfe", "傲娇少女声"),
    ("28ebd488449d43739e8961ab4a756daf", "傲娇年轻女角色"),
)
DEFAULT_MODEL = "s2.1-pro"


class FishAudioTTSProvider(TTSProvider):
    provider_id = "fish"
    label = "Fish Audio"
    capabilities = TTSCapabilities(
        provider=provider_id,
        label=label,
        clone_mode="none",
        presets=True,
        preview=True,
        singing=False,
        max_sample_seconds=0.0,
        sample_notice="只使用 Fish Audio 上已公开的社区音色，不上传任何录音。",
        docs_url="https://fish.audio/zh-CN/text-to-speech-api/",
    )

    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    @property
    def _base(self) -> str:
        value = str(getattr(self.settings, "tts_base_url", "") or "").strip().rstrip("/")
        return value or "https://api.fish.audio"

    @property
    def _key(self) -> str:
        return str(getattr(self.settings, "tts_api_key", "") or "").strip()

    @property
    def _model(self) -> str:
        """Return the Fish generation model, or ``""`` for a foreign model name.

        Switching providers leaves ``TTS_MODEL`` untouched, so a leftover Aliyun
        model name must make this provider report "not configured" instead of
        sending an id Fish Audio would reject.
        """

        value = str(getattr(self.settings, "tts_model", "") or "").strip()
        if not value:
            return DEFAULT_MODEL
        return "" if value.casefold().startswith("qwen") else value

    @property
    def _voice(self) -> str:
        return str(getattr(self.settings, "tts_voice", "") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._key and self._voice and self._model)

    def model_supported(self) -> bool:
        return bool(self._model)

    def _endpoint(self) -> str:
        base = self._base
        if base.endswith("/v1/tts"):
            return base
        if base.endswith("/v1"):
            return base + "/tts"
        return base + "/v1/tts"

    def _client(self):
        options = {"proxy": self.settings.media_http_proxy_url.strip()} if self.settings.media_http_proxy_url.strip() else {}
        return httpx.Client(timeout=90, follow_redirects=False, transport=self.transport, **options)

    def cache_material(self) -> dict[str, object]:
        return {
            "provider": self.provider_id,
            "base_url": self._base,
            "model": self._model,
            "voice": self._voice,
            "rate": getattr(self.settings, "tts_rate", 1.0),
        }

    def synthesize(self, text: str, *, mode: str = "speech", voice: str | None = None,
                   rate: float | None = None, instruction: str | None = None) -> bytes:
        payload: dict[str, object] = {
            "text": text,
            "reference_id": voice or self._voice,
            "format": "wav",
            "sample_rate": 24000,
            "normalize": True,
        }
        speed = float(getattr(self.settings, "tts_rate", 1.0) or 1.0) if rate is None else float(rate)
        if speed != 1.0:
            # Fish Audio calls the tempo knob "prosody"; 1.0 means untouched.
            payload["prosody"] = {"speed": speed}
        headers = {"Content-Type": "application/json", "model": self._model}
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
    if status in {402, 429}:
        return "tts_provider_quota"
    if status in {404, 405}:
        return "tts_provider_config"
    if status == 400:
        return "tts_provider_rejected"
    if status >= 500:
        return "tts_provider_unavailable"
    return "tts_provider_error"
