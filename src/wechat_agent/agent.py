from __future__ import annotations

import logging
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, replace

from .config import Settings
from .image import ImageClient
from .image_context import select_image_paths
from .llm import LLMClient
from .models import MemoryFact, MessageEvent, OutboundAction
from .policy import ActionPolicy
from .profiles import (
    extract_member_profile_patch,
    extract_task_patch,
    has_task_signal,
    infer_task_id,
)
from .prompt_profiles import build_profile_summary
from .profile_synthesis import synthesize_group_profile
from . import reply_jobs
from .reply_jobs import ReplyJob
from .reply_context import can_follow_up, image_quote
from .social import INTIMATE_AFFINITY_THRESHOLD, parse_address_request
from .storage import Storage
from .textnorm import normalize_chat_text
from .timefmt import stamp
from .web_research import WebResearchClient
from .platform import create_backend as WeixinUiAdapter
from .memory_runtime import MemoryRuntime
from .memory_orchestrator import MemoryOrchestrator
from .proactive_scheduler import ProactiveScheduler
from .reaction_images import ReactionImages
from .voice import VoiceService
from .speech import SpeechReplies, voice_reply_declined, voice_request_kind
from .private_welcome import choose_welcome_message, load_sent, save_sent, welcome_key

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentStats:
    seen: int = 0
    replied: int = 0
    skipped: int = 0
    duplicate_questions: int = 0
    failed: int = 0
    proactive_replied: int = 0
    reaction_images: int = 0
    voice_replies: int = 0


