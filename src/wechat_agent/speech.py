"""Aliyun's native short-ASR and TTS HTTP protocols, with bounded local audio."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import unicodedata
from urllib.parse import urlsplit, urlunsplit
import uuid
import wave

import httpx

MAX_SPEECH_BYTES = 8_000_000
MAX_REPLY_SECONDS = 55
MELODY_ALGORITHM_VERSION = 2
_MELODY_CONTOUR_SEMITONES = (0.0, 4.0, 7.0, 5.0, 3.0, 2.0, -1.0, 0.0)


def voice_reply_declined(text: str) -> bool:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = "".join(c for c in value if unicodedata.category(c) != "Cf")
    value = re.sub(r"(?:不要|不用|不想用|别|禁止|不准)(?:再用|再|用|\s){0,3}(?:文字|文本|打字)", "", value)
    return bool(re.search(
        r"(?:别|不要|不用|禁止|不准)(?:再|给我|给大家|发|用|回|\s){0,5}语音(?!识别|模型|转)"
        r"|不想听.{0,5}(?:声音|语音)"
        r"|(?:只用|只要|用|改用|先用)(?:文字|文本|打字)(?:回复|回答|说|聊)?", value,
    ))


def voice_request_kind(text: str) -> str:
    """Delivery intent in the current addressed question, never old chat text."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = " ".join("".join(c for c in value if unicodedata.category(c) != "Cf").split())
    if voice_reply_declined(value):
        return ""
    singing = re.search(r"唱(?:首|一|两|几|段|句|歌|点|一下|给|个|出来|吗)|(?:能|会|可以).{0,3}唱", value)
    no_singing = re.search(r"(?:别|不要|不用|不想|不准|停止)(?:再|听|你|她|给我|\s){0,4}唱|(?:我|他|她)(?:正在|在|刚刚|刚|已经)唱", value)
    if singing and not no_singing:
        return "singing"
    speech = re.search(
        r"(?:想|要|能|可以|让我|给我|请|听听).{0,8}听.{0,8}(?:声音|语音)"
        r"|(?:发|来|回|录|用|听)(?:给我|给大家|我|你|的|一|个|条|段|下|几|两|句|\s){0,8}(?:语音|声音)"
        r"|(?:语音|声音).{0,6}(?:说|回复|回答|聊|念|听听|听一下)"
        r"|(?:说|讲)(?:一|两|几|句|点){0,4}(?:话|故事|绕口令)"
        r"|(?:念|读).{0,8}(?:给我听|给大家听|一段|两句|这句|这段|首诗|一句|绕口令)"
        r"|(?:voice|audio)\s+(?:reply|message)", value, re.I,
    )
    return "speech" if speech else ""


class SpeechError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def audio_endpoint(base: str, service: str) -> str:
    value = urlsplit(base.strip())
    if (value.scheme not in {"http", "https"} or not value.hostname or value.username
            or value.password or value.query or value.fragment or "{" in base):
        raise SpeechError("audio_api_base_invalid")
    if value.path.rstrip("/") != "/api/v1":
        raise SpeechError("audio_api_base_requires_api_v1")
    return base.rstrip("/") + service


def wave_duration(path: Path, *, max_seconds: float = MAX_REPLY_SECONDS) -> float:
    if not path.is_file() or not 44 < path.stat().st_size <= MAX_SPEECH_BYTES:
        raise SpeechError("speech_audio_size_limit")
    try:
        with wave.open(str(path), "rb") as audio:
            channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
            duration = frames / rate
            if (channels not in {1, 2} or width != 2 or not 8000 <= rate <= 48000
                    or audio.getcomptype() != "NONE" or not 0 < duration <= max_seconds):
                raise SpeechError("speech_audio_format_or_duration")
            if len(audio.readframes(frames)) != frames * channels * width:
                raise SpeechError("speech_audio_truncated")
            return duration
    except (wave.Error, EOFError, ZeroDivisionError, OSError) as exc:
        raise SpeechError("speech_audio_invalid") from exc


