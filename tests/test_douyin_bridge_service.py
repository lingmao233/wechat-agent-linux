from __future__ import annotations

from pathlib import Path
import time

from PIL import Image
import pytest

from wechat_agent.config import Settings
from wechat_agent.douyin_bridge.media import store_image_bytes
from wechat_agent.douyin_bridge.models import ChatMessage, ParsedVideo, StoredMedia
from wechat_agent.douyin_bridge.service import DouyinBridgeService
from wechat_agent.models import MessageEvent, utc_now
from wechat_agent.platform.base import ChatInfo, SendResult
from wechat_agent.storage import Storage


def png_bytes(size=(20, 20), color=(30, 120, 220)) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGBA", size, (*color, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeChat:
    def __init__(self):
        self.messages: list[ChatMessage] = []
        self.sent: list[str] = []
        self.opened = 0
        self.card_ref = ""
        self.resolve_calls: list[tuple[int, str, str]] = []
        self.open_targets: list[str] = []
        self.recoveries: list[bool] = []

    def open_conversation(self, target=None):
        self.opened += 1
        self.open_targets.append(str(target or ""))
        return {"ok": True, "name": target or "好友", "conversation_id": "conv-1", "opened": True}

    def resolve_video_ref(self, offset=0, *, image_url="", row_text=""):
        self.resolve_calls.append((offset, image_url, row_text))
        return self.card_ref

    def read_messages(self, limit=20):
        return list(self.messages)

    def send_text(self, text):
        self.sent.append(text)
        return {"ok": True, "verified": True}

    def fetch_image_bytes(self, url, *, max_bytes):
        return png_bytes()

    def status(self):
        return {"connected": True, "conversation": "好友", "conversation_id": "conv-1"}

    def calibrate(self):
        return {"row_classes": ["MessageBox"]}

    def recover_page(self, *, relaunch_browser: bool = False):
        self.recoveries.append(bool(relaunch_browser))
        return {"ok": True, "relaunch": bool(relaunch_browser), "rows": 3, "url": "https://www.douyin.com/chat"}


class FakeDelivery:
    available = True

    def __init__(self):
        self.calls = []

    def deliver_image(self, wxid, path, *, key):
        self.calls.append(("image", wxid, str(path), key))
        return {"verified": True, "message_ids": ["m-image"]}

    def deliver_file(self, wxid, path, *, key):
        self.calls.append(("file", wxid, str(path), key))
        return {"verified": True, "message_ids": ["m-file"]}


class FakeFetcher:
    def fetch(self, url, directory, *, max_bytes, referer="https://www.douyin.com/"):
        return store_image_bytes(png_bytes(), Path(directory), max_bytes=max_bytes)


class FakeDTK:
    configured = True

    def __init__(self):
        self.parsed = []

    def parse(self, ref):
        self.parsed.append(ref)
        return ParsedVideo(aweme_id="7126745726494821640", title="视频", author="作者", video_url="https://x/v.mp4", watermark=False)

    def download(self, video, destination, *, max_bytes):
        path = Path(destination) / "v.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = b"\x00\x00\x00\x18ftypmp42" + b"x" * 2048
        path.write_bytes(body)
        return StoredMedia(path=path, sha256="a" * 64, bytes=len(body), mime="video/mp4", kind="video", watermark_verified=False)


