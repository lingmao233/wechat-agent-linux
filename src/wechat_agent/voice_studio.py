"""User-created TTS voices behind a provider-neutral voice studio.

The plugin talks to the console over the same authenticated JSON API.  This
module keeps the provider-neutral parts (upload validation, local profile store,
status polling, preview framing) on the VM; provider-specific cloning and
synthesis live in ``wechat_agent.tts``.  Reference audio never leaves the
profile directory except for the provider request the user explicitly starts.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from .speech import SpeechError, wave_duration
from .tts.base import VoiceProviderError
from .tts.registry import build_provider

MAX_SAMPLE_BYTES = 10_000_000
MAX_PROFILES = 20
MIN_SAMPLE_SECONDS = 5.0
MAX_SAMPLE_SECONDS = 30.0
MAX_PREVIEW_CHARS = 120
MAX_PREVIEW_AUDIO_BYTES = 8_000_000
PROFILE_ID_RE = re.compile(r"^[a-f0-9]{12}$")
LANGUAGE_HINTS = frozenset({"zh", "en", "ja"})
_STORE_LOCK = threading.RLock()
_CREATE_LOCK = threading.Lock()


class VoiceProfileError(RuntimeError):
    """A safe, user-facing error code; never include provider text or audio."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_root(settings) -> Path:
    value = getattr(settings, "voice_profiles_dir", None)
    if value is None:
        value = Path(settings.voice_reply_cache_dir).expanduser().parent / "voice-profiles"
    return Path(value).expanduser()


def _ensure_root(settings) -> Path:
    root = _profile_root(settings)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    return root


def _write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _clean_name(value: object) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:24]


def _language(value: object, settings) -> str:
    candidate = str(value or "").strip().casefold()
    if candidate in LANGUAGE_HINTS:
        return candidate
    configured = str(getattr(settings, "tts_language", "auto") or "auto").casefold()
    return configured if configured in LANGUAGE_HINTS else "zh"


