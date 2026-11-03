"""TTS provider abstraction used by speech replies and voice DIY."""
from .base import TTSCapabilities, TTSProvider, VoiceProviderError
from .registry import (
    AliyunTTSProvider,
    ElevenLabsTTSProvider,
    OpenAICompatibleTTSProvider,
    PROVIDERS,
    PROVIDER_IDS,
    build_provider,
)

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
