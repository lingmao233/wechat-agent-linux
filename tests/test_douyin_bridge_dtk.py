from __future__ import annotations

import httpx
import pytest

from wechat_agent.douyin_bridge.dtk import DTKClient, DTKError, parse_payload


def test_parse_payload_v5_media_video():
    video = parse_payload(
        {
            "success": True,
            "data": {
                "aweme_id": "7126745726494821640",
                "title": "一条视频",
                "author": {"nickname": "作者"},
                "media": {"video": {"url": "https://v.douyin.com/a.mp4", "watermark": False}},
            },
        },
        fallback="",
    )
    assert video.aweme_id == "7126745726494821640"
    assert video.title == "一条视频"
    assert video.video_url.endswith("a.mp4")
    assert video.watermark is False


def test_watermarked_only_stream_is_refused():
    with pytest.raises(DTKError) as excinfo:
        parse_payload({"data": {"media": {"video": {"url": "https://x/a.mp4", "watermark": True}}}})
    assert excinfo.value.code == "DTK_WATERMARKED"


def test_clean_play_addr_wins_over_watermarked_download_addr():
    video = parse_payload(
        {
            "data": {
                "video": {
                    "play_addr": {"url_list": ["https://x/clean.mp4"], "watermark": False},
                    "download_addr": {"url_list": ["https://x/watermark.mp4"], "watermark": True},
                }
            }
        }
    )
    assert video.video_url == "https://x/clean.mp4"
    assert video.watermark is False


def test_nested_task_data_is_unwrapped():
    video = parse_payload(
        {
            "data": {
                "state": "done",
                "data": {
                    "aweme_id": "1234567890123456",
                    "media": {"video": {"urls": ["https://x/v.mp4"], "watermark": False}},
                },
            }
        }
    )
    assert video.aweme_id == "1234567890123456"
    assert video.video_url == "https://x/v.mp4"


def test_missing_media_raises():
    with pytest.raises(DTKError) as excinfo:
        parse_payload({"data": {"title": "only metadata"}})
    assert excinfo.value.code == "DTK_NO_MEDIA"


def test_audio_only_card_is_not_treated_as_video():
    with pytest.raises(DTKError) as excinfo:
        parse_payload(
            {
                "data": {
                    "aweme_id": "7681300956843957617",
                    "media": {
                        "video": {
                            "url": "https://sf6-cdn-tos.douyinstatic.com/obj/ies-music/7662369252732685082.mp3",
                            "watermark": None,
                        }
                    },
                }
            }
        )
    assert excinfo.value.code == "DTK_NO_MEDIA"


def _client(handler, *, download_attempts: int = 2):
    transport = httpx.MockTransport(handler)
    return DTKClient(
        "http://dtk.test",
        api_key="dtk_test",
        wait_seconds=1,
        download_attempts=download_attempts,
        client=httpx.Client(base_url="http://dtk.test", transport=transport),
    )


def test_async_parse_polls_task():
    seen = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/parse":
            assert request.headers["authorization"] == "Bearer dtk_test"
            return httpx.Response(202, json={"success": True, "data": {"task_id": "t1", "state": "queued"}})
        if request.url.path == "/api/v1/tasks/t1":
            seen["get"] += 1
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "state": "done",
                        "data": {
                            "aweme_id": "7126745726494821640",
                            "title": "done",
                            "media": {"video": {"url": "https://x/v.mp4", "watermark": False}},
                        },
                    },
                },
            )
        return httpx.Response(404, json={"success": False, "error": {"code": "NOT_FOUND"}})

    client = _client(handler)
    video = client.parse("https://v.douyin.com/x/")
    assert video.video_url == "https://x/v.mp4"
    assert seen["get"] == 1


def test_v4_fallback_uses_hybrid_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/parse":
            return httpx.Response(404, text="not found")
        if request.url.path == "/api/hybrid/video_data":
            return httpx.Response(
                200,
                json={
                    "aweme_id": "7126745726494821640",
                    "desc": "v4 视频",
                    "video": {
                        "play_addr": {"url_list": ["https://x/v4.mp4"]},
                        "download_addr": {"url_list": ["https://x/v4-watermark.mp4"], "watermark": True},
                    },
                },
            )
        return httpx.Response(404)

    client = _client(handler)
    video = client.parse("https://v.douyin.com/x/")
    assert video.video_url == "https://x/v4.mp4"
    assert video.title == "v4 视频"


def test_failed_task_surfaces_code():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/parse":
            return httpx.Response(202, json={"success": True, "data": {"task_id": "t2", "state": "queued"}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "state": "failed",
                    "error": {"code": "UPSTREAM_RISK_CONTROL", "message": "risk", "retryable": True},
                },
            },
        )

    client = _client(handler)
    with pytest.raises(DTKError) as excinfo:
        client.parse("https://v.douyin.com/x/")
    assert excinfo.value.code == "UPSTREAM_RISK_CONTROL"
    assert excinfo.value.retryable is True


