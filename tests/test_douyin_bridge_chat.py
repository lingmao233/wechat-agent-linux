from __future__ import annotations

from datetime import datetime, timezone

from wechat_agent.douyin_bridge.chat import (
    canonical_image_url,
    canonical_video_ref,
    message_fingerprint,
    parse_row,
)


def test_text_directions_and_fingerprint():
    inbound = parse_row({"who": "__peer__", "text": "在吗", "self": False})
    outbound = parse_row({"who": "__self__", "text": "在", "self": True})
    assert inbound.direction == "in" and inbound.kind == "text"
    assert outbound.direction == "out" and outbound.kind == "text"
    assert inbound.fingerprint != outbound.fingerprint


def test_image_row_is_classified_and_cdn_query_is_not_part_of_identity():
    first = parse_row(
        {
            "who": "__peer__",
            "text": "",
            "image": "https://p3-sign.douyinpic.com/obj/abc.webp?x-expires=1&x-signature=a",
        }
    )
    second = parse_row(
        {
            "who": "__peer__",
            "text": "",
            "image": "https://p3-sign.douyinpic.com/obj/abc.webp?x-expires=2&x-signature=b",
        }
    )
    assert first.kind == "image"
    assert first.fingerprint == second.fingerprint
    assert canonical_image_url(first.image_url) == "p3-sign.douyinpic.com/obj/abc.webp"


def test_video_link_row_is_video():
    message = parse_row(
        {
            "who": "__peer__",
            "text": "分享视频",
            "image": "https://p3-sign.douyinpic.com/cover.jpeg",
            "links": ["https://www.douyin.com/video/7126745726494821640"],
        }
    )
    assert message.kind == "video"
    assert message.video_ref.endswith("7126745726494821640")


def test_forwarded_card_without_id_is_still_video_but_has_no_ref():
    message = parse_row(
        {
            "who": "__peer__",
            "text": "这个视频好好笑",
            "image": "https://p3-sign.douyinpic.com/cover.jpeg",
            "card": True,
            "play": True,
        }
    )
    assert message.kind == "video"
    assert message.video_ref == ""


def test_same_text_from_same_direction_hashes_stably():
    observed = datetime.now(timezone.utc)
    first = parse_row({"who": "__peer__", "text": "哈哈", "self": False}, observed_at=observed)
    second = parse_row({"who": "__peer__", "text": "哈哈", "self": False}, observed_at=observed)
    assert first.fingerprint == second.fingerprint


def test_video_card_without_title_is_video_by_duration_or_play():
    by_duration = parse_row(
        {
            "who": "__peer__",
            "image": "https://p3-sign.douyinpic.com/obj/cover.jpeg",
            "rowText": "00:15",
            "duration": "00:15",
        }
    )
    assert by_duration.kind == "video"
    by_play = parse_row(
        {
            "who": "__peer__",
            "image": "https://p3-sign.douyinpic.com/obj/cover2.jpeg",
            "play": True,
        }
    )
    assert by_play.kind == "video"


def test_sticker_without_play_or_duration_is_image():
    message = parse_row(
        {
            "who": "__peer__",
            "image": "https://p3-sign.douyinpic.com/obj/sticker.webp",
            "rowText": "后荡后荡",
        }
    )
    assert message.kind == "image"


def test_empty_row_is_unknown():
    message = parse_row({"who": "__peer__"})
    assert message.kind == "unknown"
    assert message.direction == "in"


def test_avatar_image_does_not_turn_a_text_row_into_video():
    avatar = "https://p3.douyinpic.com/img/aweme-avatar/tos-cn-avt-0015_abc~c5_168x168.webp"
    message = parse_row({"who": "__peer__", "text": "在吗", "image": avatar})
    assert message.kind == "text"
    assert message.image_url == ""
    assert message.video_ref == ""


def test_text_row_with_avatar_attribute_stays_text():
    avatar = "https://p3-pc.douyinpic.com/img/aweme-avatar/tos-cn-i-0813c001_abc~c5_168x168.jpeg?from=1"
    message = parse_row({"who": "__peer__", "text": "假的吧", "attrs": [avatar]})
    assert message.kind == "text"
    assert message.video_ref == ""


def test_forwarded_card_with_title_and_cover_is_video():
    message = parse_row(
        {
            "who": "__peer__",
            "text": "这个视频好好笑",
            "image": "https://p3-sign.douyinpic.com/obj/cover.jpeg",
            "card": True,
            "play": True,
        }
    )
    assert message.kind == "video"


def test_message_fingerprint_is_direction_sensitive():
    common = dict(kind="text", text="好", image_url="", video_ref="")
    assert message_fingerprint(direction="in", **common) != message_fingerprint(direction="out", **common)


def test_canonical_video_ref_turns_aweme_id_into_share_url():
    assert canonical_video_ref("7126745726494821640") == "https://www.douyin.com/video/7126745726494821640"
    url = "https://www.douyin.com/video/7126745726494821640?modal_id=7126745726494821640"
    assert canonical_video_ref(url) == url
    assert canonical_video_ref("") == ""


def test_linkless_video_card_row_is_classified_as_video():
    row = parse_row(
        {
            "who": "__peer__",
            "self": False,
            "image": "https://p3.douyinpic.com/obj/cover.jpeg",
            "card": True,
            "play": True,
            "duration": "00:12",
            "rowText": "00:12",
        }
    )
    assert row.kind == "video"
    assert row.video_ref == ""
