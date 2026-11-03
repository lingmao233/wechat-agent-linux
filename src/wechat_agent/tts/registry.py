"""TTS provider registry.

Keep provider selection in one place.  Adding another vendor should only mean
adding a module, a settings value, and capability-specific UI copy.
"""
from __future__ import annotations

from .aliyun import AliyunTTSProvider
from .base import TTSProvider, TTSCapabilities, VoiceProviderError
from .elevenlabs import ElevenLabsTTSProvider
from .openai_compat import OpenAICompatibleTTSProvider

PROVIDERS = {
    AliyunTTSProvider.provider_id: AliyunTTSProvider,
    OpenAICompatibleTTSProvider.provider_id: OpenAICompatibleTTSProvider,
    ElevenLabsTTSProvider.provider_id: ElevenLabsTTSProvider,
}
PROVIDER_IDS = tuple(PROVIDERS)


def build_provider(settings) -> TTSProvider:
    provider_id = str(getattr(settings, "tts_provider", "aliyun") or "aliyun").strip()
    provider_type = PROVIDERS.get(provider_id)
    if provider_type is None:
        raise VoiceProviderError("tts_provider_unknown")
    return provider_type(settings)


__all__ = [
    "AliyunTTSProvider",
    "ElevenLabsTTSProvider",
    "OpenAICompatibleTTSProvider",
    "PROVIDERS",
    "PROVIDER_IDS",
    "TTSCapabilities",
    "TTSProvider",
    "VoiceProviderError",
    "build_provider",
]