def test_download_writes_verified_mp4(tmp_path):
    body = b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "video/mp4", "content-length": str(len(body))},
        )

    client = _client(handler)
    video = parse_payload({"data": {"aweme_id": "1234567890123456", "title": "标题", "media": {"video": {"url": "https://x/v.mp4", "watermark": False}}}})
    media = client.download(video, tmp_path, max_bytes=1024 * 1024)
    assert media.path.is_file()
    assert media.path.suffix == ".mp4"
    assert media.bytes == len(body)
    assert media.watermark_verified is False
    assert media.sha256


def test_download_rejects_non_video_body(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>login</html>", headers={"content-type": "text/html"})

    client = _client(handler)
    video = parse_payload({"data": {"aweme_id": "1", "media": {"video": {"url": "https://x/v.mp4", "watermark": False}}}})
    with pytest.raises(DTKError) as excinfo:
        client.download(video, tmp_path, max_bytes=1024)
    assert excinfo.value.code == "DTK_NOT_VIDEO"


def test_download_enforces_max_bytes(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2000, headers={"content-type": "video/mp4"})

    client = _client(handler)
    video = parse_payload({"data": {"aweme_id": "1", "media": {"video": {"url": "https://x/v.mp4", "watermark": False}}}})
    with pytest.raises(DTKError) as excinfo:
        client.download(video, tmp_path, max_bytes=1000)
    assert excinfo.value.code == "DTK_TOO_LARGE"

MP4_BODY = b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096


def _mp4_response() -> httpx.Response:
    return httpx.Response(
        200,
        content=MP4_BODY,
        headers={"content-type": "video/mp4", "content-length": str(len(MP4_BODY))},
    )


def test_parse_image_album_returns_image_urls():
    video = parse_payload(
        {
            "data": {
                "kind": "image_album",
                "aweme_id": "7300000000000000001",
                "desc": "一条图集",
                "media": {
                    "video": None,
                    "images": [
                        {"url": "https://p3.douyinpic.com/1.webp", "urls": ["https://p3.douyinpic.com/1.webp"]},
                        {"url": "https://p3.douyinpic.com/2.webp"},
                    ],
                    "music": {"play_url": "https://sf6.douyinstatic.com/obj/ies-music/x.mp3"},
                },
            }
        }
    )
    assert video.kind == "image_album"
    assert video.image_urls == ("https://p3.douyinpic.com/1.webp", "https://p3.douyinpic.com/2.webp")
    assert video.video_url == ""


def test_parse_video_keeps_all_mirror_urls():
    video = parse_payload(
        {
            "data": {
                "aweme_id": "1234567890123456",
                "media": {
                    "video": {
                        "url": "https://a/v.mp4",
                        "urls": ["https://a/v.mp4", "https://b/v.mp4", "https://c/v.mp4"],
                    }
                },
            }
        }
    )
    assert video.kind == "video"
    assert video.video_url == "https://a/v.mp4"
    assert video.video_urls == ("https://a/v.mp4", "https://b/v.mp4", "https://c/v.mp4")


def test_download_falls_back_to_next_mirror(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "broken" in str(request.url):
            raise httpx.ConnectError("boom", request=request)
        return _mp4_response()

    client = _client(handler, download_attempts=1)
    video = parse_payload(
        {"data": {"media": {"video": {"url": "https://broken/v.mp4", "urls": ["https://broken/v.mp4", "https://ok/v.mp4"]}}}}
    )
    media = client.download(video, tmp_path, max_bytes=1024 * 1024)
    assert media.path.is_file()
    assert media.bytes == len(MP4_BODY)
    assert calls == ["https://broken/v.mp4", "https://ok/v.mp4"]


def test_download_retries_transient_connect_error(tmp_path):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return _mp4_response()

    client = _client(handler, download_attempts=2)
    video = parse_payload({"data": {"media": {"video": {"url": "https://a/v.mp4"}}}})
    media = client.download(video, tmp_path, max_bytes=1024 * 1024)
    assert media.bytes == len(MP4_BODY)
    assert attempts["n"] == 2


def test_download_exhausted_network_failure_is_retryable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client(handler, download_attempts=1)
    video = parse_payload({"data": {"media": {"video": {"url": "https://a/v.mp4"}}}})
    with pytest.raises(DTKError) as excinfo:
        client.download(video, tmp_path, max_bytes=1024 * 1024)
    assert excinfo.value.code == "DTK_DOWNLOAD_FAILED"
    assert excinfo.value.retryable is True


def test_download_skips_non_video_mirror(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "html" in str(request.url):
            return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})
        return _mp4_response()

    client = _client(handler, download_attempts=1)
    video = parse_payload(
        {"data": {"media": {"video": {"url": "https://x/html", "urls": ["https://x/html", "https://x/v.mp4"]}}}}
    )
    media = client.download(video, tmp_path, max_bytes=1024 * 1024)
    assert media.bytes == len(MP4_BODY)
    assert "v.mp4" in str(media.source_url)


def test_download_client_never_carries_dtk_credentials():
    client = DTKClient("http://dtk.test", api_key="dtk_secret")
    try:
        assert client._client.headers["authorization"] == "Bearer dtk_secret"
        assert "authorization" not in client._download_client.headers
        assert "x-api-key" not in client._download_client.headers
        assert "authorization" not in client._download_headers
    finally:
        client.close()
