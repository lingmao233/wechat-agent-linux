"""Binding codes and the Douyin-conversation -> WeChat(wxid) relation."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from .store import BridgeStore


class BindingService:
    """Owns the authorization boundary for private delivery.

    A binding is created in one of two ways:

    * an operator binds a conversation to a wxid through the console API; or
    * the operator creates a one-time code for that wxid, and the Douyin side
      redeems it from the watched conversation.

    Both paths end in the same table.  Display names never authorize delivery;
    the stable conversation id (or, when Douyin's network payload does not
    expose one, the operator-configured conversation key) is the key.
    """

    def __init__(self, store: BridgeStore, settings: Settings):
        self.store = store
        self.settings = settings

    @property
    def account_id(self) -> str:
        return str(getattr(self.settings, "wechat_account_id", "") or "").strip() or "default"

    def create_code(self, *, wxid: object, wx_name: object = "") -> dict[str, Any]:
        target = str(wxid or "").strip()
        if not target:
            raise ValueError("wxid_placeholder001")
        if target.endswith("@chatroom"):
            raise ValueError("douyin_bridge_private_only")
        if target == self.account_id:
            raise ValueError("cannot_bind_the_agent_account_itself")
        ttl = int(getattr(self.settings, "binding_code_ttl_minutes", 10) or 10)
        return self.store.create_code(
            account_id=self.account_id,
            wxid=target,
            wx_name=str(wx_name or "").strip(),
            ttl_minutes=ttl,
        )

    def redeem(self, code: object) -> dict[str, Any] | None:
        return self.store.redeem_code(self.account_id, code)

    def bind_with_code(
        self,
        *,
        conversation_id: str,
        peer_name: str,
        code: object,
        douyin_user_id: str = "",
    ) -> dict[str, Any]:
        redeemed = self.redeem(code)
        if redeemed is None:
            raise ValueError("binding_code_invalid_or_expired")
        target = str(redeemed["wxid"])
        if target.endswith("@chatroom"):
            raise ValueError("douyin_bridge_private_only")
        display_name = str(redeemed.get("wx_name") or "").strip()
        if display_name and any(
            str(item.get("wxid") or "") != target
            and str(item.get("wx_name") or "") == display_name
            for item in self.list()
        ):
            raise ValueError("duplicate_wechat_display_name")
        return self.store.bind(
            account_id=self.account_id,
            conversation_id=conversation_id,
            peer_name=peer_name,
            douyin_user_id=douyin_user_id,
            wxid=target,
            wx_name=display_name,
        )

    def bind_manual(
        self,
        *,
        conversation_id: object,
        wxid: object,
        peer_name: object = "",
        wx_name: object = "",
        douyin_user_id: object = "",
    ) -> dict[str, Any]:
        conversation = str(conversation_id or "").strip()
        target = str(wxid or "").strip()
        if not conversation:
            raise ValueError("conversation_id_required")
        if not target:
            raise ValueError("wxid_placeholder001")
        if target.endswith("@chatroom"):
            raise ValueError("douyin_bridge_private_only")
        display_name = str(wx_name or "").strip()
        if display_name:
            conflicts = [
                item
                for item in self.list()
                if str(item.get("wxid") or "") != target
                and str(item.get("wx_name") or "") == display_name
            ]
            if conflicts:
                raise ValueError("duplicate_wechat_display_name")
        return self.store.bind(
            account_id=self.account_id,
            conversation_id=conversation,
            peer_name=str(peer_name or "").strip(),
            douyin_user_id=str(douyin_user_id or "").strip(),
            wxid=target,
            wx_name=display_name,
        )

    def for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        return self.store.binding_for(self.account_id, conversation_id)

    def list(self) -> list[dict[str, Any]]:
        return self.store.list_bindings(self.account_id)

    def delete(self, binding_id: object) -> bool:
        return self.store.delete_binding(self.account_id, binding_id)

    def wxids(self) -> list[str]:
        return self.store.bound_wxids(self.account_id)
