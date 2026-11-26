from __future__ import annotations

from pathlib import Path

import pytest

from wechat_agent.douyin_bridge.delivery import DeliveryError, WeChatDelivery
from wechat_agent.platform.base import ChatInfo, SendResult


class FakeDB:
    def resolve_chat(self, value):
        if value != "wxid_friend":
            raise RuntimeError("not found")
        return ChatInfo("wxid_friend", "Friend")


class FakeBackend:
    account_id = "bot"

    def __init__(self):
        self.db = FakeDB()
        self.actions = []

    def _ensure_db(self):
        return None

    def send(self, action):
        self.actions.append(action)
        return SendResult(True, ("m1",), "image" if action.media_paths else "file" if action.file_paths else "text")


def test_delivery_builds_private_action_with_stable_wxid(tmp_path):
    backend = FakeBackend()
    delivery = WeChatDelivery(backend)
    image = tmp_path / "sticker.png"
    image.write_bytes(b"x")
    delivery.deliver_image("wxid_friend", image, key="k-image")
    action = backend.actions[-1]
    assert action.action_type == "dm"
    assert action.conversation_type == "private"
    assert action.group_id == "wxid_friend"
    assert action.target_member_id == "wxid_friend"
    assert action.media_paths == [str(image)]
    assert action.file_paths == []


def test_delivery_rejects_missing_file(tmp_path):
    delivery = WeChatDelivery(FakeBackend())
    with pytest.raises(DeliveryError) as excinfo:
        delivery.deliver_file("wxid_friend", tmp_path / "missing.mp4", key="k")
    assert excinfo.value.code == "WECHAT_FILE_MISSING"


def test_delivery_rejects_unverified_backend_result():
    class Unverified(FakeBackend):
        def send(self, action):
            return SendResult(False, (), "image")

    delivery = WeChatDelivery(Unverified())
    image = Path(__file__)
    with pytest.raises(DeliveryError) as excinfo:
        delivery.deliver_image("wxid_friend", image, key="k")
    assert excinfo.value.code == "WECHAT_UNVERIFIED"
