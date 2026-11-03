from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import MessageEvent, OutboundAction


@dataclass(frozen=True, slots=True)
class ChatInfo:
    chat_id: str
    name: str


@dataclass(frozen=True, slots=True)
class SendResult:
    verified: bool
    message_ids: tuple[str, ...] = ()
    message_type: str = "text"
    sticker_md5: str = ""


@dataclass(slots=True)
class HealthReport:
    ok: bool
    checks: dict[str, object] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class WeChatBackend(Protocol):
    def connect(self) -> None: ...
    def close(self) -> None: ...
    def check_ready(self) -> dict[str, object]: ...
    def poll(self, account_id: str, group_name: str | None = None) -> list[MessageEvent]: ...
    def send(self, action: OutboundAction) -> SendResult | None: ...
