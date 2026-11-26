"""Bilibili watermark-free video export.

Bilibili serves ordinary videos as DASH tracks (``dash.video`` + ``dash.audio``)
without a platform-burned watermark.  This module resolves a video URL, picks
the best AVC video track and the best audio track the account is allowed to
fetch, downloads both with the required Referer, and merges them losslessly with
ffmpeg.  It never removes pixels and never bypasses paid/DRM content.

High-resolution tracks (1080P+/4K) require a logged-in ``SESSDATA`` cookie; the
caller supplies it through settings.  Without it Bilibili returns a lower
``quality`` and we export what the server actually offers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .douyin_bridge.media import MediaError
from .douyin_bridge.models import StoredMedia

_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
_PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AV_RE = re.compile(r"(?:\bav|/av)(\d+)", re.IGNORECASE)
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class BilibiliError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass(slots=True)
class BilibiliStreams:
    bvid: str
    title: str
    page: int
    cid: int
    quality: int
    video_urls: list[str]
    audio_urls: list[str]
    headers: dict[str, str]
    is_dash: bool = True


def _first_url(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            text = _first_url(item)
            if text:
                return text
    if isinstance(value, dict):
        for key in ("baseUrl", "base_url", "url", "backupUrl", "backup_url"):
            text = _first_url(value.get(key))
            if text:
                return text
    return ""


def _all_urls(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str) and value.startswith("http"):
        found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(_all_urls(item))
    elif isinstance(value, dict):
        for key in ("baseUrl", "base_url", "url", "backupUrl", "backup_url"):
            for item in _all_urls(value.get(key)):
                if item not in found:
                    found.append(item)
    return found


def _sanitize_title(value: str) -> str:
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", str(value or "").strip())
    return text[:120].strip(" ._") or "bilibili"


class BilibiliDownloader:
    def __init__(self, settings: Any, *, client: httpx.Client | None = None):
        self.settings = settings
        self.timeout = max(5.0, float(getattr(settings, "bilibili_timeout_seconds", 60) or 60))
        self.max_bytes = max(1, int(getattr(settings, "bilibili_max_video_mb", 500) or 500)) * 1024 * 1024
        self.ffmpeg = str(getattr(settings, "ffmpeg_path", "ffmpeg") or "ffmpeg")
        cookie = str(getattr(settings, "bilibili_sessdata", "") or "").strip()
        self.headers = {
            "User-Agent": _DEFAULT_UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "*/*",
        }
        if cookie:
            self.headers["Cookie"] = f"SESSDATA={cookie}"
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": _DEFAULT_UA},
        )

    def close(self) -> None:
        self._client.close()

    def resolve(self, url: str) -> BilibiliStreams:
        target = str(url or "").strip()
        if not target.startswith(("http://", "https://")):
            raise BilibiliError("BILI_BAD_URL", "不是 HTTP(S) 链接")
        try:
            response = self._client.get(target, headers={"User-Agent": _DEFAULT_UA})
        except httpx.HTTPError as exc:
            raise BilibiliError("BILI_UNREACHABLE", type(exc).__name__) from exc
        final_url = str(response.url)
        parsed = urlparse(final_url)
        host = parsed.netloc.lower()
        if "bilibili.com" not in host and "b23.tv" not in host:
            raise BilibiliError("BILI_BAD_HOST", f"不是 B 站链接：{host}")
        query = parse_qs(parsed.query)
        page = 1
        try:
            page = max(1, int((query.get("p") or ["1"])[0]))
        except (TypeError, ValueError):
            page = 1
        bvid_match = _BV_RE.search(final_url) or _BV_RE.search(target)
        av_match = _AV_RE.search(final_url) or _AV_RE.search(target)
        params: dict[str, Any] = {}
        if bvid_match:
            params["bvid"] = bvid_match.group(0)
        elif av_match:
            params["aid"] = int(av_match.group(1))
        else:
            raise BilibiliError("BILI_BAD_URL", "链接里没有 BV/av 号")
        try:
            view = self._client.get(_VIEW_URL, params=params, headers=self.headers).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliError("BILI_VIEW_FAILED", type(exc).__name__) from exc
        if view.get("code") != 0 or not isinstance(view.get("data"), dict):
            raise BilibiliError("BILI_VIEW_FAILED", str(view.get("message") or "view API 返回失败"))
        data = view["data"]
        bvid = str(data.get("bvid") or (bvid_match.group(0) if bvid_match else ""))
        title = str(data.get("title") or bvid)
        pages = data.get("pages") if isinstance(data.get("pages"), list) else []
        cid = int(data.get("cid") or 0)
        if pages:
            selected = next((item for item in pages if int(item.get("page") or 0) == page), None)
            if selected is None:
                selected = pages[0]
                page = int(selected.get("page") or 1)
            cid = int(selected.get("cid") or cid)
            title = f"{title} P{page} {selected.get('part') or ''}".strip()
        if not cid:
            raise BilibiliError("BILI_CID_MISSING", "view API 没有返回 cid")

        params = {
            "bvid": bvid,
            "cid": cid,
            "qn": 127,
            "fnval": 4048,
            "fnver": 0,
            "fourk": 1,
            "platform": "pc",
        }
        try:
            play = self._client.get(_PLAYURL_URL, params=params, headers=self.headers).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliError("BILI_PLAYURL_FAILED", type(exc).__name__) from exc
        if play.get("code") != 0 or not isinstance(play.get("data"), dict):
            raise BilibiliError("BILI_PLAYURL_FAILED", str(play.get("message") or "playurl API 返回失败"))
        pdata = play["data"]
        dash = pdata.get("dash") if isinstance(pdata.get("dash"), dict) else {}
        videos = dash.get("video") if isinstance(dash.get("video"), list) else []
        audios = dash.get("audio") if isinstance(dash.get("audio"), list) else []
        if videos:
            avc = [item for item in videos if str(item.get("codecs") or "").lower().startswith("avc")]
            pool = avc or videos
            best_video = max(
                pool,
                key=lambda item: (
                    int(item.get("height") or 0),
                    int(item.get("id") or 0),
                    int(item.get("bandwidth") or 0),
                ),
            )
            best_audio = max(
                audios,
                key=lambda item: (int(item.get("id") or 0), int(item.get("bandwidth") or 0)),
            ) if audios else None
            video_urls = _all_urls(best_video)
            audio_urls = _all_urls(best_audio) if best_audio else []
            if not video_urls:
                raise BilibiliError("BILI_NO_STREAM", "DASH 视频流没有地址")
            return BilibiliStreams(
                bvid=bvid,
                title=_sanitize_title(title),
                page=page,
                cid=cid,
                quality=int(best_video.get("id") or pdata.get("quality") or 0),
                video_urls=video_urls,
                audio_urls=audio_urls,
                headers={**self.headers, "Referer": f"https://www.bilibili.com/video/{bvid}"},
                is_dash=True,
            )
        durl = pdata.get("durl") if isinstance(pdata.get("durl"), list) else []
        if durl:
            urls = [url for item in durl for url in _all_urls(item)]
            if urls:
                return BilibiliStreams(
                    bvid=bvid,
                    title=_sanitize_title(title),
                    page=page,
                    cid=cid,
                    quality=int(pdata.get("quality") or 0),
                    video_urls=urls,
                    audio_urls=[],
                    headers={**self.headers, "Referer": f"https://www.bilibili.com/video/{bvid}"},
                    is_dash=False,
                )
        raise BilibiliError("BILI_NO_STREAM", "没有可用的视频流（可能需要登录/大会员）")

    def _download(self, urls: list[str], destination: Path, *, limit: int, headers: dict[str, str] | None = None) -> int:
        last_error: Exception | None = None
        request_headers = headers or self.headers
        for url in urls:
            try:
                with self._client.stream("GET", url, headers=request_headers) as response:
                    if response.status_code >= 400:
                        raise BilibiliError("BILI_STREAM_HTTP", f"HTTP {response.status_code}")
                    total = 0
                    with destination.open("wb") as handle:
                        for chunk in response.iter_bytes(256 * 1024):
                            total += len(chunk)
                            if total > limit:
                                raise BilibiliError("BILI_TOO_LARGE", "视频超过本地大小上限")
                            handle.write(chunk)
                if destination.stat().st_size <= 0:
                    raise BilibiliError("BILI_EMPTY_STREAM", "媒体流为空")
                return int(destination.stat().st_size)
            except (httpx.HTTPError, BilibiliError) as exc:
                last_error = exc
                destination.unlink(missing_ok=True)
        if isinstance(last_error, BilibiliError):
            raise last_error
        raise BilibiliError("BILI_DOWNLOAD_FAILED", type(last_error).__name__ if last_error else "下载失败")

    def download(self, url: str, directory: Path) -> StoredMedia:
        streams = self.resolve(url)
        ffmpeg = shutil.which(self.ffmpeg) or self.ffmpeg
        if not Path(ffmpeg).is_file() and shutil.which(ffmpeg) is None:
            raise BilibiliError("BILI_FFMPEG_MISSING", "找不到 ffmpeg")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bili-", dir=str(directory)) as temp:
            temp_dir = Path(temp)
            video_part = temp_dir / "video.m4s"
            self._download(streams.video_urls, video_part, limit=self.max_bytes, headers=streams.headers)
            output = directory / f"bilibili_{streams.bvid}_p{streams.page}_{streams.quality}.mp4"
            if streams.is_dash and streams.audio_urls:
                audio_part = temp_dir / "audio.m4s"
                self._download(streams.audio_urls, audio_part, limit=self.max_bytes, headers=streams.headers)
                command = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-y",
                    "-i", str(video_part),
                    "-i", str(audio_part),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(output),
                ]
                completed = subprocess.run(command, capture_output=True, text=True, timeout=max(120, self.timeout * 4))
                if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
                    output.unlink(missing_ok=True)
                    raise BilibiliError("BILI_FFMPEG_FAILED", (completed.stderr or "ffmpeg 合并失败")[-300:])
            else:
                video_part.replace(output)
        if not output.is_file() or output.stat().st_size <= 0 or output.stat().st_size > self.max_bytes:
            raise BilibiliError("BILI_OUTPUT_INVALID", "导出文件不存在、为空或超限")
        with output.open("rb") as handle:
            head = handle.read(12)
        if b"ftyp" not in head:
            raise BilibiliError("BILI_NOT_MP4", "合并结果不是 MP4")
        data_hash = hashlib.sha256()
        with output.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                data_hash.update(chunk)
        return StoredMedia(
            path=output,
            sha256=data_hash.hexdigest(),
            bytes=output.stat().st_size,
            mime="video/mp4",
            kind="video",
            title=streams.title,
            source_url=url,
            watermark_verified=True,
        )