def _decode_upload(value: object) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise VoiceProfileError("voice_audio_missing")
    text = value.strip()
    if text.startswith("data:"):
        try:
            _, encoded = text.split(",", 1)
        except ValueError as exc:
            raise VoiceProfileError("voice_audio_invalid") from exc
        if ";base64" not in text.split(",", 1)[0].casefold():
            raise VoiceProfileError("voice_audio_invalid")
        text = encoded
    text = re.sub(r"\s+", "", text)
    # Base64 expands by 4/3; reject early so a hostile body never allocates
    # much beyond the upload ceiling.
    if len(text) > (MAX_SAMPLE_BYTES * 4 // 3) + 16:
        raise VoiceProfileError("voice_audio_too_large")
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceProfileError("voice_audio_invalid") from exc
    if not data:
        raise VoiceProfileError("voice_audio_missing")
    if len(data) > MAX_SAMPLE_BYTES:
        raise VoiceProfileError("voice_audio_too_large")
    return data


def _ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _valid_wav(path: Path) -> bool:
    try:
        wave_duration(path, max_seconds=MAX_SAMPLE_SECONDS)
        return True
    except SpeechError:
        return False


def _normalize_sample(data: bytes, settings) -> tuple[bytes, float]:
    """Return a 24 kHz mono PCM WAV and its duration."""
    root = _ensure_root(settings)
    with tempfile.TemporaryDirectory(prefix=".voice-upload-", dir=str(root)) as directory:
        work = Path(directory)
        source = work / "source"
        source.write_bytes(data)
        if _valid_wav(source):
            # A clean PCM WAV needs no external process.  Clone only after the
            # duration/format checks so malformed files cannot reach ffmpeg.
            duration = wave_duration(source, max_seconds=MAX_SAMPLE_SECONDS)
            if not MIN_SAMPLE_SECONDS <= duration <= MAX_SAMPLE_SECONDS:
                raise VoiceProfileError("voice_audio_duration")
            return source.read_bytes(), duration
        executable = _ffmpeg()
        if executable is None:
            raise VoiceProfileError("voice_ffmpeg_missing")
        target = work / "normalized.wav"
        try:
            completed = subprocess.run(
                [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-i", str(source), "-vn", "-ac", "1", "-ar", "24000",
                 "-c:a", "pcm_s16le", str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceProfileError("voice_audio_transcode_failed") from exc
        if completed.returncode != 0 or not target.is_file():
            raise VoiceProfileError("voice_audio_transcode_failed")
        try:
            duration = wave_duration(target)
        except SpeechError as exc:
            raise VoiceProfileError("voice_audio_duration") from exc
        if not MIN_SAMPLE_SECONDS <= duration <= MAX_SAMPLE_SECONDS:
            raise VoiceProfileError("voice_audio_duration")
        return target.read_bytes(), duration


def _provider_code(value: object) -> str:
    code = str(value or "voice_provider_error")
    if code.startswith("voice_provider_") and code not in {"voice_provider_error"}:
        return code
    return {
        "tts_provider_auth": "voice_provider_auth",
        "tts_provider_quota": "voice_provider_quota",
        "tts_provider_config": "voice_provider_config",
        "tts_provider_unavailable": "voice_provider_unavailable",
        "tts_provider_timeout": "voice_provider_unavailable",
        "tts_provider_unknown": "voice_provider_config",
        "tts_provider_clone_unsupported": "voice_provider_clone_unsupported",
        "tts_provider_rejected": "voice_provider_error",
        "voice_model_unsupported": "voice_model_unsupported",
    }.get(code, "voice_provider_error")


def _preview_duration(data: bytes) -> float:
    with tempfile.TemporaryDirectory(prefix=".voice-preview-") as directory:
        path = Path(directory) / "preview.wav"
        path.write_bytes(data)
        try:
            return wave_duration(path)
        except SpeechError as exc:
            raise VoiceProfileError("voice_preview_audio_invalid") from exc


class VoiceProfiles:
    """Persistent metadata for user-created voices.

    Provider voice IDs are written to ``TTS_VOICE`` only when the user activates
    a profile.  The sample file is stored locally for re-enrollment/audition and
    is removed together with its profile.
    """

    def __init__(self, settings, *, api=None, provider=None):
        self.settings = settings
        if provider is not None:
            self.provider = provider
        elif api is not None:
            from .tts.aliyun import AliyunTTSProvider
            self.provider = AliyunTTSProvider(settings, api=api)
        else:
            self.provider = build_provider(settings)

    @property
    def configured(self) -> bool:
        return bool(getattr(self.provider, "configured", False))

    def _metadata_path(self) -> Path:
        return _ensure_root(self.settings) / "profiles.json"

    def _read(self) -> dict[str, object]:
        path = self._metadata_path()
        if not path.is_file():
            return {"version": 1, "profiles": []}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VoiceProfileError("voice_profile_store_invalid") from exc
        if not isinstance(value, dict) or not isinstance(value.get("profiles"), list):
            raise VoiceProfileError("voice_profile_store_invalid")
        return value

    def _write(self, value: dict[str, object]) -> None:
        root = _ensure_root(self.settings)
        payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        _write_private(root / "profiles.json", payload)

    def _public(self, record: dict[str, object], current_voice: str) -> dict[str, object]:
        voice_id = str(record.get("voice_id") or "")
        current_model = str(getattr(self.settings, "tts_model", "") or "")
        record_model = str(record.get("model") or "")
        current_provider = str(getattr(self.settings, "tts_provider", "aliyun") or "aliyun")
        record_provider = str(record.get("provider") or "aliyun")
        return {
            "id": str(record.get("id") or ""),
            "name": str(record.get("name") or ""),
            "provider": record_provider,
            "status": str(record.get("status") or "unknown"),
            "active": bool(voice_id and voice_id == current_voice),
            "created_at": str(record.get("created_at") or ""),
            "sample_seconds": round(float(record.get("sample_seconds") or 0), 1),
            "error": str(record.get("error") or ""),
            "model_mismatch": bool(record_model and record_model != current_model),
            "provider_mismatch": bool(record_provider != current_provider),
        }

    def snapshot(self) -> dict[str, object]:
        with _STORE_LOCK:
            value = self._read()
        current_voice = str(getattr(self.settings, "tts_voice", "") or "")
        records = [record for record in value.get("profiles", []) if isinstance(record, dict)]
        public = [self._public(record, current_voice) for record in records]
        public.sort(key=lambda item: (not item["active"], not bool(item["created_at"]), item["created_at"]), reverse=False)
        current_profile = next((item for item in public if item["active"]), None)
        capabilities = self.provider.capabilities
        provider_configured = self.configured
        model_supported = bool(self.provider.model_supported())
        presets = self.provider.list_presets() if capabilities.presets else []
        return {
            "profiles": public,
            "current_voice": current_voice,
            "current_profile_id": current_profile["id"] if current_profile else "",
            "current_name": current_profile["name"] if current_profile else "",
            "model": str(getattr(self.settings, "tts_model", "") or ""),
            "provider": self.provider.provider_id,
            "provider_label": capabilities.label,
            "capabilities": capabilities.public(),
            "presets": presets,
            "supported": bool(provider_configured and model_supported),
            "configured": provider_configured,
            "limits": {
                "max_bytes": MAX_SAMPLE_BYTES,
                "min_seconds": MIN_SAMPLE_SECONDS,
                "max_seconds": capabilities.max_sample_seconds,
                "max_profiles": MAX_PROFILES,
            },
        }

    def _find(self, records: list[dict[str, object]], profile_id: str) -> dict[str, object]:
        profile_id = str(profile_id or "")
        if not PROFILE_ID_RE.fullmatch(profile_id):
            raise VoiceProfileError("voice_profile_not_found")
        for record in records:
            if str(record.get("id") or "") == profile_id:
                return record
        raise VoiceProfileError("voice_profile_not_found")

    def _query_status(self, voice_id: str) -> str:
        try:
            return self.provider.query_status(voice_id)
        except VoiceProviderError as exc:
            raise VoiceProfileError(_provider_code(exc.code)) from exc

    def _refresh_record(self, record: dict[str, object], *, strict: bool = False) -> dict[str, object]:
        voice_id = str(record.get("voice_id") or "")
        if not voice_id:
            raise VoiceProfileError("voice_profile_not_found")
        try:
            status = self._query_status(voice_id)
        except VoiceProfileError as exc:
            if strict or exc.code not in {"voice_provider_error", "voice_provider_unavailable"}:
                raise
            record["status"] = "deploying"
            return record
        if status == "ready":
            record["status"] = "ready"
            record["error"] = ""
        elif status == "failed":
            record["status"] = "failed"
            record["error"] = "voice_enrollment_not_approved"
        else:
            record["status"] = "deploying"
        record["updated_at"] = _now()
        return record

    def _poll_ready(self, record: dict[str, object], seconds: float = 20.0) -> dict[str, object]:
        deadline = time.monotonic() + seconds
        while True:
            record = self._refresh_record(record)
            if record["status"] != "deploying":
                return record
            if time.monotonic() >= deadline:
                return record
            time.sleep(0.75)

    def create(self, payload: dict[str, object]) -> dict[str, object]:
        if not _CREATE_LOCK.acquire(blocking=False):
            raise VoiceProfileError("voice_profile_busy")
        try:
            model = str(getattr(self.settings, "tts_model", "") or "")
            if not self.provider.model_supported():
                raise VoiceProfileError("voice_model_unsupported")
            if not self.configured:
                raise VoiceProfileError("voice_provider_config")
            if not self.provider.capabilities.can_clone:
                raise VoiceProfileError("voice_provider_clone_unsupported")
            name = _clean_name(payload.get("name")) or "我的声音"
            audio, sample_seconds = _normalize_sample(_decode_upload(payload.get("audio")), self.settings)
            language = _language(payload.get("language"), self.settings)
            with _STORE_LOCK:
                value = self._read()
                records = [record for record in value.get("profiles", []) if isinstance(record, dict)]
                if len(records) >= MAX_PROFILES:
                    raise VoiceProfileError("voice_profile_limit")
            profile_id = uuid.uuid4().hex[:12]
            try:
                result = self.provider.clone(audio, name=name, language=language)
            except VoiceProviderError as exc:
                raise VoiceProfileError(_provider_code(exc.code)) from exc
            voice_id = str(result.get("voice_id") or "").strip()
            if not voice_id or len(voice_id) > 240:
                raise VoiceProfileError("voice_provider_error")
            initial_status = str(result.get("status") or "deploying")
            if initial_status not in {"ready", "deploying", "failed"}:
                initial_status = "deploying"
            sample_name = profile_id + ".wav"
            _write_private(_ensure_root(self.settings) / sample_name, audio)
            record: dict[str, object] = {
                "id": profile_id,
                "name": name,
                "provider": self.provider.provider_id,
                "voice_id": voice_id,
                "model": model,
                "status": initial_status,
                "error": "voice_enrollment_not_approved" if initial_status == "failed" else "",
                "created_at": _now(),
                "updated_at": _now(),
                "sample_seconds": round(sample_seconds, 1),
                "sample_file": sample_name,
                "language": language,
            }
            with _STORE_LOCK:
                value = self._read()
                records = [item for item in value.get("profiles", []) if isinstance(item, dict)]
                records.append(record)
                value["profiles"] = records
                self._write(value)
            if initial_status == "deploying":
                record = self._poll_ready(record)
            with _STORE_LOCK:
                value = self._read()
                records = [item for item in value.get("profiles", []) if isinstance(item, dict)]
                for index, item in enumerate(records):
                    if str(item.get("id") or "") == profile_id:
                        records[index] = record
                value["profiles"] = records
                self._write(value)
            return self._public(record, str(getattr(self.settings, "tts_voice", "") or ""))
        finally:
            _CREATE_LOCK.release()

    def refresh(self, profile_id: str) -> dict[str, object]:
        with _STORE_LOCK:
            value = self._read()
            records = [item for item in value.get("profiles", []) if isinstance(item, dict)]
            record = self._find(records, profile_id)
            current_provider = str(getattr(self.settings, "tts_provider", "aliyun") or "aliyun")
            if str(record.get("provider") or "aliyun") != current_provider:
                raise VoiceProfileError("voice_profile_provider_mismatch")
            record = self._refresh_record(record, strict=True)
            value["profiles"] = records
            self._write(value)
        return self._public(record, str(getattr(self.settings, "tts_voice", "") or ""))

    def import_current(self, payload: dict[str, object]) -> dict[str, object]:
        """Adopt a voice ID that was configured before this feature existed."""
        if not self.configured:
            raise VoiceProfileError("voice_provider_config")
        if not self.provider.capabilities.can_clone:
            raise VoiceProfileError("voice_provider_clone_unsupported")
        current_voice = str(getattr(self.settings, "tts_voice", "") or "").strip()
        if not current_voice:
            raise VoiceProfileError("voice_profile_not_found")
        with _STORE_LOCK:
            value = self._read()
            records = [item for item in value.get("profiles", []) if isinstance(item, dict)]
            for record in records:
                if str(record.get("voice_id") or "") == current_voice:
                    return self._public(record, current_voice)
        try:
            status = self._query_status(current_voice)
        except VoiceProfileError as exc:
            if exc.code not in {"voice_provider_error", "voice_provider_unavailable"}:
                raise
            status = "deploying"
        if status == "ready":
            state, error = "ready", ""
        elif status == "failed":
            state, error = "failed", "voice_enrollment_not_approved"
        else:
            state, error = "deploying", ""
        record = {
            "id": uuid.uuid4().hex[:12],
            "name": _clean_name(payload.get("name")) or "当前音色",
            "provider": self.provider.provider_id,
            "voice_id": current_voice,
            "model": str(getattr(self.settings, "tts_model", "") or ""),
            "status": state,
            "error": error,
            "created_at": _now(),
            "updated_at": _now(),
            "sample_seconds": 0,
            "sample_file": "",
            "language": _language(self.settings.tts_language, self.settings),
        }
        with _STORE_LOCK:
            value = self._read()
            records = [item for item in value.get("profiles", []) if isinstance(item, dict)]
            records.append(record)
            value["profiles"] = records
            self._write(value)
        return self._public(record, current_voice)

    def activate(self, profile_id: str) -> tuple[dict[str, object], str]:
        with _STORE_LOCK:
            value = self._read()
            records = [item for item in value.get("profiles", []) if isinstance(item, dict)]
            record = self._find(records, profile_id)
        if str(record.get("status") or "") != "ready":
            raise VoiceProfileError("voice_enrollment_pending")
        current_provider = str(getattr(self.settings, "tts_provider", "aliyun") or "aliyun")
        record_provider = str(record.get("provider") or "aliyun")
        if record_provider != current_provider:
            raise VoiceProfileError("voice_profile_provider_mismatch")
        if str(record.get("model") or "") != str(getattr(self.settings, "tts_model", "") or ""):
            raise VoiceProfileError("voice_profile_model_mismatch")
        voice_id = str(record.get("voice_id") or "")
        if not voice_id:
            raise VoiceProfileError("voice_profile_not_found")
        return self._public(record, voice_id), voice_id

    def delete(self, profile_id: str) -> dict[str, object]:
        current_voice = str(getattr(self.settings, "tts_voice", "") or "")
        with _STORE_LOCK:
            value = self._read()
            records = [item for item in value.get("profiles", []) if isinstance(item, dict)]
            record = self._find(records, profile_id)
            if str(record.get("voice_id") or "") == current_voice:
                raise VoiceProfileError("voice_profile_active")
            records = [item for item in records if str(item.get("id") or "") != profile_id]
            value["profiles"] = records
            self._write(value)
            sample_file = str(record.get("sample_file") or "")
            if sample_file and PROFILE_ID_RE.fullmatch(sample_file.removesuffix(".wav")):
                try:
                    (_ensure_root(self.settings) / sample_file).unlink(missing_ok=True)
                except OSError:
                    pass
        return {"deleted": profile_id}

    def preview_current(self, text: object) -> dict[str, object]:
        """Audition the configured voice without requiring a cloned profile."""
        if not self.configured:
            raise VoiceProfileError("voice_provider_config")
        if not self.provider.capabilities.preview:
            raise VoiceProfileError("voice_preview_unsupported")
        content = str(text or "").strip()
        if not content:
            content = "你好呀，我是你的微信助手。今天也要开心。"
        if len(content) > MAX_PREVIEW_CHARS or re.search(r"<[^>]+>|https?://", content):
            raise VoiceProfileError("voice_preview_text_invalid")
        try:
            data = self.provider.synthesize(content)
        except VoiceProviderError as exc:
            raise VoiceProfileError(_provider_code(exc.code)) from exc
        if not data or len(data) > MAX_PREVIEW_AUDIO_BYTES:
            raise VoiceProfileError("voice_preview_audio_invalid")
        duration = _preview_duration(data)
        return {
            "audio": "data:audio/wav;base64," + base64.b64encode(data).decode("ascii"),
            "duration_seconds": round(duration, 2),
            "text": content,
        }

    def preview(self, profile_id: str, text: object) -> dict[str, object]:
        if not self.configured:
            raise VoiceProfileError("voice_provider_config")
        with _STORE_LOCK:
            value = self._read()
            records = [item for item in value.get("profiles", []) if isinstance(item, dict)]
            record = self._find(records, profile_id)
        if str(record.get("status") or "") != "ready":
            raise VoiceProfileError("voice_enrollment_pending")
        current_provider = str(getattr(self.settings, "tts_provider", "aliyun") or "aliyun")
        if str(record.get("provider") or "aliyun") != current_provider:
            raise VoiceProfileError("voice_profile_provider_mismatch")
        if str(record.get("model") or "") != str(getattr(self.settings, "tts_model", "") or ""):
            raise VoiceProfileError("voice_profile_model_mismatch")
        content = str(text or "").strip()
        if not content:
            content = "你好呀，我是你的微信助手。今天也要开心。"
        if len(content) > MAX_PREVIEW_CHARS or re.search(r"<[^>]+>|https?://", content):
            raise VoiceProfileError("voice_preview_text_invalid")
        voice_id = str(record.get("voice_id") or "")
        try:
            data = self.provider.synthesize(content, voice=voice_id)
        except VoiceProviderError as exc:
            raise VoiceProfileError(_provider_code(exc.code)) from exc
        if not data or len(data) > MAX_PREVIEW_AUDIO_BYTES:
            raise VoiceProfileError("voice_preview_audio_invalid")
        duration = _preview_duration(data)
        return {
            "audio": "data:audio/wav;base64," + base64.b64encode(data).decode("ascii"),
            "duration_seconds": round(duration, 2),
            "text": content,
        }
