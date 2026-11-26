"""CRUD for the bridge's bindings, codes, jobs and message fingerprints."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import secrets
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..models import utc_now
from ..storage import Storage
from .models import (
    ChatMessage,
    DouyinBridgeBaselineRow,
    DouyinBridgeBindingRow,
    DouyinBridgeCodeRow,
    DouyinBridgeJobRow,
    DouyinBridgePeerStateRow,
    DouyinBridgeSeenRow,
    DouyinBridgeWeChatCursorRow,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=utc_now().tzinfo)


def _json(value: object, default: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return default


class BridgeStore:
    def __init__(self, storage: Storage):
        self.storage = storage

    # -- bindings ----------------------------------------------------------
    def bind(
        self,
        *,
        account_id: str,
        conversation_id: str,
        peer_name: str = "",
        douyin_user_id: str = "",
        wxid: str,
        wx_name: str = "",
    ) -> dict[str, Any]:
        account = str(account_id or "").strip()
        conversation = str(conversation_id or "").strip()
        target = str(wxid or "").strip()
        if not account or not conversation or not target:
            raise ValueError("douyin_bridge_binding_required_fields")
        now = utc_now()
        with self.storage.Session.begin() as session:
            row = session.scalar(
                select(DouyinBridgeBindingRow).where(
                    DouyinBridgeBindingRow.account_id == account,
                    DouyinBridgeBindingRow.conversation_id == conversation,
                )
            )
            if row is None:
                row = DouyinBridgeBindingRow(
                    account_id=account,
                    conversation_id=conversation,
                    peer_name=str(peer_name or "").strip(),
                    douyin_user_id=str(douyin_user_id or "").strip(),
                    wxid=target,
                    wx_name=str(wx_name or "").strip(),
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            else:
                row.peer_name = str(peer_name or "").strip()
                row.douyin_user_id = str(douyin_user_id or "").strip()
                row.wxid = target
                row.wx_name = str(wx_name or "").strip()
                row.status = "active"
                row.updated_at = now
            return self._binding_dict(row)

    def binding_for(self, account_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self.storage.Session() as session:
            row = session.scalar(
                select(DouyinBridgeBindingRow).where(
                    DouyinBridgeBindingRow.account_id == str(account_id or ""),
                    DouyinBridgeBindingRow.conversation_id == str(conversation_id or ""),
                    DouyinBridgeBindingRow.status == "active",
                )
            )
            return self._binding_dict(row) if row is not None else None

    def binding_for_wxid(self, account_id: str, wxid: str) -> dict[str, Any] | None:
        with self.storage.Session() as session:
            row = session.scalar(
                select(DouyinBridgeBindingRow)
                .where(
                    DouyinBridgeBindingRow.account_id == str(account_id or ""),
                    DouyinBridgeBindingRow.wxid == str(wxid or ""),
                    DouyinBridgeBindingRow.status == "active",
                )
                .order_by(DouyinBridgeBindingRow.updated_at.desc())
                .limit(1)
            )
            return self._binding_dict(row) if row is not None else None

    def list_bindings(self, account_id: str) -> list[dict[str, Any]]:
        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    select(DouyinBridgeBindingRow)
                    .where(DouyinBridgeBindingRow.account_id == str(account_id or ""))
                    .order_by(DouyinBridgeBindingRow.updated_at.desc())
                )
            )
            return [self._binding_dict(row) for row in rows]

    def delete_binding(self, account_id: str, binding_id: object) -> bool:
        try:
            identifier = int(binding_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        with self.storage.Session.begin() as session:
            row = session.get(DouyinBridgeBindingRow, identifier)
            if row is None or row.account_id != str(account_id or ""):
                return False
            session.delete(row)
            return True

    def bound_wxids(self, account_id: str) -> list[str]:
        with self.storage.Session() as session:
            rows = session.scalars(
                select(DouyinBridgeBindingRow.wxid).where(
                    DouyinBridgeBindingRow.account_id == str(account_id or ""),
                    DouyinBridgeBindingRow.status == "active",
                )
            )
            return list(dict.fromkeys(str(value) for value in rows if str(value or "").strip()))

    # -- binding codes -----------------------------------------------------
    def create_code(
        self,
        *,
        account_id: str,
        wxid: str,
        wx_name: str = "",
        ttl_minutes: int = 10,
    ) -> dict[str, Any]:
        account = str(account_id or "").strip()
        target = str(wxid or "").strip()
        if not account or not target:
            raise ValueError("douyin_bridge_code_required_fields")
        now = utc_now()
        code = f"{secrets.randbelow(1_000_000):06d}"
        digest = hashlib.sha256(f"{account}:{code}".encode("utf-8")).hexdigest()
        expires = now + timedelta(minutes=max(1, min(int(ttl_minutes), 60)))
        with self.storage.Session.begin() as session:
            # A new code supersedes any still-valid code for the same contact.
            for old in session.scalars(
                select(DouyinBridgeCodeRow).where(
                    DouyinBridgeCodeRow.account_id == account,
                    DouyinBridgeCodeRow.wxid == target,
                    DouyinBridgeCodeRow.used_at.is_(None),
                )
            ):
                session.delete(old)
            session.add(
                DouyinBridgeCodeRow(
                    account_id=account,
                    code_hash=digest,
                    wxid=target,
                    wx_name=str(wx_name or "").strip(),
                    expires_at=expires,
                    created_at=now,
                )
            )
        return {
            "code": code,
            "wxid": target,
            "wx_name": str(wx_name or "").strip(),
            "expires_at": expires.isoformat(),
            "ttl_minutes": int(ttl_minutes),
        }

    def redeem_code(self, account_id: str, code: object) -> dict[str, Any] | None:
        value = str(code or "").strip()
        if not (len(value) == 6 and value.isdigit()):
            return None
        account = str(account_id or "").strip()
        digest = hashlib.sha256(f"{account}:{value}".encode("utf-8")).hexdigest()
        now = utc_now()
        with self.storage.Session.begin() as session:
            row = session.scalar(
                select(DouyinBridgeCodeRow).where(
                    DouyinBridgeCodeRow.account_id == account,
                    DouyinBridgeCodeRow.code_hash == digest,
                    DouyinBridgeCodeRow.used_at.is_(None),
                )
            )
            if row is None or _aware(row.expires_at) is None or _aware(row.expires_at) <= now:
                return None
            row.used_at = now
            return {"wxid": row.wxid, "wx_name": row.wx_name, "expires_at": _aware(row.expires_at).isoformat()}

    # -- seen messages -----------------------------------------------------
    def seen(self, account_id: str, conversation_id: str, fingerprint: str) -> bool:
        with self.storage.Session() as session:
            row = session.scalar(
                select(DouyinBridgeSeenRow.id).where(
                    DouyinBridgeSeenRow.account_id == str(account_id or ""),
                    DouyinBridgeSeenRow.conversation_id == str(conversation_id or ""),
                    DouyinBridgeSeenRow.fingerprint == str(fingerprint or ""),
                )
            )
            return row is not None

    def mark_seen(self, account_id: str, conversation_id: str, fingerprint: str) -> None:
        now = utc_now()
        try:
            with self.storage.Session.begin() as session:
                session.add(
                    DouyinBridgeSeenRow(
                        account_id=str(account_id or ""),
                        conversation_id=str(conversation_id or ""),
                        fingerprint=str(fingerprint or ""),
                        first_seen_at=now,
                    )
                )
        except IntegrityError:
            # Two polls can overlap; the second insert is a no-op.
            pass

    def prune_seen(self, *, before: datetime) -> int:
        with self.storage.Session.begin() as session:
            result = session.execute(
                delete(DouyinBridgeSeenRow).where(DouyinBridgeSeenRow.first_seen_at < before)
            )
            return int(result.rowcount or 0)

    # -- start cutoff ------------------------------------------------------
    def ensure_baseline(self, account_id: str, conversation_id: str, peer_name: str = "") -> bool:
        """Create the history cutoff once per conversation; True if newly created.

        The cutoff is persisted: on the first poll after the feature is enabled
        every rendered message is recorded as history.  Later restarts reuse the
        row, so messages that arrive while the bridge is down are still new and
        get processed instead of being skipped by a fresh baseline.
        """

        try:
            with self.storage.Session.begin() as session:
                session.add(
                    DouyinBridgeBaselineRow(
                        account_id=str(account_id or ""),
                        conversation_id=str(conversation_id or ""),
                        peer_name=str(peer_name or ""),
                        baselined_at=utc_now(),
                    )
                )
            return True
        except IntegrityError:
            return False

    def clear_conversation_state(self, account_id: str, conversation_id: str) -> None:
        """Forget seen/baseline markers for an explicit recovery rescan."""

        with self.storage.Session.begin() as session:
            session.execute(
                delete(DouyinBridgeSeenRow).where(
                    DouyinBridgeSeenRow.account_id == str(account_id or ""),
                    DouyinBridgeSeenRow.conversation_id == str(conversation_id or ""),
                )
            )
            session.execute(
                delete(DouyinBridgeBaselineRow).where(
                    DouyinBridgeBaselineRow.account_id == str(account_id or ""),
                    DouyinBridgeBaselineRow.conversation_id == str(conversation_id or ""),
                )
            )

    # -- peer media state --------------------------------------------------
    def set_peer_media(self, message: ChatMessage, *, account_id: str, conversation_id: str, peer_name: str = "") -> None:
        now = utc_now()
        with self.storage.Session.begin() as session:
            row = session.scalar(
                select(DouyinBridgePeerStateRow).where(
                    DouyinBridgePeerStateRow.account_id == str(account_id or ""),
                    DouyinBridgePeerStateRow.conversation_id == str(conversation_id or ""),
                )
            )
            if row is None:
                row = DouyinBridgePeerStateRow(
                    account_id=str(account_id or ""),
                    conversation_id=str(conversation_id or ""),
                    updated_at=now,
                )
                session.add(row)
            row.peer_name = str(peer_name or "").strip()
            row.last_media_kind = message.kind
            row.last_media_fingerprint = message.fingerprint
            row.last_media_url = message.image_url or message.video_ref
            row.last_media_text = message.text[:2000]
            row.last_media_at = message.observed_at or now
            row.consumed = False
            row.updated_at = now

    def peer_media(self, account_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self.storage.Session() as session:
            row = session.scalar(
                select(DouyinBridgePeerStateRow).where(
                    DouyinBridgePeerStateRow.account_id == str(account_id or ""),
                    DouyinBridgePeerStateRow.conversation_id == str(conversation_id or ""),
                )
            )
            if row is None:
                return None
            return {
                "conversation_id": row.conversation_id,
                "peer_name": row.peer_name,
                "kind": row.last_media_kind,
                "fingerprint": row.last_media_fingerprint,
                "url": row.last_media_url,
                "text": row.last_media_text,
                "at": _aware(row.last_media_at).isoformat() if row.last_media_at else None,
                "consumed": bool(row.consumed),
            }

    def consume_peer_media(self, account_id: str, conversation_id: str, fingerprint: str) -> None:
        with self.storage.Session.begin() as session:
            row = session.scalar(
                select(DouyinBridgePeerStateRow).where(
                    DouyinBridgePeerStateRow.account_id == str(account_id or ""),
                    DouyinBridgePeerStateRow.conversation_id == str(conversation_id or ""),
                    DouyinBridgePeerStateRow.last_media_fingerprint == str(fingerprint or ""),
                )
            )
            if row is not None:
                row.consumed = True
                row.updated_at = utc_now()

    # -- jobs --------------------------------------------------------------
    def create_job(
        self,
        *,
        account_id: str,
        conversation_id: str,
        peer_name: str,
        kind: str,
        source_fingerprint: str,
        source_text: str,
        source_url: str,
        target_wxid: str = "",
        target_name: str = "",
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        job_id = secrets.token_hex(16)
        try:
            with self.storage.Session.begin() as session:
                row = DouyinBridgeJobRow(
                    job_id=job_id,
                    account_id=str(account_id or ""),
                    conversation_id=str(conversation_id or ""),
                    peer_name=str(peer_name or ""),
                    kind=str(kind or ""),
                    status="detected",
                    source_fingerprint=str(source_fingerprint or ""),
                    source_text=str(source_text or "")[:4000],
                    source_url=str(source_url or "")[:4000],
                    target_wxid=str(target_wxid or ""),
                    target_name=str(target_name or ""),
                    payload_json=_json(payload or {}, "{}"),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return self._job_dict(row), True
        except IntegrityError:
            existing = self._job_by_scope(account_id, conversation_id, source_fingerprint, target_wxid)
            if existing is None:
                raise
            return existing, False

    def _job_by_scope(self, account_id: str, conversation_id: str, fingerprint: str, target: str) -> dict[str, Any] | None:
        with self.storage.Session() as session:
            row = session.scalar(
                select(DouyinBridgeJobRow).where(
                    DouyinBridgeJobRow.account_id == str(account_id or ""),
                    DouyinBridgeJobRow.conversation_id == str(conversation_id or ""),
                    DouyinBridgeJobRow.source_fingerprint == str(fingerprint or ""),
                    DouyinBridgeJobRow.target_wxid == str(target or ""),
                )
            )
            return self._job_dict(row) if row is not None else None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.storage.Session() as session:
            row = session.scalar(select(DouyinBridgeJobRow).where(DouyinBridgeJobRow.job_id == str(job_id or "")))
            return self._job_dict(row) if row is not None else None

    def list_jobs(self, account_id: str, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        with self.storage.Session() as session:
            statement = select(DouyinBridgeJobRow).where(DouyinBridgeJobRow.account_id == str(account_id or ""))
            if status:
                statement = statement.where(DouyinBridgeJobRow.status == status)
            rows = list(
                session.scalars(
                    statement.order_by(DouyinBridgeJobRow.created_at.desc()).limit(max(1, min(int(limit), 200)))
                )
            )
            return [self._job_dict(row) for row in rows]

    def pending_jobs(self, account_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.storage.Session() as session:
            rows = list(
                session.scalars(
                    select(DouyinBridgeJobRow)
                    .where(
                        DouyinBridgeJobRow.account_id == str(account_id or ""),
                        DouyinBridgeJobRow.status.in_(["detected", "resolving", "downloading", "ready"]),
                    )
                    .order_by(DouyinBridgeJobRow.created_at.asc())
                    .limit(max(1, min(int(limit), 100)))
                )
            )
            return [self._job_dict(row) for row in rows]

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "media_path",
            "media_sha256",
            "media_bytes",
            "mime",
            "target_wxid",
            "target_name",
            "error_code",
            "error_detail",
            "delivered_ids_json",
            "payload_json",
        }
        with self.storage.Session.begin() as session:
            row = session.scalar(select(DouyinBridgeJobRow).where(DouyinBridgeJobRow.job_id == str(job_id or "")))
            if row is None:
                return None
            for key, value in fields.items():
                if key in allowed:
                    setattr(row, key, value)
            row.updated_at = utc_now()
            if fields.get("status") == "delivered":
                row.delivered_at = utc_now()
            session.flush()
            return self._job_dict(row)

    def increment_attempts(self, job_id: str) -> None:
        with self.storage.Session.begin() as session:
            row = session.scalar(select(DouyinBridgeJobRow).where(DouyinBridgeJobRow.job_id == str(job_id or "")))
            if row is not None:
                row.attempts += 1
                row.updated_at = utc_now()

    def delivered_since(self, account_id: str, since: datetime) -> int:
        with self.storage.Session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(DouyinBridgeJobRow)
                    .where(
                        DouyinBridgeJobRow.account_id == str(account_id or ""),
                        DouyinBridgeJobRow.status == "delivered",
                        DouyinBridgeJobRow.delivered_at >= since,
                    )
                )
                or 0
            )

    # -- WeChat -> Douyin cursor ------------------------------------------
    def wechat_cursor(self, account_id: str, wxid: str) -> int | None:
        with self.storage.Session() as session:
            row = session.scalar(
                select(DouyinBridgeWeChatCursorRow).where(
                    DouyinBridgeWeChatCursorRow.account_id == str(account_id or ""),
                    DouyinBridgeWeChatCursorRow.wxid == str(wxid or ""),
                )
            )
            return int(row.last_local_id) if row is not None else None

    def set_wechat_cursor(self, account_id: str, wxid: str, local_id: int) -> None:
        now = utc_now()
        with self.storage.Session.begin() as session:
            row = session.scalar(
                select(DouyinBridgeWeChatCursorRow).where(
                    DouyinBridgeWeChatCursorRow.account_id == str(account_id or ""),
                    DouyinBridgeWeChatCursorRow.wxid == str(wxid or ""),
                )
            )
            if row is None:
                session.add(
                    DouyinBridgeWeChatCursorRow(
                        account_id=str(account_id or ""),
                        wxid=str(wxid or ""),
                        last_local_id=max(0, int(local_id)),
                        initialized_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.last_local_id = max(int(row.last_local_id or 0), int(local_id))
                row.updated_at = now

    # -- serializers -------------------------------------------------------
    @staticmethod
    def _binding_dict(row: DouyinBridgeBindingRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "conversation_id": row.conversation_id,
            "peer_name": row.peer_name,
            "douyin_user_id": row.douyin_user_id,
            "wxid": row.wxid,
            "wx_name": row.wx_name,
            "status": row.status,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
            "updated_at": _aware(row.updated_at).isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _job_dict(row: DouyinBridgeJobRow) -> dict[str, Any]:
        try:
            delivered = json.loads(row.delivered_ids_json or "[]")
        except ValueError:
            delivered = []
        try:
            payload = json.loads(row.payload_json or "{}")
        except ValueError:
            payload = {}
        return {
            "job_id": row.job_id,
            "account_id": row.account_id,
            "conversation_id": row.conversation_id,
            "peer_name": row.peer_name,
            "kind": row.kind,
            "status": row.status,
            "source_fingerprint": row.source_fingerprint,
            "source_text": row.source_text,
            "source_url": row.source_url,
            "media_path": row.media_path,
            "media_sha256": row.media_sha256,
            "media_bytes": int(row.media_bytes or 0),
            "mime": row.mime,
            "target_wxid": row.target_wxid,
            "target_name": row.target_name,
            "attempts": int(row.attempts or 0),
            "error_code": row.error_code,
            "error_detail": row.error_detail,
            "delivered_message_ids": delivered if isinstance(delivered, list) else [],
            "payload": payload if isinstance(payload, dict) else {},
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
            "updated_at": _aware(row.updated_at).isoformat() if row.updated_at else None,
            "delivered_at": _aware(row.delivered_at).isoformat() if row.delivered_at else None,
        }
