"""Client for a self-hosted Douyin_TikTok_Download_API (DTK) instance.

The bridge never strips pixels.  It asks DTK for the clean stream the platform
already publishes and refuses a result that explicitly says it is watermarked.
DTK v5 is asynchronous by default: ``POST /api/v1/parse`` answers ``202`` with a
task id, and ``GET /api/v1/tasks/{id}`` carries the result.  The client also
accepts v4-style immediate envelopes because the two shapes are easy to
distinguish and operators may still run v4.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import time
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from .models import ParsedVideo, StoredMedia


class DTKError(RuntimeError):
    """A bounded, user-safe DTK failure."""

    def __init__(self, code: str, message: str = "", *, retryable: bool = False):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.retryable = retryable


_RETRYABLE = {
    "RATE_LIMITED",
    "QUEUE_FULL",
    "IDENTITY_POOL_EXHAUSTED",
    "ENDPOINT_CIRCUIT_OPEN",
    "UPSTREAM_RISK_CONTROL",
    "SIGNING_FAILED",
    "UPSTREAM_UNREACHABLE",
    "DOWNLOADER_UNAVAILABLE",
    "INTERNAL",
}


def _first_url(value: Any) -> str:
    """Return the first non-empty URL from a string/list/dict-shaped field."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("url", "url_list", "urls", "play_url", "src"):
            if key in value:
                found = _first_url(value[key])
                if found:
                    return found
    return ""


def _all_urls(value: Any) -> list[str]:
    """Return every non-empty URL in a string/list/dict-shaped field.

    Douyin returns several playable mirrors for one stream; keeping all of them
    lets a failed CDN connection fall back to the next address instead of
    failing the whole transfer.
    """

    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        found: list[str] = []
        for item in value:
            found.extend(_all_urls(item))
        return found
    if isinstance(value, dict):
        found = []
        for key in ("url", "url_list", "urls", "play_url", "src"):
            if key in value:
                found.extend(_all_urls(value[key]))
        return found
    return []


