from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import json
import logging
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select

from .emotion import EmotionEngine
from .event_memory import analyze, analyze_events
from .judge import Judge, Judgement
from .memory_jobs import BACKOFF, MemoryWorker, aware, encode, event_from_row, scope_transaction, stable_id
from .memory_schema import AnalysisJobRow, DiscussionSegmentRow, EmotionCheckpointRow, EmotionEventRow, EpisodeRow, LifecycleRow, ListenerHealthRow, RelationshipEffectRow, RelationshipStateRow, RetrievalTraceRow
from .models import MessageEvent, utc_now
from .storage import AIProfileRow, FactRow, MessageRow

log = logging.getLogger(__name__)


class MemoryConflict(ValueError):
    pass


class MemoryRuntime:
    def __init__(self, storage, *, llm=None, clock=utc_now):
        self.storage, self.settings, self.llm, self.clock = storage, storage.settings, llm, clock
        self.emotion = EmotionEngine(self.settings, storage)
        # One injectable judgement port: tests replace it with a deterministic
        # fixture, production uses the model.  Nothing else reads the sentence.
        self.judge = Judge(llm, self.settings)
        self._judgements: dict[tuple[str, str, str], Judgement] = {}
        self._narrative_engine = None
        self._digest_engine = None
        self._daily_done: dict[tuple[str, str], str] = {}
        self.worker = MemoryWorker(self)
        self._analysis_owner = uuid.uuid4().hex
        self._maintenance_at = 0.0
        self._profile_inflight: set[tuple[str, str]] = set()

    def process_analysis(self, *, account_id: str = "", group_id: str = "", watermark: int | None = None, allow_model: bool = True) -> bool:
        now = self.clock()
        with scope_transaction(self.storage, "analysis", "analysis") as session:
            statement = select(AnalysisJobRow).where(AnalysisJobRow.status.in_(["pending", "running"]), AnalysisJobRow.next_retry <= now, or_(AnalysisJobRow.lease_until.is_(None), AnalysisJobRow.lease_until <= now))
            if account_id:
                statement = statement.where(AnalysisJobRow.account_id == account_id, AnalysisJobRow.group_id == group_id)
            if watermark is not None:
                statement = statement.where(AnalysisJobRow.message_id <= watermark)
            rows = list(session.scalars(statement.order_by(AnalysisJobRow.message_id).limit(32)))
            job = None
            for candidate in rows:
                earlier = session.scalar(select(AnalysisJobRow.id).where(AnalysisJobRow.account_id == candidate.account_id, AnalysisJobRow.group_id == candidate.group_id, AnalysisJobRow.message_id < candidate.message_id, AnalysisJobRow.status.in_(["pending", "running"])) .limit(1))
                if earlier is None:
                    job = candidate
                    break
            if job is None:
                return False
            job.status, job.lease_owner, job.lease_until = "running", self._analysis_owner, now + timedelta(seconds=20)
            identifier, message_id, effect_enabled = job.id, job.message_id, job.effect_enabled_at_ingest
            source = session.get(MessageRow, message_id)
            event = event_from_row(source) if source and source.lifecycle_status == "active" else None
        try:
            context = []
            if event:
                context = self.continuous_window(event.account_id, event.group_id, message_id, event.observed_at, limit=6)
                enrich = getattr(self, "media_enricher", None)
                if callable(enrich) and event.message_type in {"image", "sticker", "quote"}:
                    enrich(event)
                    self.storage.update_media_message(event, message_id)
            candidates = []
            judgement = None
            deferred_until = None
            unjudgeable = False
            if event and self.settings.memory_event_extraction_enabled:
                outcome = self._take_judgement(event, context, now, allow_model=allow_model)
                if isinstance(outcome, Judgement):
                    judgement = outcome
                    candidates = analyze_events(event, judgement, self.settings, context=context)
                elif outcome == "budget":
                    # Over the day's budget: keep the job pending until the
                    # next local midnight instead of dropping the message.
                    deferred_until = self._next_local_midnight(now)
                else:
                    # No model is available for this job at all; marking it
                    # disabled keeps the reply path from waiting forever.
                    unjudgeable = True
            with scope_transaction(self.storage, event.account_id if event else "analysis", event.group_id if event else "analysis") as session:
                job = session.get(AnalysisJobRow, identifier)
                if job.lease_owner != self._analysis_owner:
                    return True
                inserted: list[EpisodeRow] = []
                if event:
                    for candidate in candidates:
                        if candidate.evidence_excerpt and candidate.evidence_excerpt not in event.text:
                            continue
                        event_id = "event-" + stable_id(event.account_id, event.group_id, event.external_message_id or source.message_hash, candidate.event_type, candidate.target_key)
                        if session.get(EpisodeRow, event_id):
                            continue
                        source_id = str(event.external_message_id or message_id)
                        source_ids = [source_id]
                        correction = candidate.metadata.get("correction")
                        if correction and correction.get("old_source_id"):
                            source_ids.append(correction["old_source_id"])
                        metadata = {**candidate.metadata, "relayed": candidate.relayed, "reported_actor_name": candidate.reported_actor_name, "reason": candidate.reason, "effect_enabled_at_ingest": effect_enabled, "local_source_id": message_id, "source_watermark": message_id, "profile_note": judgement.profile_note if judgement else "", "judge_model": judgement.model if judgement else ""}
                        episode = EpisodeRow(event_id=event_id, account_id=event.account_id, group_id=event.group_id, actor_id=event.member_id, actor_name=event.member_name, target_type=candidate.target_type, target_id=event.account_id if candidate.target_type == "ai" else "", target_key=candidate.target_key, event_type=candidate.event_type, content=event.text[:1200], evidence_excerpt=(candidate.evidence_excerpt or event.text)[:300], primary_source_id=source_id, source_ids_json=encode(list(dict.fromkeys(source_ids))), confidence=candidate.confidence, occurred_at=event.source_created_at or event.observed_at, created_at=now, source_role=candidate.source_role, time_quality=event.time_quality, status="active" if candidate.confidence >= self.settings.emotion_event_min_confidence and not candidate.relayed else "candidate", metadata_json=encode(metadata), version=1, rule_version=self.emotion.rule_version)
                        session.add(episode)
                        session.flush()
                        inserted.append(episode)
                    if inserted:
                        occurred = event.source_created_at or event.observed_at
                        self.emotion.invalidate_checkpoints(session, event.account_id, event.group_id, occurred)
                        for episode in inserted:
                            # One episode, one O(1) application; a full replay
                            # here was O(n^2) over the day's events.
                            self.emotion.apply(session, event.account_id, event.group_id, episode, now=now)
                if deferred_until is not None:
                    job.status, job.next_retry, job.lease_until, job.lease_owner = "pending", deferred_until, None, ""
                    job.last_error = "judge_budget_exhausted"
                    return True
                if unjudgeable:
                    job.status, job.lease_until, job.lease_owner = "disabled", None, ""
                    job.last_error = "judge_unavailable"
                    return True
                job.status, job.processed_at, job.lease_until, job.lease_owner = "done", now, None, ""
                job.last_error = ""
            if judgement is not None and event is not None and judgement.facts:
                self.storage.apply_judged_facts(event, judgement.facts)
            return True
        except Exception as exc:
            with scope_transaction(self.storage, "analysis", "analysis") as session:
                job = session.get(AnalysisJobRow, identifier)
                if job.lease_owner == self._analysis_owner:
                    job.attempts += 1
                    job.status = "failed" if job.attempts >= 8 else "pending"
                    job.next_retry = now + timedelta(seconds=BACKOFF[min(job.attempts - 1, 6)])
                    job.lease_until, job.lease_owner, job.last_error = None, "", type(exc).__name__
            return True

    def _judge_local_day(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.settings.group_timezone)).date().isoformat()

    def _judge_state(self, event: MessageEvent, now: datetime, *, allow_model: bool) -> str:
        """``ok`` to judge, ``budget`` to defer, ``no_model`` to disable the job."""

        if not allow_model:
            return "no_model"
        if not isinstance(self.judge, Judge):
            # A substituted judgement port is the caller's own cost; the budget
            # only exists to cap model spend.
            return "ok"
        if not (self.llm and self.settings.llm_api_key):
            return "no_model"
        allowed = self.storage.consume_judge_budget(
            event.account_id,
            event.group_id,
            self._judge_local_day(now),
            self.settings.memory_judge_daily_budget,
        )
        return "ok" if allowed else "budget"

    @staticmethod
    def _judge_key(event: MessageEvent) -> tuple[str, str, str]:
        return (event.account_id, event.group_id, str(event.external_message_id or event.ingest_sequence or ""))

    def _take_judgement(self, event: MessageEvent, context, now: datetime, *, allow_model: bool):
        """One judgement for this message: cache, batch, single call, or a reason."""

        if not allow_model:
            return "no_model"
        if not isinstance(self.judge, Judge):
            # An injected port (tests) costs nothing, so the budget is bypassed.
            return self.judge.analyze(event, context)
        if not (self.llm and self.settings.llm_api_key):
            return "no_model"
        key = self._judge_key(event)
        cached = self._judgements.pop(key, None)
        if cached is not None:
            return cached
        if not self.storage.consume_judge_budget(
            event.account_id, event.group_id, self._judge_local_day(now), self.settings.memory_judge_daily_budget
        ):
            return "budget"
        batch = self._batch_ready(event, context)
        if len(batch) > 1:
            results = self.judge.analyze_many(batch)
            for item_key, item in results.items():
                self._judgements[(event.account_id, event.group_id, item_key)] = item
            cached = self._judgements.pop(key, None)
            if cached is not None:
                return cached
            log.warning("judge batch omitted message_id=%s", key[2])
        # No batch, or the batch skipped this message: judge it on its own.
        return self.judge.analyze(event, context)

    def _batch_ready(self, event: MessageEvent, context) -> list[tuple[MessageEvent, list, str]]:
        """The current message plus nearby plain-text jobs ready right now."""

        limit = max(1, min(20, self.settings.memory_judge_batch_size))
        if limit == 1:
            return [(event, context, self._judge_key(event)[2])]
        now = self.clock()
        with self.storage.Session() as session:
            rows = list(session.scalars(
                select(AnalysisJobRow).where(
                    AnalysisJobRow.account_id == event.account_id,
                    AnalysisJobRow.group_id == event.group_id,
                    AnalysisJobRow.status == "pending",
                    AnalysisJobRow.next_retry <= now,
                    or_(AnalysisJobRow.lease_until.is_(None), AnalysisJobRow.lease_until <= now),
                ).order_by(AnalysisJobRow.message_id).limit(limit)
            ))
            batch = [(event, context, self._judge_key(event)[2])]
            for row in rows:
                if row.message_id == event.ingest_sequence:
                    continue
                source = session.get(MessageRow, row.message_id)
                # Media rows are enriched per message right before their own
                # judgement; batching them would judge the text before the
                # caption exists.
                if source is None or source.lifecycle_status != "active":
                    continue
                if (source.message_type or "text") != "text":
                    continue
                sibling = event_from_row(source)
                if not sibling.text:
                    continue
                sibling_key = self._judge_key(sibling)
                if sibling_key in self._judgements:
                    continue
                batch.append((
                    sibling,
                    self.continuous_window(sibling.account_id, sibling.group_id, row.message_id, sibling.observed_at, limit=6),
                    sibling_key[2],
                ))
                if len(batch) >= limit:
                    break
        return batch

    def _next_local_midnight(self, now: datetime) -> datetime:
        local = now.astimezone(ZoneInfo(self.settings.group_timezone))
        tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow.astimezone(ZoneInfo("UTC"))

    @staticmethod
    def episode_payload(row) -> dict:        return {"memory_id": row.event_id, "object_id": row.event_id, "object_version": row.version, "memory_type": "event", "scope": {"account_id": row.account_id, "group_id": row.group_id}, "actor_member_id": row.actor_id, "actor_name": row.actor_name, "target": row.target_key, "source_role": row.source_role, "event_type": row.event_type, "content": row.content, "evidence_excerpt": row.evidence_excerpt, "source_message_ids": json.loads(row.source_ids_json), "occurred_at": aware(row.occurred_at).isoformat(), "time_quality": row.time_quality, "confidence": row.confidence, "lifecycle_status": row.status, "ingest_sequence": json.loads(row.metadata_json).get("source_watermark", 0)}

    def wait_for_analysis(self, event: MessageEvent, watermark: int) -> bool:
        if not self.settings.memory_event_extraction_enabled:
            return True
        deadline = time.monotonic() + self.settings.memory_analysis_wait_seconds
        while True:
            with self.storage.Session() as session:
                pending = session.scalar(select(func.count()).select_from(AnalysisJobRow).where(AnalysisJobRow.account_id == event.account_id, AnalysisJobRow.group_id == event.group_id, AnalysisJobRow.message_id <= watermark, AnalysisJobRow.status.in_(["pending", "running", "failed"])))
            if not pending:
                return True
            if time.monotonic() >= deadline:
                return False
            if not self.worker._threads and not (self.llm and self.settings.llm_api_key):
                if self.process_analysis(account_id=event.account_id, group_id=event.group_id, watermark=watermark, allow_model=False):
                    continue
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))

    def continuous_window(self, account_id: str, group_id: str, watermark: int, evaluated_at: datetime, *, limit: int | None = None):
        from .models import ConversationMessage
        with self.storage.Session() as session:
            rows = list(session.scalars(select(MessageRow).where(MessageRow.account_id == account_id, MessageRow.group_id == group_id, MessageRow.id < watermark, MessageRow.observed_at <= evaluated_at, MessageRow.lifecycle_status == "active", MessageRow.text_kind != "system").order_by(MessageRow.id.desc()).limit(limit if limit is not None else self.settings.wechat_context_messages)))
            messages = []
            used = 0
            for row in rows:
                text = self.storage._row_text(row)
                if not text or used + len(text) > self.settings.wechat_context_max_chars:
                    continue
                messages.append(ConversationMessage(sender_name=row.member_name, sender_id=row.member_id, text=text, message_id=row.external_message_id or str(row.id), observed_at=aware(row.source_created_at or row.observed_at), image_paths=json.loads(row.image_paths_json), source_role="assistant" if row.is_self else row.source_role))
                used += len(text)
            return list(reversed(messages))

    def recent_events(self, account_id: str, group_id: str, *, member_id: str = "", event_types=(), since: datetime | None = None, until: datetime | None = None, watermark: int | None = None, limit: int = 50, include_candidates: bool = True, include_inactive: bool = False) -> list[dict]:
        with self.storage.Session() as session:
            statement = select(EpisodeRow).where(EpisodeRow.account_id == account_id, EpisodeRow.group_id == group_id)
            if not include_inactive:
                statement = statement.where(EpisodeRow.status.in_(["active", "candidate"] if include_candidates else ["active"]))
            if member_id:
                statement = statement.where(EpisodeRow.actor_id == member_id)
            if event_types:
                statement = statement.where(EpisodeRow.event_type.in_(event_types))
            if since:
                statement = statement.where(EpisodeRow.occurred_at >= since)
            if until:
                statement = statement.where(EpisodeRow.occurred_at <= until)
            output = []
            for row in session.scalars(statement.order_by(EpisodeRow.occurred_at.desc()).limit(min(200, limit))):
                payload = self.episode_payload(row)
                if watermark is not None and payload["ingest_sequence"] >= watermark:
                    continue
                payload["metadata"] = json.loads(row.metadata_json)
                output.append(payload)
            return output

    def revert_event(self, event_id: str, *, reason: str, expected_version: int, account_id: str | None = None) -> dict:
        if not reason.strip():
            raise ValueError("撤销必须填写 reason")
        now = self.clock()
        with self.storage.Session() as session:
            row = session.get(EpisodeRow, event_id)
            if row is None or (account_id and row.account_id != account_id):
                raise KeyError("事件不存在")
            account, group = row.account_id, row.group_id
        with scope_transaction(self.storage, account, group) as session:
            row = session.get(EpisodeRow, event_id)
            metadata = json.loads(row.metadata_json)
            if row.status == "reverted" and metadata.get("revert_reason") == reason.strip() and row.version == expected_version + 1:
                return self.episode_payload(row)
            if row.version != expected_version:
                raise MemoryConflict("STATE_CONFLICT: 事件版本已变更")
            self.emotion.assert_replayable(session, row, now=now)
            self.emotion.invalidate_checkpoints(session, account, group, row.occurred_at)
            row.status, row.version = "reverted", row.version + 1
            metadata.update({"revert_reason": reason.strip()[:300], "reverted_at": now.isoformat()})
            row.metadata_json = encode(metadata)
            session.flush()
            undo = self.emotion.undo(session, account, group, [row], now=now, reason="admin_revert")
            profile = session.scalar(select(AIProfileRow).where(AIProfileRow.account_id == account, AIProfileRow.group_id == group))
            if profile:
                data = json.loads(profile.profile_json)
                data["semantic_refresh_required"] = True
                profile.profile_json = encode(data)
            return {**self.episode_payload(row), "emotion_undo": undo["strategy"]}

    def change_source(self, message_id: int, *, account_id: str, group_id: str, content: str | None = None, expected_version: int = 1, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("来源更正/删除必须有原因")
        now = self.clock()
        with scope_transaction(self.storage, account_id, group_id) as session:
            source = session.get(MessageRow, message_id)
            if not source or source.account_id != account_id or source.group_id != group_id:
                raise KeyError("来源不存在")
            if source.object_version != expected_version:
                raise MemoryConflict("STATE_CONFLICT")
            episodes = list(session.scalars(select(EpisodeRow).where(EpisodeRow.account_id == account_id, EpisodeRow.group_id == group_id, EpisodeRow.primary_source_id == str(source.external_message_id or source.id))))
            for episode in episodes:
                self.emotion.assert_replayable(session, episode, now=now)
            source.lifecycle_status = "superseded" if content is not None else "deleted"
            source.object_version += 1
            object_id = "raw-" + stable_id(account_id, group_id, source.external_message_id or source.message_hash)
            lifecycle = session.get(LifecycleRow, object_id)
            if lifecycle:
                lifecycle.status, lifecycle.version, lifecycle.updated_at = source.lifecycle_status, source.object_version, now
            identifiers = {str(message_id), str(source.external_message_id or "")}
            for fact in session.scalars(select(FactRow).where(FactRow.account_id == account_id, FactRow.group_id == group_id, FactRow.status.in_(["active", "confirmed", "candidate"]))):
                if identifiers.intersection(str(value) for value in json.loads(fact.source_message_ids_json or "[]")):
                    fact.status, fact.valid_to = "superseded", now
                    metadata = json.loads(fact.metadata_json or "{}")
                    metadata["source_invalidated_reason"] = reason[:300]
                    fact.metadata_json = encode(metadata)
            for segment in session.scalars(select(DiscussionSegmentRow).where(DiscussionSegmentRow.account_id == account_id, DiscussionSegmentRow.group_id == group_id)):
                if identifiers.intersection(json.loads(segment.source_ids_json)):
                    segment.status = "source_invalidated"
            profile = session.scalar(select(AIProfileRow).where(AIProfileRow.account_id == account_id, AIProfileRow.group_id == group_id))
            if profile:
                data = json.loads(profile.profile_json)
                data["semantic_refresh_required"] = True
                profile.profile_json = encode(data)
            for episode in episodes:
                episode.status, episode.version = "superseded", episode.version + 1
            session.flush()
            replay_from = min((aware(item.occurred_at) for item in episodes), default=now)
            self.emotion.invalidate_checkpoints(session, account_id, group_id, replay_from)
            self.emotion.undo(session, account_id, group_id, episodes, now=now, reason="source_changed")
            payload = {"memory_id": object_id, "object_version": source.object_version, "scope": {"account_id": account_id, "group_id": group_id}, "lifecycle_status": source.lifecycle_status, "content": "", "reason": reason[:300]}
        if content is not None:
            replacement = MessageEvent(account_id=account_id, group_id=group_id, group_name=source.group_name, member_id=source.member_id, member_name=source.member_name, text=content, external_message_id=f"correction:{message_id}:{source.object_version}", observed_at=aware(source.observed_at), source_created_at=aware(source.source_created_at) if source.source_created_at else None, historical=True)
            self.storage.record_message(replacement)
        return payload

    def groups(self) -> list[dict]:
        with self.storage.Session() as session:
            scopes = {(account, group): (name, watermark, count) for account, group, name, watermark, count in session.execute(select(MessageRow.account_id, MessageRow.group_id, func.max(MessageRow.group_name), func.max(MessageRow.id), func.count()).group_by(MessageRow.account_id, MessageRow.group_id))}
            for health in session.scalars(select(ListenerHealthRow)):
                scopes.setdefault((health.account_id, health.group_id), (health.group_id, 0, 0))
            output = []
            for (account, group), (name, watermark, count) in sorted(scopes.items()):
                analysis = dict(session.execute(select(AnalysisJobRow.status, func.count()).where(AnalysisJobRow.account_id == account, AnalysisJobRow.group_id == group).group_by(AnalysisJobRow.status)).all())
                output.append({"account_id": account, "group_id": group, "group_name": name,
                    "message_watermark": watermark, "message_count": count, "analysis": analysis,
                    "index": self.storage.index_status(account, group),
                    "degraded": bool(analysis.get("failed")),
                    "features": {"retrieval": self.settings.memory_orchestrator_enabled,
                        "event_extraction": self.settings.memory_event_extraction_enabled,
                        "semantic_profile": self.settings.semantic_group_profile_enabled}})
            return output

    def save_trace(self, bundle) -> None:
        trace = asdict(bundle)
        trace.pop("context_messages", None)
        trace["snapshot"]["evaluated_at"] = bundle.snapshot.evaluated_at.isoformat()
        trace.pop("context_projection", None)
        secrets = []
        for key in type(self.settings).model_fields:
            if any(part in key for part in ("api_key", "secret", "password", "access_key", "desktop_token", "embed_key")):
                secret = getattr(self.settings, key, "")
                if secret and isinstance(secret, str):
                    secrets.append(secret)
        def redact(value):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[REDACTED]")
                return value
            if isinstance(value, dict):
                return {redact(key): redact(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact(item) for item in value]
            return value
        encoded = encode(redact(trace))
        with self.storage.Session.begin() as session:
            session.merge(RetrievalTraceRow(request_id=bundle.request_id, account_id=bundle.snapshot.account_id, group_id=bundle.snapshot.group_id, question_message_id=bundle.snapshot.question_message_id, trace_json=encoded, status=bundle.retrieval_status, created_at=self.clock()))

    def trace(self, request_id: str, *, account_id: str | None = None) -> dict:
        with self.storage.Session() as session:
            row = session.get(RetrievalTraceRow, request_id)
            if not row or (account_id and row.account_id != account_id):
                raise KeyError("检索记录不存在")
            return json.loads(row.trace_json)

    def replay(self, *, account_id: str, group_id: str, since: datetime, until: datetime, dry_run: bool = True) -> dict:
        if not account_id or not group_id or until <= since:
            raise ValueError("回填必须明确账号、群和时间范围")
        with self.storage.Session() as session:
            rows = list(session.scalars(select(MessageRow).where(MessageRow.account_id == account_id, MessageRow.group_id == group_id, MessageRow.observed_at >= since, MessageRow.observed_at < until)))
        if not dry_run:
            with scope_transaction(self.storage, account_id, group_id) as session:
                for source in rows:
                    job = session.scalar(select(AnalysisJobRow).where(AnalysisJobRow.message_id == source.id, AnalysisJobRow.analyzer_version == 1))
                    if job and job.status == "done":
                        continue
                    if job is None:
                        session.add(AnalysisJobRow(message_id=source.id, account_id=account_id, group_id=group_id, analyzer_version=1, effect_enabled_at_ingest=False, status="pending", next_retry=self.clock(), created_at=self.clock()))
                    else:
                        job.status, job.effect_enabled_at_ingest, job.next_retry = "pending", False, self.clock()
        return {"dry_run": dry_run, "messages": len(rows), "account_id": account_id, "group_id": group_id, "emotion_backfill": False}

    def retry_failed(self, *, account_id: str = "", group_id: str = "") -> int:
        with scope_transaction(self.storage, "retry", "retry") as session:
            analysis = select(AnalysisJobRow).where(AnalysisJobRow.status == "failed")
            if account_id:
                analysis = analysis.where(AnalysisJobRow.account_id == account_id)
            if group_id:
                analysis = analysis.where(AnalysisJobRow.group_id == group_id)
            jobs = list(session.scalars(analysis))
            for job in jobs:
                job.status, job.attempts, job.next_retry, job.lease_until, job.lease_owner, job.last_error = "pending", 0, self.clock(), None, "", ""
            return len(jobs)

    def maintenance(self) -> bool:
        if time.monotonic() < self._maintenance_at:
            return False
        self._maintenance_at = time.monotonic() + 30
        now = self.clock()
        from .semantic_profiles import SemanticProfiles
        profiles = SemanticProfiles(self)
        scheduler = getattr(self, "scheduler", None)
        if scheduler:
            scheduler.settle_bids()
        local = now.astimezone(ZoneInfo(self.settings.group_timezone))
        day, yesterday = local.date().isoformat(), (local - timedelta(days=1)).date().isoformat()
        narrative, digest = self._narrative(), self._digest()
        for scope in self.groups():
            account, group = scope["account_id"], scope["group_id"]
            if self.settings.semantic_group_profile_enabled:
                profiles.refresh(account, group)
            self.checkpoint(account, group, now=now)
            self.prune_emotion_audit(account, group, now=now)
            if self._daily_due((account, group), day):
                # One pass per local day: a finished day's timeline is stable,
                # relations are a weekly aggregate and digests merge facts.
                timeline = narrative.refresh_timeline(account, group, day=yesterday)
                relations = narrative.refresh_relations(account, group, days=7)
                digest.refresh(account, group, now=now)
                model_waiting = bool(self.llm and self.settings.llm_api_key)
                if not model_waiting or timeline is not None or relations:
                    self._daily_mark((account, group), day)
        if self.storage.embedder.configured:
            # P4 backfill: 90 days of recent messages, sixteen at a time, on the
            # maintenance thread so the reply path never waits for a vector.
            self.storage.embed_pending_messages(days=90, limit=16)
        with self.storage.Session.begin() as session:
            session.execute(delete(RetrievalTraceRow).where(RetrievalTraceRow.created_at < now - timedelta(days=self.settings.memory_trace_retention_days)))
        return False

    def _narrative(self):
        if self._narrative_engine is None:
            from .narrative import Narrative
            self._narrative_engine = Narrative(self)
        return self._narrative_engine

    def _digest(self):
        if self._digest_engine is None:
            from .digest import Digest
            self._digest_engine = Digest(self)
        return self._digest_engine

    def _daily_due(self, scope: tuple[str, str], day: str) -> bool:
        return self._daily_done.get(scope) != day

    def _daily_mark(self, scope: tuple[str, str], day: str) -> None:
        self._daily_done[scope] = day

    def checkpoint(self, account_id: str, group_id: str, *, now: datetime) -> None:
        from .memory_schema import EmotionStateRow
        day = now.astimezone(ZoneInfo(self.settings.group_timezone)).date().isoformat()
        identifier = stable_id(account_id, group_id, day)
        with scope_transaction(self.storage, account_id, group_id) as session:
            if session.get(EmotionCheckpointRow, identifier):
                return
            state = session.get(EmotionStateRow, (account_id, group_id))
            if state is None:
                return
            replay_context = self.emotion.rebuild(session, account_id, group_id, now=now)
            session.flush()
            # The checkpoint stores the group mood plus the five-minute dedupe
            # cache.  Credits and penalties live in their own per-row ledgers, so
            # replay no longer reads a cached score out of the checkpoint.
            session.add(EmotionCheckpointRow(checkpoint_id=identifier, account_id=account_id, group_id=group_id, cutoff_event_id=state.last_event_id, cutoff_time=now, group_state_json=encode({"p": state.p, "a": state.a, "d": state.d, "labels": json.loads(state.labels_json), "evaluated_at": now, "replay_context": replay_context}), rule_version=2, created_at=now))

    def prune_emotion_audit(self, account_id: str, group_id: str, *, now: datetime) -> None:
        """Keep 30 days plus a covering boundary checkpoint before pruning audits."""
        with scope_transaction(self.storage, account_id, group_id) as session:
            boundary = session.scalar(select(EmotionCheckpointRow).where(EmotionCheckpointRow.account_id == account_id, EmotionCheckpointRow.group_id == group_id, EmotionCheckpointRow.rule_version >= 2, EmotionCheckpointRow.cutoff_time <= now - timedelta(days=30)).order_by(EmotionCheckpointRow.cutoff_time.desc()).limit(1))
            if boundary is None:
                return
            session.execute(delete(EmotionEventRow).where(EmotionEventRow.account_id == account_id, EmotionEventRow.group_id == group_id, EmotionEventRow.occurred_at <= boundary.cutoff_time))
            session.execute(delete(EmotionCheckpointRow).where(EmotionCheckpointRow.account_id == account_id, EmotionCheckpointRow.group_id == group_id, EmotionCheckpointRow.cutoff_time < boundary.cutoff_time))
