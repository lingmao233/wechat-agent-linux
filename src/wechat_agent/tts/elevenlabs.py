"""ElevenLabs speech synthesis and instant voice cloning provider."""
from __future__ import annotations

from urllib.parse import quote

import httpx

from .base import TTSProvider, TTSCapabilities, VoiceProviderError
from .audio import ensure_wav


class ElevenLabsTTSProvider(TTSProvider):
    provider_id = "elevenlabs"
    label = "ElevenLabs"
    capabilities = TTSCapabilities(
        provider=provider_id,
        label=label,
        clone_mode="instant",
        presets=True,
        preview=True,
        singing=False,
        max_sample_seconds=30.0,
        sample_notice="录音会上传到 ElevenLabs 用于即时复刻；本地保留一份私有样例。",
        docs_url="https://elevenlabs.io/docs/product-guides/voices/voice-cloning/instant-voice-cloning",
    )

    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    @property
    def _base(self) -> str:
        value = str(getattr(self.settings, "tts_base_url", "") or "").strip().rstrip("/")
        return value or "https://api.elevenlabs.io"

    @property
    def _key(self) -> str:
        return str(getattr(self.settings, "tts_api_key", "") or "").strip()

    @property
    def configured(self) -> bool:
        # Cloning does not need a current TTS voice; require only the key and a
        # model so the user can create the first cloned voice from an empty
        # TTS_VOICE.  Synthesis still requires a voice, checked below.
        return bool(self._key and str(getattr(self.settings, "tts_model", "") or "").strip())

    def model_supported(self) -> bool:
        return bool(str(getattr(self.settings, "tts_model", "") or "").strip())

    def _endpoint(self, path: str) -> str:
        base = self._base
        if base.endswith("/v1"):
            return base + path
        return base + "/v1" + path

    def _client(self):
        options = {"proxy": self.settings.media_http_proxy_url.strip()} if self.settings.media_http_proxy_url.strip() else {}
        return httpx.Client(timeout=60, follow_redirects=False, transport=self.transport, **options)

    def cache_material(self) -> dict[str, object]:
        return {
            "provider": self.provider_id,
            "base_url": self._base,
            "model": str(getattr(self.settings, "tts_model", "") or ""),
            "voice": str(getattr(self.settings, "tts_voice", "") or ""),
            "stability": 0.5,
            "similarity_boost": 0.75,
        }

    def synthesize(self, text: str, *, mode: str = "speech", voice: str | None = None) -> bytes:
        if not self.configured:
            raise VoiceProviderError("tts_provider_config")
        voice = (voice or str(getattr(self.settings, "tts_voice", "") or "")).strip()
        if not voice:
            raise VoiceProviderError("tts_provider_config")
        payload = {
            "text": text,
            "model_id": str(getattr(self.settings, "tts_model", "") or "").strip() or "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        }
        headers = {"xi-api-key": self._key}
        try:
            with self._client() as client:
                response = client.post(
                    self._endpoint("/text-to-speech/" + quote(voice, safe="")),
                    headers=headers, json=payload, params={"output_format": "mp3_44100_128"},
                )
        except httpx.TimeoutException as exc:
            raise VoiceProviderError("tts_provider_timeout") from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError("tts_provider_unavailable") from exc
        if response.status_code != 200:
            raise VoiceProviderError(_status_code(response.status_code))
        if not response.content:
            raise VoiceProviderError("tts_provider_audio_missing")
        return ensure_wav(response.content)

    def clone(self, audio: bytes, *, name: str, language: str) -> dict[str, object]:
        if not self._key:
            raise VoiceProviderError("tts_provider_config")
        headers = {"xi-api-key": self._key}
        try:
            with self._client() as client:
                response = client.post(
                    self._endpoint("/voices/add"),
                    headers=headers,
                    data={"name": name, "description": "由微信助手声音 DIY 创建"},
                    files={"files": ("sample.wav", audio, "audio/wav")},
                )
        except httpx.TimeoutException as exc:
            raise VoiceProviderError("tts_provider_timeout") from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError("tts_provider_unavailable") from exc
        if response.status_code not in {200, 201}:
            raise VoiceProviderError(_status_code(response.status_code))
        try:
            result = response.json()
        except ValueError as exc:
            raise VoiceProviderError("tts_provider_error") from exc
        voice_id = str(result.get("voice_id") or "").strip() if isinstance(result, dict) else ""
        if not voice_id or len(voice_id) > 240:
            raise VoiceProviderError("tts_provider_error")
        return {"voice_id": voice_id, "status": "ready"}

    def query_status(self, voice_id: str) -> str:
        if not self._key:
            raise VoiceProviderError("tts_provider_config")
        try:
            with self._client() as client:
                response = client.get(
                    self._endpoint("/voices/" + quote(voice_id, safe="")),
                    headers={"xi-api-key": self._key},
                )
        except httpx.TimeoutException as exc:
            raise VoiceProviderError("tts_provider_timeout") from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError("tts_provider_unavailable") from exc
        if response.status_code == 200:
            return "ready"
        if response.status_code == 404:
            return "failed"
        raise VoiceProviderError(_status_code(response.status_code))

    def list_presets(self) -> list[dict[str, str]]:
        return []


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
