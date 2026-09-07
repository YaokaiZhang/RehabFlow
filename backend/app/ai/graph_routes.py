"""Pure conditional-edge policy for the constrained RehabFlow graph.

State contract: reads typed route, gate, clarification, safety, and retry-control
fields and returns existing graph edge labels. It never mutates graph state.
"""
from __future__ import annotations

from app.ai.policy_gate import GateSeverity
from app.ai.repair import is_token_telemetry_only_hard_stop
from app.ai.workflow_state import RehabGraphState, Route


def route_after_router(state: RehabGraphState) -> str:
    if state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value:
        return "unsafe_fallback"
    typed_route = state.get("route")
    if typed_route is not None:
        try:
            typed_route = Route(typed_route)
        except ValueError:
            return "unsafe_fallback"
        if typed_route is Route.URGENT:
            return "urgent_end"
        if typed_route is Route.CLARIFY:
            return "clarification"
        if typed_route is Route.UNSUPPORTED:
            return "unsafe_fallback"

    decision = state.get("routing_decision")
    if decision == "urgent_end" or state.get("if_emergency"):
        return "urgent_end"
    if decision == "ask_clarification" or state.get("needs_more_info"):
        return "clarification"
    return "consultant"


def route_after_context_integrity(state: RehabGraphState) -> str:
    if state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value:
        return "unsafe_fallback"
    return "router"


def route_after_context_revalidation(state: RehabGraphState) -> str:
    if state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value:
        return "unsafe_fallback"
    return "consultant"


def route_after_safety(state: RehabGraphState) -> str:
    if state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value:
        return "unsafe_fallback"
    if state.get("safety_passed"):
        return "safe_end"

    if state.get("consult_attempts", 0) < 2:
        return "consultant_retry"

    return "unsafe_fallback"

def route_after_policy_gate(state: RehabGraphState) -> str:
    if (
        state.get("policy_gate_severity") != GateSeverity.PASS.value
        and not is_token_telemetry_only_hard_stop(state)
    ):
        return "unsafe_fallback"
    return "safety_reviewer"
