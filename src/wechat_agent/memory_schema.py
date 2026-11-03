"""Additive, portable SQL ledgers for the memory/context/emotion PRD."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .storage import Base


class SchemaVersionRow(Base):
    __tablename__ = "schema_versions"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnalysisJobRow(Base):
    __tablename__ = "message_analysis_jobs"
    __table_args__ = (UniqueConstraint("message_id", "analyzer_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(Integer, index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    analyzer_version: Mapped[int] = mapped_column(Integer, default=1)
    effect_enabled_at_ingest: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str] = mapped_column(String(64), default="")
    last_error: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EpisodeRow(Base):
    __tablename__ = "memory_episodes"
    __table_args__ = (UniqueConstraint("account_id", "group_id", "primary_source_id", "event_type", "target_key"),)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    actor_id: Mapped[str] = mapped_column(String(256), default="", index=True)
    actor_name: Mapped[str] = mapped_column(String(256), default="")
    target_type: Mapped[str] = mapped_column(String(32), default="ai")
    target_id: Mapped[str] = mapped_column(String(256), default="")
    target_key: Mapped[str] = mapped_column(String(256), default="ai")
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    topic_id: Mapped[str] = mapped_column(String(128), default="")
    content: Mapped[str] = mapped_column(Text)
    evidence_excerpt: Mapped[str] = mapped_column(Text)
    primary_source_id: Mapped[str] = mapped_column(String(256))
    source_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_role: Mapped[str] = mapped_column(String(24), default="human")
    time_quality: Mapped[str] = mapped_column(String(24), default="observed")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class EmotionStateRow(Base):
    __tablename__ = "emotion_states"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    p: Mapped[float] = mapped_column(Float, default=0.2)
    a: Mapped[float] = mapped_column(Float, default=0.35)
    d: Mapped[float] = mapped_column(Float, default=0.5)
    labels_json: Mapped[str] = mapped_column(Text, default="{}")
    emotion_activation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_event_id: Mapped[str] = mapped_column(String(128), default="")
    version: Mapped[int] = mapped_column(Integer, default=0)


class RelationshipStateRow(Base):
    __tablename__ = "relationship_states"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    base_affinity: Mapped[float] = mapped_column(Float, default=50)
    # Cache only: the authoritative credit is the sum of ``relationship_credits``
    # rows, and ``snapshot()`` applies each row's own decay clock instead of
    # decaying one re-anchored scalar.
    positive_credit: Mapped[float] = mapped_column(Float, default=0)
    credit_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_interaction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=0)


class RelationshipEffectRow(Base):
    __tablename__ = "relationship_effects"
    __table_args__ = (UniqueConstraint("origin_event_id", "member_id", "effect_kind"),)
    effect_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    origin_event_id: Mapped[str] = mapped_column(String(128), index=True)
    effect_kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[float] = mapped_column(Float)
    repair_factor: Mapped[float] = mapped_column(Float, default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class RelationshipCreditRow(Base):
    """One row per positive credit: ``positive_credit`` is now the sum of these rows.

    The old design compressed every praise into a single scalar plus one timer,
    so each new credit re-anchored the decay clock of every earlier credit.  A
    row per credit keeps each credit on its own ``occurred_at``, which makes the
    snapshot a sum of independently decayed values and a revert a one-row edit.
    """

    __tablename__ = "relationship_credits"
    __table_args__ = (UniqueConstraint("origin_event_id", "member_id"),)
    credit_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    origin_event_id: Mapped[str] = mapped_column(String(128), index=True)
    amount: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="active")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class RelationshipPauseRow(Base):
    __tablename__ = "relationship_pauses"
    effect_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256))
    response_message_id: Mapped[str] = mapped_column(String(256))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmotionEventRow(Base):
    __tablename__ = "emotion_events"
    origin_event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    affected_members_json: Mapped[str] = mapped_column(Text, default="[]")
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    repair_changes_json: Mapped[str] = mapped_column(Text, default="[]")
    decision_reason: Mapped[str] = mapped_column(String(128), default="")
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="applied")


class EmotionCheckpointRow(Base):
    __tablename__ = "emotion_checkpoints"
    __table_args__ = (UniqueConstraint("account_id", "group_id", "cutoff_time"),)
    checkpoint_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    cutoff_event_id: Mapped[str] = mapped_column(String(128), default="")
    cutoff_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    group_state_json: Mapped[str] = mapped_column(Text, default="{}")
    member_states_json: Mapped[str] = mapped_column(Text, default="{}")
    active_effects_json: Mapped[str] = mapped_column(Text, default="[]")
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LifecycleRow(Base):
    __tablename__ = "evidence_lifecycle"
    object_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="active")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupProfileVersionRow(Base):
    __tablename__ = "group_profile_versions"
    __table_args__ = (UniqueConstraint("account_id", "group_id", "version"),)
    version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    version: Mapped[int] = mapped_column(Integer)
    semantic_json: Mapped[str] = mapped_column(Text)
    evidence_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    coverage_json: Mapped[str] = mapped_column(Text, default="{}")
    source_watermark: Mapped[int] = mapped_column(Integer)
    change_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DiscussionSegmentRow(Base):
    __tablename__ = "discussion_segments"
    segment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    topic_id: Mapped[str] = mapped_column(String(128), index=True)
    topic: Mapped[str] = mapped_column(String(256))
    source_ids_json: Mapped[str] = mapped_column(Text)
    member_ids_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="closed")


class RetrievalTraceRow(Base):
    __tablename__ = "retrieval_traces"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    question_message_id: Mapped[str] = mapped_column(String(256))
    trace_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProactiveWindowRow(Base):
    __tablename__ = "proactive_windows"
    window_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    budget: Mapped[int] = mapped_column(Integer, default=3)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    next_candidate_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="scheduled", index=True)
    skip_reason: Mapped[str] = mapped_column(String(128), default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str] = mapped_column(String(64), default="")
    sent_at_json: Mapped[str] = mapped_column(Text, default="[]")


class SocialBidRow(Base):
    __tablename__ = "social_bids"
    bid_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    outbound_message_id: Mapped[str] = mapped_column(String(256), unique=True)
    window_id: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    activity_counts_json: Mapped[str] = mapped_column(Text, default="{}")
    observation_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    ack_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(24), default="observing")


class ListenerHealthRow(Base):
    __tablename__ = "listener_health"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    coverage_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_gap_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    online: Mapped[bool] = mapped_column(Boolean, default=True)


class StickerObservationRow(Base):
    __tablename__ = "sticker_observations"
    md5: Mapped[str] = mapped_column(String(32), primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    emotion: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(String(256), default="")
    image_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MediaAnalysisLeaseRow(Base):
    __tablename__ = "media_analysis_leases"
    md5: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StickerCatalogRow(Base):
    __tablename__ = "sticker_catalog"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tab: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    row: Mapped[int] = mapped_column(Integer, primary_key=True)
    col: Mapped[int] = mapped_column(Integer, primary_key=True)
    md5: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    emotion: Mapped[str] = mapped_column(String(64), default="")
    screenshot_path: Mapped[str] = mapped_column(Text, default="")
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LinuxSendRow(Base):
    __tablename__ = "linux_send_ledger"
    action_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    status: Mapped[str] = mapped_column(String(24), default="reserved")
    watermarks_json: Mapped[str] = mapped_column(Text, default="{}")
    receipts_json: Mapped[str] = mapped_column(Text, default="[]")
    message_type: Mapped[str] = mapped_column(String(32), default="text")
    expected_json: Mapped[str] = mapped_column(Text, default="{}")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LinuxNativeSendRow(Base):
    __tablename__ = "linux_native_sends"
    native_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(128), index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    message_type: Mapped[str] = mapped_column(String(24))
    message_ids_json: Mapped[str] = mapped_column(Text, default="[]")


class GroupTimelineRow(Base):
    """One narrative row per group per local day.

    Written by the maintenance thread after the day's analysis settles; read by
    retrieval so "最近群里怎么样" answers from a summary instead of raw rows.
    """

    __tablename__ = "group_timeline"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    topics_json: Mapped[str] = mapped_column(Text, default="[]")
    active_members_json: Mapped[str] = mapped_column(Text, default="[]")
    emotion_json: Mapped[str] = mapped_column(Text, default="{}")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemberRelationRow(Base):
    """Directed member-to-member relation, aggregated weekly."""

    __tablename__ = "member_relations"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    other_member_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    relation: Mapped[str] = mapped_column(String(64), default="")
    strength: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemberAddressRow(Base):
    """The address a close member asked the character to call them.

    One row per account/group/member; a later request updates the same row.  It
    is only ever used as prompt tone (never as an instruction or permission),
    and only while the member's affinity is at least 80.
    """

    __tablename__ = "member_addresses"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    address: Mapped[str] = mapped_column(String(32))
    origin_message_id: Mapped[str] = mapped_column(String(128), default="")
    source_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JudgeBudgetRow(Base):
    """Durable per-group daily counter for model judgements.

    The judge is the only place a message can create a memory effect, so its
    spend must survive a restart: a process-local counter would reset on every
    restart and silently exceed the configured daily maximum.
    """

    __tablename__ = "judge_budget"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