def _iter_dicts(value: Any, *, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_dicts(child, depth=depth + 1)


def _looks_audio(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
    except ValueError:
        return False
    return path.endswith((".mp3", ".m4a", ".aac", ".wav", ".ogg")) or "/ies-music/" in path or "/music/" in path


def _media_candidates(data: dict[str, Any]) -> list[tuple[str, bool | None]]:
    """Collect ``(url, watermark)`` pairs from the known DTK/vendor shapes."""

    found: list[tuple[str, bool | None]] = []

    def add(url: Any, watermark: Any) -> None:
        flag = watermark if isinstance(watermark, bool) else None
        for text in _all_urls(url):
            if text.startswith(("http://", "https://")) and not _looks_audio(text):
                found.append((text, flag))

    media = data.get("media")
    if isinstance(media, dict):
        video = media.get("video")
        if isinstance(video, dict):
            watermark = video.get("watermark")
            add(video.get("url"), watermark)
            add(video.get("urls"), watermark)
            add(video.get("url_list"), watermark)

    for node in _iter_dicts(data):
        video = node.get("video")
        if isinstance(video, dict):
            watermark = video.get("watermark")
            for key in ("play_addr", "download_addr", "play_addr_h264", "play_addr_265"):
                address = video.get(key)
                if isinstance(address, dict):
                    add(address.get("url_list") or address.get("url"), address.get("watermark", watermark))
            for bitrate in video.get("bit_rate") or ():
                if isinstance(bitrate, dict):
                    address = bitrate.get("play_addr")
                    if isinstance(address, dict):
                        add(address.get("url_list") or address.get("url"), address.get("watermark", watermark))
        # Some DTK versions flatten the stream to data.video_url.
        for key in ("video_url", "download_url", "play_url"):
            if key in node and isinstance(node[key], str):
                add(node[key], node.get("watermark"))

    # Keep first occurrence, preferring an explicitly clean stream.
    seen: set[str] = set()
    unique: list[tuple[str, bool | None]] = []
    for url, flag in found:
        if url not in seen:
            seen.add(url)
            unique.append((url, flag))
    unique.sort(key=lambda pair: pair[1] is True)
    return unique


def _image_candidates(data: dict[str, Any]) -> list[str]:
    """Collect image URLs from a Douyin 图集 (``kind=image_album``) payload."""

    media = data.get("media") if isinstance(data.get("media"), dict) else {}
    items = media.get("images")
    if not isinstance(items, list):
        items = data.get("images")
    if not isinstance(items, list):
        items = []
    found: list[str] = []
    for item in items:
        if isinstance(item, dict):
            found.extend(_all_urls(item.get("url") or item.get("urls") or item.get("url_list")))
        elif isinstance(item, str):
            found.append(item)
    # Some DTK versions flatten the album to image_urls / image_list.
    for node in _iter_dicts(data):
        for key in ("image_urls", "image_list"):
            if key in node:
                found.extend(_all_urls(node[key]))
    unique: list[str] = []
    seen: set[str] = set()
    for url in found:
        text = url.strip() if isinstance(url, str) else ""
        if text.startswith(("http://", "https://")) and text not in seen:
            seen.add(text)
            unique.append(text)
    return unique


def _text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _duration_ms(data: dict[str, Any]) -> int:
    for key in ("duration_ms", "duration"):
        value = data.get(key)
        if isinstance(value, (int, float)) and value > 0:
            # DTK reports milliseconds; a value below 1000 is almost certainly
            # seconds, but do not guess for a zero/absent field.
            return int(value if value >= 1000 else value * 1000)
    return 0


def _aweme_id(data: dict[str, Any], fallback: str) -> str:
    for key in ("aweme_id", "item_id", "content_id", "id"):
        value = data.get(key)
        if isinstance(value, (str, int)) and str(value).strip().isdigit():
            return str(value).strip()
    match = re.search(r"(?:video/|modal_id=|aweme_id=)(\d{15,25})", fallback)
    return match.group(1) if match else ""


def _cover(data: dict[str, Any]) -> str:
    for key in ("cover", "cover_url", "dynamic_cover"):
        value = data.get(key)
        if isinstance(value, dict):
            found = _first_url(value.get("url_list") or value.get("url"))
            if found:
                return found
        if isinstance(value, str) and value.startswith("http"):
            return value
    video = data.get("video")
    if isinstance(video, dict):
        cover = video.get("cover")
        if isinstance(cover, dict):
            return _first_url(cover.get("url_list") or cover.get("url"))
    return ""


def parse_payload(payload: dict[str, Any], *, fallback: str = "") -> ParsedVideo:
    """Normalize a DTK result; raises when no usable media is present.

    A Douyin ``image_album`` (图文/图集) has no video stream but is still
    transferable: it returns ``kind="image_album"`` with the image URLs in
    ``image_urls`` instead of raising ``DTK_NO_MEDIA``.
    """

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        raise DTKError("DTK_BAD_RESULT", "解析结果不是对象")
    # A finished task nests the result once more.
    nested = data.get("data")
    if isinstance(nested, dict):
        data = nested

    video_node = data.get("video") if isinstance(data.get("video"), dict) else {}
    title = _text(data, "title", "desc", "caption", "description") or _text(video_node, "title")
    author = _text(data, "author", "nickname", "author_name", "unique_id")
    if not author:
        author_node = data.get("author")
        if isinstance(author_node, dict):
            author = _text(author_node, "nickname", "name", "unique_id")
    kind = str(data.get("kind") or "").strip().lower()
    images = _image_candidates(data)
    candidates = _media_candidates(data)

    if kind == "image_album" or (not candidates and images):
        if not images:
            raise DTKError("DTK_NO_MEDIA", "图集解析结果里没有图片")
        return ParsedVideo(
            aweme_id=_aweme_id(data, fallback),
            title=title,
            author=author,
            video_url="",
            duration_ms=_duration_ms(data),
            cover_url=_cover(data) or images[0],
            watermark=None,
            raw=payload,
            kind="image_album",
            image_urls=tuple(images),
        )

    if not candidates:
        raise DTKError("DTK_NO_MEDIA", "解析结果里没有视频流")

    chosen_url, chosen_watermark = candidates[0]
    for url, watermark in candidates:
        if watermark is False:
            chosen_url, chosen_watermark = url, False
            break
    if chosen_watermark is True:
        raise DTKError("DTK_WATERMARKED", "解析结果只有带水印流，已拒绝")

    if not title:
        title = _text(video_node, "title")
    mirrors: list[str] = []
    for url, _watermark in [(chosen_url, chosen_watermark), *candidates]:
        if url and url not in mirrors:
            mirrors.append(url)
    return ParsedVideo(
        aweme_id=_aweme_id(data, fallback),
        title=title,
        author=author,
        video_url=chosen_url,
        duration_ms=_duration_ms(data),
        cover_url=_cover(data),
        watermark=chosen_watermark,
        raw=payload,
        kind="video",
        video_urls=tuple(mirrors),
    )


class DTKClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout_seconds: float = 30.0,
        wait_seconds: int = 20,
        download_attempts: int = 2,
        client: httpx.Client | None = None,
    ):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))
        self.wait_seconds = max(0, min(int(wait_seconds), 30))
        self.download_attempts = max(1, min(int(download_attempts), 5))
        headers = {
            "Accept": "application/json",
            "User-Agent": "wechat-agent-douyin-bridge/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        self._headers = headers
        self._download_headers = {
            "User-Agent": headers["User-Agent"],
            "Accept": "*/*",
            "Referer": "https://www.douyin.com/",
        }
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        # The CDN must never see the DTK API key, so downloads use a client
        # that only carries the media headers.  An injected test client is
        # reused as-is so mock transports keep working.
        self._download_client = client or httpx.Client(
            headers=self._download_headers,
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def close(self) -> None:
        if self._download_client is not self._client:
            self._download_client.close()
        self._client.close()

    def _unwrap(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise DTKError("DTK_BAD_RESPONSE", "解析服务返回了非 JSON 内容") from exc
        if isinstance(body, dict) and body.get("success") is False:
            error = body.get("error") or {}
            code = str(error.get("code") or "DTK_ERROR")
            message = str(error.get("message") or "")
            raise DTKError(code, message, retryable=code in _RETRYABLE)
        if not isinstance(body, dict):
            raise DTKError("DTK_BAD_RESPONSE", "解析服务返回结构异常")
        return body

    def parse(self, ref: str) -> ParsedVideo:
        if not self.configured:
            raise DTKError("DTK_NOT_CONFIGURED", "未配置 DTK_BASE_URL")
        source = str(ref or "").strip()
        if not source:
            raise DTKError("DTK_EMPTY_REF", "视频引用为空")

        params = {"wait": self.wait_seconds} if self.wait_seconds else {}
        try:
            response = self._client.post(
                "/api/v1/parse",
                params=params,
                json={"url": source},
                headers=self._headers,
            )
            if response.status_code in {404, 405}:
                # DTK v4 has no /api/v1/parse; its hybrid endpoint returns the
                # parsed object synchronously.  Keep this fallback explicit so
                # a v5 "endpoint missing" error is not silently retried as v4
                # on every call.
                response = self._client.get(
                    "/api/hybrid/video_data",
                    params={"url": source, "minimal": "false"},
                    headers=self._headers,
                )
        except httpx.HTTPError as exc:
            raise DTKError("DTK_UNREACHABLE", f"{type(exc).__name__}") from exc

        body = self._unwrap(response)
        data = body.get("data") or {}
        if response.status_code == 202 or (isinstance(data, dict) and data.get("task_id") and data.get("state") in {"queued", "running"}):
            task_id = str(data.get("task_id") or "")
            if not task_id:
                raise DTKError("DTK_NO_TASK", "解析服务未返回任务 ID")
            result = self._collect(task_id)
            return parse_payload(result, fallback=source)
        if response.status_code >= 400:
            raise DTKError("DTK_HTTP_ERROR", f"HTTP {response.status_code}")
        return parse_payload(body, fallback=source)

    def _collect(self, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        delay = 0.5
        while time.monotonic() < deadline:
            try:
                response = self._client.get(f"/api/v1/tasks/{task_id}", headers=self._headers)
            except httpx.HTTPError as exc:
                raise DTKError("DTK_UNREACHABLE", f"{type(exc).__name__}") from exc
            body = self._unwrap(response)
            task = body.get("data") if isinstance(body.get("data"), dict) else {}
            state = str(task.get("state") or "")
            if state == "done":
                return task
            if state == "failed":
                error = task.get("error") or {}
                code = str(error.get("code") or "DTK_TASK_FAILED")
                raise DTKError(code, str(error.get("message") or ""), retryable=bool(error.get("retryable")))
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
        raise DTKError("DTK_TIMEOUT", "解析任务超时；结果仍可能有效，稍后可重试", retryable=True)

    def download(self, video: ParsedVideo, destination: Path, *, max_bytes: int) -> StoredMedia:
        """Download the first playable mirror, retrying transient CDN failures.

        Douyin hands out several signed mirrors for one stream.  A single
        connection reset must not fail the transfer, so every mirror is tried
        and each is retried ``download_attempts`` times on timeout/connect
        errors before the next one is used.
        """

        destination.mkdir(parents=True, exist_ok=True)
        urls = [url for url in (video.video_urls or (video.video_url,)) if url]
        if not urls:
            raise DTKError("DTK_BAD_URL", "视频流地址不是 HTTP(S)")
        if max_bytes <= 0:
            raise DTKError("DTK_LIMIT", "视频大小上限无效")

        last_error: DTKError | None = None
        for url in urls:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                last_error = DTKError("DTK_BAD_URL", "视频流地址不是 HTTP(S)")
                continue
            for attempt in range(1, self.download_attempts + 1):
                try:
                    return self._download_one(url, video, destination, max_bytes=max_bytes)
                except DTKError as exc:
                    last_error = exc
                    if exc.retryable and attempt < self.download_attempts:
                        time.sleep(0.4 * attempt)
                        continue
                    break
        raise last_error or DTKError("DTK_DOWNLOAD_FAILED", "视频下载失败", retryable=True)

    def _download_one(self, url: str, video: ParsedVideo, destination: Path, *, max_bytes: int) -> StoredMedia:
        safe_title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", video.title)[:40].strip("_")
        stem = f"douyin_{video.aweme_id or 'video'}_{safe_title or 'clip'}"
        target = destination / f"{stem}.mp4"
        temporary = destination / f".{stem}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with self._download_client.stream(
                "GET",
                url,
                headers=self._download_headers,
                timeout=self.timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
                    raise DTKError("DTK_DOWNLOAD_FAILED", f"HTTP {response.status_code}", retryable=retryable)
                content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                if content_type and not (content_type.startswith("video/") or content_type in {"application/octet-stream", "binary/octet-stream"}):
                    raise DTKError("DTK_NOT_VIDEO", f"下载内容不是视频：{content_type}")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise DTKError("DTK_TOO_LARGE", "视频超过本地大小上限")
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise DTKError("DTK_TOO_LARGE", "视频超过本地大小上限")
                        digest.update(chunk)
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            raise DTKError("DTK_DOWNLOAD_FAILED", f"{type(exc).__name__}", retryable=True) from exc
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        if size == 0:
            temporary.unlink(missing_ok=True)
            raise DTKError("DTK_EMPTY_FILE", "下载到的视频为空", retryable=True)
        with temporary.open("rb") as handle:
            head = handle.read(64)
        if b"ftyp" not in head:
            temporary.unlink(missing_ok=True)
            raise DTKError("DTK_NOT_MP4", "下载内容缺少 MP4 文件头")
        temporary.replace(target)
        return StoredMedia(
            path=target,
            sha256=digest.hexdigest(),
            bytes=size,
            mime="video/mp4",
            kind="video",
            animated=False,
            title=video.title,
            source_url=url,
            watermark_verified=video.watermark if video.watermark is not None else None,
        )


def describe_video(video: ParsedVideo) -> dict[str, Any]:
    """Bounded, non-secret representation for the console/audit."""

    return {
        "aweme_id": video.aweme_id,
        "title": video.title[:200],
        "author": video.author[:120],
        "duration_ms": video.duration_ms,
        "watermark": video.watermark,
        "has_url": bool(video.video_url),
        "raw_keys": sorted(video.raw)[:20],
    }


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
