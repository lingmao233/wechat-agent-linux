from __future__ import annotations

import pytest

from wechat_agent.douyin_bridge.intent import (
    extract_aweme_ref,
    extract_url,
    looks_like_media_link,
    looks_like_video_ref,
    parse_command,
)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("把这个表情包转到微信", "sticker"),
        ("这个表情包帮我发到微信", "sticker"),
        ("把刚才那个视频导到微信", "video"),
        ("这个视频无水印发我微信", "video"),
        ("转到微信", "any"),
        ("这条发到微信", "any"),
    ],
)
def test_transfer_commands_are_recognized(text, kind):
    command = parse_command(text)
    assert command is not None
    assert command.action == "transfer"
    assert command.kind == kind
    assert command.confidence >= 0.75


def test_bare_link_is_media_not_command():
    text = "https://www.douyin.com/video/7126745726494821640"
    assert parse_command(text) is None
    assert looks_like_media_link(text)
    assert extract_aweme_ref(text).endswith("7126745726494821640")


def test_link_inside_command_is_extracted():
    command = parse_command("这个视频 https://v.douyin.com/L4NpDJ6/ 导出到微信")
    assert command is not None
    assert command.action == "transfer"
    assert command.url == "https://v.douyin.com/L4NpDJ6/"


def test_wechat_mention_alone_is_not_a_transfer():
    assert parse_command("我在微信看到这个视频，挺好笑的") is None
    assert parse_command("微信上那个表情包叫什么") is None


def test_negated_transfer_is_not_a_command():
    assert parse_command("不要转到微信") is None
    # "先别发到微信" is a cancel instruction, not a transfer authorization.
    cancel = parse_command("先别发到微信")
    assert cancel is not None and cancel.action == "cancel"


def test_capability_question_is_not_a_command():
    assert parse_command("能不能转到微信吗？") is None


def test_binding_code():
    command = parse_command("绑定码 123456")
    assert command is not None
    assert command.action == "bind"
    assert command.code == "123456"
    command = parse_command("绑定微信：654321")
    assert command is not None and command.code == "654321"


def test_status_and_cancel():
    status = parse_command("转到微信了吗")
    assert status is not None and status.action == "status"
    cancel = parse_command("算了，不转到微信了")
    assert cancel is not None and cancel.action == "cancel"


def test_full_width_and_whitespace_are_normalized():
    command = parse_command("把这个表情包  转 到 微信")
    assert command is not None
    assert command.action == "transfer"
    assert command.kind == "sticker"


def test_extract_url_trims_chinese_punctuation():
    assert extract_url("看这个 https://v.douyin.com/abc/，笑死") == "https://v.douyin.com/abc/"


def test_only_video_urls_are_valid_video_refs():
    assert looks_like_video_ref("https://www.douyin.com/video/7126745726494821640")
    assert looks_like_video_ref("https://v.douyin.com/L4NpDJ6/")
    assert looks_like_video_ref("7126745726494821640")
    assert not looks_like_video_ref("https://p3-sign.douyinpic.com/obj/cover.jpeg")
    assert extract_aweme_ref("https://p3.douyinpic.com/img/aweme-avatar/tos-cn-avt-0015_abc~c5_168x168.webp") == ""