def make_settings(tmp_path, **overrides):
    values = dict(
        _env_file=None,
        wechat_platform="linux",
        wechat_account_id="bot",
        wechat_group_names=["G"],
        wechat_allow_send=True,
        embedding_enabled=False,
        database_url="sqlite:///" + (tmp_path / "memory.db").as_posix(),
        wechat_stop_file=tmp_path / "STOP",
        douyin_bridge_enabled=True,
        douyin_bridge_conversation="好友",
        douyin_bridge_dry_run=False,
        douyin_bridge_reply_enabled=True,
        douyin_bridge_media_dir=tmp_path / "media",
        douyin_bridge_context_seconds=300,
        douyin_bridge_max_jobs_per_hour=10,
        douyin_bridge_max_sticker_mb=5,
        douyin_bridge_max_video_mb=20,
        douyin_bridge_quiet_hours="03:00-04:00",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def service(tmp_path):
    settings = make_settings(tmp_path)
    storage = Storage(settings)
    storage.init_schema()
    chat = FakeChat()
    delivery = FakeDelivery()
    bridge = DouyinBridgeService(
        storage,
        settings,
        chat=chat,
        delivery=delivery,
        fetcher=FakeFetcher(),
        dtk=FakeDTK(),
    )
    yield bridge, chat, delivery, storage
    storage.engine.dispose()


def text_message(text: str) -> ChatMessage:
    from wechat_agent.douyin_bridge.chat import message_fingerprint

    return ChatMessage(
        direction="in",
        kind="text",
        text=text,
        fingerprint=message_fingerprint(direction="in", kind="text", text=text, image_url="", video_ref=""),
        observed_at=utc_now(),
    )


def image_message(url: str = "https://p3-sign.douyinpic.com/a.webp") -> ChatMessage:
    from wechat_agent.douyin_bridge.chat import message_fingerprint

    return ChatMessage(
        direction="in",
        kind="image",
        image_url=url,
        fingerprint=message_fingerprint(direction="in", kind="image", text="", image_url=url, video_ref=""),
        observed_at=utc_now(),
    )


def video_message(ref: str = "https://www.douyin.com/video/7126745726494821640") -> ChatMessage:
    from wechat_agent.douyin_bridge.chat import message_fingerprint

    return ChatMessage(
        direction="in",
        kind="video",
        video_ref=ref,
        fingerprint=message_fingerprint(direction="in", kind="video", text="", image_url="", video_ref=ref),
        observed_at=utc_now(),
    )


def video_card_message(cover_url: str = "https://p3-sign.douyinpic.com/obj/cover.jpeg") -> ChatMessage:
    """A pure forwarded video card: cover image + play badge, no link text."""

    from wechat_agent.douyin_bridge.chat import message_fingerprint

    return ChatMessage(
        direction="in",
        kind="video",
        image_url=cover_url,
        fingerprint=message_fingerprint(
            direction="in", kind="video", text="", image_url=cover_url, video_ref=""
        ),
        observed_at=utc_now(),
        raw={"rowText": "00:12", "duration": "00:12", "card": True, "play": True},
    )


def bind(bridge):
    return bridge.bind_manual({"conversation_id": "conv-1", "wxid": "wxid_friend", "wx_name": "好友"})


def test_sticker_transfer_end_to_end(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    chat.messages = []
    bridge.poll_once()  # baseline
    # No command: a new sticker must be forwarded automatically.
    chat.messages = [image_message()]
    result = bridge.run_once()
    assert result["ok"] is True
    assert [call[0] for call in delivery.calls] == ["file"]
    job = bridge.jobs()[0]
    assert job["status"] == "delivered"
    assert job["kind"] == "sticker"
    assert any("发到你微信了" in text for text in chat.sent)


def test_bare_video_link_is_auto_forwarded(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [text_message("https://v.douyin.com/L4NpDJ6/")]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "delivered"
    assert job["kind"] == "video"
    assert bridge.dtk.parsed == ["https://v.douyin.com/L4NpDJ6/"]
    assert [call[0] for call in delivery.calls] == ["file"]


def test_video_transfer_uses_dtk_and_file_delivery(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [video_message(), text_message("这个视频导出到微信")]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "delivered"
    assert job["kind"] == "video"
    assert bridge.dtk.parsed == ["https://www.douyin.com/video/7126745726494821640"]
    assert [call[0] for call in delivery.calls] == ["file"]


def test_delivered_media_files_are_deleted_immediately(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [image_message()]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "delivered"
    media_dir = Path(bridge.settings.douyin_bridge_media_dir)
    leftover = list(media_dir.glob("*")) if media_dir.is_dir() else []
    assert leftover == [], f"delivered media must be removed, found {leftover}"


def test_expired_media_is_swept_but_recent_media_kept(service):
    bridge, chat, _delivery, _storage = service
    media_dir = Path(bridge.settings.douyin_bridge_media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    expired = media_dir / "old.mp4"
    recent = media_dir / "recent.mp4"
    expired.write_bytes(b"x" * 64)
    recent.write_bytes(b"y" * 64)
    old = time.time() - 3 * 3600
    expired.touch()
    import os

    os.utime(expired, (old, old))
    bridge._cleanup_expired_media()
    assert not expired.exists()
    assert recent.exists()


def test_failed_job_media_is_kept_for_retention(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_dry_run = True
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [image_message()]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "ready"  # dry-run keeps the prepared file
    media_dir = Path(bridge.settings.douyin_bridge_media_dir)
    assert list(media_dir.glob("*")), "dry-run/prepared media stays until retention sweep"


def test_dry_run_prepares_without_delivery(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_dry_run = True
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [image_message(), text_message("转到微信")]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "ready"
    assert delivery.calls == []
    assert any("dry-run" in text for text in chat.sent)


def test_unbound_conversation_is_asked_to_bind(service):
    bridge, chat, _delivery, _storage = service
    chat.messages = []
    bridge.poll_once()
    chat.messages = [image_message(), text_message("转到微信")]
    result = bridge.run_once()
    assert result["ok"] is True
    assert bridge.jobs() == []
    assert any("绑定" in text for text in chat.sent)


def test_command_without_media_asks_for_media(service):
    bridge, chat, _delivery, _storage = service
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [text_message("把这个表情包转到微信")]
    bridge.run_once()
    assert bridge.jobs() == []
    assert any("先把表情包" in text for text in chat.sent)


def test_duplicate_poll_does_not_deliver_twice(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [image_message(), text_message("转到微信")]
    bridge.run_once()
    calls_after_first = list(delivery.calls)
    bridge.run_once()
    assert delivery.calls == calls_after_first
    assert len(bridge.jobs()) == 1


def test_video_card_without_link_asks_for_a_link(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_card_click_resolve = True
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    from wechat_agent.douyin_bridge.chat import message_fingerprint

    card = ChatMessage(
        direction="in",
        kind="video",
        image_url="https://p3-sign.douyinpic.com/obj/cover.jpeg",
        fingerprint=message_fingerprint(
            direction="in",
            kind="video",
            text="",
            image_url="https://p3-sign.douyinpic.com/obj/cover.jpeg",
            video_ref="",
        ),
        observed_at=utc_now(),
    )
    chat.messages = [card, text_message("这个视频转到微信")]
    bridge.run_once()
    assert bridge.jobs() == []
    assert delivery.calls == []
    assert any("没读到可解析的链接" in text for text in chat.sent)
    assert chat.resolve_calls and chat.resolve_calls[0][1].endswith("cover.jpeg")


def test_video_card_without_link_is_resolved_and_forwarded(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_card_click_resolve = True
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.card_ref = "https://www.douyin.com/video/7126745726494821640"
    chat.messages = [video_card_message()]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "delivered"
    assert job["kind"] == "video"
    assert job["source_url"] == chat.card_ref
    assert bridge.dtk.parsed == [chat.card_ref]
    assert [call[0] for call in delivery.calls] == ["file"]
    assert chat.resolve_calls, "card resolve must be attempted for a link-less card"
    offset, image_url, row_text = chat.resolve_calls[0]
    assert offset == 0
    assert image_url.endswith("cover.jpeg")
    assert row_text == "00:12"


def test_unbound_video_card_clicks_nothing_and_creates_no_job(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_card_click_resolve = True
    chat.messages = []
    bridge.poll_once()
    chat.card_ref = "https://www.douyin.com/video/7126745726494821640"
    chat.messages = [video_card_message()]
    bridge.run_once()
    assert chat.resolve_calls == [], "unbound conversation must never click a card"
    assert bridge.jobs() == []
    assert delivery.calls == []
    assert any("绑定" in text for text in chat.sent)


def test_unbound_sticker_does_not_create_job_or_deliver(service):
    bridge, chat, delivery, _storage = service
    chat.messages = []
    bridge.poll_once()
    chat.messages = [image_message()]
    bridge.run_once()
    assert bridge.jobs() == []
    assert delivery.calls == []
    assert any("绑定" in text for text in chat.sent)


def test_existing_history_at_start_is_not_forwarded(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    # Everything already in the conversation when the bridge starts is history:
    # mark it seen without forwarding or replying.
    chat.messages = [
        image_message("https://p3-sign.douyinpic.com/old-sticker.webp"),
        video_message("https://www.douyin.com/video/7000000000000000000"),
    ]
    bridge.poll_once()
    assert bridge.jobs() == []
    assert delivery.calls == []
    assert chat.sent == []

    # Only messages that arrive after the start cutoff are processed.
    chat.messages = [video_message("https://www.douyin.com/video/7000000000000000001")]
    bridge.run_once()
    jobs = bridge.jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "delivered"
    assert jobs[0]["source_url"] == "https://www.douyin.com/video/7000000000000000001"


def test_restart_keeps_history_cutoff_and_processes_only_later_messages(service):
    bridge, chat, delivery, storage = service
    bind(bridge)
    old = video_message("https://www.douyin.com/video/7000000000000000000")
    chat.messages = [old]
    bridge.poll_once()  # persists the cutoff and marks history seen
    assert bridge.jobs() == []

    # A restart builds a new service over the same storage: the old message is
    # history, but a message that arrived after the cutoff must still be handled.
    chat2 = FakeChat()
    chat2.messages = [old]
    delivery2 = FakeDelivery()
    restarted = DouyinBridgeService(
        storage,
        bridge.settings,
        chat=chat2,
        delivery=delivery2,
        fetcher=FakeFetcher(),
        dtk=FakeDTK(),
    )
    restarted.poll_once()
    assert restarted.jobs() == []
    assert delivery2.calls == []

    chat2.messages = [old, video_message("https://www.douyin.com/video/7000000000000000001")]
    restarted.run_once()
    jobs = restarted.jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "delivered"
    assert jobs[0]["source_url"] == "https://www.douyin.com/video/7000000000000000001"


def test_job_receipt_is_sent_to_the_jobs_own_douyin_conversation(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_dry_run = True
    bridge.binding.bind_manual(
        conversation_id="name:lingge",
        wxid="wxid_lingge",
        wx_name="凌歌",
        peer_name="凌歌",
    )
    job, created = bridge.store.create_job(
        account_id=bridge.account_id,
        conversation_id="name:lingge",
        peer_name="凌歌",
        kind="sticker",
        source_fingerprint="fp-lingge-1",
        source_text="",
        source_url="https://p3-sign.douyinpic.com/obj/lingge.webp",
        target_wxid="wxid_lingge",
        target_name="凌歌",
    )
    assert created
    chat.open_targets = []
    chat.sent = []
    bridge.process_job(job["job_id"])
    # The poll loop may have left another conversation open; the receipt must
    # reopen the job's own Douyin chat before sending.
    assert chat.open_targets == ["凌歌"]
    assert chat.sent and "dry-run" in chat.sent[-1]


def test_unverified_douyin_conversation_creates_no_job(service, monkeypatch):
    from wechat_agent.douyin_bridge.chat import DouyinChatError

    bridge, chat, delivery, _storage = service
    bind(bridge)
    chat.messages = [image_message()]

    def fail(target=None):
        raise DouyinChatError("CHAT_IDENTITY_UNCONFIRMED", "点开后无法确认当前会话")

    monkeypatch.setattr(chat, "open_conversation", fail)
    result = bridge.poll_once()
    assert result["ok"] is True
    assert result["conversations"][0]["error"]
    assert bridge.jobs() == []
    assert delivery.calls == []


def test_card_click_resolve_disabled_does_not_spam_link_requests(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_card_click_resolve = False
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.card_ref = "https://www.douyin.com/video/7126745726494821640"
    chat.messages = [video_card_message()]
    bridge.run_once()
    assert chat.resolve_calls == []
    assert bridge.jobs() == []
    assert chat.sent == []


class FakeWxDB:
    def __init__(self):
        self.rows_value = []
        self.calls = []

    def resolve_chat(self, wxid):
        return ChatInfo(wxid, "测试甲")

    def rows(self, chat_id, *, limit=50, **kwargs):
        self.calls.append(chat_id)
        return list(self.rows_value[-limit:])


class FakeWxMedia:
    def __init__(self, path):
        self.path = path

    def fetch_sticker(self, md5, size, url, **kwargs):
        return self.path


class FakeWxBackend:
    account_id = "bot"

    def __init__(self, db, media):
        self.db = db
        self.media = media
        self.outbound = []

    def _ensure_db(self):
        return None

    def _event(self, row, chat):
        return row["event"]

    def send(self, action):
        self.outbound.append(action)
        return SendResult(True, ("wx:m1",), action.message_type)


def test_wechat_sticker_goes_to_douyin_only_with_explicit_command(tmp_path, service):
    bridge, chat, _delivery, _storage = service
    bridge.binding.bind_manual(
        conversation_id="conv-1",
        wxid="wxid_testpeer0001",
        wx_name="测试甲",
        peer_name="测试甲",
    )
    gif = tmp_path / "wx-sticker.gif"
    gif.write_bytes(b"GIF89a" + b"x" * 128)
    db = FakeWxDB()
    backend = FakeWxBackend(db, FakeWxMedia(gif))
    bridge.attach_backend(backend)
    chat.media_sent = []

    def send_media(path):
        chat.media_sent.append(path)
        return {"ok": True, "verified": True}

    chat.send_media = send_media
    sticker_event = MessageEvent(
        account_id="bot",
        group_id="wxid_testpeer0001",
        group_name="测试甲",
        member_id="wxid_testpeer0001",
        member_name="测试甲",
        text="[表情]",
        message_type="sticker",
        sticker_md5="a" * 32,
        sticker_size=gif.stat().st_size,
        sticker_url="https://wxapp.tc.qq.com/sticker.gif",
        external_message_id="wx:1",
    )
    text_event = MessageEvent(
        account_id="bot",
        group_id="wxid_testpeer0001",
        group_name="测试甲",
        member_id="wxid_testpeer0001",
        member_name="测试甲",
        text="把这个表情包转到抖音",
        external_message_id="wx:2",
    )
    db.rows_value = [{"local_id": 1, "_shard": "message_0.db", "event": MessageEvent(
        account_id="bot", group_id="wxid_testpeer0001", group_name="测试甲",
        member_id="wxid_testpeer0001", member_name="测试甲", text="hello", external_message_id="wx:0")}]
    bridge.poll_wechat()  # baseline
    db.rows_value = [
        {"local_id": 1, "_shard": "message_0.db", "event": db.rows_value[0]["event"]},
        {"local_id": 2, "_shard": "message_0.db", "event": sticker_event},
        {"local_id": 3, "_shard": "message_0.db", "event": text_event},
    ]
    bridge.poll_wechat()
    assert chat.media_sent == [str(gif)]
    assert any("已经发到抖音" in str(action.text) for action in backend.outbound)


def test_multiple_bindings_are_polled_independently(service):
    bridge, chat, delivery, _storage = service
    bridge.binding.bind_manual(conversation_id="name:A", wxid="wxid_a", wx_name="A", peer_name="A")
    bridge.binding.bind_manual(conversation_id="name:B", wxid="wxid_b", wx_name="B", peer_name="B")
    by_conv = {"A": [], "B": []}
    current = {"name": ""}

    def open_conversation(target=None):
        current["name"] = target
        return {"ok": True, "name": target, "conversation_id": "name:" + target, "opened": True}

    def read_messages(limit=20):
        return list(by_conv.get(current["name"], []))

    chat.open_conversation = open_conversation
    chat.read_messages = read_messages
    bridge.poll_once()  # baseline both
    by_conv["A"] = [image_message("https://p3-sign.douyinpic.com/a.webp")]
    by_conv["B"] = [image_message("https://p3-sign.douyinpic.com/b.webp")]
    bridge.run_once()
    jobs = bridge.jobs()
    assert len(jobs) == 2
    assert {job["target_wxid"] for job in jobs} == {"wxid_a", "wxid_b"}
    assert {call[1] for call in delivery.calls} == {"wxid_a", "wxid_b"}


def test_burst_window_reopens_same_preview_for_late_stickers(service):
    bridge, chat, _delivery, _storage = service
    bind(bridge)
    rows = [{"name": "好友", "preview": "hello", "unread": 0}]
    chat.list_conversations = lambda: list(rows)
    chat.messages = []
    bridge.poll_once()  # baseline
    rows[0]["preview"] = "hello | [表情]"
    chat.messages = [image_message("https://p3-sign.douyinpic.com/one.webp")]
    bridge.poll_once()
    first_jobs = len(bridge.jobs())
    # Second sticker arrives after the first read; preview stays the same.
    chat.messages = [
        image_message("https://p3-sign.douyinpic.com/one.webp"),
        image_message("https://p3-sign.douyinpic.com/two.webp"),
    ]
    bridge.poll_once()
    assert len(bridge.jobs()) == first_jobs + 1


def test_list_scan_opens_only_changed_conversations(service):
    bridge, chat, _delivery, _storage = service
    bind(bridge)
    rows = [{"name": "好友", "preview": "hello", "unread": 0}]
    chat.list_conversations = lambda: list(rows)
    chat.messages = []
    bridge.poll_once()  # first pass: full scan
    opened_after_baseline = chat.opened
    bridge.poll_once()  # unchanged preview: no click
    assert chat.opened == opened_after_baseline
    rows[0]["preview"] = "hello | 新消息"
    bridge.poll_once()
    assert chat.opened == opened_after_baseline + 1


def test_one_failed_binding_does_not_block_other_bindings(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_bindings_per_poll = 5
    bridge.binding.bind_manual(conversation_id="name:good", wxid="wxid_good", wx_name="Good", peer_name="good")
    bridge.binding.bind_manual(conversation_id="name:bad", wxid="wxid_bad", wx_name="Bad", peer_name="bad")
    by_conv = {"good": [], "bad": []}
    current = {"name": ""}

    def open_conversation(target=None):
        if target == "bad":
            from wechat_agent.douyin_bridge.chat import DouyinChatError

            raise DouyinChatError("CHAT_IDENTITY_UNCONFIRMED", "点开后当前会话不对")
        current["name"] = target
        return {"ok": True, "name": target, "conversation_id": "name:" + target, "opened": True}

    def read_messages(limit=20):
        return list(by_conv.get(current["name"], []))

    chat.open_conversation = open_conversation
    chat.read_messages = read_messages
    bridge.poll_once()
    by_conv["good"] = [image_message("https://p3-sign.douyinpic.com/good.webp")]
    bridge.run_once()
    assert bridge.jobs()[0]["status"] == "delivered"
    assert delivery.calls


def test_binding_rejects_duplicate_wechat_display_name(service):
    bridge, _chat, _delivery, _storage = service
    bridge.binding.bind_manual(conversation_id="name:one", wxid="wxid_one", wx_name="同名", peer_name="one")
    with pytest.raises(ValueError) as excinfo:
        bridge.binding.bind_manual(conversation_id="name:two", wxid="wxid_two", wx_name="同名", peer_name="two")
    assert "duplicate_wechat_display_name" in str(excinfo.value)


def test_binding_rejects_group_targets(service):
    bridge, _chat, _delivery, _storage = service
    import pytest

    with pytest.raises(ValueError) as excinfo:
        bridge.binding.bind_manual(
            conversation_id="conv-g",
            wxid="123456@chatroom",
            wx_name="群",
            peer_name="某个群",
        )
    assert "private_only" in str(excinfo.value)


def test_bind_manual_by_peer_name_without_conversation_id(service):
    bridge, _chat, _delivery, _storage = service
    binding = bridge.bind_manual({"peer_name": "新抖音", "wxid": "wxid_new", "wx_name": "新微信"})
    assert binding["conversation_id"] == bridge.binding_key("新抖音")
    assert binding["peer_name"] == "新抖音"


def test_code_with_peer_name_watches_unbound_conversation(service):
    bridge, chat, _delivery, _storage = service
    chat.open_conversation = lambda target=None: {
        "ok": True,
        "name": target or "好友",
        "conversation_id": bridge.binding_key(target or "好友"),
        "opened": True,
    }
    code = bridge.create_code({"wxid": "wxid_new", "wx_name": "新微信", "peer_name": "新抖音"})
    assert code["peer_name"] == "新抖音"
    assert bridge.status()["pending_peers"] == ["新抖音"]
    chat.messages = []
    bridge.poll_once()  # baseline the pending conversation
    chat.messages = [text_message(f"绑定码 {code['code']}")]
    bridge.run_once()
    assert bridge.bindings()[0]["wxid"] == "wxid_new"
    assert bridge.status()["pending_peers"] == []


def test_static_sticker_becomes_two_frame_native_gif(tmp_path):
    from PIL import Image

    from wechat_agent.douyin_bridge.media import sticker_gif_for_wechat
    from wechat_agent.douyin_bridge.models import StoredMedia

    source = tmp_path / "static.png"
    Image.new("RGB", (64, 64), (240, 200, 20)).save(source)
    media = StoredMedia(path=source, sha256="x", bytes=source.stat().st_size, mime="image/png", kind="image")
    gif = sticker_gif_for_wechat(media)
    with Image.open(gif) as image:
        assert image.format == "GIF"
        assert getattr(image, "n_frames", 1) >= 2


def test_resend_job_creates_new_ledger_key_for_undelivered_media(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [image_message()]
    bridge.run_once()
    original = bridge.jobs()[0]
    assert original["status"] == "delivered"
    # Force the original to look like a pre-send failure with no message ids.
    bridge.store.update_job(
        original["job_id"],
        status="failed",
        error_code="CHAT_IDENTITY_UNCONFIRMED",
        error_detail="test",
        delivered_ids_json="[]",
    )
    result = bridge.resend_job(original["job_id"])
    assert result["created"] is True
    assert result["job"]["job_id"] != original["job_id"]
    assert result["job"]["status"] == "detected"
    assert bridge._jobs.qsize() >= 1


def test_douyin_chat_bilibili_link_creates_bilibili_job(service):
    bridge, chat, _delivery, _storage = service
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [text_message("https://www.bilibili.com/video/BV1xx411c7mD")]
    bridge.poll_once()
    jobs = bridge.jobs()
    assert jobs
    assert jobs[0]["payload"]["provider"] == "bilibili"
    assert jobs[0]["payload"]["origin"] == "douyin"
    assert jobs[0]["source_url"].startswith("https://www.bilibili.com/video/BV")


def test_wechat_link_message_creates_export_job(service):
    bridge, chat, _delivery, _storage = service
    bridge.binding.bind_manual(
        conversation_id="conv-1",
        wxid="wxid_testpeer0001",
        wx_name="测试甲",
        peer_name="测试甲",
    )
    db = FakeWxDB()
    backend = FakeWxBackend(db, FakeWxMedia(None))
    bridge.attach_backend(backend)
    link_event = MessageEvent(
        account_id="bot",
        group_id="wxid_testpeer0001",
        group_name="测试甲",
        member_id="wxid_testpeer0001",
        member_name="测试甲",
        text="https://www.bilibili.com/video/BV1xx411c7mD",
        external_message_id="wx:link:1",
    )
    db.rows_value = [{"local_id": 1, "_shard": "message_0.db", "event": MessageEvent(
        account_id="bot", group_id="wxid_testpeer0001", group_name="测试甲",
        member_id="wxid_testpeer0001", member_name="测试甲", text="hello", external_message_id="wx:0")}]
    bridge.poll_wechat()
    db.rows_value.append({"local_id": 2, "_shard": "message_0.db", "event": link_event})
    bridge.poll_wechat()
    jobs = bridge.jobs()
    assert jobs
    assert jobs[0]["payload"]["origin"] == "wechat"
    assert jobs[0]["payload"]["provider"] == "bilibili"
    assert jobs[0]["target_wxid"] == "wxid_testpeer0001"


def test_binding_code_flow(service):
    bridge, chat, _delivery, _storage = service
    code = bridge.create_code({"wxid": "wxid_friend", "wx_name": "好友"})
    chat.messages = []
    bridge.poll_once()
    chat.messages = [text_message(f"绑定码 {code['code']}")]
    bridge.run_once()
    binding = bridge.bindings()[0]
    assert binding["wxid"] == "wxid_friend"
    assert binding["conversation_id"] == "conv-1"
    assert any("绑定好了" in text for text in chat.sent)

class FakeAlbumDTK(FakeDTK):
    def __init__(self, image_urls=("https://p3.douyinpic.com/a.webp", "https://p3.douyinpic.com/b.webp")):
        super().__init__()
        self.image_urls = tuple(image_urls)

    def parse(self, ref):
        self.parsed.append(ref)
        return ParsedVideo(
            aweme_id="7126745726494821640",
            title="图集",
            author="作者",
            video_url="",
            kind="image_album",
            image_urls=self.image_urls,
        )


class FailingFetcher:
    def fetch(self, url, directory, *, max_bytes, referer="https://www.douyin.com/"):
        from wechat_agent.douyin_bridge.media import MediaError

        raise MediaError("MEDIA_DECODE_FAILED", "坏图")


def test_album_link_is_forwarded_as_native_images(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    bridge.dtk = FakeAlbumDTK()
    chat.messages = []
    bridge.poll_once()
    chat.messages = [text_message("https://v.douyin.com/album123/")]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "delivered"
    assert job["kind"] == "video"
    assert job["mime"] == "image/album"
    assert job["payload"]["album_count"] == 2
    assert [call[0] for call in delivery.calls] == ["image", "image"]
    assert any("图集（2 张）发到你微信了" in text for text in chat.sent)


def test_album_respects_max_album_images(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_max_album_images = 1
    bridge.dtk = FakeAlbumDTK()
    bind(bridge)
    chat.messages = []
    bridge.poll_once()
    chat.messages = [text_message("https://v.douyin.com/album123/")]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "delivered"
    assert job["payload"]["album_count"] == 1
    assert [call[0] for call in delivery.calls] == ["image"]


def test_album_prepare_failure_is_reported_without_delivery(service):
    bridge, chat, delivery, _storage = service
    bind(bridge)
    bridge.dtk = FakeAlbumDTK()
    bridge.fetcher = FailingFetcher()
    chat.messages = []
    bridge.poll_once()
    chat.messages = [text_message("https://v.douyin.com/album123/")]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "failed"
    assert job["error_code"] == "MEDIA_DECODE_FAILED"
    assert delivery.calls == []


def test_album_dry_run_prepares_without_delivery(service):
    bridge, chat, delivery, _storage = service
    bridge.settings.douyin_bridge_dry_run = True
    bind(bridge)
    bridge.dtk = FakeAlbumDTK()
    chat.messages = []
    bridge.poll_once()
    chat.messages = [text_message("https://v.douyin.com/album123/")]
    bridge.run_once()
    job = bridge.jobs()[0]
    assert job["status"] == "ready"
    assert job["payload"]["album_count"] == 2
    assert delivery.calls == []
    assert any("图集（2 张）已经准备好了" in text for text in chat.sent)


# -- page-stall watchdog ---------------------------------------------------
def identity_failure_chat(chat):
    """Make every conversation open fail identity confirmation (stuck tab)."""

    from wechat_agent.douyin_bridge.chat import DouyinChatError

    def fail(target=None):
        raise DouyinChatError("CHAT_IDENTITY_UNCONFIRMED", "点开后无法确认当前会话")

    chat.open_conversation = fail


def test_watchdog_reloads_chat_page_after_consecutive_identity_failures(service):
    bridge, chat, _delivery, _storage = service
    bridge.settings.douyin_bridge_watchdog_identity_threshold = 2
    bridge.settings.douyin_bridge_watchdog_console_restart = False
    bind(bridge)
    identity_failure_chat(chat)
    bridge.poll_once()
    assert chat.recoveries == []
    bridge.poll_once()
    # First recovery rung: reload /chat in place (no whole-browser relaunch).
    assert chat.recoveries == [False]
    watchdog = bridge.status()["watchdog"]
    assert watchdog["level"] == 1
    assert watchdog["streak"] == 0


def test_watchdog_relaunches_browser_when_reload_does_not_help(service):
    bridge, chat, _delivery, _storage = service
    bridge.settings.douyin_bridge_watchdog_identity_threshold = 2
    bridge.settings.douyin_bridge_watchdog_console_restart = False
    bind(bridge)
    identity_failure_chat(chat)
    for _ in range(2):
        bridge.poll_once()
    for _ in range(2):
        bridge.poll_once()
    # Second rung: the persistent browser is relaunched from the same profile.
    assert chat.recoveries == [False, True]
    assert bridge.status()["watchdog"]["level"] == 2


def test_watchdog_stops_auto_recovery_when_page_needs_a_human(service):
    from wechat_agent.douyin_bridge.chat import DouyinChatError

    bridge, chat, _delivery, _storage = service
    bridge.settings.douyin_bridge_watchdog_identity_threshold = 2
    bridge.settings.douyin_bridge_watchdog_console_restart = False
    bind(bridge)
    identity_failure_chat(chat)
    attempts = []

    def challenge(*, relaunch_browser: bool = False):
        attempts.append(bool(relaunch_browser))
        raise DouyinChatError("CHAT_CHALLENGE", "抖音页面出现验证码或安全验证")

    chat.recover_page = challenge
    for _ in range(4):
        bridge.poll_once()
    # A challenge page is never auto-recovered; it must wait for the operator.
    assert attempts == [False]
    watchdog = bridge.status()["watchdog"]
    assert watchdog["blocked"].startswith("CHAT_CHALLENGE")
    assert watchdog["streak"] == 0


def test_watchdog_clears_after_a_successful_poll(service):
    bridge, chat, _delivery, _storage = service
    bridge.settings.douyin_bridge_watchdog_identity_threshold = 2
    bridge.settings.douyin_bridge_watchdog_console_restart = False
    bind(bridge)
    original = chat.open_conversation
    identity_failure_chat(chat)
    bridge.poll_once()
    bridge.poll_once()
    assert bridge.status()["watchdog"]["level"] == 1
    chat.open_conversation = original
    bridge.poll_once()
    watchdog = bridge.status()["watchdog"]
    assert watchdog["level"] == 0
    assert watchdog["streak"] == 0
    assert watchdog["blocked"] == ""
