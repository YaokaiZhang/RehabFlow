"""Metadata-only tracing boundary for the constrained RehabFlow graph.

State contract: reads route/control metadata, bounded source identifiers, evidence
and tool outcomes, reviewer status, attempts, and token counters. It writes no
graph state and never accepts transcript, prompt, or raw context content.
"""
from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
from typing import Any

from app.ai.evidence_types import EvidenceResult, EvidenceSource
from app.observability.agent_trace import TraceContext, is_safe_trace_source_id

def _trace_route(state: Mapping[str, Any], updates: Mapping[str, Any] | None = None) -> str | None:
    updates = updates or {}
    value = updates.get("route") or updates.get("current_route") or state.get("route") or state.get("current_route")
    value = getattr(value, "value", value)
    return value if isinstance(value, str) and value in {"urgent", "clarify", "consultant", "unsupported"} else None


def _trace_sources(state: Mapping[str, Any], updates: Mapping[str, Any] | None = None) -> list[str]:
    candidates: list[object] = []
    for holder in (state, updates or {}):
        for key in ("source_ids", "authorized_source_ids"):
            value = holder.get(key)
            if isinstance(value, (list, tuple, set)):
                candidates.extend(value)
        for result in holder.get("evidence_results", []) or ():
            value = getattr(result, "source_ids", None)
            if value is None and isinstance(result, Mapping):
                value = result.get("source_ids")
            if isinstance(value, (list, tuple, set)):
                candidates.extend(value)
        for document in holder.get("retrieved_docs", []) or ():
            if isinstance(document, Mapping):
                candidates.append(document.get("id") or document.get("document_id"))

    safe: list[str] = []
    seen: set[str] = set()
    for raw_value in candidates:
        value = str(raw_value or "").strip()
        if not is_safe_trace_source_id(value):
            continue
        if value not in seen:
            seen.add(value)
            safe.append(value)
    return safe


def _trace_attempt(state: Mapping[str, Any], updates: Mapping[str, Any] | None = None) -> int:
    updates = updates or {}
    for holder in (updates, state):
        for key in ("review_attempt", "consult_attempts", "attempt", "repair_count"):
            value = holder.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
    return 0


def _trace_outcome(node_name: str, state: Mapping[str, Any], updates: Mapping[str, Any] | None = None) -> str | None:
    updates = updates or {}
    status = updates.get("review_status") or updates.get("policy_gate_severity")
    if isinstance(status, str) and status in {"approve", "reject", "error", "timeout", "pass", "repairable", "hard_stop"}:
        return status
    if node_name == "unsafe_fallback":
        return "unsafe_fallback"
    if updates.get("final_response") is not None:
        return "completed"
    if node_name == "review_join":
        return "released" if updates.get("review_release") is True else "held"
    if node_name == "clarification":
        return "awaiting_input"
    return "ok"


def _trace_failure_category(exc: Exception) -> str:
    """Map implementation exceptions to the finite trace failure taxonomy."""
    name = type(exc).__name__.lower()
    if "interrupt" in name:
        return "interrupted"
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return "tool_timeout"
    if "provider" in name or "model" in name:
        return "provider_error"
    if "checkpoint" in name:
        return "checkpoint_unavailable"
    if "permission" in name or "policy" in name:
        return "policy_rejection"
    if "evidence" in name:
        return "evidence_error"
    if "tool" in name:
        return "tool_error"
    return "runtime_error"


