"""One-time private-chat allowlist notifications.

The Web console controls the allowlist, while the agent owns delivery.  This
module keeps the small durable state file and the deterministic, persona-safe
message selection in one place so a restart cannot replay a notification.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from .persona_style import sanitize_persona_reply


WELCOME_MESSAGES = (
    "哼，今天的私聊名额随机抽到你啦。想找我聊天就直接来吧～",
    "叮——刚好随机选中你了，之后可以来私聊找我玩。",
    "本鱼随手一抽，私聊名额落到你这边啦，有事就来喊我。",
)


def welcome_key(account_id: str, chat_id: str) -> str:
    """Return the stable identity used by the welcome ledger."""

    return f"{str(account_id or '').strip()}:{str(chat_id or '').strip()}"


def choose_welcome_message(
    account_id: str,
    chat_id: str,
    *,
    max_emoji: int = 1,
) -> str:
    """Choose one human-like line without calling the model during startup.

    Hashing the contact identity gives each contact a varied line while keeping
    retries deterministic if a send fails before the ledger is updated.
    """

    digest = hashlib.sha256(welcome_key(account_id, chat_id).encode("utf-8")).digest()
    message = WELCOME_MESSAGES[digest[0] % len(WELCOME_MESSAGES)]
    return sanitize_persona_reply(message, max_emoji=max_emoji)


def load_sent(path: Path) -> set[str]:
    """Read a tolerant set of sent identities from a private JSON file."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return set()
    if isinstance(payload, dict):
        values = payload.get("sent", [])
    else:
        values = payload
    if not isinstance(values, list):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def save_sent(path: Path, sent: set[str]) -> None:
    """Atomically persist the sent identities with owner-only permissions."""

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "sent": sorted(sent)}, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".private-welcome-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # ``fchmod`` is unavailable on Windows; the temporary file is already
        # owner-created there, while POSIX deployments still get 0600.
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except (FileNotFoundError, PermissionError):
            pass


__all__ = [
    "WELCOME_MESSAGES",
    "choose_welcome_message",
    "load_sent",
    "save_sent",
    "welcome_key",
]
