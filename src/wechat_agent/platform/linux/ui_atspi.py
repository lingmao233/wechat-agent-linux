from __future__ import annotations

import os
import hashlib
import logging
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

from ..base import BackendError, ChatInfo

log = logging.getLogger(__name__)


class AtspiUI:
    def __init__(self, settings):
        self.settings = settings
        self.atspi = None
        self.app = None
        self.active_chat = ""
        self.before_send = None
        self._window_id = ""
        self._panel_window_id = ""
        self._chat_header = None
        self._voice_root = None

    def _send_guard(self):
        if callable(self.before_send):
            self.before_send()

    def _command(self, arguments: list[str], *, timeout: float = 5) -> str:
        try:
            completed = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=True)
            return completed.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendError("DESKTOP_COMMAND_FAILED", f"{arguments[0]} 执行失败；检查 DISPLAY、X11 和桌面权限") from exc

    def connect(self) -> None:
        self._chat_header = None
        if not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise BackendError("DESKTOP_UNAVAILABLE", "缺少 DISPLAY / DBUS_SESSION_BUS_ADDRESS；请从已登录的图形会话启动，WSLg 先 source scripts/linux-session.sh")
        try:
            import pyatspi
            self.atspi = pyatspi
        except ImportError as exc:
            raise BackendError("ATSPI_MISSING", "安装 sudo apt install python3-pyatspi，并用 --system-site-packages 创建 venv") from exc
        try:
            desktop = self.atspi.Registry.getDesktop(0)
            self.app = next((candidate for candidate in desktop if (candidate.name or "").casefold() in {"wechat", "weixin", "微信"}), None)
        except Exception as exc:
            raise BackendError("ATSPI_UNAVAILABLE", "无法访问 AT-SPI 总线；运行 scripts/enable-a11y.sh，并从同一 D-Bus 会话启动微信") from exc
        if self.app is None:
            raise BackendError("ATSPI_UNAVAILABLE", "AT-SPI 中没有微信；先运行 scripts/enable-a11y.sh，必要时在当前图形会话重启微信")

    def _nodes(self, root=None):
        pending = [root or self.app]
        seen = set()
        visited = 0
        while pending and visited < 5000:
            node = pending.pop()
            visited += 1
            try:
                identity = (str(getattr(node, "path", "")), str(getattr(node, "bus_name", "")))
                if identity != ("", ""):
                    if identity in seen:
                        continue
                    seen.add(identity)
                yield node
                pending.extend(reversed(list(node)))
            except Exception:
                continue

    def _visible(self, node) -> bool:
        try:
            state = node.getState()
            return state.contains(self.atspi.STATE_VISIBLE) and state.contains(self.atspi.STATE_SHOWING)
        except Exception:
            return False

    def _find(self, *, role: str = "", names: tuple[str, ...] = ()):
        for node in self._nodes():
            try:
                if self._visible(node) and (not role or node.getRoleName() == role) and (not names or (node.name or "").strip() in names):
                    return node
            except Exception:
                continue
        return None

    def _click(self, node, *, delay_ms: int = 100) -> None:
        try:
            x, y, width, height = node.queryComponent().getExtents(self.atspi.DESKTOP_COORDS)
            if min(width, height) <= 0:
                raise ValueError("empty component")
            self._command(["xdotool", "mousemove", str(x + width // 2), str(y + height // 2),
                           "click", "--delay", str(delay_ms), "1"])
        except Exception as exc:
            raise BackendError("CONTROL_UNAVAILABLE", "无法定位或点击微信控件；检查 WSLg 焦点和 AT-SPI 树") from exc

    def _main_window(self) -> str:
        from .procmem import discover_pid
        pid = self.settings.wechat_linux_pid or discover_pid()
        windows = self._command(["xdotool", "search", "--class", "^wechat$"]).splitlines()
        candidates = []
        for window in windows:
            try:
                title = self._command(["xdotool", "getwindowname", window]).strip().casefold()
                if title not in {"微信", "wechat", "weixin"}:
                    continue
                owner = self._command(["xprop", "-id", window, "_NET_WM_PID"])
                match = re.search(r"=\s*(\d+)\s*$", owner)
                if not match or int(match.group(1)) != pid:
                    continue
                geometry = self._command(["xdotool", "getwindowgeometry", "--shell", window])
                values = {key: int(value) for key, value in re.findall(r"^(X|Y|WIDTH|HEIGHT)=(-?\d+)$", geometry, re.MULTILINE)}
                width, height = values["WIDTH"], values["HEIGHT"]
                if min(width, height) > 100:
                    candidates.append((width * height, window))
            except (BackendError, KeyError, ValueError):
                continue
        if not candidates:
            raise BackendError("WINDOW_NOT_FOUND", "找不到当前微信进程的主窗口")
        return max(candidates)[1]

    def focus(self) -> None:
        window = self._main_window()
        # A minimized window is still the same logged-in client. Restore only
        # its verified main window, rather than selecting a picture viewer.
        self._command(["xdotool", "windowmap", "--sync", window])
        # X11 activation is the only focus mechanism; no Windows helpers are required.
        self._command(["xdotool", "windowactivate", "--sync", window])
        self._command(["xdotool", "windowfocus", "--sync", window])
        self._window_id = window

    def open_chat(self, chat: ChatInfo) -> None:
        if self.app is None:
            self.connect()
        self.focus()
        candidates = []
        for node in self._nodes():
            try:
                label = (node.name or "").split("\n", 1)[0].strip()
                parent = node.parent
                if node.getRoleName() == "list item" and self._visible(node) and label == chat.name and parent and parent.getRoleName() == "list" and (parent.name or "").strip() in {"Chats", "聊天", "会话"}:
                    candidates.append(node)
            except Exception:
                continue
        if not candidates:
            candidates = self._search_chat_candidates(chat)
        if len(candidates) != 1:
            if not candidates:
                raise BackendError("CHAT_NOT_VISIBLE", "目标会话不在微信 Chats 列表中，且微信搜索未找到唯一联系人")
            raise BackendError("CHAT_AMBIGUOUS", "微信中有多个同名目标，已停止发送")
        # This Linux build toggles the conversation closed when its selected
        # sidebar row is clicked again. Read the actual header before clicking.
        if not self._chat_header_matches(chat.name):
            self._click(candidates[0])
            deadline = time.monotonic() + 2
            while not self._chat_header_matches(chat.name):
                if time.monotonic() >= deadline:
                    raise BackendError("CHAT_IDENTITY_UNCONFIRMED", "无法核实当前会话标题，已停止发送并保留诊断截图")
                time.sleep(0.05)
        self.active_chat = chat.chat_id

    def _search_box(self):
        """Return the visible global search entry, if the client exposes one."""

        for node in self._nodes():
            try:
                if not self._visible(node) or node.getRoleName() not in {"text", "entry"}:
                    continue
                if (node.name or "").strip().casefold() in {"search", "搜索"}:
                    return node
            except Exception:
                continue
        return None

    @staticmethod
    def _search_label(node) -> str:
        try:
            return (node.name or "").split("\n", 1)[0].strip()
        except Exception:
            return ""

    def _contact_search_candidates(self, name: str) -> list[object]:
        """Find exact rows under the visible ``联系人`` search section.

        WeChat puts groups, network results and chat-history matches in the
        same popup list. Restricting the result to the sibling range after
        the ``联系人`` heading prevents a display-name match from selecting a
        group or a web-search row by accident.
        """

        matches: list[object] = []
        target = str(name or "").strip()
        if not target:
            return matches
        for node in self._nodes():
            try:
                if node.getRoleName() != "list item" or not self._visible(node):
                    continue
                if self._search_label(node) != target:
                    continue
                parent = node.parent
                if parent is None or parent.getRoleName() != "list" or not self._visible(parent):
                    continue
                siblings = [child for child in list(parent)
                            if child.getRoleName() == "list item" and self._visible(child)]
                index = next((i for i, child in enumerate(siblings) if child == node), -1)
                if index < 0:
                    continue
                headings = {"联系人", "群聊", "公众号、服务号", "网络查找微信号：", "搜索网络结果", "聊天记录"}
                contact_indexes = [i for i, child in enumerate(siblings)
                                   if self._search_label(child) == "联系人"]
                if not contact_indexes:
                    continue
                contact_index = contact_indexes[0]
                next_section = next((i for i in range(contact_index + 1, len(siblings))
                                     if self._search_label(siblings[i]) in headings and i != contact_index),
                                    len(siblings))
                if contact_index < index < next_section:
                    matches.append(node)
            except Exception:
                continue
        return matches

    def _search_chat_candidates(self, chat: ChatInfo) -> list[object]:
        """Open global search and return one exact contact row.

        A newly allowlisted contact may be absent from the recent Chats
        sidebar, so the first private welcome searches the contact database
        display name. Search is entered through real keyboard events because
        this WeChat build does not update the popup for AT-SPI setTextContents.
        """

        box = self._search_box()
        if box is None:
            return []
        try:
            action = box.queryAction()
            if getattr(action, "nActions", 0) > 0 and action.doAction(0):
                focused = True
            else:
                focused = False
        except Exception:
            focused = False
        if not focused:
            self._click(box)

        queries = tuple(dict.fromkeys(value for value in (chat.name, chat.chat_id)
                                    if str(value or "").strip()))
        for query in queries:
            self._command(["xdotool", "key", "--window", self._window_id,
                           "--clearmodifiers", "ctrl+a"])
            self._command(["xdotool", "type", "--window", self._window_id,
                           "--clearmodifiers", "--delay", "20", str(query)])
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                candidates = self._contact_search_candidates(chat.name)
                if len(candidates) > 1:
                    raise BackendError("CHAT_AMBIGUOUS", "微信联系人搜索返回多个同名目标，已停止发送")
                if candidates:
                    return candidates
                time.sleep(0.05)
        return []

    def _chat_header_matches(self, name: str) -> bool:
        def matches(node):
            try:
                if node.getRoleName() not in {"label", "heading"} or not self._visible(node):
                    return False
                label = re.sub(r"[（(]\d+[）)]$", "", (node.name or "").strip()).strip()
                if label != name:
                    return False
                parent = node.parent
                for _ in range(64):
                    if parent is None:
                        return False
                    if parent.getRoleName() == "application":
                        return parent == self.app
                    if parent.getRoleName() in {"list item", "list"}:
                        return False
                    parent = parent.parent
            except Exception:
                pass
            return False

        # Re-read the live label, visibility and ancestry on every guard.
        # Walking message history for each audio poll adds recorded silence.
        if self._chat_header is not None and matches(self._chat_header):
            return True
        self._chat_header = None
        for node in self._nodes():
            if matches(node):
                self._chat_header = node
                return True
        return False

    def _input(self):
        candidates = []
        for node in self._nodes():
            try:
                if not self._visible(node) or node.getRoleName() not in {"text", "entry"}:
                    continue
                if (node.name or "").casefold() in {"search", "搜索"}:
                    continue
                node.queryEditableText()
                rectangle = node.queryComponent().getExtents(self.atspi.DESKTOP_COORDS)
                candidates.append((rectangle[1], node))
            except Exception:
                continue
        if not candidates:
            raise BackendError("INPUT_NOT_FOUND", "找不到 AT-SPI EditableText 输入框")
        return max(candidates, key=lambda pair: pair[0])[1]

    def _send_button(self):
        button = self._find(role="push button", names=("Send", "发送", "Send(S)", "发送(S)"))
        if button is None:
            raise BackendError("SEND_BUTTON_NOT_FOUND", "找不到微信 Send 按钮")
        return button

    def send_text(self, chat: ChatInfo, text: str) -> None:
        if not text.strip():
            raise BackendError("EMPTY_MESSAGE", "禁止发送空消息")
        # One retry is limited to preparation. A click with an unknown delivery
        # outcome must never be blindly replayed.
        owned_entry = None
        clicked = False
        try:
            for attempt in range(2):
                try:
                    self.open_chat(chat)
                    entry = self._input()
                    existing = entry.queryText().getText(0, -1)
                    if existing.strip() and existing != text:
                        raise BackendError("INPUT_OCCUPIED", "输入框已有其他草稿，未覆盖或发送")
                    if not existing.strip():
                        owned_entry = entry
                    entry.queryEditableText().setTextContents(text)
                    actual = entry.queryText().getText(0, -1)
                    if actual != text:
                        raise BackendError("INPUT_MISMATCH", "输入框回读与待发送文本不一致，已停止发送")
                    button = self._send_button()
                    break
                except BackendError as exc:
                    if attempt or exc.code not in {"INPUT_NOT_FOUND", "SEND_BUTTON_NOT_FOUND", "CHAT_NOT_VISIBLE", "CONTROL_UNAVAILABLE"}:
                        raise
                    self.diagnose(exc.code)
                    self.app = None
                    time.sleep(0.15)
            self._send_guard()
            if not self._chat_header_matches(chat.name):
                raise BackendError("CHAT_IDENTITY_UNCONFIRMED", "文字发送前当前会话发生变化")
            clicked = True
            self._click(button)
        except Exception:
            # STOP may arrive after filling the composer but before the click.
            # Only remove text we placed in an empty, still-current composer;
            # an existing draft, a user edit or an uncertain click is retained.
            if owned_entry is not None and not clicked:
                try:
                    if (self._chat_header_matches(chat.name)
                            and owned_entry.queryText().getText(0, -1) == text):
                        owned_entry.queryEditableText().setTextContents("")
                except Exception:
                    log.debug("prepared_text_cleanup_unavailable", exc_info=True)
            raise

    def send_image(self, chat: ChatInfo, path: str) -> None:
        self.open_chat(chat)
        entry = self._input()
        if entry.queryText().getText(0, -1).strip():
            raise BackendError("INPUT_OCCUPIED", "输入框已有其他草稿，未粘贴或发送图片")
        self._click(entry)
        entry.queryComponent().grabFocus()
        # Always provide real PNG bytes for the image/png clipboard target.
        import io
        with Image.open(path) as image:
            output = io.BytesIO()
            image.convert("RGB").save(output, format="PNG")
        with tempfile.TemporaryDirectory(prefix="wechat-image-clipboard-") as directory:
            clip = Path(directory) / "image.png"
            clip.write_bytes(output.getvalue())
            ready_file = Path(directory) / "ready"
            # GTK services large image transfers in its own event loop. Keep
            # the owner alive until WeChat has consumed and sent the image.
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("clipboard_worker.py")), str(clip), str(ready_file)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while True:
                    self._send_guard()
                    if process.poll() is not None:
                        raise BackendError("CLIPBOARD_FAILED", "图片剪贴板进程已退出；检查 python3-gi 和 gir1.2-gtk-3.0，未粘贴或发送")
                    try:
                        ready = ready_file.exists() and "image/png" in self._command(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"], timeout=0.4)
                    except BackendError:
                        ready = False
                    if ready:
                        break
                    if time.monotonic() >= deadline:
                        raise BackendError("CLIPBOARD_FAILED", "图片剪贴板目标未就绪，未点击发送")
                    time.sleep(0.05)
                self._send_guard()
                # Address the verified WeChat window even if host focus moved.
                self._command(["xdotool", "key", "--window", self._window_id, "--clearmodifiers", "ctrl+v"])
                deadline = time.monotonic() + 3
                while entry.queryText().getText(0, -1).strip() != "\ufffc":
                    self._send_guard()
                    if time.monotonic() >= deadline:
                        raise BackendError("IMAGE_PASTE_UNCONFIRMED", "输入框未出现原生图片，未点击发送")
                    time.sleep(0.05)
                self._send_guard()
                if not self._chat_header_matches(chat.name):
                    raise BackendError("CHAT_IDENTITY_UNCONFIRMED", "图片发送前当前会话发生变化")
                self._click(self._send_button())
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)

    def _voice_composer(self, button):
        try:
            previous = button
            node = button.parent
            for _ in range(12):
                if node is None or node.getRoleName() in {"application", "frame", "window"}:
                    break
                if node.getRoleName() == "tool bar":
                    return node if previous is button else previous
                previous = node
                node = node.parent
        except Exception:
            pass
        return None

    def _voice_controls(self):
        controls = {"cancel": [], "send": []}
        nodes = self._nodes(self._voice_root) if self._voice_root is not None else self._nodes()
        for node in nodes:
            try:
                if node.getRoleName() != "push button" or not self._visible(node):
                    continue
                name = (node.name or "").strip()
                if name in {"Cancel", "取消", "Cancel Recording", "取消录音"}:
                    controls["cancel"].append(node)
                elif name in {"发送语音", "Send Voice", "Send voice"}:
                    controls["send"].append(node)
            except Exception:
                continue
        if len(controls["cancel"]) != 1 or len(controls["send"]) != 1:
            raise BackendError("VOICE_CONTROLS_UNAVAILABLE", "未找到唯一的微信录音取消和发送控件，未点击发送")
        return controls["cancel"][0], controls["send"][0]

    def send_voice(self, chat: ChatInfo, path: str, *, expected_sha256: str = "") -> float:
        from .voice_audio import VirtualMicrophone, trim_leading_silence
        from .procmem import discover_pid
        from ...speech import MAX_SPEECH_BYTES
        self.open_chat(chat)
        if self._input().queryText().getText(0, -1).strip():
            raise BackendError("INPUT_OCCUPIED", "输入框已有草稿，未开始录音")
        button = self._find(role="push button", names=("Send Voice", "发送语音", "发语音"))
        if button is None:
            raise BackendError("VOICE_BUTTON_UNAVAILABLE", "当前微信没有可识别的原生语音入口")
        self._voice_root = self._voice_composer(button)
        with tempfile.TemporaryDirectory(prefix="wechat-native-voice-") as directory:
            # Freeze the prepared clip before opening the recording UI.
            source = Path(path)
            if not source.is_file() or source.stat().st_size > MAX_SPEECH_BYTES:
                raise BackendError("VOICE_AUDIO_INVALID", "待发送声音不可用")
            with source.open("rb") as stream:
                content = stream.read(MAX_SPEECH_BYTES + 1)
            if (len(content) > MAX_SPEECH_BYTES or
                    (expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256)):
                raise BackendError("VOICE_AUDIO_CHANGED", "待发送声音在准备期间发生变化")
            clip = Path(directory) / "clip.wav"
            with os.fdopen(os.open(clip, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                stream.write(content)
            duration = trim_leading_silence(clip)
            recording, cancel, started = False, None, 0.0
            expired = threading.Event()
            watchdog_lock = threading.Lock()

            def guard():
                self._send_guard()
                if not self._chat_header_matches(chat.name) or self.active_chat != chat.chat_id:
                    raise BackendError("CHAT_IDENTITY_UNCONFIRMED", "录音期间当前会话发生变化，未发送")
                if expired.is_set() or (started and time.monotonic() - started >= 57):
                    raise BackendError("VOICE_RECORDING_TIMEOUT", "录音接近微信时长上限，已取消")

            def timeout():
                nonlocal recording
                with watchdog_lock:
                    if recording:
                        expired.set()
                        # Only used while this method owns a confirmed recording.
                        try:
                            self._command(["xdotool", "key", "--window", self._window_id, "Escape"], timeout=2)
                        except BackendError:
                            pass

            timer = None
            with VirtualMicrophone(self.settings.wechat_linux_pid or discover_pid()) as microphone:
                try:
                    guard()
                    self._click(button, delay_ms=0)
                    recording, started = True, time.monotonic()
                    timer = threading.Timer(57, timeout)
                    timer.daemon = True
                    timer.start()
                    deadline = time.monotonic() + 2
                    while True:
                        guard()
                        try:
                            cancel, _ = self._voice_controls()
                            break
                        except BackendError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.1)
                    microphone.bind_recording(guard)
                    if time.monotonic() - started + duration + 0.5 >= 57:
                        raise BackendError("VOICE_RECORDING_TOO_LONG", "完整音频无法在微信录音上限内播放")
                    microphone.play(clip, guard)
                    cancel, send = self._voice_controls()
                    with watchdog_lock:
                        guard()
                        microphone.confirm_recording()
                        recorded_seconds = time.monotonic() - started
                        self._click(send)
                        recording = False
                    return recorded_seconds
                finally:
                    if timer is not None:
                        timer.cancel()
                    if recording:
                        try:
                            if microphone.recording_active():
                                try:
                                    current_cancel, _ = self._voice_controls()
                                except BackendError:
                                    current_cancel = None
                                if current_cancel is not None:
                                    self._click(current_cancel)
                                else:
                                    self._command(["xdotool", "key", "--window", self._window_id, "Escape"])
                        except BackendError:
                            log.error("微信录音取消失败，请检查当前录音界面")
                    self._voice_root = None

    def _conversion_menu(self):
        for node in self._nodes():
            try:
                if (self._visible(node) and node.getRoleName() in {"menu item", "check menu item"}
                        and (node.name or "").strip() in {"转文字", "语音转文字", "Convert to Text", "Convert to text", "Transcribe"}):
                    return node
            except Exception:
                continue
        return None

    def request_voice_conversion(self, chat: ChatInfo, read_result, *, guard, timeout: float) -> str:
        self.open_chat(chat)
        guard()
        def check():
            guard()
            if not self._chat_header_matches(chat.name) or self.active_chat != chat.chat_id:
                raise BackendError("CHAT_IDENTITY_UNCONFIRMED", "语音转换期间当前会话发生变化")

        messages = self._find(role="list", names=("Messages", "消息"))
        if messages is None:
            raise BackendError("VOICE_MESSAGE_NOT_VISIBLE", "当前会话没有可检查的消息列表")
        list_x, list_y, list_width, list_height = messages.queryComponent().getExtents(self.atspi.DESKTOP_COORDS)
        candidates, seen = [], set()
        for node in self._nodes(messages):
            try:
                name = (node.name or "").strip()
                name = re.sub(r"\s*(?:未播放|未读|Unplayed|Unread)$", "", name, flags=re.I).strip()
                if not self._visible(node) or not re.fullmatch(r"(?:\[?(?:语音|Voice|Audio)\]?(?:\s*\d{1,3}\s*(?:秒|s|[″”\"]\s*秒?))?|\d{1,3}\s*(?:秒|s|[″”\"]\s*秒?))", name, re.I):
                    continue
                x, y, width, height = node.queryComponent().getExtents(self.atspi.DESKTOP_COORDS)
                position = (x + width // 2, y + height // 2)
                if (min(width, height) > 0 and position not in seen
                        and list_x <= position[0] < list_x + list_width
                        and list_y <= position[1] < list_y + list_height):
                    candidates.append((y, node))
                    seen.add(position)
            except Exception:
                continue
        if not candidates:
            raise BackendError("VOICE_MESSAGE_NOT_VISIBLE", "当前画面没有可确认的语音消息")
        deadline = time.monotonic() + timeout
        for _, node in sorted(candidates, key=lambda item: item[0], reverse=True)[:8]:
            check()
            result = read_result()
            if result:
                return result
            if time.monotonic() >= deadline:
                break
            x, y, width, height = node.queryComponent().getExtents(self.atspi.DESKTOP_COORDS)
            # Qt exposes the whole row, not the bubble bounds. Its center is
            # usually blank. Probe the right/left bubble positions within this
            # verified voice row, then the center for narrow windows.
            inset = min(100, width // 4)
            for point_x in dict.fromkeys((x + width - inset, x + inset, x + width // 2)):
                check()
                if time.monotonic() >= deadline:
                    break
                self._command(["xdotool", "mousemove", str(point_x), str(y + height // 2), "click", "3"])
                selected = False
                try:
                    menu_deadline = min(deadline, time.monotonic() + 0.6)
                    menu = self._conversion_menu()
                    while menu is None and time.monotonic() < menu_deadline:
                        time.sleep(0.05)
                        menu = self._conversion_menu()
                    if menu is not None:
                        check()
                        self._click(menu)
                        selected = True
                finally:
                    # This build reports "popup menu". Never send Escape to
                    # an ordinary chat window, where it hides the client.
                    if self._find(role="menu") is not None or self._find(role="popup menu") is not None:
                        self._command(["xdotool", "key", "Escape"])
                if selected:
                    break
            for _ in range(5):
                check()
                result = read_result()
                if result:
                    return result
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.15)
        flushed = False
        while time.monotonic() < deadline:
            check()
            result = read_result()
            if result:
                return result
            if not flushed and deadline - time.monotonic() < timeout - 2:
                self._flush_voice_cache(chat)
                flushed = True
            time.sleep(0.2)
        return read_result()

    def _flush_voice_cache(self, chat: ChatInfo):
        if chat.chat_id == "filehelper":
            return
        # This client may save audioTxt only when leaving the conversation.
        for label in ("文件传输助手", "File Transfer"):
            try:
                self.open_chat(ChatInfo("filehelper", label))
            except BackendError as exc:
                if exc.code == "CHAT_NOT_VISIBLE":
                    continue
                raise
            self.open_chat(chat)
            return

    def panel(self) -> tuple[str, int, int, int, int]:
        tree = self._command(["xwininfo", "-root", "-tree"])
        pattern = r'(0x[0-9a-fA-F]+)\s+"(?:wechat|WeChat|微信)"[^\n]*?\s(\d+)x(\d+)([+-]\d+)([+-]\d+)'
        matches = []
        for match in re.finditer(pattern, tree):
            identifier, width, height, x, y = match.groups()
            if abs(int(width) - self.settings.sticker_panel_width) <= 8 and abs(int(height) - self.settings.sticker_panel_height) <= 8:
                try:
                    info = self._command(["xwininfo", "-id", identifier])
                except BackendError:
                    continue
                # Qt keeps an unmapped popup in the X11 tree after it closes.
                # Capturing or clicking that stale window cannot work.
                if not re.search(r"Map State:\s+IsViewable\b", info):
                    continue
                absolute_x = re.search(r"Absolute upper-left X:\s+(-?\d+)", info)
                absolute_y = re.search(r"Absolute upper-left Y:\s+(-?\d+)", info)
                if absolute_x and absolute_y:
                    x, y = absolute_x[1], absolute_y[1]
                matches.append((identifier, int(x), int(y), int(width), int(height)))
        if len(matches) != 1:
            raise BackendError("STICKER_PANEL_UNAVAILABLE", "找不到唯一的表情面板窗口；检查微信版本及面板尺寸配置")
        self._panel_window_id = matches[0][0]
        return matches[0]

    def open_sticker_panel(self, chat: ChatInfo):
        if self.app is None:
            self.connect()
        if self._chat_header_matches(chat.name):
            # Raising the owner of a Qt popup closes the popup. A targeted
            # shortcut needs only the verified owner id, not another activation.
            try:
                return self.panel()
            except BackendError:
                pass
            self._window_id = self._main_window()
            self.active_chat = chat.chat_id
        else:
            self.open_chat(chat)
        try:
            return self.panel()
        except BackendError:
            pass
        button = self._find(role="push button", names=("Send Sticker(Alt+E)", "Send Sticker", "发送表情(Alt+E)", "表情", "发送表情"))
        if button is None:
            raise BackendError("STICKER_BUTTON_UNAVAILABLE", "找不到表情按钮")
        if "Alt+E" in (button.name or "") and self._window_id:
            # WSLg can leave host pointer focus on another surface. The
            # shortcut advertised by the verified control targets this window.
            self._command(["xdotool", "key", "--window", self._window_id, "--clearmodifiers", "alt+e"])
        else:
            self._click(button)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                return self.panel()
            except BackendError:
                time.sleep(0.1)
        return self.panel()

    def select_favorites(self, panel) -> None:
        _, x, y, width, height = panel
        # Favorite tab is verified against emoticon.db order by read-back on send.
        self._command(["xdotool", "mousemove", str(x + round(width * 0.30)), str(y + height - 20), "click", "1"])
        time.sleep(0.15)

    def click_sticker(self, panel, row: int, col: int) -> None:
        if not (0 <= row < self.settings.sticker_panel_rows and 0 <= col < self.settings.sticker_panel_columns):
            raise BackendError("STICKER_CELL_INVALID", "表情格子超出面板")
        _, x, y, width, height = panel
        scale_x, scale_y = width / 474, height / 474
        self._send_guard()
        self._command(["xdotool", "mousemove", str(x + round((47 + col * 94) * scale_x)), str(y + round((45 + row * 96) * scale_y)), "click", "1"])

    def capture_panel(self, panel) -> bytes:
        target = self.settings.wechat_linux_screenshot_dir / "sticker-panel.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        self._command(["import", "-window", panel[0], str(target)])
        os.chmod(target, 0o600)
        return target.read_bytes()

    def close_panel(self) -> None:
        target = self._panel_window_id
        self._panel_window_id = ""
        if target:
            try:
                # Selecting a sticker can destroy its popup before this runs.
                self._command(["xwininfo", "-id", target])
                self._command(["xdotool", "key", "--window", target, "Escape"])
            except BackendError:
                pass

    def diagnose(self, reason: str) -> str:
        target = self.settings.wechat_linux_screenshot_dir / (time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^A-Z_]", "", reason)[:40] + ".png")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._command(["import", "-window", "root", str(target)])
            os.chmod(target, 0o600)
            return str(target)
        except Exception:
            return ""

    def health(self) -> dict[str, object]:
        self.connect()
        return {"atspi": True, "window": self.app.name, "sticker_button": self._find(role="push button", names=("Send Sticker(Alt+E)", "Send Sticker", "发送表情(Alt+E)", "发送表情", "表情")) is not None, "commands": {name: bool(shutil.which(name)) for name in ("xdotool", "xclip", "xwininfo", "xprop", "import")}}
