"""Orchestration: read Douyin, prepare media, deliver to WeChat, reply.

The service is deliberately conservative:

* a transfer requires an explicit command from the bound conversation;
* a job is created only after a binding exists, so an unbound message never
  becomes a pending file transfer;
* ``DOUYIN_BRIDGE_DRY_RUN`` prepares the file but does not click WeChat;
* an uncertain WeChat send is never retried automatically;
* the Douyin reply is a courtesy, not a delivery receipt.  Only the WeChat
  database read-back changes a job to ``delivered``.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time as clock_time, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlparse

from ..config import Settings
from ..models import utc_now
from ..platform.base import ChatInfo
from ..storage import Storage
from ..bilibili import BilibiliDownloader, BilibiliError
from .binding import BindingService
from .chat import DouyinChatError, DouyinChatSession
from .delivery import DeliveryError, WeChatDelivery
from .dtk import DTKClient, DTKError
from .intent import (
    extract_aweme_ref,
    extract_export_link,
    looks_like_video_ref,
    parse_command,
    parse_to_douyin_command,
)
from .media import MediaError, MediaFetcher, sticker_gif_for_wechat, store_image_bytes
from .models import ChatMessage, ParsedVideo, StoredAlbum, StoredMedia
from .store import BridgeStore

log = logging.getLogger(__name__)

# A stuck chat tab fails closed with these codes: the page is still logged in
# and lists conversations, but clicking a row no longer switches the open chat.
# They are recoverable by reloading /chat or relaunching the browser.
_RECOVERABLE_FAILURE_CODES = (
    "CHAT_IDENTITY_UNCONFIRMED",
    "CHAT_LIST_TIMEOUT",
    # Restarting only the Agent (POST /api/stop + /api/start) leaves Playwright
    # bound to the old poll thread; only a console restart heals that, and the
    # watchdog ladder walks there instead of stalling.
    "CHAT_BROWSER_THREAD_CONFLICT",
    "Cannot switch to a different thread",
)
# A challenge or a logged-out page needs a human; never auto-recover those.
_OPERATOR_FAILURE_CODES = ("CHAT_CHALLENGE", "CHAT_LOGIN_REQUIRED")
_WATCHDOG_STATE_FILE = "douyin-bridge-watchdog.json"
_WATCHDOG_RESTART_LOG = "douyin-bridge-watchdog-restart.log"

_STATUS_LABELS = {
    "detected": "已收到",
    "resolving": "解析中",
    "downloading": "下载中",
    "ready": "已准备好（dry-run，未发送）",
    "delivering": "发送中",
    "delivered": "已发到微信",
    "failed": "失败",
    "uncertain": "发送结果不确定，已停止自动重试",
    "needs_binding": "等待绑定微信",
    "cancelled": "已取消",
    "rejected": "已拒绝",
}


class ChatLike(Protocol):
    def open_conversation(self, target: str | None = None) -> dict[str, Any]: ...
    def read_messages(self, limit: int = 20) -> list[ChatMessage]: ...
    def send_text(self, text: str) -> dict[str, Any]: ...
    def fetch_image_bytes(self, url: str, *, max_bytes: int) -> bytes: ...
    def calibrate(self) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...


class DouyinBridgeService:
    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        *,
        adapter: Any = None,
        chat: ChatLike | None = None,
        delivery: Any = None,
        llm: Any = None,
        fetcher: MediaFetcher | None = None,
        dtk: DTKClient | None = None,
        bilibili: BilibiliDownloader | None = None,
    ):
        self.storage = storage
        self.settings = settings
        self.adapter = adapter
        self.chat = chat
        self.delivery = delivery
        self.backend = None
        self.llm = llm
        self.fetcher = fetcher or MediaFetcher()
        self.dtk = dtk
        self.bilibili = bilibili
        if self.dtk is None and str(getattr(settings, "dtk_base_url", "") or "").strip():
            self.dtk = DTKClient(
                settings.dtk_base_url,
                api_key=getattr(settings, "dtk_api_key", ""),
                timeout_seconds=getattr(settings, "dtk_timeout_seconds", 30.0),
                wait_seconds=getattr(settings, "dtk_parse_wait_seconds", 20),
            )
        self.store = BridgeStore(storage)
        self.binding = BindingService(self.store, settings)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._jobs: queue.Queue[str] = queue.Queue()
        self._loop_requests: queue.Queue[tuple[Any, dict[str, Any], threading.Event]] = queue.Queue()
        self._baseline_done = False
        self._baselines: set[str] = set()
        self._conversation_id = ""
        self._conversation_name = ""
        self._last_poll_at: datetime | None = None
        self._last_error = ""
        self._last_result: dict[str, Any] = {}
        self._recent_messages: list[dict[str, Any]] = []
        self._unbound_notice_at: dict[str, float] = {}
        self._wx_pending: dict[str, list[dict[str, Any]]] = {}
        # Peer names that have an unexpired binding code but no binding yet.
        # The poller watches them so the Douyin-side code can be redeemed.
        self._pending_peers: dict[str, float] = {}
        self._binding_cursor = 0
        self._conversation_signatures: dict[str, str] = {}
        self._signatures_ready = False
        self._last_full_scan = 0.0
        self._binding_backoff: dict[str, float] = {}
        self._recently_active: dict[str, float] = {}
        self._startup_baselined = False
        self._missing_bindings: set[str] = set()
        self._force_rescan: set[str] = set()
        self._reply_times: list[float] = []
        self._last_media_cleanup = 0.0
        self._started_at: datetime | None = None
        # Page-stall watchdog: consecutive polls where every attempted binding
        # failed identity confirmation, plus which recovery rung was last used.
        self._watchdog_streak = 0
        self._watchdog_level = 0
        self._watchdog_blocked = ""
        self._watchdog_last_failure_at: datetime | None = None
        self._watchdog_last_recovery: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------
    @property
    def account_id(self) -> str:
        return str(getattr(self.settings, "wechat_account_id", "") or "").strip() or "default"

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "douyin_bridge_enabled", False))

    def attach_backend(self, backend: Any) -> None:
        """Attach the running Linux backend and its bound-target allowlist."""

        self.backend = backend
        self.delivery = WeChatDelivery(backend)
        provider = getattr(backend, "set_dm_allowlist_provider", None)
        if callable(provider):
            provider(self.binding.wxids)

    def start(self) -> dict[str, Any]:
        if not self.enabled:
            return self.status()
        if self._threads and any(thread.is_alive() for thread in self._threads):
            return self.status()
        self._stop.clear()
        self._started_at = utc_now()
        # One thread owns every Playwright call for this bridge.  Playwright's
        # sync API binds its dispatcher greenlet to the creating thread, so a
        # worker thread must never touch the chat page.  Job processing is
        # therefore drained by the same poll thread (a video download may delay
        # the next poll; the next poll still reads every message that arrived).
        poll = threading.Thread(target=self._poll_loop, name="douyin-bridge-poll", daemon=True)
        self._threads = [poll]
        poll.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=8)
        self._threads = []
        # The poll thread closes its own page in ``finally``.  Only a service
        # that never started a thread (run_once/tests) closes it here.
        if isinstance(self.chat, DouyinChatSession) and self._started_at is None:
            self.chat.close()

    def request_poll(self) -> None:
        """Ask the owner thread for an immediate poll (never poll here)."""

        self._wake.set()

    def call_in_loop(self, function: Any, *, timeout: float = 15.0) -> Any:
        """Run a Playwright-bound callable in the bridge poll thread.

        Console HTTP threads must never touch the sync Playwright page.  The
        console can ask the owner thread to enumerate conversations and wait
        for the small, read-only result.
        """

        if threading.current_thread() in self._threads:
            return function()
        if not self._threads or not any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("douyin_bridge_not_running")
        event = threading.Event()
        box: dict[str, Any] = {}
        self._loop_requests.put((function, box, event))
        self.request_poll()
        if not event.wait(timeout):
            raise TimeoutError("douyin_bridge_loop_timeout")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _drain_loop_requests(self) -> None:
        while True:
            try:
                function, box, event = self._loop_requests.get_nowait()
            except queue.Empty:
                return
            try:
                box["result"] = function()
            except Exception as exc:
                box["error"] = exc
            finally:
                event.set()

    def _requeue_pending_jobs(self) -> None:
        """Resume accepted-but-unfinished jobs after a restart.

        Jobs are persisted before download; without this, a long batch that
        spans a restart would leave later rows in ``detected`` forever.
        """

        try:
            jobs = self.store.list_jobs(self.account_id, limit=500)
        except Exception as exc:
            log.warning("douyin_bridge_requeue_failed error=%s", type(exc).__name__)
            return
        pending = [job for job in jobs if job.get("status") in {"detected", "ready"}]
        for job in pending:
            self._jobs.put(job["job_id"])
        if pending:
            log.info("douyin_bridge_requeued_jobs count=%s", len(pending))

    def _poll_loop(self) -> None:
        interval = max(3.0, float(getattr(self.settings, "douyin_bridge_poll_seconds", 6.0)))
        self._requeue_pending_jobs()
        try:
            while not self._stop.is_set():
                self._drain_loop_requests()
                try:
                    self.poll_once()
                except Exception as exc:  # keep the loop alive, keep the reason visible
                    self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                    log.warning("douyin_bridge_poll_failed error=%s", self._last_error)
                try:
                    self.poll_wechat()
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                    log.warning("douyin_bridge_wechat_poll_failed error=%s", self._last_error)
                try:
                    self._drain_jobs()
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                    log.warning("douyin_bridge_jobs_failed error=%s", self._last_error)
                self._maybe_cleanup_media()
                self._wake.wait(interval)
                self._wake.clear()
        finally:
            if isinstance(self.chat, DouyinChatSession):
                self.chat.close()

    def _drain_jobs(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._jobs.get_nowait()
            except queue.Empty:
                return
            try:
                self.process_job(job_id)
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                log.warning("douyin_bridge_job_failed job=%s error=%s", job_id, self._last_error)
            # Let WeChat finish the attachment/read-back before the next file
            # in a burst; rapid file pickers are the main source of chat-race
            # failures in a long sticker batch.
            time.sleep(0.6)

    # -- chat --------------------------------------------------------------
    def _chat(self) -> ChatLike:
        if self.chat is not None:
            return self.chat
        if self.adapter is None:
            raise DouyinChatError("CHAT_ADAPTER_MISSING", "未启用抖音浏览器适配器")
        if not callable(getattr(self.adapter, "ensure_session", None)):
            raise DouyinChatError("PLATFORM_BROWSER_DISABLED", "需要先开启 PLATFORM_BROWSER_ENABLED")
        self.chat = DouyinChatSession(self.adapter, self.settings)
        return self.chat

    def _bilibili_downloader(self) -> BilibiliDownloader:
        if self.bilibili is None:
            self.bilibili = BilibiliDownloader(self.settings)
        return self.bilibili

    def _open_target(self) -> dict[str, Any]:
        target = str(getattr(self.settings, "douyin_bridge_conversation", "") or "").strip()
        opened = self._chat().open_conversation(target or None)
        if not opened.get("ok"):
            raise DouyinChatError("CHAT_OPEN_FAILED", str(opened.get("error") or "无法打开目标会话"))
        self._conversation_id = str(opened.get("conversation_id") or "")
        self._conversation_name = str(opened.get("name") or target)
        return opened

    def _open_binding_with_retry(self, target: str) -> dict[str, Any]:
        """Open one conversation, retrying a transient list/row race once."""

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                opened = self._chat().open_conversation(target)
                if not opened.get("ok"):
                    raise DouyinChatError("CHAT_OPEN_FAILED", str(opened.get("error") or f"无法打开会话 {target}"))
                return opened
            except DouyinChatError as exc:
                last_error = exc
                if attempt == 0 and exc.code in {"CHAT_IDENTITY_UNCONFIRMED", "CHAT_OPEN_FAILED"}:
                    time.sleep(0.8)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise DouyinChatError("CHAT_OPEN_FAILED", f"无法打开会话 {target}")

    # -- polling -----------------------------------------------------------
    def poll_once(self) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "reason": "disabled"}
        if self.settings.stop_file.exists():
            return {"ok": False, "reason": "stopped"}
        self._last_error = ""
        bindings = [item for item in self.binding.list() if item.get("status") == "active"]
        bound_names = {str(item.get("peer_name") or "") for item in bindings}
        current = time.monotonic()
        pending_names = [
            name
            for name, expires in self._pending_peers.items()
            if expires > current and name and name not in bound_names
        ]
        targets = list(bindings) + [
            {"peer_name": name, "wxid": "", "status": "pending_code"}
            for name in pending_names
        ]
        if not targets:
            fallback = str(getattr(self.settings, "douyin_bridge_conversation", "") or "").strip()
            if not fallback:
                return {"ok": True, "new": 0, "bindings": 0, "reason": "no_binding"}
            # Keep the configured default conversation readable so an operator
            # can complete the first binding-code handshake before any binding
            # exists.  Media in this fallback is not auto-forwarded.
            targets = [{"peer_name": fallback, "wxid": "", "status": "fallback"}]
        list_scan = bool(getattr(self.settings, "douyin_bridge_list_scan", True))
        snapshot: dict[str, dict[str, Any]] = {}
        if list_scan:
            try:
                for row in self._chat().list_conversations() or []:
                    name = str((row or {}).get("name") or "").strip()
                    if name:
                        snapshot[name] = row
            except Exception as exc:
                log.warning("douyin_bridge_list_scan_failed error=%s", type(exc).__name__)
        scan_now = time.monotonic()
        full_scan_seconds = max(60, int(getattr(self.settings, "douyin_bridge_full_scan_seconds", 600) or 600))
        full_scan = scan_now - self._last_full_scan >= full_scan_seconds
        per_poll = max(1, int(getattr(self.settings, "douyin_bridge_bindings_per_poll", 3) or 3))
        if full_scan:
            selected = list(targets)
        elif len(targets) > per_poll:
            start = self._binding_cursor % len(targets)
            selected = [targets[(start + index) % len(targets)] for index in range(per_poll)]
            self._binding_cursor = (start + per_poll) % len(targets)
        else:
            selected = list(targets)
        now = utc_now()
        new_messages = 0
        opened_count = 0
        actions: list[dict[str, Any]] = []
        conversations: list[dict[str, Any]] = []

        for binding in selected:
            is_bound = bool(str(binding.get("wxid") or "").strip())
            target = str(binding.get("peer_name") or getattr(self.settings, "douyin_bridge_conversation", "") or "")
            if not target:
                continue
            signature = self._conversation_signature(snapshot.get(target))
            if (
                list_scan
                and snapshot
                and self._signatures_ready
                and not full_scan
                and self._binding_backoff.get(target, 0.0) <= scan_now
                and self._recently_active.get(target, 0.0) <= scan_now
                and signature
                and self._conversation_signatures.get(target) == signature
            ):
                conversations.append({"conversation": target, "skipped": True})
                continue
            if self._binding_backoff.get(target, 0.0) > scan_now and not full_scan:
                conversations.append({"conversation": target, "backoff": True})
                continue
            try:
                opened = self._open_binding_with_retry(target)
                conversation_id = str(opened.get("conversation_id") or self.binding_key(target))
                conversation_name = str(opened.get("name") or target)
                self._conversation_id = conversation_id
                self._conversation_name = conversation_name
                scan_limit = max(30, int(getattr(self.settings, "douyin_bridge_scan_limit", 200) or 200))
                messages = self._chat().read_messages(scan_limit)
                # Douyin renders a burst one row at a time.  Re-read shortly
                # after opening so three stickers sent together are not
                # truncated to whichever row was mounted first.
                time.sleep(0.7)
                later = self._chat().read_messages(scan_limit)
                if len(later) >= len(messages):
                    messages = later
                opened_count += 1
                self._binding_backoff.pop(target, None)
                was_missing = target in self._missing_bindings
                self._missing_bindings.discard(target)
                force_rescan = target in self._force_rescan
                self._force_rescan.discard(target)
                if signature:
                    self._conversation_signatures[target] = signature
                if not force_rescan and (not self._startup_baselined or was_missing):
                    # Never replay a backlog after an agent restart or after a
                    # chat that was temporarily missing reappears.  Existing
                    # rows are marked seen; only messages arriving after this
                    # baseline are forwarded.
                    self.store.ensure_baseline(self.account_id, conversation_id, conversation_name)
                    for message in messages:
                        self.store.mark_seen(self.account_id, conversation_id, message.fingerprint)
                    conversations.append(
                        {
                            "conversation": conversation_name,
                            "conversation_id": conversation_id,
                            "baseline": len(messages),
                            "new": 0,
                        }
                    )
                    continue
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                self._binding_backoff[target] = scan_now + 120
                self._missing_bindings.add(target)
                conversations.append(
                    {"conversation": target, "error": f"{type(exc).__name__}: {exc}"[:240]}
                )
                log.warning("douyin_bridge_binding_skipped name=%s error=%s", target, self._last_error)
                continue
            self._recent_messages = [
                {
                    "conversation": conversation_name,
                    "direction": message.direction,
                    "kind": message.kind,
                    "text": message.text[:200],
                    "image_host": urlparse(message.image_url).netloc if message.image_url else "",
                    "image_url": message.image_url[:300],
                    "video_ref": message.video_ref[:200],
                    "fingerprint": message.fingerprint,
                    "row_text": str((message.raw or {}).get("rowText") or "")[:160],
                    "duration": str((message.raw or {}).get("duration") or "")[:20],
                    "raw_class": str((message.raw or {}).get("cls") or "")[:200],
                }
                for message in messages[-12:]
            ]
            if not force_rescan and self.store.ensure_baseline(self.account_id, conversation_id, conversation_name):
                # First poll for this conversation after the feature was enabled:
                # everything currently rendered is history and must not be
                # forwarded.  The cutoff is persisted, so a later restart does
                # not re-baseline messages that arrived while the bridge was down.
                for message in messages:
                    self.store.mark_seen(self.account_id, conversation_id, message.fingerprint)
                self._baselines.add(conversation_id)
                conversations.append({"conversation": conversation_name, "conversation_id": conversation_id, "baseline": len(messages), "new": 0})
                continue

            before = new_messages
            media_occurrences: dict[str, int] = {}
            for index, message in enumerate(messages):
                occurrence = 0
                if message.kind in {"image", "video"} and message.fingerprint:
                    occurrence = media_occurrences.get(message.fingerprint, 0)
                    media_occurrences[message.fingerprint] = occurrence + 1
                dedupe = self._dedupe_key(message, conversation_id, occurrence=occurrence)
                if message.direction == "out":
                    self.store.mark_seen(self.account_id, conversation_id, dedupe)
                    continue
                if self.store.seen(self.account_id, conversation_id, dedupe):
                    continue
                new_messages += 1
                try:
                    if message.kind in {"image", "video"}:
                        self.store.set_peer_media(
                            message,
                            account_id=self.account_id,
                            conversation_id=conversation_id,
                            peer_name=conversation_name,
                        )
                        self.store.mark_seen(self.account_id, conversation_id, dedupe)
                        outcome = self._auto_forward_media(
                            message,
                            conversation_id,
                            binding=binding if is_bound else None,
                            peer_name=conversation_name,
                            offset=len(messages) - 1 - index,
                        )
                        actions.append({"kind": "media", "media": message.kind, "outcome": outcome})
                        continue
                    if message.kind == "text":
                        command = parse_command(message.text)
                        if command is None and looks_like_video_ref(message.text):
                            outcome = self._auto_forward_link(
                                message,
                                conversation_id,
                                binding=binding if is_bound else None,
                                peer_name=conversation_name,
                            )
                        else:
                            outcome = self._handle_text(
                                message,
                                conversation_id,
                                binding=binding if is_bound else None,
                                peer_name=conversation_name,
                            )
                        self.store.mark_seen(self.account_id, conversation_id, dedupe)
                        actions.append({"kind": "text", "outcome": outcome})
                        continue
                    self.store.mark_seen(self.account_id, conversation_id, dedupe)
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                    log.warning("douyin_bridge_handle_failed error=%s", self._last_error)
                    # A message that repeatedly throws would otherwise be
                    # replayed forever; record it as seen and surface the error.
                    self.store.mark_seen(self.account_id, conversation_id, dedupe)
            conversations.append({
                "conversation": conversation_name,
                "conversation_id": conversation_id,
                "new": new_messages - before,
            })
            if new_messages > before:
                # Keep this conversation hot for a short burst window; the
                # sidebar preview can stay identical for repeated stickers.
                self._recently_active[target] = time.monotonic() + 20

        self._baseline_done = bool(self._baselines)
        if opened_count and not self._startup_baselined:
            self._startup_baselined = True
        if list_scan and snapshot:
            self._signatures_ready = True
            if full_scan:
                self._last_full_scan = scan_now
        if new_messages:
            self.store.prune_seen(before=now - timedelta(days=7))
        self._last_poll_at = utc_now()
        self._last_result = {
            "bindings": len(bindings),
            "pending": len(pending_names),
            "scanned": len(snapshot),
            "opened": opened_count,
            "conversations": conversations,
            "conversation": conversations[-1].get("conversation", "") if conversations else "",
            "conversation_id": conversations[-1].get("conversation_id", "") if conversations else "",
            "new": new_messages,
            "actions": actions[-10:],
        }
        try:
            self._chat().calibrate(live=True)
        except Exception:
            pass
        result = {"ok": True, **self._last_result}
        self._review_watchdog(result)
        return result

    # -- page-stall watchdog -----------------------------------------------
    def _watchdog_conf(self) -> tuple[bool, int]:
        enabled = bool(getattr(self.settings, "douyin_bridge_watchdog_enabled", True))
        threshold = int(getattr(self.settings, "douyin_bridge_watchdog_identity_threshold", 3) or 3)
        return enabled, max(2, threshold)

    @staticmethod
    def _watchdog_error_contains(row: dict[str, Any], codes: tuple[str, ...]) -> bool:
        error = str(row.get("error") or "")
        return any(code in error for code in codes)

    def _review_watchdog(self, result: dict[str, Any]) -> None:
        """Turn a silently stuck chat tab into a graded recovery.

        The bridge fails closed when it cannot confirm the open conversation,
        which is correct but used to surface only as an endless run of
        warnings while nothing was forwarded.  A challenge or a logged-out page
        is *not* auto-recovered: that needs the operator.
        """

        enabled, threshold = self._watchdog_conf()
        if not enabled or not result.get("ok"):
            return
        if str(result.get("reason") or "") in {"disabled", "stopped"}:
            return
        conversations = [row for row in (result.get("conversations") or []) if isinstance(row, dict)]
        attempted = [row for row in conversations if not row.get("skipped") and not row.get("backoff")]
        if not attempted:
            return
        operator_rows = [row for row in attempted if self._watchdog_error_contains(row, _OPERATOR_FAILURE_CODES)]
        if operator_rows:
            codes = sorted(
                {
                    code
                    for row in operator_rows
                    for code in _OPERATOR_FAILURE_CODES
                    if code in str(row.get("error") or "")
                }
            )
            reason = "+".join(codes) or "operator_required"
            self._watchdog_streak = 0
            self._watchdog_last_failure_at = utc_now()
            if self._watchdog_blocked != reason:
                self._watchdog_blocked = reason
                log.error(
                    "douyin_bridge_watchdog_operator_required reason=%s names=%s",
                    reason,
                    ",".join(str(row.get("conversation") or "") for row in operator_rows[:6]),
                )
            return
        recoverable = [row for row in attempted if self._watchdog_error_contains(row, _RECOVERABLE_FAILURE_CODES)]
        if len(recoverable) < len(attempted):
            if self._watchdog_streak or self._watchdog_level or self._watchdog_blocked:
                log.info(
                    "douyin_bridge_watchdog_cleared streak=%s level=%s",
                    self._watchdog_streak,
                    self._watchdog_level,
                )
            self._watchdog_streak = 0
            self._watchdog_level = 0
            self._watchdog_blocked = ""
            return
        if self._watchdog_blocked:
            return
        self._watchdog_streak += 1
        self._watchdog_last_failure_at = utc_now()
        log.warning(
            "douyin_bridge_watchdog_identity_failure streak=%s/%s names=%s",
            self._watchdog_streak,
            threshold,
            ",".join(str(row.get("conversation") or "") for row in recoverable[:6]),
        )
        if self._watchdog_streak < threshold:
            return
        self._watchdog_streak = 0
        self._watchdog_recover()

    def _watchdog_recover(self) -> None:
        """Reload the page, then relaunch the browser, then restart the console."""

        level = min(self._watchdog_level, 2)
        action = ("reload_page", "relaunch_browser", "restart_console")[level]
        try:
            if action == "reload_page":
                report = self._recover_chat_page(relaunch_browser=False)
            elif action == "relaunch_browser":
                report = self._recover_chat_page(relaunch_browser=True)
            else:
                report = self._attempt_console_restart()
                if not report.get("ok"):
                    # Console restart is rate-limited (and missing on Windows
                    # dev machines); fall back to the browser rung instead of
                    # leaving the bridge stalled.
                    log.warning(
                        "douyin_bridge_watchdog_console_restart_deferred reason=%s",
                        report.get("reason") or "unknown",
                    )
                    action = "relaunch_browser"
                    report = self._recover_chat_page(relaunch_browser=True)
        except DouyinChatError as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if exc.code in _OPERATOR_FAILURE_CODES:
                self._watchdog_blocked = f"{exc.code}: {exc.message}"[:300]
                log.error("douyin_bridge_watchdog_operator_required reason=%s", self._watchdog_blocked)
            else:
                log.warning("douyin_bridge_watchdog_recovery_failed action=%s error=%s", action, exc)
            self._watchdog_last_recovery = {
                "action": action,
                "ok": False,
                "error": self._last_error,
                "at": utc_now().isoformat(),
            }
            if action != "restart_console":
                self._watchdog_level = min(level + 1, 2)
            return
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("douyin_bridge_watchdog_recovery_failed action=%s error=%s", action, self._last_error)
            self._watchdog_last_recovery = {
                "action": action,
                "ok": False,
                "error": self._last_error,
                "at": utc_now().isoformat(),
            }
            if action != "restart_console":
                self._watchdog_level = min(level + 1, 2)
            return
        self._watchdog_last_recovery = {
            "action": action,
            "ok": True,
            "report": report,
            "at": utc_now().isoformat(),
        }
        log.warning(
            "douyin_bridge_watchdog_recovered action=%s report=%s",
            action,
            json.dumps(report, ensure_ascii=False, default=str)[:300],
        )
        if action == "reload_page":
            self._watchdog_level = 1
        else:
            # A console restart is one rung; if the process survives it (e.g.
            # prerequisites failed) keep the browser rung next time.
            self._watchdog_level = 2

    def _recover_chat_page(self, *, relaunch_browser: bool) -> dict[str, Any]:
        chat = self._chat()
        recover = getattr(chat, "recover_page", None)
        if not callable(recover):
            raise DouyinChatError("CHAT_RECOVER_UNSUPPORTED", "当前 chat 适配器没有 recover_page")
        report = recover(relaunch_browser=relaunch_browser)
        return dict(report or {})

    def _watchdog_state_path(self) -> Path:
        return self.settings.stop_file.parent / _WATCHDOG_STATE_FILE

    def _watchdog_restart_log_path(self) -> Path:
        return self.settings.stop_file.parent / _WATCHDOG_RESTART_LOG

    def _console_restart_script(self) -> Path | None:
        script = Path(__file__).resolve().parents[3] / "scripts" / "restart-desktop.sh"
        return script if script.is_file() else None

    def _console_port(self) -> int:
        try:
            return int(getattr(self.settings, "desktop_port", 8765) or 8765)
        except (TypeError, ValueError):
            return 8765

    @staticmethod
    def _read_watchdog_state(path: Path) -> list[float]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        stamps = data.get("console_restarts") if isinstance(data, dict) else None
        return [float(value) for value in stamps or [] if isinstance(value, (int, float))]

    @staticmethod
    def _write_watchdog_state(path: Path, stamps: list[float]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"console_restarts": stamps[-10:]}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _attempt_console_restart(self) -> dict[str, Any]:
        if os.name != "posix":
            return {"ok": False, "reason": "unsupported_platform"}
        if not bool(getattr(self.settings, "douyin_bridge_watchdog_console_restart", True)):
            return {"ok": False, "reason": "disabled"}
        script = self._console_restart_script()
        if script is None:
            return {"ok": False, "reason": "script_missing"}
        cooldown = max(60, int(getattr(self.settings, "douyin_bridge_watchdog_console_cooldown_seconds", 1800) or 1800))
        state_path = self._watchdog_state_path()
        now = time.time()
        recent = [stamp for stamp in self._read_watchdog_state(state_path) if now - stamp < 86_400]
        if recent and now - recent[-1] < cooldown:
            return {
                "ok": False,
                "reason": "cooldown",
                "retry_in": int(cooldown - (now - recent[-1])),
                "restarts_24h": len(recent),
            }
        if len(recent) >= 5:
            return {"ok": False, "reason": "limit", "restarts_24h": len(recent)}
        log_path = self._watchdog_restart_log_path()
        sink: Any = subprocess.DEVNULL
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            sink = open(log_path, "ab", buffering=0)
        except OSError:
            sink = subprocess.DEVNULL
        command = [
            "/bin/bash",
            str(script),
            "--start-agent",
            "--keep-log",
            "--port",
            str(self._console_port()),
        ]
        try:
            subprocess.Popen(
                command,
                cwd=str(script.parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as exc:
            return {"ok": False, "reason": "spawn_failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        finally:
            if sink is not subprocess.DEVNULL:
                try:
                    sink.close()
                except Exception:
                    pass
        recent.append(now)
        self._write_watchdog_state(state_path, recent)
        return {
            "ok": True,
            "reason": "restarting",
            "script": str(script),
            "restarts_24h": len(recent),
            "log": str(log_path),
        }

    def binding_key(self, name: str) -> str:
        return DouyinChatSession._name_key(name)

    @staticmethod
    def _conversation_signature(row: dict[str, Any] | None) -> str:
        """Stable-ish sidebar preview signature; no clicking is involved."""

        if not row:
            return ""
        preview = str(row.get("preview") or "")
        preview = re.sub(
            r"(刚刚|\d+\s*分钟前|\d+\s*小时前|昨天|前天|\d+\s*天前|\d{1,2}:\d{2})",
            "",
            preview,
        )
        preview = re.sub(r"\s+", " ", preview).strip()
        return hashlib.sha256(
            f"{row.get('name') or ''}|{preview}|{int(row.get('unread') or 0)}".encode("utf-8")
        ).hexdigest()

    # -- text handling -----------------------------------------------------
    def _notice_unbound(self, conversation_id: str) -> None:
        now = time.monotonic()
        last = self._unbound_notice_at.get(conversation_id, 0.0)
        if now - last >= 600:
            self._unbound_notice_at[conversation_id] = now
            self._reply(
                "收到媒体了，但这个抖音会话还没绑定微信。"
                "先在微信侧生成绑定码，再在这里发「绑定码 123456」。",
                important=True,
            )

    def _create_transfer_job(
        self,
        *,
        conversation_id: str,
        binding: dict[str, Any],
        kind: str,
        source_url: str,
        source_fingerprint: str,
        source_text: str = "",
        announce: bool = False,
        peer_name: str = "",
        provider: str = "douyin",
        origin: str = "douyin",
    ) -> dict[str, Any]:
        delivered_last_hour = self.store.delivered_since(self.account_id, utc_now() - timedelta(hours=1))
        if delivered_last_hour >= int(getattr(self.settings, "douyin_bridge_max_jobs_per_hour", 10)):
            self._reply("这一小时转得有点多，先停一下，过会儿再发。")
            return {"ok": False, "reason": "rate_limited"}
        job, created = self.store.create_job(
            account_id=self.account_id,
            conversation_id=conversation_id,
            peer_name=peer_name or self._conversation_name,
            kind=kind,
            source_fingerprint=source_fingerprint or self._url_fingerprint(source_url),
            source_text=source_text,
            source_url=source_url,
            target_wxid=str(binding["wxid"]),
            target_name=str(binding.get("wx_name") or ""),
            payload={
                "image_url": source_url if kind == "sticker" else "",
                "video_ref": source_url if kind == "video" else "",
                "provider": provider,
                "origin": origin,
            },
        )
        if not created:
            return {
                "ok": True,
                "duplicate": True,
                "job_id": job["job_id"],
                "status": job["status"],
            }
        self._jobs.put(job["job_id"])
        if announce:
            self._reply(f"收到，{self._kind_label(kind)}正在处理。")
        return {"ok": True, "job_id": job["job_id"], "kind": kind, "auto": not announce}

    def _auto_forward_media(
        self,
        message: ChatMessage,
        conversation_id: str,
        *,
        binding: dict[str, Any] | None = None,
        peer_name: str = "",
        offset: int = 0,
    ) -> dict[str, Any]:
        """A new image/video is forwarded without waiting for a command."""

        binding = binding or self.binding.for_conversation(conversation_id)
        if binding is None:
            self._notice_unbound(conversation_id)
            return {"ok": False, "reason": "no_binding"}
        video_ref = message.video_ref
        if message.kind == "video" and not video_ref and bool(getattr(self.settings, "douyin_bridge_card_click_resolve", True)):
            resolver = getattr(self._chat(), "resolve_video_ref", None)
            if callable(resolver):
                try:
                    row_text = str((message.raw or {}).get("rowText") or "")
                    try:
                        video_ref = resolver(
                            offset,
                            image_url=message.image_url,
                            row_text=row_text,
                        )
                    except TypeError:
                        # Backward compatibility for a three-argument-less
                        # resolver implementation.
                        video_ref = resolver(offset)
                except Exception as exc:
                    log.warning("douyin_bridge_card_resolve_failed error=%s", type(exc).__name__)
                    video_ref = ""
        if message.kind == "video" and not video_ref:
            # Auto-forward runs for every bound chat.  Replying in the chat
            # when a card has no resolvable id produces a stream of "no link"
            # messages to many people, so keep the failure local to the job
            # log and let the sender re-send a link explicitly.
            return {"ok": False, "reason": "video_ref_missing"}
        kind = "sticker" if message.kind == "image" else "video"
        source_url = message.image_url if kind == "sticker" else video_ref
        if not source_url:
            return {"ok": False, "reason": "media_url_missing"}
        return self._create_transfer_job(
            conversation_id=conversation_id,
            binding=binding,
            kind=kind,
            source_url=source_url,
            source_fingerprint=message.fingerprint,
            source_text=message.text,
            announce=False,
            peer_name=peer_name,
        )

    def _auto_forward_link(
        self,
        message: ChatMessage,
        conversation_id: str,
        *,
        binding: dict[str, Any] | None = None,
        peer_name: str = "",
    ) -> dict[str, Any]:
        """A bare supported video link is a video transfer, not chat."""

        binding = binding or self.binding.for_conversation(conversation_id)
        if binding is None:
            self._notice_unbound(conversation_id)
            return {"ok": False, "reason": "no_binding"}
        link = extract_export_link(message.text)
        if link is None:
            return {"ok": False, "reason": "video_ref_missing"}
        provider, source_url = link
        return self._create_transfer_job(
            conversation_id=conversation_id,
            binding=binding,
            kind="video",
            source_url=source_url,
            source_fingerprint=message.fingerprint,
            source_text=message.text,
            announce=False,
            peer_name=peer_name,
            provider=provider,
        )

    def poll_wechat(self) -> dict[str, Any]:
        """Read bound WeChat contacts for a sticker + explicit 抖音 command."""

        if self.backend is None or not self.enabled or self.settings.stop_file.exists():
            return {"ok": False, "reason": "unavailable"}
        if not bool(getattr(self.settings, "douyin_bridge_wechat_enabled", True)):
            return {"ok": False, "reason": "wechat_bridge_disabled"}
        ensure = getattr(self.backend, "_ensure_db", None)
        if callable(ensure):
            ensure()
        db = getattr(self.backend, "db", None)
        if db is None:
            return {"ok": False, "reason": "no_db"}
        handled = 0
        for binding in self.binding.list():
            wxid = str(binding.get("wxid") or "").strip()
            if not wxid or wxid.endswith("@chatroom"):
                continue
            try:
                chat = db.resolve_chat(wxid)
                rows = db.rows(chat.chat_id, limit=50)
            except Exception as exc:
                log.warning("douyin_bridge_wechat_read_failed wxid=%s error=%s", wxid, type(exc).__name__)
                continue
            if not rows:
                continue
            latest = max(int(row.get("local_id") or 0) for row in rows)
            cursor = self.store.wechat_cursor(self.account_id, wxid)
            if cursor is None:
                self.store.set_wechat_cursor(self.account_id, wxid, latest)
                continue
            auto = bool(getattr(self.settings, "douyin_bridge_wechat_auto_sticker", True))
            auto_sent = 0
            last_event = None
            for row in rows:
                local_id = int(row.get("local_id") or 0)
                if local_id <= cursor:
                    continue
                try:
                    event = self.backend._event(row, chat)
                except Exception:
                    self.store.set_wechat_cursor(self.account_id, wxid, local_id)
                    continue
                last_event = event
                try:
                    if not event.is_self:
                        if event.message_type == "sticker" and event.sticker_md5:
                            sticker = {
                                "md5": event.sticker_md5,
                                "size": int(event.sticker_size or 0),
                                "url": event.sticker_url or "",
                                "at": event.observed_at,
                            }
                            self._wx_pending.setdefault(wxid, []).append(sticker)
                            if auto:
                                auto_result = self._forward_wechat_stickers(binding, event, [sticker])
                                auto_sent += int(auto_result.get("sent") or 0)
                        elif event.message_type == "text":
                            link = extract_export_link(event.text)
                            if link is not None:
                                provider, url = link
                                self._create_wechat_link_job(binding, event, provider, url)
                                handled += 1
                            else:
                                command = parse_to_douyin_command(event.text)
                                if command is not None:
                                    self._handle_wechat_to_douyin(binding, event, command)
                                    handled += 1
                finally:
                    self.store.set_wechat_cursor(self.account_id, wxid, local_id)
            if auto_sent:
                self._wx_reply(wxid, f"{auto_sent} 个表情包已经发到抖音了。", last_event)
        return {"ok": True, "handled": handled}

    def _handle_wechat_to_douyin(self, binding: dict[str, Any], event: Any, command: Any) -> None:
        wxid = str(binding.get("wxid") or "")
        if command.kind not in {"sticker", "any"}:
            self._wx_reply(wxid, "现在只支持把微信表情包转到抖音；视频暂不支持。", event)
            return
        context_seconds = int(getattr(self.settings, "douyin_bridge_context_seconds", 300))
        pending = [
            item
            for item in self._wx_pending.get(wxid, [])
            if self._age_seconds(item.get("at")) is not None and self._age_seconds(item.get("at")) <= context_seconds
        ]
        if not pending:
            self._wx_reply(wxid, "先发一个微信表情包，再补一句「转到抖音」。", event)
            return
        result = self._forward_wechat_stickers(binding, event, pending)
        if result.get("sent"):
            self._wx_reply(wxid, f"{result['sent']} 个表情包已经发到抖音了。", event)
        elif result.get("failed"):
            self._wx_reply(wxid, "表情包发到抖音的结果不确定，先不自动重发。", event)

    def _forward_wechat_stickers(self, binding: dict[str, Any], event: Any, stickers: list[dict[str, Any]]) -> dict[str, int]:
        sent = 0
        failed = 0
        wxid = str(binding.get("wxid") or "")
        media = getattr(self.backend, "media", None) if self.backend is not None else None
        if media is None:
            return {"sent": 0, "failed": len(stickers)}
        try:
            self._chat().open_conversation(str(binding.get("peer_name") or ""))
        except Exception as exc:
            self._wx_reply(wxid, "打不开对应的抖音会话，表情包没发出去。", event)
            log.warning("douyin_bridge_wechat_open_failed wxid=%s error=%s", wxid, type(exc).__name__)
            return {"sent": 0, "failed": len(stickers)}
        remaining = list(self._wx_pending.get(wxid, []))
        for sticker in stickers:
            path = media.fetch_sticker(
                str(sticker.get("md5") or ""),
                int(sticker.get("size") or 0),
                str(sticker.get("url") or ""),
            )
            if not path:
                failed += 1
                continue
            try:
                result = self._chat().send_media(str(path))
            except Exception as exc:
                failed += 1
                log.warning("douyin_bridge_wechat_send_failed wxid=%s error=%s", wxid, type(exc).__name__)
                continue
            if result.get("verified"):
                sent += 1
                remaining = [item for item in remaining if item is not sticker]
            else:
                failed += 1
        if remaining:
            self._wx_pending[wxid] = remaining
        else:
            self._wx_pending.pop(wxid, None)
        return {"sent": sent, "failed": failed}

    def _wx_reply(self, wxid: str, text: str, event: Any) -> None:
        if self.delivery is None:
            return
        key = f"wxdy:{getattr(event, 'external_message_id', '')}:{text[:20]}"
        try:
            self.delivery.deliver_text(wxid, text, key=key)
        except Exception as exc:
            log.warning("douyin_bridge_wechat_reply_failed wxid=%s error=%s", wxid, type(exc).__name__)

    def _handle_text(
        self,
        message: ChatMessage,
        conversation_id: str,
        *,
        binding: dict[str, Any] | None = None,
        peer_name: str = "",
    ) -> dict[str, Any]:
        command = parse_command(message.text)
        if command is None:
            if getattr(self.settings, "douyin_bridge_chat_enabled", False):
                self._chat_reply(message)
                return {"action": "chat"}
            return {"action": "ignored"}

        if command.action == "bind":
            try:
                binding = self.binding.bind_with_code(
                    conversation_id=conversation_id,
                    peer_name=self._conversation_name,
                    code=command.code,
                )
            except ValueError:
                self._reply("绑定码不对或已经过期，请在微信侧重新生成一个。")
                return {"action": "bind", "ok": False}
            backend = getattr(getattr(self, "delivery", None), "backend", None)
            if backend is not None and callable(getattr(backend, "set_dm_allowlist_provider", None)):
                backend.set_dm_allowlist_provider(self.binding.wxids)
            self._pending_peers.pop(self._conversation_name, None)
            self._reply(
                f"绑定好了（{binding.get('wx_name') or '微信联系人'}）。之后把表情包或视频发到这里会自动转。",
                important=True,
            )
            return {"action": "bind", "ok": True}

        if command.action == "cancel":
            jobs = self.store.list_jobs(self.account_id, limit=20)
            pending = next(
                (
                    job
                    for job in jobs
                    if job["conversation_id"] == conversation_id
                    and job["status"] in {"detected", "resolving", "downloading", "ready"}
                ),
                None,
            )
            if pending is None:
                self._reply("现在没有正在处理的转移。")
                return {"action": "cancel", "ok": False}
            self.store.update_job(pending["job_id"], status="cancelled", error_code="CANCELLED", error_detail="用户取消")
            self._reply("好，这条不转了。")
            return {"action": "cancel", "ok": True}

        if command.action == "status":
            jobs = [
                job
                for job in self.store.list_jobs(self.account_id, limit=20)
                if job["conversation_id"] == conversation_id
            ]
            if not jobs:
                self._reply("还没有转过的记录。")
            else:
                job = jobs[0]
                self._reply(
                    f"最近一条：{self._kind_label(job['kind'])}，"
                    f"{_STATUS_LABELS.get(job['status'], job['status'])}。"
                )
            return {"action": "status", "ok": True}

        if command.action != "transfer":
            return {"action": command.action}

        binding = binding or self.binding.for_conversation(conversation_id)
        if binding is None:
            self._reply(
                "还没绑定微信。先在微信侧生成一个绑定码（控制台「抖音桥」里可以生成），"
                "再在这里发「绑定码 123456」。"
            )
            return {"action": "transfer", "ok": False, "reason": "no_binding"}

        pending = self.store.peer_media(self.account_id, conversation_id)
        source = None
        if command.url:
            source = {
                "kind": "video" if command.kind != "sticker" else "image",
                "url": command.url,
                "fingerprint": self._url_fingerprint(command.url),
                "text": message.text,
                "at": message.observed_at,
            }
        elif pending and not pending.get("consumed"):
            age = self._age_seconds(pending.get("at"))
            if age is not None and age <= int(getattr(self.settings, "douyin_bridge_context_seconds", 300)):
                source = {
                    "kind": pending.get("kind") or "",
                    "url": pending.get("url") or "",
                    "fingerprint": pending.get("fingerprint") or "",
                    "text": pending.get("text") or "",
                    "at": pending.get("at"),
                }

        if source is None:
            self._reply(
                "先把表情包或视频发给我，再补一句「转到微信」；"
                "也可以直接把视频链接和这句话一起发。"
            )
            return {"action": "transfer", "ok": False, "reason": "no_media"}

        kind = command.kind if command.kind != "any" else ("sticker" if source["kind"] == "image" else "video")
        if kind == "sticker" and source["kind"] != "image":
            self._reply("这条不是表情包/图片。要转视频的话说「把这个视频转到微信」。")
            return {"action": "transfer", "ok": False, "reason": "kind_mismatch"}
        if kind == "video" and not looks_like_video_ref(source.get("url") or ""):
            self._reply("这条视频卡片没读到可解析的链接。点开视频复制链接发我，或直接把链接和「转到微信」一起发。")
            return {"action": "transfer", "ok": False, "reason": "kind_mismatch"}
        if not source.get("url"):
            self._reply("这条媒体没有读到可下载的地址。麻烦重新转发一次，或把视频链接发我。")
            return {"action": "transfer", "ok": False, "reason": "url_missing"}

        delivered_last_hour = self.store.delivered_since(self.account_id, utc_now() - timedelta(hours=1))
        if delivered_last_hour >= int(getattr(self.settings, "douyin_bridge_max_jobs_per_hour", 10)):
            self._reply("这一小时转得有点多，先停一下，过会儿再发。")
            return {"action": "transfer", "ok": False, "reason": "rate_limited"}

        job, created = self.store.create_job(
            account_id=self.account_id,
            conversation_id=conversation_id,
            peer_name=peer_name or self._conversation_name,
            kind=kind,
            source_fingerprint=source["fingerprint"] or self._url_fingerprint(source["url"]),
            source_text=source.get("text") or "",
            source_url=source.get("url") or "",
            target_wxid=str(binding["wxid"]),
            target_name=str(binding.get("wx_name") or ""),
            payload={
                "image_url": source.get("url") if kind == "sticker" else "",
                "video_ref": source.get("url") if kind == "video" else "",
                "provider": (extract_export_link(source.get("url") or "") or ("douyin", ""))[0],
                "origin": "douyin",
            },
        )
        if not created:
            self._reply(
                f"这条已经在处理或发过了（{_STATUS_LABELS.get(job['status'], job['status'])}）。"
            )
            return {"action": "transfer", "ok": True, "duplicate": True, "job_id": job["job_id"]}

        if pending and pending.get("fingerprint"):
            self.store.consume_peer_media(self.account_id, conversation_id, pending["fingerprint"])
        self._reply(f"收到，{self._kind_label(kind)}正在处理。")
        self._jobs.put(job["job_id"])
        return {"action": "transfer", "ok": True, "job_id": job["job_id"], "kind": kind}

    def _chat_reply(self, message: ChatMessage) -> None:
        if self.llm is None or self._in_quiet_hours():
            return
        persona = self._persona_excerpt()
        system = (
            "你是一个中文聊天助手，正在抖音私信里回复。只输出一条口语化短回复，"
            "不超过 60 个汉字，不解释规则，不使用 Markdown，不提自己是 AI 或模型。"
            + (f"\n人设参考：{persona}" if persona else "")
        )
        user = f"对方说：{message.text[:500]}"
        try:
            text = str(self.llm.complete(system, user, temperature=0.8, max_tokens=200) or "").strip()
        except Exception as exc:
            log.warning("douyin_bridge_chat_llm_failed error=%s", type(exc).__name__)
            return
        text = re.sub(r"\s+", " ", text).strip()[:120]
        if not text or (text.startswith("[") and text.endswith("]")):
            return
        self._reply(text)

    def _persona_excerpt(self) -> str:
        path = Path(getattr(self.settings, "persona_file", "") or "")
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")[:1200]
        except OSError:
            pass
        return str(getattr(self.settings, "persona_name", "") or "")

    # -- job pipeline ------------------------------------------------------
    def _create_wechat_link_job(self, binding: dict[str, Any], event: Any, provider: str, url: str) -> dict[str, Any]:
        """WeChat message containing a Douyin/Bilibili link -> export to that chat."""

        wxid = str(binding.get("wxid") or "")
        if not wxid:
            return {"ok": False, "reason": "no_binding"}
        message_id = str(getattr(event, "external_message_id", "") or "")
        fingerprint = hashlib.sha256(f"wechat-link|{wxid}|{message_id}|{url}".encode("utf-8")).hexdigest()
        conversation_id = f"wechat:{wxid}"
        job, created = self.store.create_job(
            account_id=self.account_id,
            conversation_id=conversation_id,
            peer_name=str(binding.get("wx_name") or wxid),
            kind="video",
            source_fingerprint=fingerprint,
            source_text=str(getattr(event, "text", "") or ""),
            source_url=url,
            target_wxid=wxid,
            target_name=str(binding.get("wx_name") or ""),
            payload={"origin": "wechat", "provider": provider, "video_ref": url},
        )
        if created:
            self._jobs.put(job["job_id"])
            log.info("douyin_bridge_wechat_link_queued wxid=%s provider=%s", wxid, provider)
        return {"ok": True, "job_id": job["job_id"], "created": created}

    def _process_wechat_export_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Download a link and send it back to the same WeChat chat.

        There is no Douyin conversation and no Douyin receipt in this path.
        """

        job_id = str(job["job_id"])
        target = str(job.get("target_wxid") or "")
        if not target or self.delivery is None:
            self.store.update_job(job_id, status="failed", error_code="WECHAT_TARGET_MISSING", error_detail="微信后端或目标缺失")
            return {"ok": False, "reason": "target_missing"}
        self.store.increment_attempts(job_id)
        self.store.update_job(job_id, status="resolving", error_code="", error_detail="")
        try:
            media = self._prepare_video(job)
        except (MediaError, DTKError, BilibiliError) as exc:
            self.store.update_job(
                job_id,
                status="failed",
                error_code=getattr(exc, "code", "MEDIA_FAILED"),
                error_detail=str(exc)[:500],
            )
            return {"ok": False, "reason": "prepare_failed", "error": str(exc)}
        except Exception as exc:
            self.store.update_job(
                job_id,
                status="failed",
                error_code="PREPARE_FAILED",
                error_detail=f"{type(exc).__name__}: {exc}"[:500],
            )
            return {"ok": False, "reason": "prepare_failed", "error": str(exc)}
        if isinstance(media, StoredAlbum):
            media_path = media.paths[0] if media.paths else ""
            payload = {"album_count": len(media.paths), "album_paths": [str(path) for path in media.paths], "title": media.title}
        else:
            media_path = str(media.path)
            payload = {"animated": media.animated, "title": media.title, "watermark": media.watermark_verified}
        self.store.update_job(
            job_id,
            status="ready",
            media_path=str(media_path),
            media_sha256=media.sha256 if not isinstance(media, StoredAlbum) else "",
            media_bytes=media.bytes if not isinstance(media, StoredAlbum) else sum(path.stat().st_size for path in media.paths),
            mime=media.mime if not isinstance(media, StoredAlbum) else "image/jpeg",
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        try:
            receipts, warning = self._deliver(job, media)
        except DeliveryError as exc:
            if exc.code == "SEND_BUSY" and int(job.get("attempts") or 0) < 8:
                self.store.update_job(
                    job_id,
                    status="ready",
                    error_code="SEND_BUSY",
                    error_detail=str(exc)[:500],
                )
                time.sleep(2)
                self._jobs.put(job_id)
                return {"ok": False, "reason": "retry_scheduled", "error": str(exc)}
            status = "uncertain" if exc.code in {"SEND_UNCERTAIN", "WECHAT_UNVERIFIED", "SEND_UNVERIFIED", "SEND_BUSY"} else "failed"
            self.store.update_job(job_id, status=status, error_code=exc.code, error_detail=str(exc)[:500])
            return {"ok": False, "reason": status, "error": str(exc)}
        except Exception as exc:
            self.store.update_job(job_id, status="failed", error_code="DELIVERY_FAILED", error_detail=str(exc)[:500])
            return {"ok": False, "reason": "delivery_failed", "error": str(exc)}
        self.store.update_job(
            job_id,
            status="delivered",
            delivered_ids_json=json.dumps(receipts, ensure_ascii=False),
            error_code="PARTIAL_FILE" if warning else "",
            error_detail=warning,
        )
        self._delete_delivered_media(job, media)
        return {"ok": True, "job_id": job_id, "message_ids": receipts, "warning": warning}

    def process_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            return {"ok": False, "reason": "not_found"}
        if job["status"] in {"delivered", "cancelled", "uncertain", "rejected"}:
            return {"ok": False, "reason": job["status"]}
        if str((job.get("payload") or {}).get("origin") or "douyin") == "wechat":
            return self._process_wechat_export_job(job)

        self.store.increment_attempts(job_id)
        binding = self.binding.for_conversation(job["conversation_id"])
        if binding is None:
            # Console bindings may use the numeric conversation id captured at
            # bind time while the poller keys the chat by its display name.
            # Only accept an unambiguous peer-name match, and only if the
            # stored target wxid agrees: a duplicate display name must never
            # silently redirect a job to another person.
            candidates = [
                candidate
                for candidate in self.binding.list()
                if candidate.get("peer_name")
                and candidate.get("peer_name") == job.get("peer_name")
            ]
            if len(candidates) == 1:
                binding = candidates[0]
        if binding is None:
            self.store.update_job(job_id, status="needs_binding", error_code="NO_BINDING")
            return {"ok": False, "reason": "no_binding"}
        if job.get("target_wxid") and str(binding["wxid"]) != str(job["target_wxid"]):
            self.store.update_job(
                job_id,
                status="needs_binding",
                error_code="TARGET_MISMATCH",
                error_detail="任务目标与当前绑定不一致，已拒绝发送",
            )
            return {"ok": False, "reason": "target_mismatch"}

        self.store.update_job(
            job_id,
            status="resolving",
            target_wxid=str(binding["wxid"]),
            target_name=str(binding.get("wx_name") or ""),
        )
        target_conversation = str(job.get("peer_name") or "")
        try:
            if job["kind"] == "sticker":
                media = self._prepare_sticker(job)
            elif job["kind"] == "video":
                media = self._prepare_video(job)
            else:
                raise ValueError("unknown_job_kind")
        except (MediaError, DTKError, BilibiliError) as exc:
            self.store.update_job(
                job_id,
                status="failed",
                error_code=getattr(exc, "code", "MEDIA_FAILED"),
                error_detail=str(exc)[:500],
            )
            self._reply(f"没弄成：{self._error_label(exc)}", important=True, conversation=target_conversation)
            return {"ok": False, "reason": "prepare_failed", "error": str(exc)}
        except Exception as exc:
            self.store.update_job(
                job_id,
                status="failed",
                error_code="PREPARE_FAILED",
                error_detail=f"{type(exc).__name__}: {exc}"[:500],
            )
            self._reply("这次没弄成，稍后再试。", important=True, conversation=target_conversation)
            return {"ok": False, "reason": "prepare_failed", "error": str(exc)}

        if isinstance(media, StoredAlbum):
            album_paths = [str(path) for path in media.paths]
            media_path = album_paths[0] if album_paths else ""
            media_payload = {
                "album_count": len(album_paths),
                "album_paths": album_paths,
                "title": media.title,
            }
        else:
            media_path = str(media.path)
            media_payload = {
                "animated": media.animated,
                "preview_path": str(media.preview_path) if media.preview_path else "",
                "watermark": media.watermark_verified,
                "title": media.title,
            }
        self.store.update_job(
            job_id,
            status="ready",
            media_path=media_path,
            media_sha256=media.sha256,
            media_bytes=media.bytes,
            mime=media.mime,
            payload_json=json.dumps(
                {**(job.get("payload") or {}), **media_payload},
                ensure_ascii=False,
            ),
        )
        prepared_label = (
            f"图集（{len(media.paths)} 张）"
            if isinstance(media, StoredAlbum)
            else self._kind_label(job["kind"])
        )
        if getattr(self.settings, "douyin_bridge_dry_run", True):
            self._reply(
                f"{prepared_label}已经准备好了（dry-run，没发到微信）。",
                important=True,
                conversation=target_conversation,
            )
            return {"ok": True, "dry_run": True, "job_id": job_id}

        if self.delivery is None:
            self.store.update_job(job_id, status="needs_binding", error_code="WECHAT_BACKEND_UNAVAILABLE")
            return {"ok": False, "reason": "delivery_unavailable"}

        self.store.update_job(job_id, status="delivering")
        try:
            receipts, warning = self._deliver(job, media)
        except DeliveryError as exc:
            if exc.code == "SEND_BUSY" and int(job.get("attempts") or 0) < 8:
                # Another sender (group reply/proactive) briefly owns the
                # WeChat window.  Nothing was sent, so requeue instead of
                # losing the sticker as "uncertain".
                self.store.update_job(
                    job_id,
                    status="ready" if job.get("media_path") else "detected",
                    error_code="SEND_BUSY",
                    error_detail=str(exc)[:500],
                )
                time.sleep(2)
                self._jobs.put(job_id)
                return {"ok": False, "reason": "retry_scheduled", "error": str(exc)}
            status = (
                "uncertain"
                if exc.code in {"SEND_UNCERTAIN", "WECHAT_UNVERIFIED", "SEND_UNVERIFIED", "SEND_BUSY"}
                else "failed"
            )
            self.store.update_job(job_id, status=status, error_code=exc.code, error_detail=str(exc)[:500])
            self._reply(
                "微信发送结果不确定，我已经停下来，不会自动重发。请到微信里确认一下。"
                if status == "uncertain"
                else f"发到微信失败：{self._error_label(exc)}",
                important=True,
                conversation=target_conversation,
            )
            return {"ok": False, "reason": status, "error": str(exc)}
        except Exception as exc:
            self.store.update_job(
                job_id,
                status="failed",
                error_code="DELIVERY_FAILED",
                error_detail=f"{type(exc).__name__}: {exc}"[:500],
            )
            self._reply("发到微信时出错了，没发出去。", important=True, conversation=target_conversation)
            return {"ok": False, "reason": "delivery_failed", "error": str(exc)}

        self.store.update_job(
            job_id,
            status="delivered",
            delivered_ids_json=json.dumps(receipts, ensure_ascii=False),
            error_code="PARTIAL_FILE" if warning else "",
            error_detail=warning,
        )
        suffix = "（动画原文件没发出去）" if warning else ""
        delivered_label = (
            f"图集（{len(media.paths)} 张）"
            if isinstance(media, StoredAlbum)
            else self._kind_label(job["kind"])
        )
        self._reply(f"{delivered_label}发到你微信了{suffix}。", important=True, conversation=target_conversation)
        self._delete_delivered_media(job, media)
        return {"ok": True, "job_id": job_id, "message_ids": receipts, "warning": warning}

    # -- media retention ---------------------------------------------------
    def _media_directory(self) -> Path:
        return Path(getattr(self.settings, "douyin_bridge_media_dir", "state/douyin-bridge"))

    def _maybe_cleanup_media(self) -> None:
        """Sweep expired leftover media at most once every 30 minutes."""

        now = time.monotonic()
        if now - self._last_media_cleanup < 1800:
            return
        self._last_media_cleanup = now
        try:
            self._cleanup_expired_media()
        except Exception as exc:
            log.warning("douyin_bridge_media_cleanup_failed error=%s", type(exc).__name__)

    def _cleanup_expired_media(self) -> None:
        directory = self._media_directory()
        if not directory.is_dir():
            return
        hours = max(1, int(getattr(self.settings, "douyin_bridge_media_retention_hours", 2)))
        cutoff = time.time() - hours * 3600
        removed = 0
        freed = 0
        for path in directory.iterdir():
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                if stat.st_mtime > cutoff:
                    continue
                path.unlink()
                removed += 1
                freed += int(stat.st_size)
            except OSError:
                continue
        if removed:
            log.info(
                "douyin_bridge_media_cleanup removed=%s freed_mb=%.1f retention_hours=%s",
                removed, freed / 1024 / 1024, hours,
            )

    def _active_media_names(self) -> set[str]:
        names: set[str] = set()
        for job in self.store.list_jobs(self.account_id, limit=200):
            status = str(job.get("status") or "")
            if status in {"delivered", "cancelled", "rejected"}:
                continue
            path = str(job.get("media_path") or "")
            if path:
                names.add(Path(path).name)
            payload = job.get("payload") or {}
            for item in payload.get("album_paths") or []:
                names.add(Path(str(item)).name)
        return names

    def _delete_delivered_media(self, job: dict[str, Any], media: StoredMedia | StoredAlbum) -> None:
        """Remove local copies once delivery is verified.

        Files still referenced by an unfinished/failed job are kept until the
        retention sweep, so a retry or later inspection is not broken.
        """

        try:
            directory = self._media_directory()
            if not directory.is_dir():
                return
            paths = list(media.paths) if isinstance(media, StoredAlbum) else [Path(media.path)]
            if not isinstance(media, StoredAlbum) and media.preview_path:
                paths.append(Path(media.preview_path))
            names: set[str] = set()
            for path in paths:
                names.add(path.name)
                names.add(f"{path.stem}_wechat.gif")
                names.add(f"{path.stem}_wechat.png")
                names.add(f".{path.stem}.part")
            active = self._active_media_names()
            removed = 0
            for name in names:
                if not name or name in active:
                    continue
                target = directory / name
                try:
                    if target.is_file():
                        target.unlink()
                        removed += 1
                except OSError:
                    continue
            if removed:
                log.info("douyin_bridge_media_deleted job=%s files=%s", job.get("job_id", ""), removed)
        except Exception as exc:
            log.warning(
                "douyin_bridge_media_delete_failed job=%s error=%s",
                job.get("job_id", ""), type(exc).__name__,
            )

    def _prepare_sticker(self, job: dict[str, Any]) -> StoredMedia:
        url = str(job.get("payload", {}).get("image_url") or job.get("source_url") or "").strip()
        if not url:
            raise MediaError("STICKER_URL_MISSING", "没有读到表情包图片地址")
        max_bytes = int(getattr(self.settings, "douyin_bridge_max_sticker_mb", 10)) * 1024 * 1024
        directory = Path(getattr(self.settings, "douyin_bridge_media_dir", "state/douyin-bridge"))
        if url.startswith(("blob:", "data:")):
            data = self._chat().fetch_image_bytes(url, max_bytes=max_bytes)
            return store_image_bytes(data, directory, max_bytes=max_bytes)
        try:
            return self.fetcher.fetch(url, directory, max_bytes=max_bytes)
        except MediaError as exc:
            if exc.code not in {"MEDIA_FORBIDDEN", "MEDIA_UNREACHABLE", "MEDIA_HTTP_ERROR"}:
                raise
            # The CDN may reject a bare HTTP client while accepting the page's
            # own cookies.  Fetch through the visible tab, then validate bytes.
            data = self._chat().fetch_image_bytes(url, max_bytes=max_bytes)
            return store_image_bytes(data, directory, max_bytes=max_bytes)

    def _prepare_bilibili(self, job: dict[str, Any]) -> StoredMedia:
        if not bool(getattr(self.settings, "bilibili_enabled", True)):
            raise BilibiliError("BILI_DISABLED", "B 站导出未启用")
        url = str(job.get("source_url") or (job.get("payload") or {}).get("video_ref") or "").strip()
        if not url:
            raise BilibiliError("BILI_BAD_URL", "没有 B 站链接")
        directory = Path(getattr(self.settings, "douyin_bridge_media_dir", "state/douyin-bridge"))
        return self._bilibili_downloader().download(url, directory)

    def _prepare_video(self, job: dict[str, Any]) -> StoredMedia | StoredAlbum:
        provider = str((job.get("payload") or {}).get("provider") or "douyin")
        if provider == "bilibili":
            return self._prepare_bilibili(job)
        if self.dtk is None or not getattr(self.dtk, "configured", False):
            raise DTKError("DTK_NOT_CONFIGURED", "还没有配置无水印解析服务（DTK_BASE_URL）")
        ref = str(job.get("source_url") or job.get("payload", {}).get("video_ref") or "").strip()
        if not ref:
            raise DTKError("VIDEO_REF_MISSING", "没有读到视频链接")
        parsed_video = self.dtk.parse(ref)
        if parsed_video.kind == "image_album":
            return self._prepare_album(parsed_video)
        if parsed_video.watermark is True:
            raise DTKError("DTK_WATERMARKED", "解析结果只有带水印流")
        max_bytes = int(getattr(self.settings, "douyin_bridge_max_video_mb", 500)) * 1024 * 1024
        directory = Path(getattr(self.settings, "douyin_bridge_media_dir", "state/douyin-bridge"))
        media = self.dtk.download(parsed_video, directory, max_bytes=max_bytes)
        return StoredMedia(
            path=media.path,
            sha256=media.sha256,
            bytes=media.bytes,
            mime=media.mime,
            kind="video",
            animated=False,
            title=parsed_video.title,
            source_url=media.source_url or parsed_video.video_url,
            watermark_verified=parsed_video.watermark,
        )

    def _prepare_album(self, parsed_album: ParsedVideo) -> StoredAlbum:
        """Download a 图集's images so they can be sent as native WeChat images.

        Images are bounded by the sticker size limit (per image) and by
        ``DOUYIN_BRIDGE_MAX_ALBUM_IMAGES``.  Nothing is sent unless every
        image in the selected range downloaded successfully, so a half-broken
        album never turns into a half-album in WeChat.
        """

        limit = max(1, int(getattr(self.settings, "douyin_bridge_max_album_images", 9)))
        urls = [url for url in parsed_album.image_urls if url][:limit]
        if not urls:
            raise DTKError("DTK_NO_MEDIA", "图集解析结果里没有图片")
        max_bytes = int(getattr(self.settings, "douyin_bridge_max_sticker_mb", 10)) * 1024 * 1024
        directory = Path(getattr(self.settings, "douyin_bridge_media_dir", "state/douyin-bridge"))
        images: list[StoredMedia] = []
        for url in urls:
            try:
                images.append(self.fetcher.fetch(url, directory, max_bytes=max_bytes))
            except MediaError as exc:
                if exc.code not in {"MEDIA_FORBIDDEN", "MEDIA_UNREACHABLE", "MEDIA_HTTP_ERROR"}:
                    raise
                # The CDN may reject the bare client while accepting the page's
                # own cookies; reuse the visible Douyin tab in that case.
                data = self._chat().fetch_image_bytes(url, max_bytes=max_bytes)
                images.append(store_image_bytes(data, directory, max_bytes=max_bytes))
        if not images:
            raise MediaError("ALBUM_EMPTY", "图集里没有可下载的图片")
        digest = hashlib.sha256("".join(image.sha256 for image in images).encode("utf-8")).hexdigest()
        return StoredAlbum(
            paths=tuple(image.path for image in images),
            sha256=digest,
            bytes=sum(image.bytes for image in images),
            title=parsed_album.title,
            source_url=parsed_album.image_urls[0] if parsed_album.image_urls else "",
        )

    def _deliver(self, job: dict[str, Any], media: StoredMedia | StoredAlbum) -> tuple[list[str], str]:
        target = str(job.get("target_wxid") or "")
        if not target:
            raise DeliveryError("WECHAT_TARGET_MISSING", "任务没有绑定微信 wxid")
        receipts: list[str] = []
        warning = ""
        base = f"dybridge:{job['job_id']}"

        def attempt(call):
            # A chat-list race before the file dialog leaves no attachment;
            # one short retry is safe and keeps a batch from losing a row.
            for index in range(2):
                try:
                    return call()
                except DeliveryError as exc:
                    if index == 0 and exc.code in {"CHAT_IDENTITY_UNCONFIRMED", "CHAT_NOT_VISIBLE"}:
                        time.sleep(1.0)
                        continue
                    raise
            raise DeliveryError("WECHAT_SEND_FAILED", "发送重试失败")

        if isinstance(media, StoredAlbum):
            # Keep the author's original order; each image gets its own ledger
            # key so a retry after a partial failure never resends a verified
            # bubble (the backend returns the existing receipt for that key).
            for index, path in enumerate(media.paths):
                result = attempt(lambda: self.delivery.deliver_image(target, path, key=f"{base}:album:{index}"))
                receipts.extend(result.get("message_ids") or [])
                time.sleep(0.4)
        elif media.kind == "image":
            # Send the sticker itself (GIF), never a native image/PNG preview.
            # WeChat receives a GIF chosen through the file picker as an
            # appmsg type-8 native animated sticker.
            sticker = sticker_gif_for_wechat(media)
            result = attempt(lambda: self.delivery.deliver_file(target, sticker, key=base + ":sticker"))
            receipts.extend(result.get("message_ids") or [])
        else:
            result = attempt(lambda: self.delivery.deliver_file(target, media.path, key=base + ":video"))
            receipts.extend(result.get("message_ids") or [])
        return list(dict.fromkeys(receipts)), warning

    # -- replies -----------------------------------------------------------
    def _in_quiet_hours(self, now: datetime | None = None) -> bool:
        value = str(getattr(self.settings, "douyin_bridge_quiet_hours", "") or "").strip()
        match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", value)
        if not match:
            return False
        current = (now or datetime.now().astimezone()).time()
        start = clock_time(int(match.group(1)), int(match.group(2)))
        end = clock_time(int(match.group(3)), int(match.group(4)))
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _reply(self, text: str, *, important: bool = False, conversation: str = "") -> None:
        if not getattr(self.settings, "douyin_bridge_reply_enabled", True):
            return
        if self.chat is None:
            return
        now = time.monotonic()
        self._reply_times = [value for value in self._reply_times if now - value < 3600]
        if len(self._reply_times) >= 30:
            return
        if not important and self._reply_times and now - self._reply_times[-1] < 2.0:
            return
        try:
            if conversation:
                # Job receipts are sent after the poll loop has already moved
                # to another conversation, so address the job's own chat
                # explicitly instead of using whatever is currently open.
                opened = self._chat().open_conversation(conversation)
                if not opened.get("ok"):
                    log.warning(
                        "douyin_bridge_reply_target_failed conversation=%s error=%s",
                        conversation, str(opened.get("error") or "")[:120],
                    )
                    return
                opened_name = str(opened.get("name") or "").strip()
                if opened_name and opened_name != str(conversation).strip():
                    log.warning(
                        "douyin_bridge_reply_identity_mismatch expected=%s actual=%s",
                        conversation, opened_name,
                    )
                    return
            self._chat().send_text(text)
            self._reply_times.append(now)
            log.info(
                "douyin_bridge_reply_sent conversation=%s text=%s",
                conversation or self._conversation_name,
                str(text)[:80],
            )
        except Exception as exc:
            log.warning("douyin_bridge_reply_failed error=%s", type(exc).__name__)

    # -- console/API -------------------------------------------------------
    def read_conversation(self, name: str, limit: int = 200) -> dict[str, Any]:
        """Read one conversation without creating jobs or changing seen state."""

        target = str(name or "").strip()
        if not target:
            raise ValueError("peer_name_required")

        def read() -> dict[str, Any]:
            chat = self._chat()
            opened = chat.open_conversation(target)
            messages = chat.read_messages(max(1, min(int(limit), 200)))
            return {
                "conversation": str(opened.get("name") or target),
                "conversation_id": str(opened.get("conversation_id") or ""),
                "messages": [
                    {
                        "index": index,
                        "direction": message.direction,
                        "kind": message.kind,
                        "text": message.text,
                        "image_url": message.image_url,
                        "video_ref": message.video_ref,
                        "fingerprint": message.fingerprint,
                        "row_text": str((message.raw or {}).get("rowText") or ""),
                        "observed_at": message.observed_at.isoformat() if message.observed_at else "",
                    }
                    for index, message in enumerate(messages)
                ],
            }

        return self.call_in_loop(read)

    def forward_conversation_messages(self, name: str, indexes: list[int]) -> dict[str, Any]:
        """Create sticker jobs only for explicitly selected message indexes."""

        target = str(name or "").strip()
        selected = {int(value) for value in indexes}
        if not target or not selected:
            raise ValueError("peer_name_and_indexes_required")
        read = self.read_conversation(target, limit=200)
        binding = next((item for item in self.binding.list() if item.get("peer_name") == target), None)
        if binding is None:
            raise ValueError("binding_not_found")
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for message in read["messages"]:
            index = int(message["index"])
            if index not in selected:
                continue
            if message["direction"] != "in" or message["kind"] != "image" or not message["image_url"]:
                skipped.append({"index": index, "reason": "not_an_inbound_image"})
                continue
            outcome = self._create_transfer_job(
                conversation_id=str(binding.get("conversation_id") or self.binding_key(target)),
                binding=binding,
                kind="sticker",
                source_url=message["image_url"],
                source_fingerprint=hashlib.sha256(
                    f"{message['fingerprint']}|selected:{index}".encode("utf-8")
                ).hexdigest(),
                source_text=message["text"] or "[表情]",
                announce=False,
                peer_name=target,
            )
            if outcome.get("job_id") and not outcome.get("duplicate"):
                created.append({"index": index, "job_id": outcome["job_id"]})
            else:
                skipped.append({
                    "index": index,
                    "reason": "duplicate" if outcome.get("duplicate") else str(outcome.get("reason") or "not_created"),
                    "job_id": outcome.get("job_id", ""),
                })
        return {"conversation": target, "created": created, "skipped": skipped}

    def list_conversations(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            chat = self._chat()
            if refresh:
                return chat.refresh_conversations()
            return chat.list_conversations()

        rows = self.call_in_loop(load)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows or []:
            name = str((row or {}).get("name") or "").strip()
            placeholder = bool(re.fullmatch(r"\d{8,}", name))
            if not name or name in seen or placeholder:
                # The chat page briefly renders long numeric placeholders
                # while hydrating; short real names such as "12" stay valid.
                continue
            seen.add(name)
            result.append({**row, "name": name, "conversation_id": self.binding_key(name)})
        return result

    def dom_probe(self) -> dict[str, Any]:
        probe = getattr(self._chat(), "dom_probe", None)
        if not callable(probe):
            return {"available": False, "hint": "当前 chat 适配器没有 dom_probe"}
        return {"available": True, **self.call_in_loop(probe)}

    def status(self) -> dict[str, Any]:
        conversation_id = self._conversation_id or (
            self.binding_key(self._conversation_name) if self._conversation_name else ""
        )
        binding = self.binding.for_conversation(conversation_id) if conversation_id else None
        all_bindings = self.binding.list()
        if binding is None and all_bindings:
            binding = all_bindings[0]
        jobs = self.store.list_jobs(self.account_id, limit=200)
        counts = Counter(job["status"] for job in jobs)
        chat_status: dict[str, Any] = {}
        if self.chat is not None:
            try:
                chat_status = self.chat.status()
            except Exception as exc:
                chat_status = {"connected": False, "last_error": f"{type(exc).__name__}: {exc}"}
        return {
            "enabled": self.enabled,
            "account_id": self.account_id,
            "conversation": self._conversation_name
            or str(getattr(self.settings, "douyin_bridge_conversation", "") or ""),
            "conversation_id": conversation_id,
            "chat": chat_status,
            "binding": binding,
            "bindings": all_bindings,
            "bindings_count": len(all_bindings),
            "pending_peers": [name for name in self._pending_peers],
            "wechat_to_douyin_enabled": bool(getattr(self.settings, "douyin_bridge_wechat_enabled", True)),
            "wechat_sticker_auto": bool(getattr(self.settings, "douyin_bridge_wechat_auto_sticker", True)),
            "dry_run": bool(getattr(self.settings, "douyin_bridge_dry_run", True)),
            "reply_enabled": bool(getattr(self.settings, "douyin_bridge_reply_enabled", True)),
            "chat_enabled": bool(getattr(self.settings, "douyin_bridge_chat_enabled", False)),
            "dtk_configured": bool(self.dtk is not None and getattr(self.dtk, "configured", False)),
            "delivery_available": bool(self.delivery is not None and getattr(self.delivery, "available", False)),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_poll_at": self._last_poll_at.isoformat() if self._last_poll_at else None,
            "last_error": self._last_error,
            "last_result": self._last_result,
            "recent_messages": self._recent_messages,
            "watchdog": self._watchdog_status(),
            "counts": dict(counts),
            "jobs": jobs[:20],
        }

    def _watchdog_status(self) -> dict[str, Any]:
        enabled, threshold = self._watchdog_conf()
        return {
            "enabled": enabled,
            "threshold": threshold,
            "streak": self._watchdog_streak,
            "level": self._watchdog_level,
            "blocked": self._watchdog_blocked,
            "last_failure_at": self._watchdog_last_failure_at.isoformat() if self._watchdog_last_failure_at else "",
            "last_recovery": dict(self._watchdog_last_recovery),
        }

    def jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_jobs(self.account_id, limit=limit)

    def bindings(self) -> list[dict[str, Any]]:
        return self.binding.list()

    def create_code(self, payload: dict[str, Any]) -> dict[str, Any]:
        peer_name = str(payload.get("peer_name") or "").strip()
        result = self.binding.create_code(
            wxid=payload.get("wxid"),
            wx_name=payload.get("wx_name") or "",
        )
        if peer_name:
            ttl = int(getattr(self.settings, "binding_code_ttl_minutes", 10) or 10)
            self._pending_peers[peer_name] = time.monotonic() + ttl * 60
            result = {**result, "peer_name": peer_name}
        return result

    def bind_manual(self, payload: dict[str, Any]) -> dict[str, Any]:
        peer_name = str(payload.get("peer_name") or self._conversation_name or "").strip()
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id and peer_name:
            # The chat's stable key is deterministic from its display name, so
            # an operator can bind by name without copying a hash.
            conversation_id = self.binding_key(peer_name)
        return self.binding.bind_manual(
            conversation_id=conversation_id or self._conversation_id,
            wxid=payload.get("wxid"),
            peer_name=peer_name,
            wx_name=payload.get("wx_name") or "",
            douyin_user_id=payload.get("douyin_user_id") or "",
        )

    def delete_binding(self, binding_id: object) -> dict[str, Any]:
        deleted = self.binding.delete(binding_id)
        return {"deleted": deleted, "bindings": self.binding.list()}

    def retry_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError("job_not_found")
        if job["status"] not in {"failed", "ready", "detected", "cancelled"}:
            raise ValueError(f"job_not_retryable:{job['status']}")
        self.store.update_job(job_id, status="detected", error_code="", error_detail="")
        self._jobs.put(job_id)
        return {"job": self.store.get_job(job_id)}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError("job_not_found")
        if job["status"] in {"delivered", "delivering", "uncertain"}:
            raise ValueError(f"job_not_cancellable:{job['status']}")
        self.store.update_job(job_id, status="cancelled", error_code="CANCELLED", error_detail="操作员取消")
        return {"job": self.store.get_job(job_id)}

    def resend_job(self, job_id: str) -> dict[str, Any]:
        """Create a fresh job/ledger key for media that never got a message.

        A failed job can have an old ``SEND_UNCERTAIN`` ledger entry, which
        retry_job must not reuse.  Here the old job has no delivered message
        ids, so a new job id is safe and lets a batch finish without replaying
        anything that actually reached WeChat.
        """

        old = self.store.get_job(job_id)
        if old is None:
            raise ValueError("job_not_found")
        if old.get("delivered_message_ids"):
            raise ValueError("job_already_delivered")
        if old["status"] not in {"failed", "uncertain", "cancelled", "detected", "ready"}:
            raise ValueError(f"job_not_resendable:{old['status']}")
        source_url = str(old.get("source_url") or "")
        if not source_url:
            raise ValueError("job_source_missing")
        binding = self.binding.for_conversation(old["conversation_id"])
        if binding is None:
            raise ValueError("binding_missing")
        stamp = utc_now().isoformat()
        fingerprint = hashlib.sha256(
            f"{old.get('source_fingerprint')}|resend|{stamp}".encode("utf-8")
        ).hexdigest()
        job, created = self.store.create_job(
            account_id=self.account_id,
            conversation_id=old["conversation_id"],
            peer_name=old.get("peer_name") or "",
            kind=old["kind"],
            source_fingerprint=fingerprint,
            source_text=old.get("source_text") or "",
            source_url=source_url,
            target_wxid=str(binding["wxid"]),
            target_name=str(binding.get("wx_name") or ""),
            payload={
                "image_url": source_url if old["kind"] == "sticker" else "",
                "video_ref": source_url if old["kind"] == "video" else "",
            },
        )
        if created:
            self._jobs.put(job["job_id"])
        return {"job": job, "created": created}

    def probe_open(self, name: str) -> dict[str, Any]:
        """Diagnostic: open one conversation without reading or creating jobs."""

        target = str(name or "").strip()
        if not target:
            raise ValueError("peer_name_required")
        return self.call_in_loop(lambda: self._chat().open_conversation(target))

    def calibrate(self) -> dict[str, Any]:
        if self.chat is None:
            self._chat()
        # Never touch Playwright from the console thread; the poll thread
        # caches the structure after its baseline.
        return self._chat().calibrate(live=False)

    def run_once(self, *, process: bool = True) -> dict[str, Any]:
        result = self.poll_once()
        self.poll_wechat()
        if process:
            self._drain_jobs()
        return result

    # -- helpers -----------------------------------------------------------
    def _dedupe_key(self, message: ChatMessage, conversation_id: str, *, occurrence: int = 0) -> str:
        """Content fingerprints are enough for media, not for repeated commands.

        A user may legitimately say "转到微信" again for a different sticker.
        A transfer command is therefore keyed by the media it currently refers
        to; non-transfer text is bucketed by ten minutes so a repeat status
        query still works while a polling loop cannot spam the same reply.

        Media are keyed by content plus their ordinal among identical rows in
        the current window.  The first occurrence keeps the legacy plain
        fingerprint (so existing seen rows remain valid); a second identical
        sticker gets a numbered key and is forwarded instead of being mistaken
        for a polling duplicate.
        """

        if message.kind != "text" or not message.text:
            if occurrence <= 0:
                return message.fingerprint
            return hashlib.sha256(f"{message.fingerprint}|occurrence:{occurrence}".encode("utf-8")).hexdigest()
        command = parse_command(message.text)
        if command is not None and command.action == "transfer":
            pending = self.store.peer_media(self.account_id, conversation_id)
            context = str((pending or {}).get("fingerprint") or "") or command.url or "no-media"
            return hashlib.sha256(f"{message.fingerprint}|{context}".encode("utf-8")).hexdigest()
        bucket = int(time.time() // 600)
        return hashlib.sha256(f"{message.fingerprint}|{bucket}".encode("utf-8")).hexdigest()

    @staticmethod
    def _url_fingerprint(url: str) -> str:
        return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _age_seconds(value: object) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (utc_now() - parsed).total_seconds())

    @staticmethod
    def _kind_label(kind: str) -> str:
        return {"sticker": "表情包", "video": "视频", "album": "图集"}.get(str(kind), str(kind))

    @staticmethod
    def _error_label(exc: Exception) -> str:
        code = str(getattr(exc, "code", "") or "")
        return {
            "DTK_NOT_CONFIGURED": "无水印解析服务还没配置",
            "DTK_WATERMARKED": "只拿到带水印的视频，已拒绝",
            "DTK_TIMEOUT": "解析超时",
            "DTK_NO_MEDIA": "没有解析到视频流",
            "DTK_DOWNLOAD_FAILED": "视频下载连不上，已自动重试仍失败",
            "DTK_NOT_VIDEO": "下载到的不是视频",
            "DTK_EMPTY_FILE": "视频下载为空",
            "ALBUM_EMPTY": "图集里没有可下载的图片",
            "STICKER_URL_MISSING": "没读到表情包地址",
            "MEDIA_TOO_LARGE": "文件超过大小上限",
            "MEDIA_FORBIDDEN": "图片资源拒绝访问",
            "WECHAT_CHAT_NOT_FOUND": "微信里找不到这个绑定联系人",
            "WECHAT_UNVERIFIED": "微信没有回读到消息",
        }.get(code, code or "未知错误")