class WeChatAgent:
    def __init__(
        self,
        settings: Settings,
        *,
        ui=None,
        llm=None,
        storage=None,
        policy=None,
        platform_service=None,
        platform_automation=None,
        platform_memory=None,
        web_research=None,
        image_client=None,
        voice_service=None,
        speech_service=None,
    ):
        self.settings = settings
        self.storage = storage or Storage(settings)
        self.storage.init_schema()
        self.ui = ui or WeixinUiAdapter(settings)
        self.llm = llm or LLMClient(settings)
        bind_storage = getattr(self.ui, "bind_storage", None)
        if callable(bind_storage):
            bind_storage(self.storage)
        self.memory = MemoryRuntime(self.storage, llm=self.llm)
        self.memory.media_enricher = getattr(self.ui, "enrich", None)
        self.orchestrator = MemoryOrchestrator(self.memory)
        self.scheduler = ProactiveScheduler(self.memory)
        self.memory.scheduler = self.scheduler
        self.policy = policy or ActionPolicy(settings, self.storage, emotion=self.memory.emotion)
        self.web_research = web_research or WebResearchClient(
            settings,
            platform_service=platform_service,
        )
        # Optional three-way platform layer.  ``platform_automation`` owns the
        # identity/memory services; both may be injected independently so old
        # CLI/test constructions keep working unchanged.
        self.platform_automation = platform_automation
        self.platform_memory = platform_memory or getattr(platform_automation, "memory", None)
        self.image_client = image_client or ImageClient(settings)
        self.reaction_images = ReactionImages(settings, self.image_client)
        self.voice = voice_service or VoiceService(settings, self.llm, wechat_transcriber=getattr(self.ui, "transcribe_voice", None))
        self.speech = speech_service or SpeechReplies(settings)
        self.stats = AgentStats()
        # Reply obligations live in SQLite; the owner id only fences leases.
        self._reply_owner = uuid.uuid4().hex
        self._recent_outbound: dict[str, float] = {}
        # Per-group pictures that a member explicitly pointed at again.  Each
        # entry keeps the image in the prompt for a few more replies instead of
        # re-attaching every historical picture for the whole context window.
        self._image_refresh: dict[str, dict[str, int]] = {}
        self._synthesis_inflight: set[tuple[str, str]] = set()
        self._training_export_attempted: set[str] = set()
        self._training_export_retry_at: dict[str, float] = {}

    def ready(self) -> dict[str, str]:
        """Readiness check only. It never opens a conversation or sends text."""
        return self.ui.check_ready()

    def _maybe_export_daily_training(self) -> None:
        """Export one local calendar day after the configured evening time.

        It is deliberately opt-in and local-only.  If the process was down at
        the scheduled time, the next poll catches up on the previous day once;
        a marker prevents duplicate exports on restart.
        """

        if not self.settings.training_export_enabled:
            return
        now = datetime.now().astimezone()
        scheduled_today = now.replace(
            hour=int(self.settings.training_export_hour),
            minute=int(self.settings.training_export_minute),
            second=0,
            microsecond=0,
        )
        target_day = now.date() if now >= scheduled_today else (now.date() - timedelta(days=1))
        day_text = target_day.isoformat()
        if day_text in self._training_export_attempted:
            return
        retry_at = self._training_export_retry_at.get(day_text, 0.0)
        if retry_at > time.monotonic():
            return
        self._training_export_attempted.add(day_text)

        target = Path(self.settings.training_export_dir).expanduser() / day_text
        marker = target / ".exported"
        if marker.exists():
            return
        start = datetime.combine(target_day, clock_time.min, tzinfo=now.tzinfo)
        since = start.astimezone(timezone.utc)
        until = (start + timedelta(days=1)).astimezone(timezone.utc)
        try:
            from .training_data import export_training_data

            report = export_training_data(
                self.storage,
                target,
                window_messages=self.settings.training_export_context_messages,
                anonymize=self.settings.training_export_anonymize,
                since=since,
                until=until,
            )
            marker.write_text(day_text + "\n", encoding="utf-8")
            log.info(
                "daily training export day=%s messages=%s path=%s",
                day_text,
                report.get("messages"),
                target,
            )
        except Exception:
            # Allow a bounded retry instead of silently missing the day.
            self._training_export_attempted.discard(day_text)
            self._training_export_retry_at[day_text] = time.monotonic() + 1_800.0
            log.exception("daily training export failed day=%s", day_text)

    def run(self) -> None:
        if self.settings.effective_platform == "linux":
            return self._run_linux()
        try:
            self.ui.connect()
            self.memory.worker.start()
            self._recover_reply_jobs()
            if (not getattr(self.settings, "wechat_group_names", None)
                    and not getattr(self.settings, "wechat_private_reply_enabled", False)):
                raise RuntimeError("请在 .env 设置 WECHAT_GROUP_NAMES 或开启 WECHAT_PRIVATE_REPLY_ENABLED 后再启动真实微信监听")
            log.info(
                "Listening to configured test groups: %s",
                ", ".join(self.settings.wechat_group_names),
            )
            while not self.policy.is_stopped():
                try:
                    for event in self.ui.poll(account_id=self._account_id()):
                        self._enqueue_reply(event)
                    self._reply_next()
                    self.storage.purge_expired_messages()
                    self._maybe_export_daily_training()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self.stats.failed += 1
                    log.exception("poll cycle failed")
                time.sleep(self.settings.wechat_poll_seconds)
            log.warning("STOP file detected; listener stopped")
        finally:
            self.memory.worker.close()
            self.voice.close()
            self._log_pending_reply_jobs()
            close = getattr(self.ui, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.exception("failed to close Weixin listener")

    def _run_linux(self) -> None:
        """DB ingestion keeps its own cadence while model/UI work stays serial."""
        stopped = threading.Event()
        self.ui.connect()
        self._send_private_welcome_messages()
        chat_names = (self.ui.configured_chat_names() if callable(getattr(self.ui, "configured_chat_names", None))
                      else list(self.settings.wechat_group_names))
        scopes = {(self._account_id(), self.ui.db.resolve_chat(name).chat_id) for name in chat_names}
        if not scopes:
            raise RuntimeError("请设置 WECHAT_GROUP_NAMES 后再启动")
        # Scheduled/idle proactive participation is group-only.  Direct
        # conversations are passive: they are answered through the private
        # allowlist and must never open a proactive window or a group-style
        # idle candidate.
        proactive_scopes = self._linux_proactive_scopes(scopes)
        self.memory.worker.start()
        self._recover_reply_jobs()

        def capture():
            while not stopped.is_set() and not self.policy.is_stopped():
                try:
                    for event in self.ui.poll(account_id=self._account_id()):
                        self._enqueue_reply(event)
                    for account, group in list(proactive_scopes):
                        self.scheduler.heartbeat(account, group)
                except Exception:
                    self.stats.failed += 1
                    for account, group in list(proactive_scopes):
                        self.scheduler.heartbeat(account, group, online=False)
                    log.exception("Linux read cycle failed; pending work retained")
                    self.ui.close()
                stopped.wait(self.settings.wechat_poll_seconds)

        reader = threading.Thread(target=capture, name="wechat-linux-reader", daemon=True)
        reader.start()
        last_purge = 0.0
        try:
            while not self.policy.is_stopped():
                try:
                    replied = self._reply_next()
                    if not replied and not reply_jobs.any_active(self.storage) and self.settings.proactive_schedule_enabled:
                        for account, group in list(proactive_scopes):
                            candidate = self.scheduler.evaluate_proactive_window(account, group)
                            if candidate is not None:
                                if self._has_pending_direct_questions(candidate):
                                    self.scheduler.release(candidate.proactive_window_id, reason="direct_question_pending")
                                else:
                                    self.handle_event(candidate)
                    if time.monotonic() - last_purge >= 60:
                        self.storage.purge_expired_messages()
                        reply_jobs.prune(self.storage)
                        last_purge = time.monotonic()
                    self._maybe_export_daily_training()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self.stats.failed += 1
                    log.exception("Linux reply cycle failed")
                stopped.wait(0.2)
        finally:
            stopped.set()
            reader.join(timeout=5)
            for account, group in list(proactive_scopes):
                self.scheduler.heartbeat(account, group, online=False)
            self.memory.worker.close()
            self.voice.close()
            self._log_pending_reply_jobs()
            self.ui.close()

    def _linux_proactive_scopes(self, listener_scopes: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """Return the listener scopes that may run proactive scheduling.

        Only configured group chats participate in scheduled/idle proactive
        windows and listener-health monitoring.  One-to-one conversations are
        passive by design: their inbound messages are answered through the
        private allowlist, but the scheduler must never open a window for them
        or synthesise a group-style "群友们" idle candidate.
        """

        group_ids = {
            self.ui.db.resolve_chat(name).chat_id
            for name in self.settings.wechat_group_names
            if str(name).strip()
        }
        return {scope for scope in listener_scopes if scope[1] in group_ids}

    def _recover_reply_jobs(self) -> None:
        stale_claims = self.storage.recover_stale_processing(
            self.settings.wechat_processing_lease_seconds
        )
        if stale_claims:
            log.warning("上次运行中断的处理记录已关闭 count=%s", stale_claims)
        recovered = reply_jobs.recover_all(self.storage)
        if recovered["uncertain"]:
            log.warning("发送结果不确定的回复任务已转人工确认 count=%s", recovered["uncertain"])
        if recovered["retry"]:
            log.info("未发出的回复任务已恢复重试 count=%s", recovered["retry"])
        if recovered["expired"]:
            log.info("超过时效的回复任务已过期 count=%s", recovered["expired"])

    def _log_pending_reply_jobs(self) -> None:
        counts = reply_jobs.counts(self.storage)
        active = counts.get(reply_jobs.STATE_PENDING, 0) + counts.get(reply_jobs.STATE_RUNNING, 0)
        if active:
            log.warning("listener stopped with durable reply jobs pending=%s (will resume on start)", active)

    def _send_private_welcome_messages(self) -> None:
        """Notify newly allowlisted private contacts exactly once.

        Private conversations are passive by default. This optional notification
        also requires private replies, an explicit non-empty allowlist and the
        normal send gate.
        """

        if (
            not getattr(self.settings, "wechat_private_proactive_enabled", False)
            or not getattr(self.settings, "wechat_private_reply_enabled", False)
            or not getattr(self.settings, "wechat_private_chat_names", None)
            or not self.settings.wechat_allow_send
            or self.policy.is_stopped()
        ):
            return
        discover = getattr(self.ui, "private_chats", None)
        if not callable(discover):
            log.info("private welcome skipped: current WeChat adapter has no contact discovery")
            return
        configured = tuple(self.settings.wechat_private_chat_names)
        try:
            contacts = discover(configured)
        except TypeError:
            contacts = discover()
        except Exception:
            log.exception("private contact discovery failed; welcome messages retained for retry")
            return
        state_path = Path(self.settings.wechat_private_welcome_state_file).expanduser()
        sent = load_sent(state_path)
        changed = False
        account_id = self._account_id()
        for contact in contacts or ():
            chat_id = str(getattr(contact, "chat_id", "") or "").strip()
            chat_name = str(getattr(contact, "name", "") or chat_id).strip()
            if not chat_id or not chat_name:
                continue
            key = welcome_key(account_id, chat_id)
            if key in sent:
                continue
            action = OutboundAction(
                account_id=account_id,
                group_id=chat_id,
                group_name=chat_name,
                text=choose_welcome_message(
                    account_id,
                    chat_id,
                    max_emoji=self.settings.persona_max_emoji,
                ),
                action_type="dm",
                target_member_id=chat_id,
                request_id="private-welcome:" + key,
                idempotency_key="private-welcome:" + key,
                conversation_type="private",
            )
            allowed, reason = self.policy.authorize(action)
            if not allowed:
                log.info("private welcome blocked chat=%s reason=%s", chat_id, reason)
                continue
            try:
                result = self.ui.send(action)
            except Exception:
                log.exception("private welcome send failed chat=%s; will retry", chat_id)
                continue
            if not result or not getattr(result, "verified", False):
                log.warning("private welcome send was not verified chat=%s; will retry", chat_id)
                continue
            sent.add(key)
            changed = True
            log.info("private welcome sent chat=%s", chat_id)
        if changed:
            try:
                save_sent(state_path, sent)
            except OSError:
                # A successful WeChat send is still auditable; retrying after a
                # state-file failure is prevented by the native send ledger's
                # idempotency key on the next restart.
                log.exception("private welcome ledger write failed path=%s", state_path)

    def _has_pending_direct_questions(self, event: MessageEvent) -> bool:
        pending = reply_jobs.pending_events(self.storage, event.account_id, event.group_id)
        return any(not item.is_self and (
            self.policy.direct_trigger_reason(item) or
            (self.settings.voice_enabled and item.message_type == "voice" and self.policy.is_allowed_chat(item)
             and (not self.settings.wechat_allowed_members or self.policy._member_is_allowed(item)))
        ) for item in pending)

    def _enqueue_reply(self, event: MessageEvent) -> int | None:
        """Persist one inbound message together with its reply obligation.

        The reply job row is written in the same transaction as the raw
        message.  The Weixin cursor is only acknowledged afterwards, so a
        crash can lose the answer but never the obligation to give one.
        """

        event.ingest_sequence = self.storage.record_message(
            event, enqueue_reply=not event.historical
        )
        acknowledge = getattr(self.ui, "ack", None)
        if callable(acknowledge):
            acknowledge(event)
        if event.historical:
            return event.ingest_sequence
        depth = reply_jobs.pending_depth(self.storage, event.account_id, event.group_id)
        if depth > 1:
            log.info(
                "message queued member=%s depth=%s",
                event.member_name,
                depth,
            )
        return event.ingest_sequence

    def _reply_next(self) -> bool:
        """Answer the oldest due job; one reply at a time, in arrival order."""

        job = reply_jobs.claim_next(
            self.storage,
            lease_seconds=self.settings.wechat_processing_lease_seconds,
            owner=self._reply_owner,
        )
        if job is None:
            return False
        log.debug(
            "reply job claimed member=%s job=%s attempts=%s",
            job.event.member_name,
            job.job_id,
            job.attempts,
        )
        try:
            replied = bool(self.handle_event(job.event, reply_job=job))
        except Exception as exc:
            if job.intent_sent:
                # The UI may already have delivered it; never send twice.
                reply_jobs.finish(
                    self.storage,
                    job.job_id,
                    reply_jobs.STATE_UNCERTAIN,
                    {"replied": False, "error": type(exc).__name__},
                    error=f"{type(exc).__name__}: {str(exc)[:240]}",
                )
            else:
                reply_jobs.release(
                    self.storage,
                    job.job_id,
                    error=f"{type(exc).__name__}: {str(exc)[:240]}",
                )
            raise
        reply_jobs.finish_if_running(
            self.storage,
            job.job_id,
            reply_jobs.STATE_DONE,
            {"replied": replied},
        )
        return True

    def handle_event(self, event: MessageEvent, *, reply_job: ReplyJob | None = None) -> bool:
        self.stats.seen += 1
        log.info(
            "message received group=%s member=%s text=%s",
            event.group_name,
            event.member_name,
            (event.text or "")[:240],
        )
        self_event = self._is_self_event(event)
        if self_event:
            event.is_self = True
        event_id = event.ingest_sequence or self.storage.record_message(event)
        event.ingest_sequence = event_id
        enrich = getattr(self.ui, "enrich", None)
        if callable(enrich) and event.message_type in {"image", "sticker", "quote"}:
            enrich(event)
            self.storage.update_media_message(event, event_id)
        if (event.message_type == "voice" and not self_event and not event.historical
                and self.policy.is_allowed_chat(event)
                and (not self.settings.wechat_allowed_members or self.policy._member_is_allowed(event))):
            if event.media_metadata.get("voice_transcription", {}).get("status") != "ok":
                if self.settings.voice_enabled and callable(enrich):
                    try:
                        enrich(event)
                    except Exception as exc:
                        log.warning("voice resource unavailable error=%s", type(exc).__name__)
                self.voice.enrich(event)
                self.storage.update_media_message(event, event_id)
        semantic_ready = event.message_type != "voice" or event.media_metadata.get("voice_transcription", {}).get("status") == "ok"
        normalized_event = self.storage.normalize_event(event)
        analysis_text = normalized_event.text
        trigger_terms = [
            *self.settings.wechat_ai_aliases,
            *self.settings.wechat_keywords,
        ]
        if self.settings.wechat_self_name.strip():
            trigger_terms.append(self.settings.wechat_self_name)
        question_hash = self.storage.question_hash(event, trigger_terms)
        claim_event = replace(event, external_message_id=f"scheduled:{event.proactive_window_id}:{event.external_message_id}", source="proactive_scheduler") if event.proactive_window_id else event
        if event.proactive_window_id:
            from .memory_jobs import stable_id
            question_hash = stable_id(question_hash, event.proactive_window_id)
        claim = self.storage.claim_processing(
            claim_event,
            question_hash,
            lease_seconds=self.settings.wechat_processing_lease_seconds,
        )
        if not claim.claimed and not claim.is_new_event:
            if event.proactive_window_id:
                self.scheduler.release(event.proactive_window_id, reason=claim.reason)
            self.stats.skipped += 1
            if claim.reason == "question_already_answered":
                self.stats.duplicate_questions += 1
            log.debug("skip %s: %s", event.member_name, claim.reason)
            return False

        # The message row and its analysis job were committed in one
        # transaction by ``record_message``, so this only reports durability.
        if claim.is_new_event:
            mirror_raw = getattr(self.storage, "mirror_raw_message", None)
            if callable(mirror_raw):
                mirror_raw(event, event_id)

        # Only explicit, low-risk statements become durable fact candidates.
        # Ordinary chatter is accumulated separately as soft behavior signals;
        # it is never promoted to a confirmed preference or sensitive trait.
        if claim.is_new_event and not self_event and not event.proactive_window_id and semantic_ready:
            behavior_patch = (
                extract_member_profile_patch(
                    event.text,
                    str(event_id or event.external_message_id or "").strip() or None,
                    ai_aliases=self.settings.wechat_ai_aliases,
                )
                if self._is_known_member(event)
                else {}
            )
            self._ingest_scoped_memory(event, event_id, behavior_patch=behavior_patch)
            self._remember_requested_address(event, event_id)

        # Fit a group-level speaking style from ordinary member messages. The
        # profile is account/group scoped and aggregate-only; it never becomes
        # a member fact and is refreshed in batches to avoid per-message model
        # calls.  Self echoes and replayed UI rows are excluded here.
        if (
            self.settings.group_style_enabled
            and claim.is_new_event
            and not self_event
            and not event.proactive_window_id
            and semantic_ready
            and self._is_style_candidate(event)
        ):
            refreshed = self.storage.observe_group_style(
                event,
                min_messages=self.settings.group_style_min_messages,
                refresh_messages=self.settings.group_style_refresh_messages,
                sample_messages=self.settings.group_style_sample_messages,
                max_chars=self.settings.group_style_max_chars,
            )
            if refreshed:
                log.debug(
                    "group style profile ready account=%s group=%s messages=%s",
                    event.account_id,
                    event.group_id,
                    self.settings.group_style_min_messages,
                )
                self._schedule_synthesis(event.account_id, event.group_id)

        if not claim.claimed:
            self.stats.skipped += 1
            if claim.reason == "question_already_answered":
                self.stats.duplicate_questions += 1
            log.debug("skip %s: %s", event.member_name, claim.reason)
            return False

        decision = self.policy.decision(event)
        should_reply, reason = decision.reply, decision.reason
        if reply_job is not None:
            reply_jobs.describe(
                self.storage,
                reply_job.job_id,
                basis=decision.basis,
                origin=decision.origin,
                rules=decision.rules,
            )

        def finish(status: str, finish_reason: str, text: str | None = None) -> None:
            # Every terminal path records why, so the console can explain a
            # reply or a silence without reading logs.
            self.storage.finish_processing(
                claim.event_key,
                status,
                finish_reason,
                text,
                basis=decision.basis,
                rules=decision.rules,
                origin=decision.origin,
            )

        if not should_reply:
            if event.proactive_window_id:
                self.scheduler.release(event.proactive_window_id, reason=reason)
            finish("skipped", reason)
            self.stats.skipped += 1
            log.info("message skipped member=%s reason=%s", event.member_name, reason)
            return False
        if normalized_event.is_system:
            finish("skipped", "system_message")
            self.stats.skipped += 1
            log.info("message skipped member=%s reason=system_message", event.member_name)
            return False
        proactive = reason.startswith("proactive_")
        voice_clarification = reason in {"voice_unintelligible", "voice_unavailable"}
        followup_to = self._followup_for(event) if not proactive and not voice_clarification else None
        if followup_to is not None:
            analysis_text = self.storage.normalize_event(followup_to).text
        if proactive:
            log.info("proactive candidate group=%s member=%s reason=%s", event.group_id, event.member_name, reason)

        # Detect image intent before semantic memory, web search and profile
        # assembly. Image generation does not need those calls, so an image
        # request cannot be delayed by a slow memory path.
        is_image_request = not voice_clarification and self.image_client.should_generate(analysis_text or event.text)
        if proactive and is_image_request:
            if event.proactive_window_id:
                self.scheduler.release(event.proactive_window_id, reason="proactive_image_skipped")
            finish("skipped", "proactive_image_skipped")
            self.stats.skipped += 1
            log.info("message skipped member=%s reason=proactive_image_skipped", event.member_name)
            return False
        memories = []
        context_messages = []
        profile_context: dict[str, object] = {}
        profile_summary = ""
        group_style_profile = ""
        task_id = event.task_id
        memory_context = None
        evidence_bundle = None

        if not is_image_request and not voice_clarification and not self.settings.memory_orchestrator_enabled:
            memories = self.storage.search_facts(
                account_id=event.account_id,
                group_id=event.group_id,
                member_id=event.member_id,
                query=analysis_text,
                half_life_days=self.settings.half_life_for("chat"),
            )
            # Use the hybrid scoped path when available. The legacy lexical
            # call above remains as a safe fallback for custom Storage doubles.
            try:
                memories = self.storage.search_scoped_memories(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    member_id=event.member_id,
                    query=analysis_text,
                    half_life_days=self.settings.half_life_for("chat"),
                    task_id=task_id,
                )
            except AttributeError:
                pass
            context_query = analysis_text
            if event.reply_to and event.reply_to.text.strip():
                quote = normalize_chat_text(
                    event.reply_to.text,
                    sender_name=event.reply_to.sender_name,
                    ai_aliases=self.settings.wechat_ai_aliases,
                ).text
                context_query += " " + (quote or event.reply_to.text)
            context_messages = self.storage.recent_messages(
                account_id=event.account_id,
                group_id=event.group_id,
                limit=self.settings.wechat_context_messages,
                max_chars=self.settings.wechat_context_max_chars,
                exclude_message_hash=self.storage.hash_event(event),
                query=None,
                thread_id=event.thread_id,
                task_id=task_id,
                before_message_id=event.ingest_sequence,
                before_time=event.observed_at,
            )
            try:
                related = self.storage.related_messages(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    query=context_query,
                    limit=max(0, self.settings.wechat_context_messages // 2),
                    max_chars=self.settings.wechat_context_max_chars // 2,
                    exclude_message_hash=self.storage.hash_event(event),
                    thread_id=event.thread_id,
                    task_id=task_id,
                    before_message_id=event.ingest_sequence,
                    before_time=event.observed_at,
                )
                context_messages = self._merge_context_messages(context_messages, related)
            except AttributeError:
                pass
            profile_context = {
                "user": self.storage.user_profile(event.account_id, event.group_id, event.member_id),
                "ai": self.storage.ai_profile(event.account_id, event.group_id),
                "task": self.storage.task_profile(
                    event.account_id,
                    event.group_id,
                    task_id,
                    event.member_id if event.task_id else "",
                )
                if task_id
                else {},
            }
            profile_summary = build_profile_summary(
                profile_context.get("user"),
                profile_context.get("ai"),
                profile_context.get("task"),
                max_chars=self.settings.profile_prompt_max_chars,
            )
            group_style_profile = (
                self.storage.group_style_profile(event.account_id, event.group_id)
                if self.settings.group_style_enabled
                else ""
            )
        if not is_image_request and not voice_clarification and self.settings.memory_orchestrator_enabled:
            evidence_bundle = self.orchestrator.retrieve_evidence(event, message_id=event_id)
            memory_context = self.orchestrator.assemble_context(evidence_bundle)
            context_messages = evidence_bundle.context_messages
            memories = [MemoryFact(account_id=item.account_id, group_id=item.group_id, member_id=item.actor_member_id, memory_type=item.memory_type, content=item.content, confidence=item.confidence, status=item.lifecycle_status, source_message_ids=item.source_message_ids, metadata={"actor_name": item.actor_name, "source_role": item.source_role, "occurred_at": item.occurred_at, "source_uri": item.source_uri}) for item in evidence_bundle.evidence]
        elif not is_image_request and not voice_clarification and (self.settings.emotion_layer_enabled or self.settings.semantic_group_profile_enabled):
            from .semantic_profiles import SemanticProfiles
            semantic = SemanticProfiles(self.memory).snapshot(event.account_id, event.group_id) if self.settings.semantic_group_profile_enabled else {}
            memory_context = {
                "emotion": self.memory.emotion.response_snapshot(event.account_id, event.group_id, event.member_id, question_id=str(event_id), question=event.text, now=self.memory.clock()) if self.settings.emotion_layer_enabled else {},
                "related_profiles": {"semantic": semantic} if semantic else {},
                "profile_version": semantic.get("semantic_version", 0),
            }
        # 成员自己指定的称呼只作为提示语气；好感门槛在提示词组装时会再判一次。
        if not is_image_request and not voice_clarification and self.settings.emotion_layer_enabled:
            preferred_address = ""
            read_address = getattr(self.storage, "member_address", None)
            if callable(read_address):
                try:
                    preferred_address = read_address(event.account_id, event.group_id, event.member_id)
                except Exception as exc:
                    log.warning("member address read failed error=%s", type(exc).__name__)
            if preferred_address:
                if memory_context is None:
                    memory_context = {}
                memory_context["member_address"] = preferred_address
        conversation_type = getattr(event, "conversation_type", "") or str(event.media_metadata.get("conversation_type") or "group")
        source_context = {
            "channel": "weixin",
            "conversationType": conversation_type,
            "senderId": event.member_id,
            "senderName": event.member_name,
            "conversationTitle": event.group_name,
            "chatId": event.group_id,
            "sentAt": stamp(event.observed_at, timezone_name=self.settings.group_timezone),
        }
        reply_text: str | None = None
        web_sources = []
        image_reply = None
        reaction_image = None
        speech_clip = None
        image_only = False
        try:
            if voice_clarification:
                reply_text = ("这段我没听明白，能换个说法或者打字说一下嘛？" if reason == "voice_unintelligible"
                              else "这条语音暂时没读出来，能再发一次或者打字说一下嘛？")
            elif is_image_request:
                try:
                    image_reply = self.image_client.generate(
                        self.image_client.prompt(analysis_text)
                    )
                except Exception as exc:
                    # Do not kill the polling loop when a provider rejects a
                    # request. Send a short diagnostic reply through the same
                    # reliable UI path and keep the full detail in the ledger.
                    log.warning("image generation failed error=%s", type(exc).__name__)
                    reply_text = f"图片生成失败：{str(exc)[:240]}"
                else:
                    reply_text = image_reply.text
                    log.info(
                        "image route model=%s used_api=%s files=%s",
                        image_reply.model,
                        image_reply.used_api,
                        len(image_reply.local_paths),
                    )
            else:
                # Proactive interjections should stay cheap and local; do not
                # launch web research just because a casual cue matched.
                web_sources = [] if proactive else self._research_sources(analysis_text)
                if web_sources:
                    log.info("online research sources=%d for %s", len(web_sources), event.member_name)
                    reply = self._reply_with_context(
                        event,
                        memories,
                        context_messages,
                        source_context,
                        web_sources=web_sources,
                        group_style_profile=group_style_profile,
                        profile_summary=profile_summary,
                        proactive=proactive,
                        memory_context=memory_context,
                        followup_to=followup_to,
                    )
                else:
                    reply = self._reply_with_context(
                        event,
                        memories,
                        context_messages,
                        source_context,
                        group_style_profile=group_style_profile,
                        profile_summary=profile_summary,
                        proactive=proactive,
                        memory_context=memory_context,
                        followup_to=followup_to,
                    )
                reply_text = reply.text
                if proactive and str(reply_text or "").strip().strip("[] ") == "不回复":
                    if event.proactive_window_id:
                        self.scheduler.release(event.proactive_window_id, reason="generator_declined", close=event.media_metadata.get("proactive_mode") == "idle")
                    finish("skipped", "proactive_declined")
                    self.stats.skipped += 1
                    log.info("proactive declined group=%s member=%s", event.group_id, event.member_name)
                    return False
                plan = getattr(reply, "reaction_image", None)
                requested_voice_kind = voice_request_kind(analysis_text or event.text) if not proactive else ""
                voice_requested = bool(requested_voice_kind)
                if (getattr(reply, "voice_reply", False) and self._voice_reply_supported()
                        and self.speech.should_offer(event, requested=voice_requested,
                                                     request_kind=requested_voice_kind,
                                                     declined=voice_reply_declined(analysis_text or event.text))):
                    try:
                        speech_mode = "singing" if (requested_voice_kind == "singing" or getattr(reply, "singing_reply", False)) else "speech"
                        speech_clip = self.speech.prepare(reply_text or "", account_id=event.account_id, group_id=event.group_id, mode=speech_mode)
                    except Exception as exc:
                        log.warning("speech preparation unavailable error=%s; using text", getattr(exc, "code", type(exc).__name__))
                if speech_clip is None and plan is not None and self.reaction_images.should_offer(event):
                    own_names = {*self.settings.wechat_ai_aliases, self.settings.wechat_self_name, self.settings.persona_name}
                    names = [event.member_name, event.group_name, *(item.sender_name for item in context_messages)]
                    if event.reply_to:
                        names.append(event.reply_to.sender_name)
                    try:
                        reaction_image = self.reaction_images.prepare(
                            plan,
                            names=[name for name in names if name not in own_names],
                            scene_hint=analysis_text or event.text,
                        )
                    except Exception as exc:
                        # An optional image must not discard a good text reply.
                        # No text has been sent while generation was in progress.
                        log.warning("reaction image unavailable error=%s; using text", type(exc).__name__)
                    if reaction_image:
                        image_only = bool(getattr(reply, "image_only", False))
                        if image_only:
                            reply_text = ""
                        log.info("reaction image prepared id=%s reused=%s delivery=%s", reaction_image.image_id,
                                 reaction_image.reused, "image" if image_only else "text_image")
            if proactive and self._has_pending_direct_questions(event):
                if event.proactive_window_id:
                    self.scheduler.release(event.proactive_window_id, reason="direct_question_pending")
                finish("skipped", "direct_question_pending")
                self.stats.skipped += 1
                return False
            action = OutboundAction(
                account_id=event.account_id,
                group_id=event.group_id,
                group_name=event.group_name,
                text=reply_text or "",
                action_type="voice_clarification" if voice_clarification else "proactive_reply" if proactive else "dm" if conversation_type == "private" else "group_reply",
                target_member_id=event.member_id,
                media_paths=[reaction_image.path] if reaction_image else list(image_reply.local_paths) if image_reply else [],
                sticker_intent=getattr(reply, "sticker_intent", "") if not is_image_request and not voice_clarification and not self.settings.reaction_images_enabled else "",
                request_id=evidence_bundle.request_id if evidence_bundle else claim.event_key,
                idempotency_key=claim.event_key,
                proactive_window_id=event.proactive_window_id,
                proactive_lease_owner=str(event.media_metadata.get("proactive_lease_owner") or ""),
                send_text_with_media=bool(reaction_image and not image_only),
                voice_path=speech_clip.path if speech_clip else "",
                voice_duration_seconds=speech_clip.duration_seconds if speech_clip else 0,
                conversation_type=conversation_type,
            )
            allowed, auth_reason = self.policy.authorize(action)
            if not allowed:
                if event.proactive_window_id:
                    self.scheduler.release(event.proactive_window_id, reason=auth_reason)
                finish("skipped", auth_reason, reply_text)
                log.info("reply blocked (%s): %s", auth_reason, (reply_text or "")[:80])
                self.stats.skipped += 1
                return False
            # The intent is durable before the first pixel reaches WeChat: a
            # crash from here on may have delivered, so it must never retry.
            if reply_job is not None:
                reply_jobs.mark_intent(self.storage, reply_job.job_id, {
                    "idempotency_key": action.idempotency_key,
                    "basis": decision.basis,
                    "origin": decision.origin,
                    "action_type": action.action_type,
                    "text": (reply_text or "")[:2000],
                    "media_count": len(action.media_paths),
                    "voice": bool(action.voice_path),
                    "at": datetime.now(timezone.utc).isoformat(),
                })
                reply_job.intent_sent = True
            # A record becomes ``sent`` only after the UI adapter returns.  A
            # model response or a dry-run audit entry alone is not an answer.
            try:
                send_result = self.ui.send(action)
            finally:
                if image_reply and image_reply.local_paths:
                    cleanup = getattr(self.image_client, "cleanup_local", None)
                    if cleanup:
                        cleanup(image_reply.local_paths)
            self.policy.mark_sent()
            if event.proactive_window_id:
                self.scheduler.mark_sent(event, send_result, reply_text or "")
            self._remember_outbound(reply_text or "")
            finish("sent", reason, reply_text)
            if not proactive:
                self._platform_after_reply(event, reply_text or "", web_sources)
            try:
                fit = self.storage.record_reply_style(
                    event.account_id, event.group_id, reply_text or ""
                ) if reply_text and reply_text.strip() and not voice_clarification else None
                if fit:
                    log.debug("style fit group=%s fit=%s", event.group_id, fit)
            except Exception:
                log.exception("failed to record style fit group=%s", event.group_id)
            self.stats.replied += 1
            self.stats.proactive_replied += int(proactive)
            self.stats.reaction_images += int(reaction_image is not None)
            self.stats.voice_replies += int(speech_clip is not None)
            log.info(
                "reply sent and verified member=%s text=%s",
                event.member_name,
                (reply_text or "")[:240],
            )
            return True
        except Exception as exc:
            if event.proactive_window_id:
                self.scheduler.release(event.proactive_window_id, reason=type(exc).__name__)
            finish("failed", f"{type(exc).__name__}: {str(exc)[:240]}", reply_text)
            self.stats.failed += 1
            raise

    def _research_sources(self, analysis_text: str):
        """Run web research, handing an explicit group intent to the client.

        The platform trigger used to require the literal words 小红书/抖音 in
        the question.  ``PLATFORM_INTENT_ENABLED`` now also lets an intent such
        as "我想去看海，有没有攻略" browse the configured platforms.  Older
        injected test doubles only accept the question argument, so the
        keyword call falls back to the original signature on ``TypeError``.
        """

        intent = None
        if self.settings.platform_intent_enabled:
            try:
                from .platform_automation import detect_intent

                available: list[str] = []
                service = getattr(self.platform_automation, "service", None)
                adapters = getattr(service, "adapters", None)
                if isinstance(adapters, dict) and adapters:
                    available = [str(name) for name in adapters]
                intent = detect_intent(analysis_text, available_platforms=available or ("douyin", "xiaohongshu"))
            except Exception:
                log.debug("platform intent detection unavailable", exc_info=True)
                intent = None
        research = self.web_research.research
        if intent and intent.get("matched"):
            try:
                return research(analysis_text, intent=intent)
            except TypeError:
                pass
        return research(analysis_text)

    def _platform_after_reply(self, event: MessageEvent, reply_text: str, web_sources) -> None:
        """Create the guarded platform comment plan after the group reply was sent.

        A platform problem must never affect a delivered WeChat reply, so every
        failure is logged and swallowed here.
        """

        automation = self.platform_automation
        if automation is None or not web_sources:
            return
        try:
            result = automation.after_reply(event, reply_text, web_sources)
        except Exception:
            log.exception("platform after_reply wiring failed group=%s", event.group_id)
            return
        if isinstance(result, dict) and result.get("action_key"):
            log.info(
                "platform comment plan action=%s platform=%s dry_run=%s reason=%s",
                result.get("action_key"),
                result.get("platform"),
                result.get("dry_run"),
                result.get("reason"),
            )

    def _reply_with_context(
        self,
        event: MessageEvent,
        memories: list[MemoryFact],
        context_messages,
        source_context: dict[str, object],
        *,
        web_sources=None,
        group_style_profile: str = "",
        profile_context: dict[str, object] | None = None,
        profile_summary: str = "",
        proactive: bool = False,
        memory_context: dict | None = None,
        followup_to: MessageEvent | None = None,
    ):
        """Call the richer prompt contract while keeping small test doubles compatible."""

        normalized = self.storage.normalize_event(event)
        question = normalized.text
        if followup_to is None and not proactive:
            followup_to = self._followup_for(event)
        reply_to = event.reply_to
        if reply_to is not None:
            quote = normalize_chat_text(
                reply_to.text,
                sender_name=reply_to.sender_name,
                ai_aliases=self.settings.wechat_ai_aliases,
            ).text
            if quote:
                reply_to = replace(reply_to, text=quote)
        image_paths = self._images_for_prompt(event, question, reply_to, proactive=proactive, followup_to=followup_to)
        image_sampled = self.reaction_images.should_offer(event)
        native_voice_supported = self._voice_reply_supported()
        media_question = followup_to.text if followup_to else question
        request_kind = voice_request_kind(media_question) if not proactive else ""
        allow_voice_reply = bool(native_voice_supported and self.speech.should_offer(
            event, requested=bool(request_kind), request_kind=request_kind,
            declined=voice_reply_declined(media_question)))
        # One media type per reply. Voice wins a simultaneous draw; otherwise
        # the image probability is sampled for the remaining replies. The LLM
        # writes the contents instead of silently vetoing every media draw.
        allow_reaction_image = bool(image_sampled and not allow_voice_reply)
        try:
            catalog = self.reaction_images.catalog() if allow_reaction_image else []
        except OSError:
            catalog, allow_reaction_image = [], False
        reply_delivery = ("singing" if allow_voice_reply and request_kind == "singing" else
                          "voice" if allow_voice_reply else "image" if allow_reaction_image else "text")
        log.info("reply media offered group=%s image=%s voice=%s native_voice=%s requested=%s delivery=%s",
                 event.group_id, allow_reaction_image, allow_voice_reply, native_voice_supported,
                 request_kind or "none", reply_delivery)
        if self.settings.platform_memory_enabled and self.platform_memory is not None:
            try:
                platform_facts = self.platform_memory.memories_as_facts(
                    event.account_id,
                    event.group_id,
                    event.member_id,
                    limit=5,
                )
            except Exception:
                log.debug("platform memory facts unavailable", exc_info=True)
                platform_facts = []
            if platform_facts:
                memories = [*(memories or []), *platform_facts]
                log.debug(
                    "platform memory facts attached group=%s count=%d",
                    event.group_id,
                    len(platform_facts),
                )
        try:
            result = self.llm.reply(
                question,
                event.member_name,
                event.group_name,
                memories,
                web_sources=web_sources,
                context_messages=context_messages,
                source_context=source_context,
                reply_to=reply_to,
                followup_to=followup_to,
                group_style_profile=group_style_profile,
                profile_summary=profile_summary,
                proactive=proactive,
                image_paths=image_paths,
                context_images=False,
                memory_context=memory_context,
                allow_reaction_image=allow_reaction_image,
                reaction_image_catalog=catalog,
                proactive_idle=bool(proactive and event.source == "proactive_scheduler" and event.media_metadata.get("proactive_mode") == "idle"),
                allow_voice_reply=allow_voice_reply,
                reply_delivery=reply_delivery,
            )
            delivery = ("voice" if getattr(result, "voice_reply", False) else
                        "image" if getattr(result, "image_only", False) else
                        "text_image" if getattr(result, "reaction_image", None) else "text")
            log.info("reply media selected group=%s delivery=%s", event.group_id, delivery)
            return result
        except TypeError as exc:
            # Existing local integrations may provide a four-argument fake LLM.
            # Do not make those adapters change just because context is now
            # available on the production client.
            message = str(exc).casefold()
            if "unexpected keyword" not in message and "positional argument" not in message:
                raise
            # Retry progressively for small local test doubles and older
            # integrations that only implement the original four arguments.
            if web_sources:
                try:
                    return self.llm.reply(
                        question,
                        event.member_name,
                        event.group_name,
                        memories,
                        web_sources=web_sources,
                    )
                except TypeError as second_exc:
                    if "unexpected keyword" not in str(second_exc).casefold():
                        raise
            return self.llm.reply(question, event.member_name, event.group_name, memories)

    def _voice_reply_supported(self) -> bool:
        return bool(getattr(self.ui, "supports_native_voice", False))

    def _followup_for(self, event: MessageEvent) -> MessageEvent | None:
        if not can_follow_up(event, self.storage.normalize_event(event).text):
            return None
        lookup = getattr(self.storage, "preceding_member_message", None)
        prior = lookup(event) if callable(lookup) else None
        if prior is not None and (prior.message_type == "image" or image_quote(prior.reply_to)):
            enrich = getattr(self.ui, "enrich", None)
            if callable(enrich):
                enrich(prior)
        return prior

    def _recent_image_window(self, event: MessageEvent):
        """Unfiltered recent rows, used only to age pictures for the prompt.

        ``recent_messages`` normally filters rows by the current question; that
        would make an old picture look "recent" whenever the new text resembles
        its row, which is how one bottle stayed in context for dozens of turns.
        Images are aged against the real conversation instead.
        """

        try:
            limit = min(
                50,
                max(
                    6,
                    int(self.settings.wechat_context_messages)
                    + int(self.settings.wechat_image_context_turns)
                    + 2,
                    int(self.settings.wechat_image_reference_window) + 1,
                ),
            )
            return self.storage.recent_messages(
                account_id=event.account_id,
                group_id=event.group_id,
                limit=limit,
                max_chars=self.settings.wechat_context_max_chars,
                exclude_message_hash=self.storage.hash_event(event),
                query=None,
                before_message_id=event.ingest_sequence,
                before_time=event.observed_at,
            )
        except Exception:
            log.debug("image context window unavailable", exc_info=True)
            return []

    def _images_for_prompt(
        self,
        event: MessageEvent,
        question: str,
        reply_to,
        *,
        proactive: bool = False,
        followup_to: MessageEvent | None = None,
    ) -> list[str]:
        """Pick the pictures that may accompany this one reply.

        A picture is visible for a few messages after it arrives, comes back
        when somebody explicitly talks about it, and never comes back merely
        because the semantic context search matched its row.
        """

        current = list(event.image_paths or [])
        # An explicit image reply has a stable channel identity. Never replace
        # a missing quoted image with the newest unrelated picture in the room.
        if image_quote(reply_to):
            return list(reply_to.image_paths or [])
        if followup_to is not None:
            if image_quote(followup_to.reply_to):
                return list(followup_to.reply_to.image_paths or [])
            if followup_to.message_type == "image":
                return list(followup_to.image_paths or [])
        if proactive:
            # Proactive interjections must not resurrect an old picture.
            return current
        selected, next_state = select_image_paths(
            current,
            question,
            quoted_text=(reply_to.text if reply_to is not None else ""),
            recent_messages=self._recent_image_window(event),
            turns=int(self.settings.wechat_image_context_turns),
            reference_window=int(self.settings.wechat_image_reference_window),
            max_count=int(self.settings.wechat_image_max_count),
            refreshed=self._image_refresh.get(event.group_id, {}),
        )
        if next_state:
            self._image_refresh[event.group_id] = next_state
        else:
            self._image_refresh.pop(event.group_id, None)
        return selected

    @staticmethod
    def _merge_context_messages(primary, related):
        seen = {(item.sender_name, item.text, item.observed_at) for item in primary}
        merged = list(primary)
        for item in related:
            key = (item.sender_name, item.text, item.observed_at)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _remember_requested_address(self, event: MessageEvent, event_id: int | None) -> None:
        """Store a close member's self-chosen address (「以后叫我XX」).

        Only members already at ``INTIMATE_AFFINITY_THRESHOLD`` are honored.  The
        request is sanitized to a short label, never becomes an instruction or a
        permission, and a later request simply replaces the previous one.
        """
        if not self.settings.emotion_layer_enabled or self._is_self_event(event):
            return
        if not self._is_known_member(event):
            return
        address = parse_address_request(event.text or "")
        if not address:
            return
        try:
            snapshot = self.memory.emotion.snapshot(
                event.account_id, event.group_id, event.member_id, now=self.memory.clock()
            )
            affinity = float(snapshot.get("affinity", 50.0))
        except Exception as exc:
            log.warning("address request affinity unavailable member=%s error=%s", event.member_name, type(exc).__name__)
            return
        if affinity < INTIMATE_AFFINITY_THRESHOLD:
            log.info(
                "address request ignored member=%s affinity=%.1f address=%s",
                event.member_name, affinity, address,
            )
            return
        remember = getattr(self.storage, "remember_member_address", None)
        if not callable(remember):
            return
        try:
            remember(
                account_id=event.account_id,
                group_id=event.group_id,
                member_id=event.member_id,
                address=address,
                origin_message_id=str(event_id or event.external_message_id or ""),
                source_text=(event.text or "")[:240],
                now=self.memory.clock(),
            )
        except Exception as exc:
            log.warning("address request save failed member=%s error=%s", event.member_name, type(exc).__name__)
            return
        log.info(
            "address request saved member=%s address=%s affinity=%.1f",
            event.member_name, address, affinity,
        )

    def _ingest_scoped_memory(
        self,
        event: MessageEvent,
        event_id: int | None,
        *,
        behavior_patch: dict[str, object] | None = None,
    ) -> None:
        source_id = str(event_id or event.external_message_id or "").strip() or None
        # A sender that the UI could not identify is safe for the shared group
        # corpus and group-level AI style, but not for a member profile.
        known_member = self._is_known_member(event)
        # Long-term facts now come from the analysis thread's judgement
        # (``MemoryRuntime.process_analysis`` -> ``Storage.apply_judged_facts``),
        # not from a keyword pass in the reply path: the reply must not be
        # delayed by, or compete with, the single model call that reads a
        # message.  Only the behaviour counters stay synchronous.
        profile_patch: dict[str, list[str]] = {}
        if known_member and behavior_patch:
            profile_patch.update(behavior_patch)
        if profile_patch and known_member:
            self.storage.upsert_user_profile(
                account_id=event.account_id,
                group_id=event.group_id,
                member_id=event.member_id,
                member_name=event.member_name,
                patch=profile_patch,
                source_message_ids=[source_id] if source_id else [],
            )
        if self._is_style_candidate(event):
            self.storage.observe_ai_profile(event, source_id)
        normalized_text = self.storage.normalize_event(event).text
        task_patch = extract_task_patch(normalized_text)
        if task_patch or has_task_signal(normalized_text):
            existing_task = self.storage.latest_task_id(event.account_id, event.group_id)
            task_id = infer_task_id(event, existing_task or f"task-current-{event.group_id}")
            if task_id:
                # An explicitly supplied task id can be member-owned.  A
                # synthesized current-group task remains shared by the group.
                task_member_id = event.member_id if event.task_id else ""
                self.storage.upsert_task_profile(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    task_id=task_id,
                    patch=task_patch or {"progress": normalized_text[:220]},
                    source_message_ids=[source_id] if source_id else [],
                    member_id=task_member_id,
                )

    def _is_style_candidate(self, event: MessageEvent) -> bool:
        """Reject empty/system placeholder rows before fitting group style."""

        normalized = normalize_chat_text(
            event.text,
            sender_id=event.member_id,
            sender_name=event.member_name,
            ai_aliases=self.settings.wechat_ai_aliases,
        )
        if normalized.is_system:
            return False
        text = " ".join(normalized.text.split()).strip()
        if not text:
            return False
        if text in {"查看更多消息", "以下为新消息"}:
            return False
        if text.startswith("[") and text.endswith("]"):
            return False
        return True

    def rebuild_memory(self, *, window_messages: int | None = None) -> dict[str, object]:
        """Local backfill plus optional semantic synthesis of group profiles."""

        report = self.storage.rebuild_memory(window_messages=window_messages)
        report["synthesis"] = self.synthesize_group_profiles(window_messages=window_messages)
        return report

    def synthesize_group_profiles(self, *, window_messages: int | None = None) -> list[dict[str, object]]:
        if not self.settings.profile_synthesis_enabled or not self.settings.llm_api_key:
            return []
        limit = max(
            20,
            min(
                int(window_messages or self.settings.profile_synthesis_messages),
                int(self.settings.profile_synthesis_messages),
            ),
        )
        results: list[dict[str, object]] = []
        for account_id, group_id, group_name in self.storage.group_scopes():
            items = self.storage.profile_window_messages(account_id, group_id, limit=limit)
            if len(items) < 10:
                results.append(
                    {
                        "group_id": group_id,
                        "group_name": group_name,
                        "messages": len(items),
                        "saved": False,
                        "reason": "too_few_messages",
                    }
                )
                continue
            data = synthesize_group_profile(
                self.llm,
                items,
                max_input_chars=self.settings.profile_synthesis_max_input_chars,
            )
            saved = self.storage.save_synthesized_group_profile(
                account_id,
                group_id,
                data,
                group_name=group_name,
            )
            results.append(
                {
                    "group_id": group_id,
                    "group_name": group_name,
                    "messages": len(items),
                    "saved": saved,
                    "fields": sorted(data),
                }
            )
        return results

    def _schedule_synthesis(self, account_id: str, group_id: str) -> None:
        """Refresh the semantic profile in the background after a batch refresh."""

        if not self.settings.profile_synthesis_enabled or not self.settings.llm_api_key:
            return
        key = (account_id, group_id)
        if key in self._synthesis_inflight:
            return
        self._synthesis_inflight.add(key)
        threading.Thread(
            target=self._synthesis_worker,
            args=(account_id, group_id),
            name="wechat-profile-synthesis",
            daemon=True,
        ).start()

    def _synthesis_worker(self, account_id: str, group_id: str) -> None:
        try:
            items = self.storage.profile_window_messages(
                account_id,
                group_id,
                limit=self.settings.profile_synthesis_messages,
            )
            if len(items) < 10:
                return
            data = synthesize_group_profile(
                self.llm,
                items,
                max_input_chars=self.settings.profile_synthesis_max_input_chars,
            )
            if data:
                self.storage.save_synthesized_group_profile(account_id, group_id, data)
                log.info(
                    "synthesized group profile account=%s group=%s fields=%s",
                    account_id,
                    group_id,
                    ",".join(sorted(data)),
                )
        except Exception:
            log.exception("profile synthesis failed account=%s group=%s", account_id, group_id)
        finally:
            self._synthesis_inflight.discard((account_id, group_id))

    def _is_self_event(self, event: MessageEvent) -> bool:
        if event.is_self:
            return True
        sender = event.member_name.strip().casefold()
        # The bot's own UI echo can carry its display name instead of
        # "unknown".  Treat the configured identity and aliases as self,
        # otherwise the group AI profile learns from its own replies.
        bot_names = {
            str(self.settings.wechat_self_name or "").strip().casefold(),
            *[str(alias or "").strip().casefold() for alias in self.settings.wechat_ai_aliases],
        }
        bot_names.discard("")
        if sender and sender in bot_names:
            return True
        if sender not in {"unknown", "我", "自己", "me", "you", "you (me)"}:
            return False
        self._expire_outbound()
        if self._normalize_outbound(event.text) in self._recent_outbound:
            return True
        # The UIA sender label can be ``unknown`` after a restart.  The
        # durable reply ledger still knows which text this agent successfully
        # sent to this account/group, so use it as the final self-echo guard.
        try:
            return self.storage.matches_recent_sent_reply(
                account_id=event.account_id,
                group_id=event.group_id,
                text=event.text,
            )
        except Exception:
            # A locked or unavailable ledger must not take down the polling
            # loop; the UI adapter's database identity guard remains active.
            return False

    @staticmethod
    def _is_known_member(event: MessageEvent) -> bool:
        value = str(event.member_id or "").strip().casefold()
        return value not in {"", "unknown", "我", "自己", "me", "you", "you (me)"}

    def _remember_outbound(self, text: str) -> None:
        self._expire_outbound()
        if text.strip():
            self._recent_outbound[self._normalize_outbound(text)] = time.monotonic()

    def _expire_outbound(self) -> None:
        cutoff = time.monotonic() - 300
        self._recent_outbound = {
            text: seen_at for text, seen_at in self._recent_outbound.items() if seen_at >= cutoff
        }

    @staticmethod
    def _normalize_outbound(text: str) -> str:
        # UIA and the database can disagree on full-width punctuation and
        # special-width spaces.  Keep the outbound echo guard on the same
        # normalized representation so an ``unknown`` sender cannot re-enter
        # the model after a successful send.
        value = unicodedata.normalize("NFKC", text or "")
        value = "".join(char for char in value if unicodedata.category(char) not in {"Cf"})
        return " ".join(value.casefold().split())

    def _extract_interest(self, event: MessageEvent, event_id: int | None) -> None:
        patterns = (
            r"我(?:比较)?喜欢([^，。！？!?]{1,40})",
            r"我(?:最近)?在关注([^，。！？!?]{1,40})",
            r"我爱([^，。！？!?]{1,40})",
        )
        for pattern in patterns:
            match = re.search(pattern, event.text)
            if not match:
                continue
            content = match.group(1).strip()
            self.storage.add_fact(
                MemoryFact(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    member_id=event.member_id,
                    memory_type="interest",
                    content=content,
                    confidence=0.75,
                    status="inferred",
                    source_event_id=str(event_id) if event_id else event.external_message_id,
                    observed_at=event.observed_at,
                    metadata={"extractor": "pattern_v1", "member_name": event.member_name},
                )
            )

    def _account_id(self) -> str:
        return self.settings.wechat_account_id or getattr(self.ui, "account_id", "") or "wechat-test-account"
