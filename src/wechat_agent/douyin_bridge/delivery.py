"""Deliver a prepared transfer through the existing Linux WeChat backend.

The backend is the only component allowed to click WeChat.  This adapter builds
ordinary :class:`OutboundAction` objects and calls ``backend.send`` so every
delivery inherits the existing STOP gate, rate limit, idempotency ledger and
database read-back.  It never talks to AT-SPI directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import OutboundAction


class DeliveryError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, details: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = details or {}


class WeChatDelivery:
    def __init__(self, backend: Any):
        self.backend = backend

    @property
    def available(self) -> bool:
        return self.backend is not None and callable(getattr(self.backend, "send", None))

    def _ensure_ready(self) -> None:
        if not self.available:
            raise DeliveryError("WECHAT_BACKEND_UNAVAILABLE", "微信后端尚未启动")
        ensure = getattr(self.backend, "_ensure_db", None)
        if callable(ensure):
            ensure()

    def _chat(self, wxid: str):
        self._ensure_ready()
        target = str(wxid or "").strip()
        if not target:
            raise DeliveryError("WECHAT_TARGET_MISSING", "绑定关系没有微信 wxid")
        try:
            return self.backend.db.resolve_chat(target)
        except Exception as exc:
            raise DeliveryError("WECHAT_CHAT_NOT_FOUND", str(exc)[:200]) from exc

    def _send(self, action: OutboundAction) -> dict[str, Any]:
        try:
            result = self.backend.send(action)
        except Exception as exc:
            raise DeliveryError(
                getattr(exc, "code", "WECHAT_SEND_FAILED"),
                str(exc)[:300],
            ) from exc
        if result is None or not getattr(result, "verified", False):
            raise DeliveryError("WECHAT_UNVERIFIED", "微信发送未通过数据库回读")
        return {
            "verified": True,
            "message_ids": list(getattr(result, "message_ids", ()) or ()),
            "message_type": str(getattr(result, "message_type", "") or ""),
        }

    def _action(self, chat, *, key: str, text: str = "", media_path: str = "", file_path: str = "") -> OutboundAction:
        account_id = str(getattr(self.backend, "account_id", "") or "")
        return OutboundAction(
            account_id=account_id,
            group_id=chat.chat_id,
            group_name=chat.name,
            text=text,
            action_type="dm",
            conversation_type="private",
            target_member_id=chat.chat_id,
            idempotency_key=key,
            media_paths=[media_path] if media_path else [],
            file_paths=[file_path] if file_path else [],
        )

    def deliver_text(self, wxid: str, text: str, *, key: str) -> dict[str, Any]:
        if not str(text or "").strip():
            raise DeliveryError("WECHAT_EMPTY_TEXT", "确认文案为空")
        chat = self._chat(wxid)
        return self._send(self._action(chat, key=key, text=str(text)))

    def deliver_image(self, wxid: str, path: Path | str, *, key: str) -> dict[str, Any]:
        file_path = Path(path)
        if not file_path.is_file():
            raise DeliveryError("WECHAT_MEDIA_MISSING", "待发送图片不存在")
        chat = self._chat(wxid)
        return self._send(self._action(chat, key=key, media_path=str(file_path)))

    def deliver_file(self, wxid: str, path: Path | str, *, key: str) -> dict[str, Any]:
        file_path = Path(path)
        if not file_path.is_file():
            raise DeliveryError("WECHAT_FILE_MISSING", "待发送文件不存在")
        if file_path.stat().st_size <= 0:
            raise DeliveryError("WECHAT_FILE_EMPTY", "待发送文件为空")
        chat = self._chat(wxid)
        return self._send(self._action(chat, key=key, file_path=str(file_path)))
