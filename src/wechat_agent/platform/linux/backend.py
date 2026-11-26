from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import tempfile
import time
import uuid

from sqlalchemy import select

from ...memory_jobs import aware, encode, scope_transaction, stable_id
from ...memory_schema import LinuxSendRow, LinuxNativeSendRow
from ...models import utc_now
from ...storage import Storage
from ..base import BackendError, SendResult
from .db import WeChatLinuxDB, voice_text
from .keys import private_json_write
from .media import MediaService
from .parser import parse_message
from .procmem import CONFIG_XOR_MASK, discover_pid
from .ui_atspi import AtspiUI

log = logging.getLogger(__name__)
_desktop_thread_lock = threading.Lock()


@contextmanager
def desktop_lease():
    """Share the desktop between sends and native transcript conversion.

    The bridge and the normal group/private reply path run in the same process
    but may be triggered by different threads.  Waiting briefly here is much
    safer than failing one side with SEND_BUSY: a group reply that loses the
    race should queue behind the bridge send, not disappear.
    """
    if not _desktop_thread_lock.acquire(timeout=12):
        raise BackendError("SEND_BUSY", "微信界面处理超时，请稍后重试")
    descriptor = None
    try:
        if os.name == "posix":
            import fcntl
            runtime = Path(f"/run/user/{os.getuid()}")
            if not runtime.is_dir():
                runtime = Path(tempfile.gettempdir())
            try:
                descriptor = os.open(runtime / f"wechat-agent-ui-{os.getuid()}.lock",
                                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BackendError("SEND_BUSY", "微信界面已被其他进程使用或无法取得界面锁") from exc
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _desktop_thread_lock.release()


class LinuxBackend:
    def __init__(self, settings, *, db=None, ui=None, storage=None):
        self.settings = settings
        self.db, self.ui, self.storage = db, ui or AtspiUI(settings), storage
        self.account_id = settings.wechat_account_id
        self.media = None
        self.catalog = None
        self._cursors: dict[str, int] = {}
        self._generations: dict[str, str] = {}
        self._initial: set[str] = set()
        self._startup_cutoffs: dict[str, int] = {}
        self._cursor_lock = threading.RLock()
        self._ui_lock = threading.Lock()
        self._connected = False
        self.last_send_result = None
        # Optional provider for the Douyin bridge.  A bound private target may
        # be delivered to even when it is not in WECHAT_PRIVATE_CHAT_NAMES; the
        # binding table is the authorization, and the provider is re-read on
        # every gate so an unbind takes effect immediately.
        self._dm_allowlist_provider = None

    def set_dm_allowlist_provider(self, provider) -> None:
        self._dm_allowlist_provider = provider

    def _dm_target_allowed(self, chat_id: str) -> bool:
        provider = self._dm_allowlist_provider
        if provider is None:
            return False
        try:
            allowed = {str(value).strip() for value in (provider() or ()) if str(value).strip()}
        except Exception:
            return False
        return str(chat_id or "").strip() in allowed


    def bind_storage(self, storage) -> None:
        self.storage = storage
        if self.media:
            self.media.storage = storage

    def _ensure_db(self):
        if self.db is None:
            self.db = WeChatLinuxDB(self.settings)
        if not self.account_id:
            self.account_id = self.db.account
        elif self.account_id != self.db.account:
            raise BackendError("ACCOUNT_MISMATCH", "WECHAT_ACCOUNT_ID 与数据目录账号不一致")
        if self.storage is None:
            self.storage = Storage(self.settings)
            self.storage.init_schema()
        if self.media is None:
            self.media = MediaService(self.settings, self.db, storage=self.storage)
        if self.catalog is None:
            from ...stickers import StickerCatalog
            self.catalog = StickerCatalog(self.settings, self.storage, self.db, self.ui)

    def connect(self) -> None:
        self._ensure_db()
        if not self.db.message_databases:
            raise BackendError("DATABASE_MISSING", "账号目录中没有 message_*.db")
        if hasattr(self.db, "keys"):
            missing = [path for path in self.db.message_databases if self.db.keys.database_key(path.relative_to(self.db.root).as_posix(), path) is None]
            if missing:
                pid = self.settings.wechat_linux_pid or discover_pid()
                self.db.keys.scan(pid, self.db.root, missing, max_mb=self.settings.wechat_linux_memory_scan_max_mb)
                for path in missing:
                    self.db.keys.require(path.relative_to(self.db.root).as_posix(), path)
        path = self.settings.wechat_linux_cursor_file
        with self._cursor_lock:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("account") == self.account_id:
                    self._cursors = {str(key): int(value) for key, value in data.get("cursors", {}).items()}
                    self._generations = data.get("generations", {})
        self._connected = True

    def configured_chat_names(self) -> list[str]:
        """Resolve the configured groups plus opt-in private conversations."""

        self._ensure_db()
        names = list(self.settings.wechat_group_names)
        if getattr(self.settings, "wechat_private_reply_enabled", False):
            configured = getattr(self.settings, "wechat_private_chat_names", None) or []
            # Private polling is allowlist-only.  An empty allowlist must not
            # turn into an implicit "discover every direct chat" scope.
            if configured:
                # Keep the stable chat id, not the display name: two WeChat
                # contacts can share the same nickname ("Oo" on 2026-11-10),
                # and re-resolving by display name is then ambiguous.
                names.extend(chat.chat_id for chat in self.db.private_chats(configured))
        return list(dict.fromkeys(name for name in names if str(name).strip()))

    def private_chats(self, configured: list[str] | tuple[str, ...] = ()):
        """Return discovered one-to-one chats for the Web allowlist picker.

        This is a read-only database operation.  It deliberately does not
        open a chat window or send anything, so the console can refresh the
        picker while the listener is stopped or running.
        """

        self._ensure_db()
        if not self._connected:
            self.connect()
        return self.db.private_chats(configured)

    def _chat_allowed(self, chat_name: str, chat_id: str = "") -> bool:
        if chat_name in self.settings.wechat_group_names:
            return True
        if not getattr(self.settings, "wechat_private_reply_enabled", False):
            return False
        configured = getattr(self.settings, "wechat_private_chat_names", None) or []
        if not configured:
            return False
        wanted = {str(value).strip().casefold() for value in configured if str(value).strip()}
        return any(value in wanted for value in {
            str(chat_name or "").strip().casefold(),
            str(chat_id or "").strip().casefold(),
        } if value)

    def check_ready(self) -> dict[str, object]:
        checks, errors = {}, []
        checks["platform"] = "linux"
        try:
            checks["pid"] = self.settings.wechat_linux_pid or discover_pid()
        except BackendError as exc:
            errors.append({"code": exc.code, "hint": str(exc)})
        try:
            self.connect()
            checks["account_id"] = self.account_id
            checks["data_dir"] = str(self.db.data_dir)
            checks["databases"] = [path.name for path in self.db.message_databases]
            checks["names"] = len(self.db.names())
            checks["groups"] = [asdict(self.db.resolve_chat(name)) for name in self.settings.wechat_group_names]
            checks["private_chats"] = [asdict(chat) for chat in self.db.private_chats(getattr(self.settings, "wechat_private_chat_names", []) or [])] if getattr(self.settings, "wechat_private_reply_enabled", False) else []
            for chat in checks["groups"]:
                self.db.rows(chat["chat_id"], limit=1)
            checks["decryptable"] = True
        except Exception as exc:
            errors.append({"code": getattr(exc, "code", "DATABASE_UNAVAILABLE"), "hint": str(exc) if isinstance(exc, BackendError) else "数据库自检失败；检查独立库密钥和文件权限"})
        try:
            checks.update(self.ui.health())
            if self.settings.sticker_enabled and not checks.get("sticker_button"):
                errors.append({"code": "STICKER_BUTTON_UNAVAILABLE", "hint": "表情按钮不可用；手动打开已配置会话后重试 ready"})
            missing = [name for name, available in checks.get("commands", {}).items() if not available]
            if missing:
                errors.append({"code": "DESKTOP_DEPENDENCY_MISSING", "hint": "安装 Linux 桌面依赖：" + ", ".join(missing)})
        except Exception as exc:
            errors.append({"code": getattr(exc, "code", "ATSPI_UNAVAILABLE"), "hint": str(exc) if isinstance(exc, BackendError) else "AT-SPI 检查失败，运行 scripts/enable-a11y.sh"})
        if self.settings.wechat_linux_binary.is_file():
            with self.settings.wechat_linux_binary.open("rb") as stream:
                mask_seen = False
                tail = b""
                while chunk := stream.read(1024 * 1024):
                    if CONFIG_XOR_MASK in tail + chunk:
                        mask_seen = True
                        break
                    tail = chunk[-31:]
                checks["cipher_mask_present"] = mask_seen
            if not mask_seen:
                errors.append({"code": "WECHAT_VERSION_DRIFT", "hint": "微信二进制密钥常量已变化；运行回归并更新常量，未尝试发送"})
        return {"ready": not errors, "checks": checks, "errors": errors, "allow_send": self.settings.wechat_allow_send, "stop": self.settings.stop_file.exists()}

    def _event(self, row, chat):
        names = self.db.names(row.get("_shard", "message_0.db"))
        return parse_message(row, account_id=self.account_id, chat_id=chat.chat_id, chat_name=chat.name, names=names, display_names=self.db.display_names(), self_id=self.db.account, shard=row.get("_shard", "message_0.db"))

    def poll(self, account_id: str = "", group_name: str | None = None):
        if not self._connected:
            self.connect()
        groups = [group_name] if group_name else self.configured_chat_names()
        events = []
        for name in groups:
            chat = self.db.resolve_chat(name)
            for path in self.db.message_databases:
                key = stable_id(self.account_id, chat.chat_id, path.name)
                with self._cursor_lock:
                    previous = self._cursors.get(key)
                    generation = self.db.generation(path)
                    if self._generations.get(key) != generation:
                        previous = None
                        self._cursors.pop(key, None)
                        self._startup_cutoffs.pop(key, None)
                        self._generations[key] = generation
                if previous is not None:
                    latest = self.db.rows(chat.chat_id, shard=path, limit=1)
                    if latest and int(latest[-1].get("local_id") or 0) < previous:
                        previous = None
                        with self._cursor_lock:
                            self._cursors.pop(key, None)
                            self._startup_cutoffs.pop(key, None)
                first = key not in self._initial
                if first or key not in self._startup_cutoffs:
                    latest = self.db.rows(chat.chat_id, shard=path, limit=1)
                    self._startup_cutoffs[key] = int(latest[-1].get("local_id") or 0) if latest else 0
                rows = self.db.rows(chat.chat_id, shard=path, after_id=previous, limit=self.settings.wechat_linux_backfill_messages if previous is None else 200)
                if first:
                    self._initial.add(key)
                if previous is None and not rows:
                    with self._cursor_lock:
                        self._cursors[key] = 0
                for row in rows:
                    event = self._event(row, chat)
                    event.historical = (self.settings.wechat_skip_existing_on_start
                                        and int(row.get("local_id") or 0) <= self._startup_cutoffs[key])
                    event.media_metadata.update({"cursor_key": key, "cursor_value": int(row.get("local_id") or 0)})
                    events.append(event)
        return sorted(events, key=lambda event: (event.observed_at, event.external_message_id or ""))

    def ack(self, event) -> None:
        key = event.media_metadata.get("cursor_key")
        if key:
            with self._cursor_lock:
                self._cursors[key] = max(self._cursors.get(key, 0), int(event.media_metadata.get("cursor_value", 0)))
                private_json_write(self.settings.wechat_linux_cursor_file, {"account": self.account_id, "cursors": self._cursors, "generations": self._generations})

    def enrich(self, event) -> None:
        self._ensure_db()
        self.media.enrich(event)

    @property
    def supports_native_voice(self) -> bool:
        from .voice_audio import VirtualMicrophone
        return callable(getattr(self.ui, "send_voice", None)) and VirtualMicrophone.available()

    def transcribe_voice(self, event) -> dict:
        self._ensure_db()

        def guard():
            if self.settings.stop_file.exists():
                raise BackendError("STOPPED", "STOP 文件存在，停止语音转换")
            if (event.account_id != self.account_id or not self._chat_allowed(event.group_name, event.group_id)
                    or (self.settings.wechat_allowed_members and event.member_id not in self.settings.wechat_allowed_members)):
                raise BackendError("VOICE_SCOPE_REJECTED", "语音不在当前账号和已配置会话范围")

        guard()
        chat = self.db.resolve_chat(event.group_name)
        if chat.chat_id != event.group_id:
            return {"error": "voice_identity_unconfirmed"}
        row = self.db.voice_row(event)
        if row is None:
            return {"error": "voice_identity_unconfirmed"}
        parsed = self._event(row, chat)
        duration = parsed.media_metadata.get("voice_duration_seconds", 0)
        if not 0 < duration <= self.settings.voice_max_duration_seconds:
            return {"error": "audio_duration_limit"}

        def read_result():
            current = self.db.voice_row(event)
            return voice_text(current) if current is not None else ""

        text = read_result()
        if not text:
            with self._ui_lock, desktop_lease():
                guard()
                text = self.ui.request_voice_conversion(chat, read_result, guard=guard,
                    timeout=self.settings.wechat_voice_conversion_timeout_seconds)
                # Read only this exact original message, even if conversion
                # also touched a neighboring visible voice with the same length.
                text = read_result() if text else ""
        return ({"text": text, "duration_seconds": duration, "provider": "wechat", "model": "wechat-built-in"}
                if text else {"error": "wechat_conversion_no_result"})

    def read_messages(self, limit: int = 30):
        self._ensure_db()
        events = []
        for name in self.configured_chat_names():
            chat = self.db.resolve_chat(name)
            events.extend(self._event(row, chat) for row in self.db.rows(chat.chat_id, limit=limit))
        return events[-limit:]

    def _gate(self, action) -> None:
        if self.settings.stop_file.exists():
            raise BackendError("STOPPED", "STOP 文件存在，禁止发送")
        if not self.settings.wechat_allow_send:
            raise BackendError("SENDING_DISABLED", "WECHAT_ALLOW_SEND=false")
        # A Douyin-bridge delivery is authorized by its explicit binding, not
        # by the group listener allowlist or the member whitelist.  Both still
        # apply to every other private action.
        dm_delivery = action.action_type == "dm" and self._dm_target_allowed(action.group_id)
        if not dm_delivery and not self._chat_allowed(action.group_name, action.group_id):
            raise BackendError("CHAT_NOT_ALLOWED", "目标会话不在白名单")
        if (self.settings.wechat_allowed_members and not dm_delivery
                and action.target_member_id not in self.settings.wechat_allowed_members):
            raise BackendError("MEMBER_NOT_ALLOWED", "目标成员不在白名单")
        if action.account_id != self.account_id:
            raise BackendError("ACCOUNT_MISMATCH", "动作账号与本机微信账号不一致")
        if action.proactive_window_id:
            from ...proactive_scheduler import authorize_window
            if not self.settings.proactive_schedule_enabled:
                raise BackendError("PROACTIVE_DISABLED", "主动参与调度已关闭")
            allowed, reason = authorize_window(self.storage, action, now=utc_now())
            if not allowed:
                raise BackendError("PROACTIVE_REJECTED", reason)

    def _native_reserve(self, action, action_id, kind):
        self._gate(action)
        now = utc_now()
        with scope_transaction(self.storage, action.account_id, "__outbound__") as session:
            recent = list(session.execute(select(LinuxNativeSendRow.attempted_at, LinuxNativeSendRow.action_id).where(LinuxNativeSendRow.account_id == action.account_id, LinuxNativeSendRow.attempted_at > now - timedelta(hours=1))))
            ledger = session.get(LinuxSendRow, action_id)
            if action.send_text_with_media and not (ledger and ledger.attempts):
                needed = 1 + len(action.media_paths) + len(action.file_paths)
            elif not action.text.strip():
                needed = len(action.media_paths) + len(action.file_paths)
            else:
                needed = 1
            needed = max(1, needed)
            if sum(aware(value.attempted_at) >= now - timedelta(minutes=1) for value in recent) + needed > 20:
                raise BackendError("RATE_LIMITED", "每分钟原生消息熔断上限已达到")
            if self.settings.wechat_max_replies_per_hour and len(recent) + needed > self.settings.wechat_max_replies_per_hour:
                raise BackendError("RATE_LIMITED", "每小时原生消息上限已达到")
            other_replies = [aware(value.attempted_at) for value in recent if value.action_id != action_id]
            if other_replies and (now - max(other_replies)).total_seconds() < self.settings.wechat_reply_cooldown_seconds:
                raise BackendError("RATE_LIMITED", "原生消息最短间隔尚未结束")
            ledger.attempts += 1
            ledger.status, ledger.lease_until = "sending", now + timedelta(seconds=120)
            identifier = uuid.uuid4().hex
            session.add(LinuxNativeSendRow(native_id=identifier, action_id=action_id, account_id=action.account_id, group_id=action.group_id, attempted_at=now, message_type=kind))
        return identifier

    def _native_verified(self, identifier, result):
        with self.storage.Session.begin() as session:
            session.get(LinuxNativeSendRow, identifier).message_ids_json = encode(result.message_ids)

    def _verify(self, chat, before: dict, *, kind: str, text: str = "", md5: str = "", duration: float = 0, size: int = 0) -> SendResult | None:
        deadline = time.monotonic() + self.settings.wechat_linux_verify_seconds
        while True:
            voices = []
            for row in self.db.rows(chat.chat_id, limit=100):
                if int(row.get("local_id") or 0) <= before.get(row.get("_shard", "message_0.db"), 0):
                    continue
                event = self._event(row, chat)
                if not event.is_self:
                    continue
                if kind == "file":
                    appmsg_type = str(event.media_metadata.get("appmsg_type") or "")
                    if (event.message_type == "appmsg" and appmsg_type == "6"
                            and (not text or event.text == text)):
                        return SendResult(True, (str(event.external_message_id),), "file")
                    if (event.message_type == "appmsg" and appmsg_type == "8"
                            and size > 0
                            and int(event.media_metadata.get("appmsg_totallen") or 0) == int(size)):
                        extension = Path(text).suffix.lower().lstrip(".") if text else ""
                        actual = str(event.media_metadata.get("appmsg_fileext") or "")
                        if extension and actual and extension != actual:
                            continue
                        # WeChat turns a GIF chosen through the file picker
                        # into a native animated emoticon (appmsg type 8);
                        # that is a successful delivery, not a partial file.
                        return SendResult(True, (str(event.external_message_id),), "animation")
                    # WeChat may also import a GIF chosen through the file
                    # picker as a native sticker (local_type 47) instead of
                    # appmsg type 8.  Identify it by the exact sticker md5 and
                    # byte length of the file we sent.
                    if (event.message_type == "sticker" and event.is_self and size > 0
                            and str(text).lower().endswith(".gif")
                            and int(event.sticker_size or 0) == int(size)
                            and event.sticker_md5):
                        return SendResult(True, (str(event.external_message_id),), "sticker", event.sticker_md5)
                    # A video selected through the file picker is uploaded as
                    # a native type-43 message; its exact byte length is the
                    # read-back identity.  Do not mistake it for an unrelated
                    # video of a different size.
                    if (event.message_type == "video" and size > 0
                            and int(event.media_metadata.get("video_length") or 0) == int(size)):
                        return SendResult(True, (str(event.external_message_id),), "video")
                    continue
                if event.message_type != kind:
                    continue
                if kind == "voice":
                    voices.append(event)
                    continue
                if kind == "text" and event.text != text:
                    continue
                if kind == "sticker" and event.sticker_md5 != md5:
                    self.catalog.invalidate(self.account_id)
                    raise BackendError("STICKER_MD5_MISMATCH", "实际表情 md5 与索引不一致；索引已失效，未重发表情", details={"expected_md5": md5, "actual_md5": event.sticker_md5, "message_id": str(event.external_message_id)})
                return SendResult(True, (str(event.external_message_id),), kind, event.sticker_md5)
            if len(voices) > 1:
                raise BackendError("SEND_AMBIGUOUS", "录音期间出现多条本号新语音，无法确认本次发送；禁止重放")
            if voices:
                voice = voices[0]
                length = voice.media_metadata.get("voice_duration_seconds", 0)
                server = str(voice.media_metadata.get("server_id") or "")
                if server.isdigit() and int(server) > 0 and length > 0 and abs(length - duration) <= 2:
                    return SendResult(True, (str(voice.external_message_id),), "voice")
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    def send(self, action) -> SendResult:
        self._ensure_db()
        self._gate(action)
        # A bound private delivery already carries the stable wxid; resolve by
        # id so two contacts with the same nickname cannot trigger
        # CHAT_AMBIGUOUS.  Groups keep resolving by their configured name.
        chat = self.db.resolve_chat(action.group_id if action.action_type == "dm" else action.group_name)
        if chat.chat_id != action.group_id:
            raise BackendError("CHAT_ID_MISMATCH", "动作稳定群 ID 与配置解析结果不一致")
        action_id = stable_id(action.account_id, action.group_id, action.idempotency_key or action.request_id or uuid.uuid4().hex)
        expected = {
            "text": action.text,
            "images": action.media_paths,
            "files": action.file_paths,
            "sticker_intent": action.sticker_intent,
        }
        if action.voice_path:
            from ...speech import wave_duration
            if action.media_paths or action.sticker_intent or action.at_names or action.send_text_with_media:
                raise BackendError("VOICE_ACTION_CONFLICT", "一条语音动作不能同时包含图片、表情或额外文字发送")
            clip = Path(action.voice_path)
            actual_duration = wave_duration(clip)
            if abs(actual_duration - action.voice_duration_seconds) > 0.05:
                raise BackendError("VOICE_AUDIO_CHANGED", "待发送声音的时长与已准备结果不同")
            expected["voice_sha256"] = hashlib.sha256(clip.read_bytes()).hexdigest()
            expected["voice_duration_seconds"] = actual_duration
        if action.send_text_with_media:
            expected["send_text_with_media"] = True
        now = utc_now()
        with scope_transaction(self.storage, action.account_id, "__outbound__") as session:
            ledger = session.get(LinuxSendRow, action_id)
            if ledger:
                stored_expected = json.loads(ledger.expected_json)
                # ``files`` was added after the original ledger shape.  An
                # empty file list is semantically identical to the old row,
                # so an upgrade must not turn a previously valid action into
                # ACTION_CONFLICT.
                if "files" not in stored_expected and not action.file_paths:
                    stored_expected["files"] = []
                if stored_expected != expected:
                    raise BackendError("ACTION_CONFLICT", "幂等键对应的发送内容发生变化")
            if ledger and ledger.status == "verified":
                data = json.loads(ledger.receipts_json)
                return SendResult(True, tuple(data), ledger.message_type)
            if ledger and ledger.attempts:
                raise BackendError("SEND_UNCERTAIN", "此前发送结果尚未核实，禁止重放；检查发送台账和微信数据库")
            if ledger and ledger.lease_until and aware(ledger.lease_until) > now:
                raise BackendError("SEND_BUSY", "该动作已由另一个发送者持有租约")
            busy = session.scalar(select(LinuxSendRow.action_id).where(LinuxSendRow.account_id == action.account_id, LinuxSendRow.lease_until > now, LinuxSendRow.status.in_(["reserved", "sending"])).limit(1))
            if busy:
                raise BackendError("SEND_BUSY", "微信界面正由另一个发送者使用")
            if ledger is None:
                ledger = LinuxSendRow(action_id=action_id, account_id=action.account_id, group_id=action.group_id, status="reserved", expected_json=encode(expected), created_at=now, updated_at=now, attempts=0)
                session.add(ledger)
            ledger.lease_until = now + timedelta(seconds=120)
        with self._ui_lock, desktop_lease():
            receipts = []
            try:
                self._gate(action)
                self.ui.before_send = lambda: self._gate(action)
                choice = None
                if self.settings.sticker_enabled and action.sticker_intent and not action.media_paths and not action.voice_path:
                    try:
                        choice = self.catalog.choose(self.account_id, action.sticker_intent, chat=chat)
                    except Exception:
                        log.warning("sticker_catalog_unavailable action_id=%s; using text fallback", action_id)
                before = self.db.watermarks(chat.chat_id)
                with scope_transaction(self.storage, action.account_id, action.group_id) as session:
                    ledger = session.get(LinuxSendRow, action_id)
                    ledger.watermarks_json = encode(before)
                if choice:
                    clicked = False
                    try:
                        self._gate(action)
                        panel = self.ui.open_sticker_panel(chat)
                        try:
                            self.ui.select_favorites(panel)
                            native_id = self._native_reserve(action, action_id, "sticker")
                            self.ui.click_sticker(panel, choice["row"], choice["col"])
                            clicked = True
                        finally:
                            self.ui.close_panel()
                        result = self._verify(chat, before, kind="sticker", md5=choice["md5"])
                        if not result:
                            raise BackendError("SEND_UNVERIFIED", "表情发送后未在数据库找到本号新消息")
                        receipts.extend(result.message_ids)
                        self._native_verified(native_id, result)
                    except BackendError as exc:
                        if exc.code == "STICKER_MD5_MISMATCH":
                            with scope_transaction(self.storage, action.account_id, action.group_id) as session:
                                session.get(LinuxSendRow, action_id).error_code = exc.code
                            log.warning("sticker_verification_failed action_id=%s native_id=%s code=%s expected_md5=%s actual_md5=%s message_id=%s fallback=text", action_id, native_id, exc.code, exc.details.get("expected_md5", choice["md5"]), exc.details.get("actual_md5", ""), exc.details.get("message_id", ""))
                            self.catalog.invalidate(self.account_id)
                            # Rebuild only maps the panel; it never clicks a sticker.
                            try:
                                self.catalog.rebuild(self.account_id, chat)
                            except Exception:
                                log.warning("sticker_rebuild_failed action_id=%s", action_id)
                        if clicked and exc.code != "STICKER_MD5_MISMATCH":
                            raise
                        choice = None
                if action.voice_path:
                    self._gate(action)
                    native_id = self._native_reserve(action, action_id, "voice")
                    duration = self.ui.send_voice(chat, action.voice_path, expected_sha256=expected["voice_sha256"])
                    result = self._verify(chat, before, kind="voice", duration=duration)
                    if not result:
                        raise BackendError("SEND_UNVERIFIED", "原生语音未通过账号、会话、作者、服务器 ID 和时长回读核验；禁止重放")
                    receipts.extend(result.message_ids)
                    self._native_verified(native_id, result)
                elif not choice:
                    if not action.media_paths and not action.file_paths and not action.text.strip():
                        raise BackendError("EMPTY_MESSAGE", "表情索引不可用，且没有可发送的文字兜底")
                    if action.text.strip() and (not action.media_paths or action.send_text_with_media):
                        self._gate(action)
                        before = self.db.watermarks(chat.chat_id)
                        native_id = self._native_reserve(action, action_id, "text")
                        self.ui.send_text(chat, action.text)
                        result = self._verify(chat, before, kind="text", text=action.text)
                        if not result:
                            raise BackendError("SEND_UNVERIFIED", "文本未通过数据库回读核验；已停止自动重发")
                        receipts.extend(result.message_ids)
                        self._native_verified(native_id, result)
                    for path in action.media_paths:
                        self._gate(action)
                        before = self.db.watermarks(chat.chat_id)
                        native_id = self._native_reserve(action, action_id, "image")
                        self.ui.send_image(chat, path)
                        result = self._verify(chat, before, kind="image")
                        if not result:
                            raise BackendError("SEND_UNVERIFIED", "原生图片未通过数据库回读核验")
                        receipts.extend(result.message_ids)
                        self._native_verified(native_id, result)
                    for path in action.file_paths:
                        self._gate(action)
                        file_path = Path(path)
                        if not file_path.is_file() or file_path.stat().st_size <= 0:
                            raise BackendError("FILE_MISSING", "待发送文件不存在或为空")
                        before = self.db.watermarks(chat.chat_id)
                        native_id = self._native_reserve(action, action_id, "file")
                        self.ui.send_file(chat, str(file_path))
                        result = self._verify(chat, before, kind="file", text=file_path.name, size=file_path.stat().st_size)
                        if not result:
                            raise BackendError("SEND_UNVERIFIED", "原生文件未通过数据库回读核验")
                        receipts.extend(result.message_ids)
                        self._native_verified(native_id, result)
                if action.voice_path:
                    message_type = "voice"
                elif choice:
                    message_type = "sticker"
                elif action.file_paths:
                    message_type = "image_file" if action.media_paths else "text_file" if action.text.strip() else "file"
                elif action.media_paths:
                    message_type = "text_image" if action.send_text_with_media else "image"
                else:
                    message_type = "text"
                with scope_transaction(self.storage, action.account_id, action.group_id) as session:
                    ledger = session.get(LinuxSendRow, action_id)
                    ledger.status, ledger.receipts_json, ledger.updated_at = "verified", encode(receipts), utc_now()
                    ledger.lease_until = None
                    ledger.message_type = message_type
                self.last_send_result = SendResult(True, tuple(receipts), message_type, choice["md5"] if choice else "")
                return self.last_send_result
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
                # Keep the real failure visible: a third-party exception may
                # carry a non-string code, and the failure screenshot must not
                # replace the send error with its own TypeError.
                log.warning(
                    "send_failed action_id=%s code=%s error=%s",
                    action_id, code, exc, exc_info=True,
                )
                try:
                    self.ui.diagnose(str(code))
                except Exception:
                    log.debug("send_diagnose_failed action_id=%s", action_id, exc_info=True)
                with scope_transaction(self.storage, action.account_id, action.group_id) as session:
                    ledger = session.get(LinuxSendRow, action_id)
                    ledger.status, ledger.error_code, ledger.updated_at = "uncertain", str(code)[:64], utc_now()
                    ledger.lease_until = None
                raise
            finally:
                self.ui.before_send = None

    def close(self) -> None:
        self._connected = False
