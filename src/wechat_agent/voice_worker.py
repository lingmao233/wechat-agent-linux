"""Private JSON-lines worker. Heavy codecs and the ASR model load only here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import wave


def silk_duration(data: bytes, max_seconds: float) -> float:
    if not data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        raise ValueError("invalid_silk")
    offset = 10 if data.startswith(b"\x02#!SILK_V3") else 9
    frames = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("invalid_silk")
        size = struct.unpack_from("<h", data, offset)[0]
        offset += 2
        if size == -1 and offset == len(data):
            break
        if size <= 0 or offset + size > len(data):
            raise ValueError("invalid_silk")
        frames += 1
        if frames * 0.02 > max_seconds:
            raise ValueError("audio_duration_limit")
        offset += size
    if frames == 0:
        raise ValueError("invalid_silk")
    return frames * 0.02


def decode_audio(path: Path, *, max_seconds: float, max_bytes: int):
    import numpy as np

    if not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
        raise ValueError("audio_size_limit")
    with path.open("rb") as source:
        head = source.read(10)
    if head.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        import pilk

        silk_duration(path.read_bytes(), max_seconds)
        with tempfile.TemporaryDirectory(prefix="wechat-voice-") as directory:
            pcm = Path(directory) / "voice.pcm"
            pilk.decode(str(path), str(pcm), pcm_rate=16000)
            if not 0 < pcm.stat().st_size <= int(max_seconds * 16000) * 2:
                raise ValueError("audio_duration_limit")
            return np.frombuffer(pcm.read_bytes(), dtype="<i2").astype(np.float32) / 32768
    import av

    # Resample incrementally, stopping before a tiny compressed file expands
    # into hours of PCM. Decode in the child so a broken codec is cancellable.
    frames, samples = [], 0
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                samples += converted.samples
                if samples > max_seconds * 16000:
                    raise ValueError("audio_duration_limit")
                frames.append(converted.to_ndarray().flatten())
        for converted in resampler.resample(None):
            samples += converted.samples
            if samples > max_seconds * 16000:
                raise ValueError("audio_duration_limit")
            frames.append(converted.to_ndarray().flatten())
    if not frames:
        raise ValueError("audio_invalid")
    return np.concatenate(frames)


class Worker:
    def __init__(self):
        self._models = {}

    def transcribe(self, job: dict) -> dict:
        try:
            max_seconds = min(300, max(1, float(job["max_seconds"])))
            max_bytes = min(20_000_000, max(1024, int(job["max_bytes"])))
            audio = decode_audio(Path(job["path"]), max_seconds=max_seconds, max_bytes=max_bytes)
            if job.get("operation") == "decode_wav":
                import numpy as np
                output = Path(job["output"])
                with output.open("xb") as stream:
                    if os.name == "posix":
                        os.chmod(output, 0o600)
                    with wave.open(stream, "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(16000)
                        wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
                return {"duration_seconds": len(audio) / 16000}
            from faster_whisper import WhisperModel

            model_name = str(job["model"])
            if model_name not in self._models:
                self._models[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8",
                                                        cpu_threads=min(4, os.cpu_count() or 1))
            language = str(job.get("language") or "auto")
            segments, info = self._models[model_name].transcribe(
                audio, language=None if language == "auto" else language,
                beam_size=5, vad_filter=True, condition_on_previous_text=False,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            parts = []
            for segment in segments:
                # Do not send likely non-speech hallucinations to the chat model.
                if segment.no_speech_prob > 0.8 and segment.avg_logprob < -1:
                    continue
                parts.append(segment.text)
                if sum(map(len, parts)) > 4000:
                    raise ValueError("transcript_size_limit")
            return {"text": "".join(parts).strip(), "duration_seconds": len(audio) / 16000,
                    "language": info.language}
        except ImportError:
            return {"error": "voice_dependencies_missing"}
        except ValueError as exc:
            code = str(exc)
            return {"error": code if code in {"audio_size_limit", "audio_duration_limit", "invalid_silk", "audio_invalid", "transcript_size_limit"} else "audio_invalid"}
        except Exception:
            return {"error": "recognition_failed"}


def main():
    worker = Worker()
    for line in sys.stdin:
        try:
            result = worker.transcribe(json.loads(line)) if len(line) <= 8192 else {"error": "invalid_job"}
        except (ValueError, KeyError, TypeError):
            result = {"error": "invalid_job"}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
