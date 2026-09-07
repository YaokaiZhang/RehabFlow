"""Typed machine-control state for the constrained rehabilitation workflow."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from operator import add
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ai.evidence_types import EvidenceResultMetadata


WORKFLOW_VERSION = "2026-07-18"


def reduce_evidence_results(
    existing: list[EvidenceResultMetadata],
    updates: list[EvidenceResultMetadata],
) -> list[EvidenceResultMetadata]:
    """Reset evidence metadata at an attempt boundary while merging branches."""
    if not updates:
        return []
    if not existing:
        return list(updates)
    return [*existing, *updates]


def _reviewer_decision_attempt(value: object) -> int | None:
    raw = value.get("attempt") if isinstance(value, Mapping) else getattr(value, "attempt", None)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


class ReviewerDecisionReplacement(list):
    """Reducer marker for an explicit current-attempt decision replacement."""


def replace_reviewer_decisions(decisions: list[object] | None = None) -> ReviewerDecisionReplacement:
    """Return a reducer update that replaces, rather than merges, active decisions."""
    return ReviewerDecisionReplacement(decisions or [])


def reduce_reviewer_decisions(
    existing: list[object],
    updates: list[object],
) -> list[object]:
    """Keep only the newest tagged review attempt in active join state.

    Audit history is carried separately with an additive reducer. This field is
    intentionally current-attempt-only so checkpointed decisions from an older
    turn cannot reach a later join.
    """
    if isinstance(updates, ReviewerDecisionReplacement):
        return list(updates)
    merged = [*existing, *updates]
    tagged = [value for value in merged if _reviewer_decision_attempt(value) is not None]
    if not tagged:
        return merged
    current_attempt = max(_reviewer_decision_attempt(value) for value in tagged)
    current = [
        value
        for value in merged
        if _reviewer_decision_attempt(value) == current_attempt
    ]
    reviewer_order = {"safety": 0, "grounding": 1}
    return sorted(
        current,
        key=lambda value: (
            reviewer_order.get(
                value.get("reviewer") if isinstance(value, Mapping) else getattr(value, "reviewer", ""),
                99,
            ),
            str(value),
        ),
    )


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum with stable serialized values."""

    def __str__(self) -> str:
        return self.value


class Route(StrEnum):
    URGENT = "urgent"
    CLARIFY = "clarify"
    CONSULTANT = "consultant"
    UNSUPPORTED = "unsupported"


class ReviewStatus(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    ERROR = "error"
    TIMEOUT = "timeout"


class ReviewerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: Literal["safety", "grounding"]
    status: ReviewStatus
    feedback: str


class WorkflowBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deadline_at: datetime
    max_model_calls: int = Field(ge=1, le=128)
    max_tool_calls: int = Field(ge=0, le=256)
    # Legacy configuration retained for caller compatibility; never enforced.
    max_total_tokens: int = Field(ge=1, le=100_000)
    max_repairs: int = Field(default=1, ge=0, le=3)

    @field_validator("deadline_at")
    @classmethod
    def require_timezone_aware_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must be timezone-aware")
        return value


class RehabGraphState(TypedDict, total=False):
    """Graph state with typed control fields and typed workflow fields."""

    # Durable identity and workflow control
    session_id: str
    patient_id: str
    care_episode_id: str | None
    trace_turn_id: str
    workflow_version: str
    route: Route
    repair_count: int
    status: str
    budget: WorkflowBudget
    evidence_turn_id: str
    evidence_results: Annotated[list[EvidenceResultMetadata], reduce_evidence_results]
    reviewer_decisions: Annotated[list[ReviewerDecision], reduce_reviewer_decisions]
    reviewer_decision_history: Annotated[list[ReviewerDecision], add]
    review_attempt: int
    review_turn_id: str
    review_release: bool

    # Deterministic preflight and process-gate control
    principal_authorized: bool
    session_authorized: bool
    care_episode_authorized: bool
    requested_route: Route
    authorized_source_ids: list[str]
    source_ids: list[str]
    context_identity: dict[str, object]
    source_allowances: dict[str, dict[str, object]]
    source_ids_by_source: dict[str, list[str]]
    context_integrity: object
    context_version: str
    evidence_context_version: str
    evidence_source_versions: dict[str, str]
    repair_context_snapshot: dict[str, object]
    repair_action_count: int
    repair_requires_revalidation: bool
    force_revalidate_context: bool
    discard_previous_evidence: bool
    rehab_knowledge_requested: bool
    exercise_request: bool
    rehab_knowledge_scope: str
    episode_memory_level1_session_ids: list[str]
    authorized_tool_names: list[str]
    allowed_catalog_ids: list[str]
    model_calls: int
    tool_calls: int
    total_tokens: int | None
    input_tokens: int | None
    output_tokens: int | None
    idempotency_key: str | None
    request_hash: str
    prior_request_hash: str | None
    policy_gate_severity: str
    policy_gate_reason_codes: list[str]

    # Clarification and context control
    user_input: str
    clarification_question: str
    clarification_required_fields: list[str]
    clarification_field_reasons: dict[str, str]
    clarification_checkpoint: dict[str, object]
    clarification_answered: bool
    context_rehydrated: bool
    memory_context: str
    context_packets: dict[str, dict[str, object]]
    context_disclosure_trace: list[dict[str, object]]
    context_planner_error: str
    consultant_context_snapshot: str
    conversation_history: list[dict[str, object]]

    # Safety routing control
    if_emergency: bool
    emergency_reason: str
    needs_more_info: bool
    follow_up_question: str
    routing_decision: Literal[
        "urgent_end",
        "continuation",
        "ask_clarification",
    ]

    # Consultant control and natural-language draft
    consult_attempts: int
    retrieved_docs: list[dict[str, object]]
    web_search_summary: str
    retrieval_status: dict[str, str]
    retrieval_outcomes: dict[str, str]
    retrieval_errors: dict[str, str]
    catalog_exercise_suggestions: list[dict[str, object]]
    consult_response: str
    tool_calling_used: bool
    tool_calling_error: str
    tool_events: list[dict[str, object]]
    tool_call_names: list[str]
    web_sources: list[dict[str, str]]

    # Natural-language reviewer output and machine approval state
    safety_passed: bool
    safety_feedback: str

    # Operational trace and final natural-language output
    debug_trace: list[dict[str, object]]
    internal_log_events: Annotated[list[dict[str, object]], add]
    final_response: str
