"""Persistence rows and value objects for the Douyin -> WeChat bridge.

The bridge deliberately owns a small, separate table set instead of reusing
``platform_identities``.  An @ mention binding and a media-delivery binding are
different grants: one authorizes a public comment, the other authorizes sending
a private file.  Keeping them apart means a mention binding can never become a
delivery target by accident.

No row stores a raw Douyin cookie, a bound-WeChat password or a media body;
media lives on disk under ``DOUYIN_BRIDGE_MEDIA_DIR`` and the row keeps only
its path, size and SHA-256.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..storage import Base


class DouyinBridgeBindingRow(Base):
    """One Douyin private conversation -> one WeChat contact (wxid)."""

    __tablename__ = "douyin_bridge_bindings"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "conversation_id",
            name="uq_douyin_bridge_binding_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    conversation_id: Mapped[str] = mapped_column(String(256), index=True)
    peer_name: Mapped[str] = mapped_column(String(256), default="")
    douyin_user_id: Mapped[str] = mapped_column(String(256), default="")
    wxid: Mapped[str] = mapped_column(String(256), index=True)
    wx_name: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DouyinBridgeCodeRow(Base):
    """Short-lived binding code, stored as a digest rather than plaintext."""

    __tablename__ = "douyin_bridge_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    wxid: Mapped[str] = mapped_column(String(256), index=True)
    wx_name: Mapped[str] = mapped_column(String(256), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DouyinBridgeJobRow(Base):
    """One requested transfer, from "command seen" to "WeChat read-back"."""

    __tablename__ = "douyin_bridge_jobs"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "conversation_id",
            "source_fingerprint",
            "target_wxid",
            name="uq_douyin_bridge_job_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    conversation_id: Mapped[str] = mapped_column(String(256), index=True)
    peer_name: Mapped[str] = mapped_column(String(256), default="")
    kind: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    source_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    source_text: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    media_path: Mapped[str] = mapped_column(Text, default="")
    media_sha256: Mapped[str] = mapped_column(String(64), default="")
    media_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str] = mapped_column(String(128), default="")
    target_wxid: Mapped[str] = mapped_column(String(256), default="", index=True)
    target_name: Mapped[str] = mapped_column(String(256), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    delivered_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DouyinBridgeSeenRow(Base):
    """Message fingerprint table.

    Douyin's web chat exposes no message id, so an inbound row is identified by
    a content fingerprint.  The table is pruned by age; it is not an archive.
    """

    __tablename__ = "douyin_bridge_seen"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "conversation_id",
            "fingerprint",
            name="uq_douyin_bridge_seen_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    conversation_id: Mapped[str] = mapped_column(String(256), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DouyinBridgeWeChatCursorRow(Base):
    """Last WeChat local_id processed for one bound contact (WeChat -> Douyin)."""

    __tablename__ = "douyin_bridge_wechat_cursors"
    __table_args__ = (
        UniqueConstraint("account_id", "wxid", name="uq_douyin_bridge_wechat_cursor"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    wxid: Mapped[str] = mapped_column(String(256), index=True)
    last_local_id: Mapped[int] = mapped_column(Integer, default=0)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DouyinBridgeBaselineRow(Base):
    """First-poll cutoff per conversation.

    Messages already present when the conversation is first seen (feature
    enabled / first poll) are history and are never forwarded.  The row is
    persisted so a bridge restart does not re-baseline: messages that arrive
    after the cutoff are still processed even if the process was down.
    """

    __tablename__ = "douyin_bridge_baselines"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "conversation_id",
            name="uq_douyin_bridge_baseline_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    conversation_id: Mapped[str] = mapped_column(String(256), index=True)
    peer_name: Mapped[str] = mapped_column(String(256), default="")
    baselined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DouyinBridgePeerStateRow(Base):
    """The last peer media message, so a follow-up command can refer to it."""

    __tablename__ = "douyin_bridge_peer_state"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "conversation_id",
            name="uq_douyin_bridge_peer_state_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    conversation_id: Mapped[str] = mapped_column(String(256), index=True)
    peer_name: Mapped[str] = mapped_column(String(256), default="")
    last_media_kind: Mapped[str] = mapped_column(String(16), default="")
    last_media_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    last_media_url: Mapped[str] = mapped_column(Text, default="")
    last_media_text: Mapped[str] = mapped_column(Text, default="")
    last_media_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


MessageKind = Literal["text", "image", "video", "unknown"]
Direction = Literal["in", "out"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One row read from the visible Douyin chat, already classified.

    ``fingerprint`` is derived from direction, kind and stable content only;
    volatile CDN query strings and DOM ordering do not participate.
    """

    direction: Direction
    kind: MessageKind
    text: str = ""
    image_url: str = ""
    video_ref: str = ""
    fingerprint: str = ""
    observed_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_media(self) -> bool:
        return self.kind in {"image", "video"}


@dataclass(frozen=True, slots=True)
class TransferCommand:
    """A parsed message-level intent.

    ``action`` is one of ``transfer``/``bind``/``status``/``cancel``/``chat``.
    """

    action: Literal["transfer", "bind", "status", "cancel", "chat"]
    kind: Literal["sticker", "video", "any"] = "any"
    url: str = ""
    code: str = ""
    reason: str = ""
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class StoredMedia:
    """A downloaded, validated media file plus what delivery needs."""

    path: Path
    sha256: str
    bytes: int
    mime: str
    kind: Literal["image", "video"]
    animated: bool = False
    # A flattened PNG used for a native WeChat image message.  For an animated
    # sticker this is the first frame; the original file is delivered too so
    # the animation is not lost.
    preview_path: Path | None = None
    title: str = ""
    source_url: str = ""
    watermark_verified: bool | None = None


@dataclass(frozen=True, slots=True)
class StoredAlbum:
    """Several images from one Douyin 图集 post, delivered as native images.

    A 图集 has no video stream, so it cannot use :class:`StoredMedia`'s single
    file shape.  Images are downloaded and validated one by one; delivery sends
    them as separate WeChat image messages in the original order.
    """

    paths: tuple[Path, ...]
    sha256: str
    bytes: int
    mime: str = "image/album"
    kind: Literal["album"] = "album"
    title: str = ""
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class ParsedVideo:
    """The subset of a DTK parse result the bridge relies on."""

    aweme_id: str
    title: str
    author: str
    video_url: str
    duration_ms: int = 0
    cover_url: str = ""
    watermark: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # ``image_album`` payloads have no video stream; they carry image URLs.
    kind: Literal["video", "image_album"] = "video"
    # All playable mirrors in preference order (the CDN may return several).
    video_urls: tuple[str, ...] = ()
    image_urls: tuple[str, ...] = ()
