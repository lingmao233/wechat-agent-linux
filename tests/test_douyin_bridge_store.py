from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from wechat_agent.config import Settings
from wechat_agent.models import utc_now
from wechat_agent.storage import Storage
from wechat_agent.douyin_bridge.models import ChatMessage
from wechat_agent.douyin_bridge.store import BridgeStore


@pytest.fixture
def store(tmp_path):
    settings = Settings(
        _env_file=None,
        wechat_platform="linux",
        wechat_account_id="bot",
        wechat_group_names=["G"],
        wechat_allow_send=True,
        embedding_enabled=False,
        database_url="sqlite:///" + (tmp_path / "memory.db").as_posix(),
        wechat_stop_file=tmp_path / "STOP",
    )
    storage = Storage(settings)
    storage.init_schema()
    yield BridgeStore(storage)
    storage.engine.dispose()


def test_binding_is_idempotent_and_replaces_target(store):
    first = store.bind(account_id="bot", conversation_id="conv-1", peer_name="好友", wxid="wxid_a", wx_name="A")
    second = store.bind(account_id="bot", conversation_id="conv-1", peer_name="好友", wxid="wxid_b", wx_name="B")
    assert first["id"] == second["id"]
    assert second["wxid"] == "wxid_b"
    assert store.binding_for("bot", "conv-1")["wxid"] == "wxid_b"
    assert store.bound_wxids("bot") == ["wxid_b"]


def test_code_is_one_time_and_expires(store):
    created = store.create_code(account_id="bot", wxid="wxid_a", wx_name="A", ttl_minutes=10)
    assert len(created["code"]) == 6 and created["code"].isdigit()
    redeemed = store.redeem_code("bot", created["code"])
    assert redeemed == {"wxid": "wxid_a", "wx_name": "A", "expires_at": redeemed["expires_at"]}
    assert store.redeem_code("bot", created["code"]) is None
    assert store.redeem_code("bot", "000000") is None


def test_job_unique_per_media_and_target(store):
    payload = dict(
        account_id="bot",
        conversation_id="conv-1",
        peer_name="好友",
        kind="sticker",
        source_fingerprint="fp-1",
        source_text="",
        source_url="https://x/a.webp",
        target_wxid="wxid_a",
    )
    first, created = store.create_job(**payload)
    second, created_again = store.create_job(**payload)
    assert created is True and created_again is False
    assert first["job_id"] == second["job_id"]


def test_peer_media_and_consumption(store):
    message = ChatMessage(direction="in", kind="image", image_url="https://x/a.webp", fingerprint="fp-a", observed_at=utc_now())
    store.set_peer_media(message, account_id="bot", conversation_id="conv-1", peer_name="好友")
    pending = store.peer_media("bot", "conv-1")
    assert pending["kind"] == "image" and pending["fingerprint"] == "fp-a"
    assert pending["consumed"] is False
    store.consume_peer_media("bot", "conv-1", "fp-a")
    assert store.peer_media("bot", "conv-1")["consumed"] is True


def test_seen_is_scoped_and_prunable(store):
    store.mark_seen("bot", "conv-1", "fp")
    store.mark_seen("bot", "conv-1", "fp")  # duplicate insert is a no-op
    assert store.seen("bot", "conv-1", "fp") is True
    assert store.seen("bot", "conv-2", "fp") is False
    assert store.prune_seen(before=utc_now() + timedelta(days=8)) >= 1
