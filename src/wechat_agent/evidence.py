from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Snapshot:
    account_id: str
    group_id: str
    message_watermark: int
    evaluated_at: datetime
    question_message_id: str = ""


@dataclass(slots=True)
class EvidenceItem:
    evidence_id: str
    object_id: str
    account_id: str
    group_id: str
    content: str
    object_version: int = 1
    actor_member_id: str = ""
    actor_name: str = ""
    target: str = ""
    source_role: str = "human"
    occurred_at: str = ""
    time_quality: str = "observed"
    source_message_ids: list[str] = field(default_factory=list)
    source_uri: str = ""
    confidence: float = 0.5
    lifecycle_status: str = "active"
    source_availability: str = "available"
    memory_type: str = "raw_message"
    event_type: str = ""
    member_id: str = ""
    task_id: str = ""
    semantic_score: float = 0.0
    rank_score: float = 0.0
    ingest_sequence: int = 0
    quoted: bool = False
    source_neighbors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class RetrievalPlan:
    intent: str = "chat"
    needs_memory: bool = False
    memory_types: list[str] = field(default_factory=list)
    subject_member_ids: list[str] = field(default_factory=list)
    target_type: str = ""
    event_types: list[str] = field(default_factory=list)
    time_hint: str = "recent"
    queries: list[str] = field(default_factory=list)
    task_id: str | None = None
    need_source_neighbors: bool = False
    valid: bool = True
    time_start: str | None = None
    time_end: str | None = None


@dataclass(slots=True)
class EvidenceBundle:
    request_id: str
    snapshot: Snapshot
    plan: RetrievalPlan
    evidence: list[EvidenceItem] = field(default_factory=list)
    context_messages: list = field(default_factory=list)
    profiles: dict = field(default_factory=dict)
    emotion: dict = field(default_factory=dict)
    timeline: list = field(default_factory=list)
    relations: list = field(default_factory=list)
    retrieval_status: str = "OK"
    coverage: dict = field(default_factory=dict)
    used_evidence_ids: list[str] = field(default_factory=list)
    token_usage: dict = field(default_factory=dict)
    dropped: list[dict] = field(default_factory=list)
    timing: dict = field(default_factory=dict)
    routes: list[dict] = field(default_factory=list)
    profile_version: int = 0
    candidate_decisions: list[dict] = field(default_factory=list)
    fixed_input_tokens: int = 0
    context_projection: dict = field(default_factory=dict)


def text_tokens(value: str) -> int:
    """Conservative portable estimate: Chinese characters and ASCII bytes / 3."""
    chinese = sum(ord(character) > 127 for character in value)
    return chinese + (len(value) - chinese + 2) // 3
