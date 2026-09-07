"""Node-specific context sections without fixed per-section token budgets."""

from __future__ import annotations

from types import MappingProxyType

from app.ai.context_types import NodeSpec


ROUTER_SPEC = NodeSpec(
    node_id="router",
    context_sections=("current_user_message", "episode_memory", "triage_summary", "recent_turns"),
)

CONSULTANT_SPEC = NodeSpec(
    node_id="consultant",
    context_sections=(
        "current_user_message",
        "patient_memory",
        "episode_memory",
        "triage_summary",
        "recent_turns",
        "retrieval_context",
    ),
)

SAFETY_REVIEWER_SPEC = NodeSpec(
    node_id="safety_reviewer",
    context_sections=(
        "current_user_message",
        "consultant_response",
        "patient_memory",
        "episode_memory",
        "safety_feedback",
    ),
)

GROUNDING_REVIEWER_SPEC_VERSION = "2026-07-18"
GROUNDING_REVIEWER_SPEC = NodeSpec(
    node_id="grounding_reviewer",
    context_sections=("consultant_draft", "evidence_manifest"),
)

NODE_SPECS = MappingProxyType(
    {
        ROUTER_SPEC.node_id: ROUTER_SPEC,
        CONSULTANT_SPEC.node_id: CONSULTANT_SPEC,
        SAFETY_REVIEWER_SPEC.node_id: SAFETY_REVIEWER_SPEC,
        GROUNDING_REVIEWER_SPEC.node_id: GROUNDING_REVIEWER_SPEC,
    }
)
