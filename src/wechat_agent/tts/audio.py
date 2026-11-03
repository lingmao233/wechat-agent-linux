"""Audio helpers that do not depend on the speech module."""
from __future__ import annotations

import io
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
import wave

from .base import VoiceProviderError

MAX_PROVIDER_AUDIO_BYTES = 8_000_000


def complete_streaming_wav(data: bytes) -> bytes:
    """Complete the exact streaming PCM header returned by Qwen's HTTP TTS.

    Other providers return normal WAV/MP3; those bytes are passed through.
    Never repair an ordinary truncated WAV or pad/cut samples.
    """
    if (len(data) >= 44 and data[:4] == b"RIFF" and data[8:16] == b"WAVEfmt "
            and data[36:40] == b"data"
            and struct.unpack_from("<I", data, 4)[0] == 0x7FFFFFBF
            and struct.unpack_from("<I", data, 16)[0] == 16
            and struct.unpack_from("<I", data, 40)[0] == 0x7FFFFF9B):
        encoding, channels, rate, byte_rate, block, bits = struct.unpack_from("<HHIIHH", data, 20)
        size = len(data) - 44
        if (encoding != 1 or channels not in {1, 2} or bits != 16 or block != channels * 2
                or not 8000 <= rate <= 48000 or byte_rate != rate * block or not size or size % block):
            raise VoiceProviderError("tts_provider_audio_invalid")
        completed = bytearray(data)
        struct.pack_into("<I", completed, 4, len(data) - 8)
        struct.pack_into("<I", completed, 40, size)
        return bytes(completed)
    return data


def valid_wav(data: bytes) -> bool:
    if len(data) <= 44 or len(data) > MAX_PROVIDER_AUDIO_BYTES:
        return False
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            return (audio.getnchannels() in {1, 2}
                    and audio.getsampwidth() == 2
                    and 8000 <= audio.getframerate() <= 48000
                    and audio.getcomptype() == "NONE"
                    and audio.getnframes() > 0
                    and len(audio.readframes(audio.getnframes())) == audio.getnframes() * audio.getnchannels() * 2)
    except (wave.Error, EOFError, ValueError, OSError):
        return False


def _ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ensure_wav(data: bytes) -> bytes:
    """Return 24 kHz mono 16-bit PCM WAV.

    OpenAI-compatible gateways normally honour ``response_format=wav`` but some
    only return MP3.  ElevenLabs returns compressed audio by default.  The
    console deployment includes ffmpeg; Windows development uses the bundled
    imageio-ffmpeg binary.
    """
    if not data:
        raise VoiceProviderError("tts_provider_audio_missing")
    if len(data) > MAX_PROVIDER_AUDIO_BYTES:
        raise VoiceProviderError("tts_provider_audio_too_large")
    completed = complete_streaming_wav(data)
    if valid_wav(completed):
        return completed
    executable = _ffmpeg()
    if not executable:
        raise VoiceProviderError("tts_provider_audio_format")
    try:
        with tempfile.TemporaryDirectory(prefix="wechat-tts-audio-") as directory:
            source = Path(directory) / "source.audio"
            target = Path(directory) / "normalized.wav"
            source.write_bytes(data)
            result = subprocess.run(
                [executable, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                 "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(target)],
                capture_output=True, timeout=45,
            )
            if result.returncode or not target.is_file():
                raise VoiceProviderError("tts_provider_audio_invalid")
            output = target.read_bytes()
    except subprocess.TimeoutExpired as exc:
        raise VoiceProviderError("tts_provider_audio_timeout") from exc
    except OSError as exc:
        raise VoiceProviderError("tts_provider_audio_unavailable") from exc
    if not valid_wav(output):
        raise VoiceProviderError("tts_provider_audio_invalid")
    return output