def _trace_elapsed(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _trace_usage_field(
    updates: Mapping[str, Any],
    field_name: str,
) -> int | None:
    direct = updates.get(field_name)
    if isinstance(direct, int) and not isinstance(direct, bool) and direct >= 0:
        return direct
    decisions = updates.get("reviewer_decisions") or ()
    decision_total = 0
    saw_decision_tokens = False
    for decision in decisions:
        value = getattr(decision, field_name, None)
        if value is None and isinstance(decision, Mapping):
            value = decision.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            decision_total += value
            saw_decision_tokens = True
    if saw_decision_tokens:
        return decision_total
    return None


def _trace_input_tokens(state: Mapping[str, Any], updates: Mapping[str, Any]) -> int | None:
    del state
    return _trace_usage_field(updates, "input_tokens")


def _trace_output_tokens(state: Mapping[str, Any], updates: Mapping[str, Any]) -> int | None:
    del state
    return _trace_usage_field(updates, "output_tokens")


def _trace_review_status(node_name: str, updates: Mapping[str, Any]) -> str:
    status = updates.get("review_status")
    if not isinstance(status, str):
        status = updates.get(f"{node_name}_review_status")
    if not isinstance(status, str) and node_name.endswith("_reviewer"):
        status = updates.get(f"{node_name[:-9]}_review_status")
    if not isinstance(status, str):
        for decision in updates.get("reviewer_decisions") or ():
            status = getattr(decision, "status", None)
            if status is None and isinstance(decision, Mapping):
                status = decision.get("status")
            status = getattr(status, "value", status)
            if isinstance(status, str):
                break
    status = getattr(status, "value", status)
    if isinstance(status, str) and status in {"approve", "reject", "error", "timeout"}:
        return status
    return "error"


def _trace_tool_events(
    context: TraceContext,
    state: Mapping[str, Any],
    updates: Mapping[str, Any],
    *,
    attempt: int,
) -> None:
    raw_events = updates.get("tool_events") or []
    for raw_event in raw_events:
        if not isinstance(raw_event, Mapping):
            continue
        name = str(raw_event.get("tool_name") or raw_event.get("tool") or "").strip()
        if name not in {"rehab_exercise_kb_search", "web_search"}:
            continue
        elapsed = raw_event.get("latency_ms")
        elapsed = round(elapsed) if isinstance(elapsed, (int, float)) and elapsed >= 0 else None
        outcome = raw_event.get("outcome")
        if outcome in {"ok_nonempty", "ok_empty"}:
            outcome = "ok"
        if outcome not in {"ok", "ok_nonempty", "ok_empty", "error", "blocked", "invalid", "timeout"}:
            outcome = "error" if raw_event.get("error") else "ok"
        context.emit(
            "tool_finished",
            node="consultant",
            tool_name=name,
            elapsed_ms=elapsed,
            source_ids=_trace_sources(state, updates),
            outcome=outcome,
            attempt=attempt,
        )


def _trace_evidence_result(
    context: TraceContext,
    state: Mapping[str, Any],
    result: EvidenceResult,
    *,
    attempt: int,
) -> None:
    """Emit only bounded metadata for one completed evidence adapter call."""
    status = str(result.status)
    outcome = {
        "ok": "ok",
        "empty": "ok",
        "forbidden": "blocked",
        "timeout": "error",
        "error": "error",
    }.get(status, "error")
    result_source_ids = [
        str(getattr(packet, "source_id", ""))
        for packet in (result.packets or [])
        if getattr(packet, "source_id", None)
    ]
    trace_state = {
        **dict(state),
        "source_ids": [
            *list(state.get("source_ids", []) or []),
            *result_source_ids,
        ],
    }
    tool_name = (
        "rehab_exercise_kb_search"
        if result.source.value == EvidenceSource.REHAB_KNOWLEDGE.value
        else "evidence_adapter"
    )
    context.emit(
        "tool_finished",
        node="gather_evidence",
        tool_name=tool_name,
        elapsed_ms=result.elapsed_ms,
        source_ids=_trace_sources(trace_state),
        outcome=outcome,
        failure_category=(
            {
                "error": "evidence_error",
                "timeout": "evidence_timeout",
                "forbidden": "forbidden",
            }.get(status)
            if outcome != "ok"
            else None
        ),
        attempt=attempt,
    )


def _trace_begin(context: TraceContext, state: Mapping[str, Any]) -> None:
    if context.event_index == 0:
        context.emit(
            "turn_started",
            route=_trace_route(state),
            source_ids=_trace_sources(state),
            outcome="started",
        )


def _trace_node_started(context: TraceContext, state: Mapping[str, Any], node_name: str) -> None:
    _trace_begin(context, state)
    attempt = _trace_attempt(state)
    context.emit(
        "node_started",
        node=node_name,
        route=_trace_route(state),
        source_ids=_trace_sources(state),
        attempt=attempt,
    )
    if node_name in {"consultant_repair", "repair_revalidate_context"}:
        context.emit(
            "repair_started",
            node=node_name,
            route=_trace_route(state),
            source_ids=_trace_sources(state),
            attempt=attempt,
        )


def _trace_node_finished(
    context: TraceContext,
    state: Mapping[str, Any],
    updates: Mapping[str, Any],
    *,
    node_name: str,
    started: float,
) -> None:
    attempt = _trace_attempt(state, updates)
    _trace_tool_events(context, state, updates, attempt=attempt)
    context.emit(
        "node_finished",
        node=node_name,
        route=_trace_route(state, updates),
        elapsed_ms=_trace_elapsed(started),
        input_tokens=_trace_input_tokens(state, updates),
        output_tokens=_trace_output_tokens(state, updates),
        source_ids=_trace_sources(state, updates),
        outcome=_trace_outcome(node_name, state, updates),
        attempt=attempt,
    )


def _trace_turn_finished(
    context: TraceContext,
    state: Mapping[str, Any],
    updates: Mapping[str, Any],
    *,
    node_name: str,
) -> None:
    if context.closed or updates.get("final_response") is None:
        return
    context.emit(
        "turn_finished",
        node=node_name,
        route=_trace_route(state, updates),
        source_ids=_trace_sources(state, updates),
        outcome=_trace_outcome(node_name, state, updates),
        attempt=_trace_attempt(state, updates),
    )


def _trace_turn_failed(context: TraceContext, state: Mapping[str, Any], node_name: str, exc: Exception) -> None:
    if context.closed:
        return
    context.emit(
        "turn_failed",
        node=node_name,
        route=_trace_route(state),
        source_ids=_trace_sources(state),
        outcome="failed",
        failure_category=_trace_failure_category(exc),
        attempt=_trace_attempt(state),
    )


# ---------------------------------------------------------
# 2. LLM Configuration (OpenAI API)
# ---------------------------------------------------------
