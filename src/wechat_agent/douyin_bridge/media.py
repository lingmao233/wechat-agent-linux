"""Download and normalize Douyin stickers for WeChat delivery.

Douyin stickers are ordinary image resources (PNG/JPEG/GIF/WebP) rendered by
the chat page.  The bridge downloads the original bytes, validates the magic
itself instead of trusting ``Content-Type``, and derives a flattened PNG for the
native WeChat image message.  An animated original is kept and delivered as a
file as well, so animation is not silently lost.
"""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path
import re

import httpx
from PIL import Image, ImageSequence

from .models import StoredMedia

MAX_IMAGE_PIXELS = 40_000_000
_PNG = b"\x89PNG\r\n\x1a\n"
_GIF = (b"GIF87a", b"GIF89a")


class MediaError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def image_kind(data: bytes) -> tuple[str, str]:
    """Return ``(extension, mime)`` from magic bytes, never from a header."""

    if data.startswith(_PNG):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(_GIF):
        return "gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise MediaError("MEDIA_NOT_IMAGE", "下载内容不是受支持的图片")


def _flatten_to_png(data: bytes, destination: Path, *, max_side: int = 1024) -> tuple[bool, int]:
    """Write a first-frame PNG and return ``(animated, frame_count)``."""

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise MediaError("MEDIA_TOO_LARGE_PIXELS", "图片像素超过上限")
            frames = getattr(image, "n_frames", 1)
            animated = bool(frames and frames > 1)
            image.seek(0)
            frame = image.convert("RGBA")
            background = Image.new("RGBA", frame.size, (255, 255, 255, 255))
            background.alpha_composite(frame)
            output = background.convert("RGB")
            if max(output.size) > max_side:
                output.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            output.save(destination, format="PNG", optimize=True)
            return animated, int(frames or 1)
    except MediaError:
        raise
    except Exception as exc:  # Pillow raises several decoder-specific errors
        raise MediaError("MEDIA_DECODE_FAILED", type(exc).__name__) from exc


def store_image_bytes(
    data: bytes,
    directory: Path,
    *,
    max_bytes: int,
    stem_prefix: str = "sticker",
) -> StoredMedia:
    if not data:
        raise MediaError("MEDIA_EMPTY", "图片为空")
    if len(data) > max_bytes:
        raise MediaError("MEDIA_TOO_LARGE", "图片超过本地大小上限")
    extension, mime = image_kind(data)
    digest = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{stem_prefix}_{digest[:16]}.{extension}"
    if not target.exists():
        temporary = directory / f".{target.name}.part"
        temporary.write_bytes(data)
        temporary.replace(target)
    preview = directory / f"{stem_prefix}_{digest[:16]}_wechat.png"
    animated, _frames = _flatten_to_png(data, preview)
    return StoredMedia(
        path=target,
        sha256=digest,
        bytes=len(data),
        mime=mime,
        kind="image",
        animated=animated,
        preview_path=preview,
    )


def sticker_gif_for_wechat(media: StoredMedia, *, max_side: int = 512) -> Path:
    """Return a GIF that WeChat accepts as a native animated sticker.

    WeChat turns a GIF chosen through the file picker into an appmsg type 8
    emoticon.  Sending a PNG there would produce a picture/file instead, so a
    static sticker is converted to a one-frame GIF and an animated WebP keeps
    all frames.  The original GIF is returned untouched.
    """

    source = Path(media.path)
    if source.suffix.lower() == ".gif":
        try:
            with Image.open(source) as image:
                if getattr(image, "n_frames", 1) > 1 and image.format == "GIF":
                    return source
        except Exception:
            pass
    target = source.with_name(source.stem + "_wechat.gif")
    try:
        with Image.open(source) as image:
            frames = []
            durations = []
            for frame in ImageSequence.Iterator(image):
                duration = int(frame.info.get("duration") or 80)
                rgba = frame.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                background.alpha_composite(rgba)
                rgb = background.convert("RGB")
                if max(rgb.size) > max_side:
                    rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                frames.append(rgb.convert("P", palette=Image.Palette.ADAPTIVE, colors=255))
                durations.append(max(20, min(duration, 1000)))
                if len(frames) >= 120:
                    break
            if not frames:
                raise MediaError("STICKER_NO_FRAMES", "表情没有可转换的帧")
            if len(frames) == 1:
                # WeChat treats a still one-frame GIF as a normal file/image.
                # A two-frame GIF is classified as a native animated sticker;
                # duplicate the visual with one palette pixel changed so Pillow
                # and WeChat do not collapse it back to a still image.
                second = frames[0].copy()
                if second.width and second.height:
                    second.putpixel((0, 0), (second.getpixel((0, 0)) + 1) % 256)
                frames.append(second)
                durations.append(max(100, durations[0]))
            frames[0].save(
                target,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=0,
                disposal=2,
                optimize=False,
            )
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError("STICKER_CONVERT_FAILED", type(exc).__name__) from exc
    return target


class MediaFetcher:
    """Bounded HTTP image fetcher with a test-injectable client."""


    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self._client = client or httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str, directory: Path, *, max_bytes: int, referer: str = "https://www.douyin.com/") -> StoredMedia:
        target = str(url or "").strip()
        if not target.startswith(("http://", "https://")):
            raise MediaError("MEDIA_BAD_URL", "图片地址不是 HTTP(S)")
        try:
            with self._client.stream("GET", target, headers={"Referer": referer}) as response:
                if response.status_code in {401, 403}:
                    raise MediaError("MEDIA_FORBIDDEN", f"图片资源拒绝访问（HTTP {response.status_code}）")
                if response.status_code >= 400:
                    raise MediaError("MEDIA_HTTP_ERROR", f"HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise MediaError("MEDIA_TOO_LARGE", "图片超过本地大小上限")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaError("MEDIA_TOO_LARGE", "图片超过本地大小上限")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise MediaError("MEDIA_UNREACHABLE", type(exc).__name__) from exc
        return store_image_bytes(b"".join(chunks), directory, max_bytes=max_bytes)

    def fetch_from_browser_data(self, value: str, directory: Path, *, max_bytes: int) -> StoredMedia:
        """Accept a ``data:image/...;base64,`` payload produced by the page."""

        text = str(value or "").strip()
        match = re.match(r"^data:image/[^;]+;base64,(.+)$", text, re.IGNORECASE | re.DOTALL)
        if not match:
            raise MediaError("MEDIA_BAD_DATA_URL", "浏览器返回的不是图片 data URL")
        try:
            data = base64.b64decode(match.group(1), validate=True)
        except (ValueError, TypeError) as exc:
            raise MediaError("MEDIA_BAD_BASE64", "图片 data URL 解码失败") from exc
        return store_image_bytes(data, directory, max_bytes=max_bytes)
