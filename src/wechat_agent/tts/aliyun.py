"""Aliyun DashScope Qwen-Audio native HTTP TTS provider."""
from __future__ import annotations

import base64
from pathlib import Path
import uuid

from .base import TTSProvider, TTSCapabilities, VoiceProviderError
from .audio import MAX_PROVIDER_AUDIO_BYTES, complete_streaming_wav

SUPPORTED_TTS_MODELS = frozenset({
    "qwen-audio-3.1-tts-plus",
    "qwen-audio-3.1-tts-flash",
    "qwen-audio-3.0-tts-plus",
    "qwen-audio-3.0-tts-flash",
})
TTS_PRESETS = (
    ("longanhuan_v3.1", "龙安欢"),
    ("longanlingxin_v3.1", "龙安灵心"),
    ("longanxiaoxin_v3.1", "龙安小新"),
    ("longanqianxue_v3.1", "龙安千雪"),
)


class AliyunTTSProvider(TTSProvider):
    provider_id = "aliyun"
    label = "阿里云 Qwen-Audio"
    capabilities = TTSCapabilities(
        provider=provider_id,
        label=label,
        clone_mode="async",
        presets=True,
        preview=True,
        singing=True,
        max_sample_seconds=30.0,
        sample_notice="录音会上传到阿里云用于复刻；本地仅保留一份私有样例。",
        docs_url="https://help.aliyun.com/zh/model-studio/voice-cloning",
    )

    def __init__(self, settings, *, api=None):
        effective = _effective_settings(settings)
        super().__init__(effective)
        self._effective = effective
        if api is not None:
            self.api = api
        else:
            from ..speech import AliyunAudio
            self.api = AliyunAudio(effective)

    @property
    def configured(self) -> bool:
        return bool(getattr(self.api, "configured", False))

    def model_supported(self) -> bool:
        return str(getattr(self.settings, "tts_model", "") or "") in SUPPORTED_TTS_MODELS

    def cache_material(self) -> dict[str, object]:
        return {
            "provider": self.provider_id,
            "base_url": str(getattr(self._effective, "aliyun_audio_base_url", "") or ""),
            "model": str(getattr(self.settings, "tts_model", "") or ""),
            "voice": str(getattr(self.settings, "tts_voice", "") or ""),
            "instruction": str(getattr(self.settings, "tts_instruction", "") or ""),
            "singing_instruction": str(getattr(self.settings, "tts_singing_instruction", "") or ""),
            "rate": getattr(self.settings, "tts_rate", 1.0),
            "pitch": getattr(self.settings, "tts_pitch", 1.0),
            "seed": getattr(self.settings, "tts_seed", 0),
            "language": str(getattr(self.settings, "tts_language", "auto") or "auto"),
        }

    def _request(self, service: str, payload: dict) -> dict:
        from ..speech import SpeechError
        try:
            return self.api.request(service, payload)
        except SpeechError as exc:
            raise VoiceProviderError(_provider_code(exc.code)) from exc

    def synthesize(self, text: str, *, mode: str = "speech", voice: str | None = None) -> bytes:
        from ..speech import SpeechError
        settings = self._effective
        synthesis_input = {
            "text": text, "voice": voice or settings.tts_voice, "format": "wav", "sample_rate": 24000,
            "rate": settings.tts_rate, "pitch": settings.tts_pitch, "seed": settings.tts_seed,
        }
        instruction = settings.tts_singing_instruction if mode == "singing" else settings.tts_instruction
        if str(instruction or "").strip():
            synthesis_input["instruction"] = instruction
        if settings.tts_language != "auto":
            synthesis_input["language_hints"] = [settings.tts_language]
        result = self._request("/services/audio/tts/SpeechSynthesizer", {
            "model": settings.tts_model, "input": synthesis_input,
        })
        output = result.get("output") if isinstance(result, dict) else None
        audio = output.get("audio") if isinstance(output, dict) else None
        if not isinstance(audio, dict) or not isinstance(audio.get("url"), str) or not audio["url"]:
            raise VoiceProviderError("tts_audio_missing")
        try:
            data = self.api.download(audio["url"])
        except SpeechError as exc:
            raise VoiceProviderError(_provider_code(exc.code)) from exc
        if len(data) > MAX_PROVIDER_AUDIO_BYTES:
            raise VoiceProviderError("tts_provider_audio_too_large")
        return complete_streaming_wav(data)

    def clone(self, audio: bytes, *, name: str, language: str) -> dict[str, object]:
        model = str(getattr(self.settings, "tts_model", "") or "")
        if model not in SUPPORTED_TTS_MODELS:
            raise VoiceProviderError("voice_model_unsupported")
        prefix = "diy" + uuid.uuid4().hex[:7]
        result = self._request("/services/audio/tts/customization", {
            "model": "voice-enrollment",
            "input": {
                "action": "create_voice",
                "target_model": model,
                "prefix": prefix,
                "audio": "data:audio/wav;base64," + base64.b64encode(audio).decode("ascii"),
                "language_hints": [language],
                "max_prompt_audio_length": 30.0,
                "enable_preprocess": False,
                "enable_volume_normalization": "false",
            },
        })
        output = result.get("output") if isinstance(result, dict) else None
        if not isinstance(output, dict):
            raise VoiceProviderError("tts_provider_error")
        voice_id = str(output.get("voice_id") or "").strip()
        if not voice_id or len(voice_id) > 240:
            raise VoiceProviderError("tts_provider_error")
        return {"voice_id": voice_id, "status": "deploying"}

    def query_status(self, voice_id: str) -> str:
        result = self._request("/services/audio/tts/customization", {
            "model": "voice-enrollment",
            "input": {"action": "query_voice", "voice_id": voice_id},
        })
        output = result.get("output") if isinstance(result, dict) else None
        if not isinstance(output, dict):
            raise VoiceProviderError("tts_provider_error")
        status = str(output.get("status") or "").upper()
        if status == "OK":
            return "ready"
        if status in {"UNDEPLOYED", "FAILED", "ERROR"}:
            return "failed"
        if status in {"DEPLOYING", "PENDING", "PROCESSING"}:
            return "deploying"
        return "unknown"

    def list_presets(self) -> list[dict[str, str]]:
        return [{"id": voice_id, "name": name} for voice_id, name in TTS_PRESETS]


def _effective_settings(settings):
    base = str(getattr(settings, "tts_base_url", "") or "").strip()
    key = str(getattr(settings, "tts_api_key", "") or "").strip()
    if not base and not key:
        return settings
    updates = {}
    if base:
        updates["aliyun_audio_base_url"] = base
    if key:
        updates["aliyun_audio_api_key"] = key
    return settings.model_copy(update=updates)


def _provider_code(code: str) -> str:
    code = str(code or "voice_provider_error")
    if "401" in code or "403" in code:
        return "voice_provider_auth"
    if "429" in code:
        return "voice_provider_quota"
    if code in {"aliyun_audio_not_configured", "audio_api_base_invalid", "audio_api_base_requires_api_v1"}:
        return "voice_provider_config"
    if code in {"audio_api_timeout", "audio_api_unavailable", "tts_audio_download_failed"}:
        return "voice_provider_unavailable"
    return "voice_provider_error"
