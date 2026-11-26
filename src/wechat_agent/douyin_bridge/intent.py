"""Deterministic command parsing for Douyin private messages.

The bridge must not call a model to decide whether a message authorizes a file
transfer.  A model may chat, but only this module can turn text into a
transfer/bind/cancel command.  That keeps the dangerous path reviewable and
testable, and it lets an ambiguous sentence fall through to plain chat instead
of silently exporting media.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

from .models import TransferCommand

_URL_RE = re.compile(r"https?://[^\s<>\"'，。；：！？）】》、]+", re.IGNORECASE)
_AWEME_RE = re.compile(
    r"(?:/video/|/share/video/|modal_id=|aweme_id=|item_id=)(\d{15,25})",
    re.IGNORECASE,
)
_BILIBILI_RE = re.compile(
    r"https?://(?:(?:www|m)\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+)[^\s<>\"'，。；：！？）】》、]*"
    r"|https?://b23\.tv/[0-9A-Za-z]+",
    re.IGNORECASE,
)

_WECHAT_RE = re.compile(r"(?:微信|wechat|weixin|\bvx\b|v信|威信)", re.IGNORECASE)
_DOUYIN_RE = re.compile(r"(?:抖音|douyin|\bdy\b)", re.IGNORECASE)
_TRANSFER_VERB_RE = re.compile(
    r"(?:转到|转去|转给|转进|转发到|发到|发去|发给|发我|发给我|导到|导出|"
    r"传到|同步到|放到|搬到|弄到|存到)"
)
_CAPABILITY_RE = re.compile(r"(?:能不能|能否|可不可以|可以吗|能吗|支持吗|支持不支持)")
_HELP_RE = re.compile(r"(?:帮我|替我|帮忙|给我|劳驾)")
_NEGATION_RE = re.compile(
    r"(?:别|不要|不用|甭|勿|先别)[^。！？!?\n]{0,10}"
    r"(?:转到|转给|发到|发给|发我|导出|传到|同步到)"
)
_BIND_RE = re.compile(r"(?:绑定|绑一下|关联)(?:码|微信)?\s*[:：]?\s*(\d{6})")
_STATUS_RE = re.compile(
    r"(?:状态|进度|到哪了|到哪一步|好了吗|好了没|发了吗|发好了吗|发好了没|"
    r"弄好了吗|弄好了没|转好了吗|转好了没|转了吗|"
    r"(?:转|发|弄|导)[^。！？!?\n]{0,10}(?:了吗|好了吗|好了没|完了吗))"
)
_CANCEL_RE = re.compile(r"(?:取消|算了|不转了|别转了|先别发|不发了)")
_STICKER_RE = re.compile(r"(?:表情包|表情|贴纸|梗图|动图|图片|截图|图)")
_VIDEO_RE = re.compile(r"(?:视频|作品|无水印|抖音视频|短视频)")
_DEICTIC_RE = re.compile(r"(?:这个|这条|这张|那个|那条|那张|刚才|刚刚|上面|前面|它)")


def normalize_text(value: object) -> str:
    """NFKC-normalize and collapse whitespace without dropping punctuation."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def extract_url(text: str) -> str:
    match = _URL_RE.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:!?)]}，。；：！？）】》")


def extract_aweme_ref(text: str) -> str:
    """Return a video id/URL from ``text``, never an arbitrary image URL.

    A message row contains avatar and cover image URLs alongside real links.
    Returning the first http URL here used to turn every avatar into a video
    reference; only a URL that actually names a Douyin video qualifies.
    """

    url = extract_url(text)
    if url and looks_like_video_ref(url):
        return url
    match = _AWEME_RE.search(text)
    if match:
        return match.group(1)
    return ""


def extract_bilibili_ref(text: str) -> str:
    match = _BILIBILI_RE.search(str(text or ""))
    return match.group(0).rstrip(".,;:!?)]}，。；：！？）】》") if match else ""


def extract_export_link(text: str) -> tuple[str, str] | None:
    """Return ``(provider, url)`` for a supported video link in a message."""

    value = str(text or "")
    bilibili = extract_bilibili_ref(value)
    if bilibili:
        return "bilibili", bilibili
    douyin = extract_aweme_ref(value)
    if douyin and looks_like_video_ref(douyin):
        return "douyin", douyin
    return None


