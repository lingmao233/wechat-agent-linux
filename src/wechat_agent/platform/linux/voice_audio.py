"""Feed a completed WAV to one verified WeChat recording through PulseAudio."""
from __future__ import annotations

from array import array
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import wave

from ...speech import wave_duration
from ..base import BackendError

log = logging.getLogger(__name__)


def trim_leading_silence(path: Path) -> float:
    """Trim quiet TTS pre-roll in the private send copy, keeping 40 ms padding."""
    duration = wave_duration(path)
    with wave.open(str(path), "rb") as audio:
        channels, rate = audio.getnchannels(), audio.getframerate()
        frames = audio.getnframes()
        content = audio.readframes(frames)
    samples = array("h")
    samples.frombytes(content)
    if sys.byteorder != "little":
        samples.byteswap()
    block_frames = max(1, rate // 100)
    block_samples = block_frames * channels
    start = None
    # Ignore isolated encoder clicks before speech. Keep soft onsets by using
    # a low threshold, requiring 30 ms of sound and retaining 40 ms before it.
    threshold = (32768 * 10 ** (-55 / 20)) ** 2
    audible_blocks = 0
    candidate = 0
    for offset in range(0, len(samples), block_samples):
        part = samples[offset:offset + block_samples]
        if part and sum(value * value for value in part) / len(part) > threshold:
            if not audible_blocks:
                candidate = offset // channels
            audible_blocks += 1
            if audible_blocks >= 3:
                start = candidate
                break
        else:
            audible_blocks = 0
    if start is None:
        return duration
    cut = max(0, start - int(rate * .04))
    if not cut:
        return duration
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(content[cut * channels * 2:])
    return wave_duration(path)


class VirtualMicrophone:
    def __init__(self, wechat_pid: int):
        self.wechat_pid = wechat_pid
        self.name = "wechat_agent_voice_" + uuid.uuid4().hex[:12]
        self.source = self.name + "_mic"
        self.modules = []
        self.default_source = ""
        self.lock = None
        self.process = None
        self.baseline = set()
        self.env = dict(os.environ, LC_ALL="C")
        if os.name == "posix" and not self.env.get("PULSE_SERVER"):
            socket = Path(f"/run/user/{os.getuid()}/pulse/native")
            if socket.is_socket():
                self.env["PULSE_SERVER"] = "unix:" + str(socket)

    @staticmethod
    def available() -> bool:
        return os.name == "posix" and bool(shutil.which("pactl") and shutil.which("paplay"))

    def command(self, *args, json_result=False):
        try:
            result = subprocess.run(["pactl", *args], env=self.env, capture_output=True, text=True,
                                    timeout=5, check=True).stdout.strip()
            return json.loads(result) if json_result else result
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise BackendError("VOICE_AUDIO_UNAVAILABLE", "无法连接桌面音频服务或配置虚拟麦克风") from exc

    def streams(self):
        result = self.command("-f", "json", "list", "source-outputs", json_result=True)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise BackendError("VOICE_AUDIO_UNAVAILABLE", "无法核实录音流")
        return result

    @staticmethod
    def _application_stream(stream) -> bool:
        return stream.get("client") is not None or bool(stream.get("properties", {}).get("application.process.id"))

    def __enter__(self):
        if not self.available():
            raise BackendError("VOICE_AUDIO_DEPENDENCY_MISSING", "原生语音需要 pactl 和 paplay（pulseaudio-utils）")
        import fcntl
        try:
            descriptor = os.open(f"/run/user/{os.getuid()}/wechat-agent-voice.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self.lock = os.fdopen(descriptor, "w")
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.close()
            raise BackendError("VOICE_AUDIO_BUSY", "另一个语音任务正在使用桌面音频") from exc
        try:
            streams = self.streams()
            if any(self._application_stream(stream) for stream in streams):
                raise BackendError("VOICE_AUDIO_BUSY", "已有程序正在录音或通话，稍后再试")
            self.baseline = {str(stream["index"]) for stream in streams}
            self.default_source = self.command("get-default-source")
            for arguments in [
                ("module-null-sink", "sink_name=" + self.name, "rate=24000", "channels=1",
                 "sink_properties=device.description=WeChatAgentVoice"),
                ("module-remap-source", "master=" + self.name + ".monitor", "source_name=" + self.source,
                 "channels=1", "source_properties=device.description=WeChatAgentVoiceMic"),
            ]:
                identifier = self.command("load-module", *arguments)
                if not identifier.isdigit():
                    raise BackendError("VOICE_AUDIO_UNAVAILABLE", "虚拟麦克风模块没有有效编号")
                self.modules.append(identifier)
            self.command("set-source-mute", self.source, "0")
            self.command("set-source-volume", self.source, "100%")
            self.command("set-default-source", self.source)
            return self
        except BaseException:
            self.close()
            raise

    def _owned_by_wechat(self, stream) -> bool:
        identifier = str(stream.get("properties", {}).get("application.process.id", ""))
        if not identifier.isdigit():
            return False
        pid = int(identifier)
        for _ in range(12):
            if pid == self.wechat_pid:
                return True
            try:
                status = Path(f"/proc/{pid}/status").read_text()
                pid = int(next(line.split(":", 1)[1] for line in status.splitlines() if line.startswith("PPid:")))
            except (OSError, ValueError, StopIteration):
                return False
            if pid <= 1:
                return False
        return False

    def bind_recording(self, guard):
        deadline = time.monotonic() + 3
        while True:
            guard()
            streams = [stream for stream in self.streams() if str(stream["index"]) not in self.baseline
                       and self._application_stream(stream)]
            candidates = [stream for stream in streams if self._owned_by_wechat(stream)]
            if len(candidates) == 1 and len(streams) == 1:
                identifier = str(candidates[0]["index"])
                sources = self.command("-f", "json", "list", "sources", json_result=True)
                expected = [source["index"] for source in sources if source.get("name") == self.source]
                if len(expected) != 1:
                    raise BackendError("VOICE_CAPTURE_UNCONFIRMED", "微信录音未连接到指定虚拟麦克风")
                self.recording_id = identifier
                self.recording_source = str(expected[0])
                # PipeWire moves following streams when the default changes.
                # Mute during that transition, restore the desktop default,
                # then explicitly pin this exact recording to our source.
                self.command("set-source-output-mute", identifier, "1")
                self._restore_default()
                self.command("move-source-output", identifier, self.source)
                self.command("set-source-output-volume", identifier, "100%")
                stable_since, last_move = 0.0, time.monotonic()
                while True:
                    guard()
                    now = time.monotonic()
                    actual = [stream for stream in self.streams()
                              if str(stream["index"]) == identifier and self._owned_by_wechat(stream)]
                    if (len(actual) == 1 and str(actual[0].get("source")) == self.recording_source
                            and actual[0].get("corked") is False):
                        stable_since = stable_since or now
                        if now - stable_since >= 0.2:
                            break
                    else:
                        stable_since = 0.0
                        # The default-source notification can arrive after
                        # the first move. Re-pin while muted until it settles.
                        if len(actual) == 1 and now - last_move >= 0.1:
                            self.command("move-source-output", identifier, self.source)
                            last_move = now
                    if now >= deadline:
                        raise BackendError("VOICE_CAPTURE_UNCONFIRMED", "微信录音未开始从指定虚拟麦克风采集")
                    time.sleep(0.05)
                self.command("set-source-output-mute", identifier, "0")
                self.confirm_recording()
                return
            if streams:
                raise BackendError("VOICE_AUDIO_BUSY", "录音流来源或数量不符合本次微信录音")
            if time.monotonic() >= deadline:
                raise BackendError("VOICE_CAPTURE_UNCONFIRMED", "微信没有开启可核实的录音流")
            time.sleep(0.1)

    def recording_active(self) -> bool:
        return any(self._application_stream(stream) and self._owned_by_wechat(stream)
                   and (not getattr(self, "recording_id", "") or str(stream["index"]) == self.recording_id)
                   for stream in self.streams())

    def confirm_recording(self):
        candidates = [stream for stream in self.streams()
                      if str(stream["index"]) == self.recording_id and self._owned_by_wechat(stream)]
        if (len(candidates) != 1 or candidates[0].get("corked") is not False
                or str(candidates[0].get("source")) != self.recording_source):
            raise BackendError("VOICE_CAPTURE_LOST", "微信录音流已关闭、暂停或切换麦克风，语音未发送")

    def play(self, path: Path, guard):
        duration = wave_duration(path)
        guard()
        self.process = subprocess.Popen(["paplay", "--device=" + self.name,
                                         "--latency-msec=40", "--process-time-msec=10",
                                         "--client-name=WeChatAgentVoice", str(path)],
                                        env=self.env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + duration + 3
        try:
            while self.process.poll() is None:
                guard()
                # A closed or replaced recording must not consume the rest of
                # the clip and then click an unrelated Send button.
                self.confirm_recording()
                if time.monotonic() > deadline:
                    raise BackendError("VOICE_PLAYBACK_TIMEOUT", "音频没有在预期时间内完整播放")
                time.sleep(0.15)
            if self.process.returncode:
                raise BackendError("VOICE_PLAYBACK_FAILED", "虚拟麦克风播放失败")
            guard()
            self.confirm_recording()
        finally:
            self._stop_playback()

    def _stop_playback(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None

    def _restore_default(self):
        if self.default_source and self.command("get-default-source") == self.source:
            self.command("set-default-source", self.default_source)

    def close(self):
        self._stop_playback()
        if self.modules:
            try:
                self._restore_default()
            except BackendError:
                log.error("voice microphone default restore failed")
        for identifier in reversed(self.modules):
            try:
                self.command("unload-module", identifier)
            except BackendError:
                log.error("voice microphone cleanup failed module=%s", identifier)
        self.modules.clear()
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def __exit__(self, *args):
        self.close()