def complete_tts_wave(data: bytes) -> bytes:
    """Finish the exact streaming PCM header returned by Qwen's HTTP TTS.

    The HTTP download must have finished successfully first. Do not repair
    ordinary truncated WAVs or pad/cut samples to satisfy their declared size.
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
            raise SpeechError("speech_audio_invalid")
        completed = bytearray(data)
        struct.pack_into("<I", completed, 4, len(data) - 8)
        struct.pack_into("<I", completed, 40, size)
        return bytes(completed)
    return data


def decode_to_wave(source: Path, destination: Path, settings) -> float:
    job = {"operation": "decode_wav", "path": str(source.resolve()), "output": str(destination.resolve()),
           "max_seconds": settings.voice_max_duration_seconds, "max_bytes": settings.voice_max_bytes}
    try:
        completed = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).with_name("voice_worker.py"))],
            input=json.dumps(job) + "\n", capture_output=True, text=True, encoding="utf-8",
            timeout=min(settings.voice_timeout_seconds, 60),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode or len(completed.stdout) > 65536:
            raise SpeechError("audio_decode_failed")
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        if result.get("error"):
            raise SpeechError(str(result["error"])[:80])
        return wave_duration(destination, max_seconds=settings.voice_max_duration_seconds)
    except subprocess.TimeoutExpired as exc:
        raise SpeechError("audio_decode_timeout") from exc
    except (OSError, ValueError, IndexError) as exc:
        raise SpeechError("audio_decode_failed") from exc


def _rubberband_ffmpeg() -> str | None:
    """Return an ffmpeg binary that actually exposes the rubberband filter.

    The Linux deployment normally has ``/usr/bin/ffmpeg``.  Windows does not
    require a system installation because ``imageio-ffmpeg`` ships a binary
    with the project dependencies.  Checking the filter list avoids silently
    treating a plain ffmpeg build as a melody processor.
    """

    candidates: list[str] = []
    path = shutil.which("ffmpeg")
    if path:
        candidates.append(path)
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled:
            candidates.append(str(bundled))
    except (ImportError, OSError, RuntimeError):
        pass
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = subprocess.run(
                [candidate, "-hide_banner", "-filters"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and "rubberband" in result.stdout:
            return candidate
    return None


def _numpy_pitch_chunk(samples, semitones: float):
    """Small dependency-free fallback used when rubberband is unavailable."""

    import numpy as np

    length, channels = samples.shape
    if length < 2:
        return samples.copy()
    ratio = 2.0 ** (float(semitones) / 12.0)
    # A periodic interpolation keeps a pitch-shifted tone moving through the
    # whole chunk.  The production path uses rubberband; this is deliberately
    # bounded and deterministic for stripped-down Windows environments.
    positions = (np.arange(length, dtype=np.float32) * ratio) % length
    base = np.arange(length, dtype=np.float32)
    return np.column_stack([
        np.interp(positions, base, samples[:, channel]) for channel in range(channels)
    ])


def _write_pcm_wave(path: Path, samples, *, channels: int, rate: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.clip(-32768, 32767).astype("<i2").tobytes())


def _rubberband_pitch_chunk(ffmpeg: str, samples, *, channels: int, rate: int,
                            semitones: float, directory: Path, index: int):
    """Pitch-shift one note while keeping the original channel/rate contract."""

    import numpy as np

    source = directory / f"segment-{index}.in.wav"
    target = directory / f"segment-{index}.out.wav"
    _write_pcm_wave(source, samples, channels=channels, rate=rate)
    ratio = 2.0 ** (float(semitones) / 12.0)
    filter_expr = f"rubberband=pitch={ratio:.9f}:tempo=1.0"
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(source), "-af", filter_expr, "-ar", str(rate), "-ac", str(channels),
             "-c:a", "pcm_s16le", str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not target.is_file():
            raise SpeechError("singing_audio_transform_failed")
        with wave.open(str(target), "rb") as audio:
            if (audio.getnchannels() != channels or audio.getsampwidth() != 2
                    or audio.getframerate() != rate):
                raise SpeechError("singing_audio_format")
            raw = audio.readframes(audio.getnframes())
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        if not len(values) or len(values) % channels:
            raise SpeechError("singing_audio_transform_failed")
        return values.reshape(-1, channels)
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def _fit_frames(samples, frame_count: int):
    """Normalize rubberband's small frame-count drift without changing pitch."""

    import numpy as np

    if len(samples) == frame_count:
        return samples
    if len(samples) < 1:
        raise SpeechError("singing_audio_transform_failed")
    if frame_count <= 1:
        return samples[:1]
    if len(samples) == 1:
        return np.repeat(samples, frame_count, axis=0)
    positions = np.linspace(0.0, len(samples) - 1.0, frame_count, dtype=np.float32)
    base = np.arange(len(samples), dtype=np.float32)
    return np.column_stack([
        np.interp(positions, base, samples[:, channel])
        for channel in range(samples.shape[1])
    ])