def parse_command(text: object) -> TransferCommand | None:
    """Parse one message; ``None`` means ordinary chat, not a command."""

    value = normalize_text(text)
    if not value:
        return None
    # Operators and users sometimes type a space between Chinese characters
    # ("转 到 微信").  Match on a whitespace-free view; URLs stay untouched in
    # ``value`` for extraction.
    compact = re.sub(r"\s+", "", value)

    bind = _BIND_RE.search(compact)
    if bind:
        return TransferCommand(action="bind", code=bind.group(1), reason="binding_code", confidence=0.99)

    if (_CAPABILITY_RE.search(compact)
            and value.rstrip().endswith(("?", "？", "吗"))
            and not _HELP_RE.search(compact)):
        # "能不能转到微信" asks about capability; "能不能帮我转到微信" asks
        # for the action and continues through the normal checks.
        return None

    if _CANCEL_RE.search(compact) and (
        _WECHAT_RE.search(compact) or _STICKER_RE.search(compact) or _VIDEO_RE.search(compact) or _DEICTIC_RE.search(compact)
    ):
        return TransferCommand(action="cancel", reason="cancel_media_transfer", confidence=0.9)

    has_wechat = bool(_WECHAT_RE.search(compact))
    if _STATUS_RE.search(compact) and (has_wechat or _STICKER_RE.search(compact) or _VIDEO_RE.search(compact)):
        return TransferCommand(action="status", reason="status_query", confidence=0.85)

    if not has_wechat:
        return None
    if not _TRANSFER_VERB_RE.search(compact):
        return None
    if _NEGATION_RE.search(compact):
        return None

    if _VIDEO_RE.search(compact):
        kind: str = "video"
    elif _STICKER_RE.search(compact):
        kind = "sticker"
    else:
        kind = "any"

    url = extract_aweme_ref(value) or extract_bilibili_ref(value)
    confidence = 0.92 if _DEICTIC_RE.search(compact) else 0.8
    if url:
        confidence = 0.95
    return TransferCommand(
        action="transfer",
        kind=kind,  # type: ignore[arg-type]
        url=url,
        reason="explicit_wechat_destination",
        confidence=confidence,
    )


def parse_to_douyin_command(text: object) -> TransferCommand | None:
    """Parse an explicit WeChat -> Douyin sticker request.

    This direction is deliberately command-only: a WeChat sticker sent to the
    agent is not forwarded until the same contact says "转到抖音".  No model
    decides it.
    """

    value = normalize_text(text)
    if not value:
        return None
    compact = re.sub(r"\s+", "", value)
    if not _DOUYIN_RE.search(compact):
        return None
    if not _TRANSFER_VERB_RE.search(compact):
        return None
    if _NEGATION_RE.search(compact):
        return None
    if (_CAPABILITY_RE.search(compact)
            and value.rstrip().endswith(("?", "？", "吗"))
            and not _HELP_RE.search(compact)):
        return None
    kind = "sticker" if _STICKER_RE.search(compact) else "any"
    return TransferCommand(
        action="transfer",
        kind=kind,  # type: ignore[arg-type]
        reason="explicit_douyin_destination",
        confidence=0.92 if _STICKER_RE.search(compact) else 0.8,
    )


def looks_like_media_link(text: object) -> bool:
    """True when a bare message is a supported video link/id."""

    value = normalize_text(text)
    return bool(extract_export_link(value))


def looks_like_video_ref(value: object) -> bool:
    """True only for something a video downloader can plausibly parse."""

    text = normalize_text(value)
    if not text:
        return False
    if extract_bilibili_ref(text):
        return True
    if _AWEME_RE.search(text) or re.fullmatch(r"\d{15,25}", text):
        return True
    url = extract_url(text)
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    if host.endswith("v.douyin.com"):
        return True
    if host.endswith("douyin.com"):
        return any(token in url for token in ("/video/", "/share/video/", "modal_id=", "aweme_id="))
    return False
