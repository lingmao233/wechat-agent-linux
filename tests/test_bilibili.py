from __future__ import annotations

from urllib.parse import urlparse

from wechat_agent.bilibili import BilibiliDownloader
from wechat_agent.config import Settings
from wechat_agent.douyin_bridge.intent import extract_export_link


class FakeResponse:
    def __init__(self, payload, *, url=""):
        self._payload = payload
        self.url = url
        self.status_code = 200

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params or {}))
        if "b23.tv" in url:
            return FakeResponse({}, url="https://www.bilibili.com/video/BV1xx411c7mD?p=2")
        if "web-interface/view" in url:
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "bvid": "BV1xx411c7mD",
                        "title": "测试视频",
                        "cid": 100,
                        "pages": [
                            {"page": 1, "cid": 100, "part": "第一段"},
                            {"page": 2, "cid": 200, "part": "第二段"},
                        ],
                    },
                }
            )
        if "player/playurl" in url:
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "quality": 64,
                        "dash": {
                            "video": [
                                {"id": 32, "codecs": "hev1.1.6", "height": 720, "bandwidth": 900, "baseUrl": "https://v/hevc.m4s"},
                                {"id": 64, "codecs": "avc1.64001f", "height": 720, "bandwidth": 1200, "baseUrl": "https://v/avc.m4s"},
                                {"id": 80, "codecs": "avc1.640028", "height": 1080, "bandwidth": 2000, "baseUrl": "https://v/avc1080.m4s"},
                            ],
                            "audio": [
                                {"id": 30216, "bandwidth": 100, "baseUrl": "https://a/low.m4s"},
                                {"id": 30280, "bandwidth": 300, "baseUrl": "https://a/high.m4s"},
                            ],
                        },
                    },
                }
            )
        raise AssertionError(url)

    def close(self):
        pass


def test_extract_export_link_supports_bilibili_and_douyin():
    assert extract_export_link("看这个 https://www.bilibili.com/video/BV1xx411c7mD?p=2") == (
        "bilibili",
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
    )
    assert extract_export_link("https://b23.tv/abc123") == ("bilibili", "https://b23.tv/abc123")
    assert extract_export_link("https://v.douyin.com/abc/") == ("douyin", "https://v.douyin.com/abc/")


def test_bilibili_resolve_selects_avc_and_page():
    downloader = BilibiliDownloader(Settings(_env_file=None), client=FakeClient())
    streams = downloader.resolve("https://b23.tv/abc123")
    assert streams.bvid == "BV1xx411c7mD"
    assert streams.page == 2
    assert streams.cid == 200
    assert streams.quality == 80
    assert streams.video_urls[0] == "https://v/avc1080.m4s"
    assert streams.audio_urls[0] == "https://a/high.m4s"
    assert urlparse(streams.headers["Referer"]).netloc == "www.bilibili.com"
    downloader.close()