def _render_melody(samples, *, channels: int, rate: int, ffmpeg: str | None, directory: Path):
    import numpy as np

    total = len(samples)
    if total < 2:
        return samples.copy()
    # Eight note positions are enough for a short spoken line and avoid a
    # twenty-second clip turning into dozens of separate ffmpeg processes.
    segment_count = min(len(_MELODY_CONTOUR_SEMITONES), max(2, int(np.ceil(total / (rate * 0.35)))))
    boundaries = np.linspace(0, total, segment_count + 1, dtype=int)
    output = np.zeros_like(samples, dtype=np.float32)
    for index in range(segment_count):
        start, end = int(boundaries[index]), int(boundaries[index + 1])
        chunk = samples[start:end]
        if len(chunk) == 0:
            continue
        # Do not amplify silence or feed it through a pitch shifter, which can
        # turn a clean leading pause into a noisy click.
        if float(np.max(np.abs(chunk))) < 4.0:
            shifted = chunk.copy()
        elif ffmpeg:
            shifted = _rubberband_pitch_chunk(
                ffmpeg, chunk, channels=channels, rate=rate, semitones=_MELODY_CONTOUR_SEMITONES[index],
                directory=directory, index=index,
            )
        else:
            shifted = _numpy_pitch_chunk(chunk, _MELODY_CONTOUR_SEMITONES[index])
        shifted = _fit_frames(shifted, end - start)
        fade = min(max(16, int(rate * 0.012)), len(shifted) // 2, start)
        if fade:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
            shifted[:fade] = output[start - fade:start] * (1.0 - ramp) + shifted[:fade] * ramp
        output[start:end] = shifted
    return output


def _apply_melody_contour(source: Path, destination: Path) -> None:
    """Turn a TTS line into a bounded, deterministic short melody.

    The provider may return speech even when given a singing instruction.  The
    primary path uses Rubber Band's phase-vocoder pitch shifting over eight
    note regions, then restores each region's exact frame count.  That gives a
    measurable musical contour while preserving words, duration, sample rate,
    channels, and WAV validity.  The original provider file is left untouched.
    """

    try:
        import numpy as np

        with wave.open(str(source), "rb") as audio:
            channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
            raw = audio.readframes(frames)
            if channels not in {1, 2} or width != 2 or rate < 8_000 or not raw:
                raise SpeechError("singing_audio_format")
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".singing-", dir=str(destination.parent)) as directory:
            try:
                ffmpeg = _rubberband_ffmpeg()
                output = _render_melody(samples, channels=channels, rate=rate, ffmpeg=ffmpeg, directory=Path(directory))
            except SpeechError:
                # A distro ffmpeg can advertise the filter but fail on a
                # particular build. Keep the voice usable with the bounded
                # NumPy contour instead of falling back to plain speech.
                output = _render_melody(samples, channels=channels, rate=rate, ffmpeg=None, directory=Path(directory))
        _write_pcm_wave(destination, output, channels=channels, rate=rate)
    except SpeechError:
        raise
    except (ImportError, OSError, ValueError, wave.Error) as exc:
        raise SpeechError("singing_audio_transform_failed") from exc


class AliyunAudio:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    @property
    def configured(self) -> bool:
        return bool(self.settings.aliyun_audio_base_url.strip() and self.settings.aliyun_audio_api_key.strip())

    def _client(self):
        options = {"proxy": self.settings.media_http_proxy_url.strip()} if self.settings.media_http_proxy_url.strip() else {}
        return httpx.Client(timeout=60, follow_redirects=False, transport=self.transport, **options)

    def request(self, service: str, payload: dict) -> dict:
        if not self.configured:
            raise SpeechError("aliyun_audio_not_configured")
        url = audio_endpoint(self.settings.aliyun_audio_base_url, service)
        try:
            with self._client() as client:
                # Provider error strings and signed audio URLs never enter logs.
                with client.stream("POST", url, headers={"Authorization": "Bearer " + self.settings.aliyun_audio_api_key,
                                                        "X-DashScope-SSE": "disable"}, json=payload) as response:
                    data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data) > 256_000:
                            raise SpeechError("audio_api_response_size_limit")
                    if response.status_code != 200:
                        raise SpeechError("audio_api_http_" + str(response.status_code))
            result = json.loads(data)
            if not isinstance(result, dict) or result.get("code"):
                raise SpeechError("audio_api_rejected")
            return result
        except httpx.TimeoutException as exc:
            raise SpeechError("audio_api_timeout") from exc
        except httpx.HTTPError as exc:
            raise SpeechError("audio_api_unavailable") from exc
        except (ValueError, TypeError) as exc:
            raise SpeechError("audio_api_invalid_response") from exc

    def recognize(self, path: Path) -> dict:
        if not self.configured:
            return {"error": "aliyun_audio_not_configured"}
        try:
            with tempfile.TemporaryDirectory(prefix="wechat-asr-") as directory:
                wav = Path(directory) / "audio.wav"
                duration = decode_to_wave(path, wav, self.settings)
                encoded = base64.b64encode(wav.read_bytes()).decode("ascii")
            result = self.request("/services/aigc/multimodal-generation/generation", {
                "model": self.settings.asr_model,
                "input": {"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {
                    "data": "data:audio/wav;base64," + encoded}}]}]},
                "parameters": {"format": "wav", "sample_rate": "16000", "keep_dialect": True},
            })
            output = result.get("output")
            text = output.get("text") if isinstance(output, dict) else None
            if not isinstance(text, str) or len(text) > 4000:
                raise SpeechError("asr_invalid_transcript")
            return {"text": text.strip(), "duration_seconds": duration, "model": self.settings.asr_model, "provider": "aliyun"}
        except SpeechError as exc:
            return {"error": exc.code}

    def download(self, url: str) -> bytes:
        value = urlsplit(url)
        host = value.hostname or ""
        if (value.scheme not in {"http", "https"} or value.username or value.password
                or not any(host.endswith(suffix) for suffix in (".aliyuncs.com", ".qwencloud.com"))):
            raise SpeechError("tts_audio_url_invalid")
        # OSS's documented response may contain http; the same object and
        # signature can be fetched over HTTPS without forwarding our API key.
        url = urlunsplit(("https", value.netloc, value.path, value.query, ""))
        try:
            with self._client() as client, client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise SpeechError("tts_audio_download_failed")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_SPEECH_BYTES:
                        raise SpeechError("speech_audio_size_limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise SpeechError("tts_audio_download_failed") from exc


@dataclass(frozen=True)
class SpeechClip:
    path: str
    duration_seconds: float
    model: str
    voice: str
    reused: bool = False
    mode: str = "speech"


class SpeechReplies:
    def __init__(self, settings, *, api=None):
        self.settings = settings
        self.api = api or AliyunAudio(settings)
        self._lock = threading.Lock()

    def should_offer(
        self,
        event,
        *,
        requested: bool = False,
        request_kind: str = "",
        declined: bool = False,
    ) -> bool:
        if (not self.settings.voice_reply_enabled or not self.api.configured
                or declined or voice_reply_declined(event.text)):
            return False
        request_kind = str(request_kind or "").strip().casefold()
        if request_kind == "singing" and not getattr(self.settings, "singing_reply_enabled", True):
            # ``voice_reply_enabled`` controls speech in general; singing has
            # its own switch so a point-song request cannot silently consume a
            # normal voice opportunity when singing is disabled.
            return False
        # An addressed request to hear us is a delivery request, not another
        # random opportunity. Zero disables random speech, not explicit asks.
        if requested:
            return True
        if self.settings.voice_reply_probability <= 0:
            return False
        identity = event.external_message_id or (event.text + event.observed_at.isoformat())
        value = int.from_bytes(hashlib.sha256((event.account_id + "\0" + event.group_id + "\0voice\0" + identity).encode()).digest()[:8], "big") / 2**64
        return value < self.settings.voice_reply_probability

    def prepare(self, text: str, *, account_id: str, group_id: str, mode: str = "speech") -> SpeechClip:
        text = text.strip()
        if not text or len(text) > 300 or re.search(r"<[^>]+>|https?://", text):
            raise SpeechError("tts_text_not_suitable")
        mode = "singing" if mode == "singing" else "speech"
        if mode == "singing" and not getattr(self.settings, "singing_reply_enabled", True):
            raise SpeechError("singing_disabled")
        settings = self.settings
        synthesis_input = {"text": text, "voice": settings.tts_voice, "format": "wav", "sample_rate": 24000,
                           "rate": settings.tts_rate, "pitch": settings.tts_pitch, "seed": settings.tts_seed}
        instruction = settings.tts_singing_instruction if mode == "singing" else settings.tts_instruction
        if instruction.strip():
            synthesis_input["instruction"] = instruction
        if settings.tts_language != "auto":
            synthesis_input["language_hints"] = [settings.tts_language]
        # Keep ordinary speech cache keys stable.  Singing gets its own
        # algorithm version so clips made by the old contour are never reused.
        cache_version = (3 + MELODY_ALGORITHM_VERSION) if mode == "singing" else 3
        fingerprint = json.dumps([cache_version, mode, settings.aliyun_audio_base_url, settings.tts_model, synthesis_input],
                                 ensure_ascii=False, sort_keys=True)
        scope = hashlib.sha256((account_id + "\0" + group_id).encode()).hexdigest()[:16]
        target = settings.voice_reply_cache_dir / scope / (hashlib.sha256(fingerprint.encode()).hexdigest() + ".wav")
        with self._lock:
            try:
                return SpeechClip(str(target), wave_duration(target), settings.tts_model, settings.tts_voice, True, mode)
            except SpeechError:
                pass
            result = self.api.request("/services/audio/tts/SpeechSynthesizer", {
                "model": settings.tts_model, "input": synthesis_input,
            })
            output = result.get("output")
            audio = output.get("audio") if isinstance(output, dict) else None
            if not isinstance(audio, dict) or not isinstance(audio.get("url"), str) or not audio["url"]:
                raise SpeechError("tts_audio_missing")
            data = complete_tts_wave(self.api.download(audio["url"]))
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                os.chmod(target.parent, 0o700)
            temporary = target.parent / (uuid.uuid4().hex[:16] + ".part")
            singing_temporary = target.parent / (uuid.uuid4().hex[:16] + ".singing.part") if mode == "singing" else None
            try:
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                    stream.write(data)
                output_path = temporary
                if singing_temporary is not None:
                    _apply_melody_contour(temporary, singing_temporary)
                    output_path = singing_temporary
                duration = wave_duration(output_path)
                output_path.replace(target)
                return SpeechClip(str(target), duration, settings.tts_model, settings.tts_voice, False, mode)
            finally:
                temporary.unlink(missing_ok=True)
                if singing_temporary is not None:
                    singing_temporary.unlink(missing_ok=True)
