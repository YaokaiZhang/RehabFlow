"""Concurrent, bounded Safety and Grounding Reviewer nodes.

Reviewer inputs are immutable and packet bodies live only in this process-local
store/active-turn context. LangGraph state carries reviewer decisions and
metadata, never evidence packet bodies.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from threading import BoundedSemaphore, RLock
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Send
from pydantic import BaseModel, ConfigDict, Field

from app.ai.active_turn_context import active_consultant_snapshot, active_triaged_context, active_tool_records
from app.ai.grounding_reviewer import GroundingReviewerOutput
from app.ai.model_input_budget import (
    assert_model_messages_within_limit,
)
from app.ai.policy_gate import GateSeverity
from app.ai.runtime_dependencies import ReviewerRuntimeConfig, structured_output_model
from app.ai.workflow_state import ReviewStatus, ReviewerDecision


class ReviewerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safety_passed: bool = Field(description="Whether the draft is safe and ethically cautious.")
    safety_feedback: str = Field(description="Natural-language safety feedback.")


SafetyReviewerOutput = ReviewerOutput


_review_provider_limiter: tuple[int, BoundedSemaphore] | None = None
_REVIEW_PROVIDER_LIMITERS_LOCK = RLock()


def _evaluator_review_provider_limiter() -> BoundedSemaphore | None:
    if os.getenv("REHAB_EVAL_MODE", "").strip().lower() != "true":
        return None
    try:
        concurrency = int(
            os.getenv("REHAB_EVAL_REVIEW_PROVIDER_CONCURRENCY", "1")
        )
    except ValueError:
        return None
    if concurrency <= 0:
        return None

    global _review_provider_limiter
    with _REVIEW_PROVIDER_LIMITERS_LOCK:
        configured = _review_provider_limiter
        if configured is None or configured[0] != concurrency:
            limiter = BoundedSemaphore(concurrency)
            _review_provider_limiter = (concurrency, limiter)
            return limiter
        return configured[1]


class AttemptedReviewerDecision(ReviewerDecision):
    """Reducer value carrying the exact review attempt used to produce it."""

    attempt: int = Field(ge=0)
    review_turn_id: str = Field(default="")
    model_calls: int = Field(default=1, ge=0)
    token_count: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


ReviewerDecisionWithAttempt = AttemptedReviewerDecision


@dataclass(frozen=True)
class AuthorizedEvidencePacket:
    source_id: str
    content: str


@dataclass(frozen=True)
class EvidenceManifest:
    packets: tuple[AuthorizedEvidencePacket, ...] = ()
    catalog_ids: tuple[str, ...] = ()
    catalog_titles: tuple[str, ...] = ()
    unavailable_evidence: tuple[str, ...] = ()
    omitted_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewInput:
    draft: str
    evidence_manifest: EvidenceManifest
    attempt: int
    review_turn_id: str
    context_snapshot: str = ""
    tool_results: tuple[str, ...] = ()


_REVIEW_INPUTS: dict[str, ReviewInput] = {}
_REVIEW_INPUTS_LOCK = RLock()
_MAX_REVIEW_INPUTS = 256


def _current_attempt(state: Mapping[str, Any]) -> int:
    for key in ("review_attempt", "consult_attempts", "attempt"):
        value = state.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    value = state.get("repair_count", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _current_review_turn(state: Mapping[str, Any]) -> str:
    for key in ("review_turn_id", "evidence_turn_id"):
        value = state.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def build_review_input(state: Mapping[str, Any]) -> ReviewInput:
    triaged = active_triaged_context(state.get("evidence_turn_id"))
    context_snapshot = str(
        state.get("consultant_context_snapshot")
        or active_consultant_snapshot(state.get("evidence_turn_id"))
        or ""
    ).strip()
    tool_results = tuple(
        str(record.get("rendered") or "")
        for record in active_tool_records(state.get("evidence_turn_id"))
        if isinstance(record, Mapping) and str(record.get("rendered") or "").strip()
    )
    if not tool_results:
        persisted_tool_results = str(state.get("web_search_summary") or "").strip()
        if persisted_tool_results:
            tool_results = (persisted_tool_results,)
    packets: list[AuthorizedEvidencePacket] = []
    if triaged is not None:
        for packet in getattr(triaged, "packets", ()) or ():
            source_id = str(getattr(packet, "source_id", "")).strip()
            if source_id:
                packets.append(
                    AuthorizedEvidencePacket(
                        source_id=source_id,
                        content=str(getattr(packet, "content", "") or ""),
                    )
                )
        suggestions = getattr(triaged, "catalog_exercise_suggestions", ()) or ()
        unavailable = tuple(
            sorted(
                {
                    str(value).strip()
                    for value in (getattr(triaged, "unavailable_evidence", ()) or ())
                    if str(value).strip()
                }
            )
        )
        omitted = tuple(
            sorted(
                {
                    str(value).strip()
                    for value in (getattr(triaged, "omitted_source_ids", ()) or ())
                    if str(value).strip()
                }
            )
        )
    else:
        suggestions = state.get("catalog_exercise_suggestions", ()) or ()
        unavailable = tuple(
            sorted(
                {
                    str(event.get("tool_name")).strip()
                    for event in (state.get("tool_events", ()) or ())
                    if isinstance(event, Mapping)
                    and event.get("outcome") in {"failed", "timeout", "blocked", "invalid"}
                }
            )
        )
        omitted = ()

    catalog_entries = {
        (
            str(item.get("exercise_id")).strip(),
            str(item.get("title") or item.get("name") or "").strip(),
        )
        for item in suggestions
        if isinstance(item, Mapping) and item.get("exercise_id")
    }
    catalog_ids = tuple(sorted(entry[0] for entry in catalog_entries))
    catalog_titles = tuple(sorted({entry[1] for entry in catalog_entries if entry[1]}))
    return ReviewInput(
        draft=str(state.get("consult_response", state.get("consultant_draft", "")) or ""),
        evidence_manifest=EvidenceManifest(
            packets=tuple(packets),
            catalog_ids=catalog_ids,
            catalog_titles=catalog_titles,
            unavailable_evidence=unavailable,
            omitted_source_ids=omitted,
        ),
        attempt=_current_attempt(state),
        review_turn_id=_current_review_turn(state),
        context_snapshot=context_snapshot,
        tool_results=tool_results,
    )


def create_review_context(state: Mapping[str, Any]) -> str:
    review_id = str(uuid4())
    review_input = build_review_input(state)
    with _REVIEW_INPUTS_LOCK:
        if len(_REVIEW_INPUTS) >= _MAX_REVIEW_INPUTS:
            oldest = next(iter(_REVIEW_INPUTS))
            _REVIEW_INPUTS.pop(oldest, None)
        _REVIEW_INPUTS[review_id] = review_input
    return review_id


def _review_input_for(state: Mapping[str, Any]) -> ReviewInput:
    review_id = state.get("review_context_id")
    if review_id:
        with _REVIEW_INPUTS_LOCK:
            review_input = _REVIEW_INPUTS.get(str(review_id))
        if review_input is None:
            raise KeyError("review context is unavailable")
        return review_input
    direct = state.get("review_input")
    if isinstance(direct, ReviewInput):
        return direct
    return build_review_input(state)


def clear_review_context(review_id: object) -> None:
    if review_id:
        with _REVIEW_INPUTS_LOCK:
            _REVIEW_INPUTS.pop(str(review_id), None)


def dispatch_reviewers(state: Mapping[str, Any]) -> list[Send] | str:
    process_gate_severity = state.get("policy_gate_severity")
    if process_gate_severity != GateSeverity.PASS.value:
        return "unsafe_fallback"
    budget = state.get("budget")
    current_model_calls = state.get("model_calls", 0)
    max_model_calls = getattr(budget, "max_model_calls", None)
    if (
        isinstance(current_model_calls, int)
        and not isinstance(current_model_calls, bool)
        and isinstance(max_model_calls, int)
        and current_model_calls >= max_model_calls
    ):
        return [
            Send(
                "unsafe_fallback",
                {
                    "policy_gate_severity": GateSeverity.HARD_STOP.value,
                    "policy_gate_reason_codes": ["model_budget_exceeded"],
                },
            )
        ]
    review_id = create_review_context(state)
    review_input = _review_input_for({"review_context_id": review_id})
    control = {
        "session_id": state.get("session_id"),
        "trace_turn_id": state.get("trace_turn_id"),
        "review_context_id": review_id,
        "review_attempt": review_input.attempt,
        "review_turn_id": review_input.review_turn_id,
    }
    return [
        Send("safety_reviewer", {"reviewer": "safety", **control}),
        Send("grounding_reviewer", {"reviewer": "grounding", **control}),
    ]


def _manifest_prompt(manifest: EvidenceManifest) -> str:
    lines = ["Authorized evidence manifest:"]
    if manifest.packets:
        for packet in manifest.packets:
            lines.append(f"- Packet {packet.source_id}: {packet.content}")
    else:
        lines.append("- Packets: none")
    lines.append(
        "Validated catalog exercise IDs: "
        + (", ".join(manifest.catalog_ids) if manifest.catalog_ids else "none")
    )
    lines.append(
        "Validated catalog exercise titles: "
        + (", ".join(manifest.catalog_titles) if manifest.catalog_titles else "none")
    )
    unavailable = sorted(
        {*manifest.unavailable_evidence, *manifest.omitted_source_ids}
    )
    lines.append(
        "Evidence gaps: " + (", ".join(unavailable) if unavailable else "none")
    )
    return "\n".join(lines)


def _messages(review_input: ReviewInput, reviewer: Literal["safety", "grounding"]) -> list[object]:
    if reviewer == "safety":
        system = (
            "You are RehabFlow's Safety Reviewer. Assess contraindications, "
            "escalation, unsafe advice, and clinical caution. Judge safety only; "
            "evaluate the draft against the visible patient context and evidence. Do not "
            "reject a deliberately high-level, bounded response merely because it omits "
            "specificity that the evidence does not support. Reject advice that conflicts "
            "with an explicit restriction, misses escalation required by the visible facts, "
            "or introduces a harmful action. Return only the requested structured result "
            "with concise natural-language feedback."
        )
    else:
        system = (
            "You are RehabFlow's Grounding Reviewer. Review the draft against only the frozen "
            "Consultant-visible context and returned tool results. Require direct semantic support "
            "for every material patient fact, diagnosis, exercise choice, exercise instruction, dose, "
            "progression, clearance, treatment action, causal claim, and comparative claim. Reject "
            "invented facts, unsupported clinical inferences, invalid exercise selections, "
            "unavailable citations, and certainty stronger than the evidence. Common "
            "knowledge, plausibility, generic titles, placeholders, and unstated clinical "
            "equivalence are not evidence. Human-readable titles from validated catalog "
            "results are authorized without requiring internal IDs in patient-facing prose. "
            "A concise, non-diagnostic safety-net boundary does not need to repeat source wording "
            "verbatim. It may conditionally tell the patient to pause or stop if symptoms materially "
            "worsen, or to seek care for a severe or new concerning change relevant to the visible "
            "presentation. Treat this as a boundary on the advice, not as evidence of a diagnosis or "
            "as an unsupported treatment claim. Reject specific unsupported thresholds, treatment "
            "details, and unrelated warning checklists. Allow clearly labeled general "
            "education without evidence only when it stays non-individualized and high-level; "
            "do not approve personalized clearance, exact dosing, named exercise selection, "
            "or progression thresholds under that exception. Preserve material qualifiers. "
            "An authorized durable goal or bounded preference may be restated as a goal, "
            "focus, or constraint without requiring an exercise, dose, progression, or "
            "clearance. Do not reject that bounded restatement merely because it does not "
            "provide a complete protocol; reject only unsupported protocol details. "
            "Preserve the latest timing, symptom, goal, allowance, restriction, and lifecycle "
            "state in authorized context. Inactive memory is not current evidence. Do not "
            "infer broader treatment rules from narrow facts. Return only the requested structured result with concise natural-language feedback."
        )
    context_snapshot = review_input.context_snapshot.strip() or "none"
    tool_transcript = "\n\n".join(review_input.tool_results).strip() or "none"
    return [
        SystemMessage(content=system),
        HumanMessage(
            content=(
                "Exact frozen Consultant-visible context and compressed tool transcript:\n"
                f"{context_snapshot or 'none'}\n\n"
                f"Consultant tool calls and returned results:\n{tool_transcript}\n\n"
                f"Consultant final answer:\n{review_input.draft or 'none'}"
            )
        ),
    ]


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    name = type(exc).__name__.lower()
    return name.endswith("timeouterror") or "timeout" in name


class _ReviewerTimeout(asyncio.TimeoutError):
    def __init__(self, attempts: int, original: BaseException) -> None:
        self.attempts = attempts
        self.original_type = type(original).__name__
        super().__init__(str(original) or "review provider timed out")


class _ReviewerInvocationError(RuntimeError):
    def __init__(self, attempts: int, original: BaseException) -> None:
        self.attempts = attempts
        self.original_type = type(original).__name__
        super().__init__(str(original) or "review provider failed")


async def _invoke_with_timeout_counted(
    model: Any,
    schema: type[BaseModel],
    messages: list[object],
    runtime: ReviewerRuntimeConfig,
) -> tuple[object, int]:
    if runtime.max_calls < 1:
        raise _CallBudgetExceeded("reviewer call budget is exhausted")

    limiter = _evaluator_review_provider_limiter()

    async def invoke_once() -> object:
        structured_model = structured_output_model(model, schema)
        sync_invoke = getattr(structured_model, "invoke", None)
        if limiter is not None and callable(sync_invoke):
            return await asyncio.wait_for(
                asyncio.to_thread(sync_invoke, messages),
                timeout=runtime.timeout_seconds,
            )
        ainvoke = getattr(structured_model, "ainvoke", None)
        if callable(ainvoke):
            return await asyncio.wait_for(
                ainvoke(messages),
                timeout=runtime.timeout_seconds,
            )
        return await asyncio.wait_for(
            asyncio.to_thread(structured_model.invoke, messages),
            timeout=runtime.timeout_seconds,
        )

    if limiter is not None:
        while not limiter.acquire(blocking=False):
            await asyncio.sleep(0.01)
    try:
        attempts = 0
        while True:
            attempts += 1
            try:
                return await invoke_once(), attempts
            except Exception as exc:
                if not _is_timeout_error(exc):
                    raise _ReviewerInvocationError(attempts, exc) from exc
                if attempts >= runtime.max_calls:
                    raise _ReviewerTimeout(attempts, exc) from exc
    finally:
        if limiter is not None:
            limiter.release()


async def _invoke_with_timeout(
    model: Any,
    schema: type[BaseModel],
    messages: list[object],
    runtime: ReviewerRuntimeConfig,
) -> object:
    result, _attempts = await _invoke_with_timeout_counted(
        model,
        schema,
        messages,
        runtime,
    )
    return result


class _CallBudgetExceeded(RuntimeError):
    pass


def _render_model_messages(messages: list[object]) -> str:
    return json.dumps(
        [
            {
                "role": str(getattr(message, "type", None) or message.__class__.__name__),
                "content": getattr(message, "content", message),
            }
            for message in messages
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _serialize_model_output(value: object) -> str:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        value = value.model_dump()
    elif hasattr(value, "content"):
        value = getattr(value, "content")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _trace(
    state: Mapping[str, Any],
    *,
    reviewer: str,
    status: ReviewStatus,
    review_input: ReviewInput,
    elapsed_ms: int,
) -> list[dict[str, Any]]:
    trace = list(state.get("debug_trace", []) or [])
    manifest = review_input.evidence_manifest
    trace.append(
        {
            "step": len(trace) + 1,
            "node": f"{reviewer}_reviewer",
            "reviewer": reviewer,
            "status": status.value,
            "attempt": review_input.attempt,
            "review_turn_id": review_input.review_turn_id,
            "elapsed_ms": elapsed_ms,
            "evidence_packet_ids": [packet.source_id for packet in manifest.packets],
            "catalog_ids": list(manifest.catalog_ids),
            "unavailable_evidence_count": len(manifest.unavailable_evidence),
            "omitted_source_count": len(manifest.omitted_source_ids),
        }
    )
    return trace


def _decision(
    *,
    reviewer: Literal["safety", "grounding"],
    status: ReviewStatus,
    feedback: str,
    attempt: int,
    review_turn_id: str,
    model_calls: int = 1,
    token_count: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> AttemptedReviewerDecision:
    return AttemptedReviewerDecision(
        reviewer=reviewer,
        status=status,
        feedback=feedback or "Approval is not available.",
        attempt=attempt,
        review_turn_id=review_turn_id,
        model_calls=model_calls,
        token_count=token_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _response_usage(response: object) -> tuple[int | None, int | None, int | None]:
    for metadata in (
        getattr(response, "usage_metadata", None),
        getattr(response, "response_metadata", None),
        getattr(response, "usage", None),
    ):
        if not isinstance(metadata, Mapping):
            continue
        for candidate in (metadata, metadata.get("token_usage"), metadata.get("usage")):
            if not isinstance(candidate, Mapping):
                continue
            input_tokens = candidate.get("input_tokens", candidate.get("prompt_tokens"))
            output_tokens = candidate.get("output_tokens", candidate.get("completion_tokens"))
            total_tokens = candidate.get("total_tokens")
            input_tokens = input_tokens if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0 else None
            output_tokens = output_tokens if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0 else None
            total_tokens = total_tokens if isinstance(total_tokens, int) and not isinstance(total_tokens, bool) and total_tokens >= 0 else None
            if total_tokens is not None or input_tokens is not None or output_tokens is not None:
                return input_tokens, output_tokens, total_tokens
    return None, None, None


def _response_total_tokens(response: object) -> int | None:
    return _response_usage(response)[2]


async def _run_reviewer(
    state: Mapping[str, Any],
    *,
    reviewer: Literal["safety", "grounding"],
    model: Any,
    runtime: ReviewerRuntimeConfig,
    legacy_grounding_compatibility: bool = False,
) -> dict[str, Any]:
    started = perf_counter()
    try:
        review_input = _review_input_for(state)
    except Exception as exc:
        review_input = ReviewInput(
            draft="",
            evidence_manifest=EvidenceManifest(),
            attempt=_current_attempt(state),
            review_turn_id=_current_review_turn(state),
            context_snapshot=str(state.get("consultant_context_snapshot") or ""),
        )
        status = ReviewStatus.ERROR
        feedback = f"Review failed ({type(exc).__name__}); approval is not available."
        decision = _decision(
            reviewer=reviewer,
            status=status,
            feedback=feedback,
            attempt=review_input.attempt,
            review_turn_id=review_input.review_turn_id,
            model_calls=0,
        )
        return {
            "reviewer_decisions": [decision],
            "reviewer_decision_history": [decision],
            "reviewer_call_counts": {reviewer: 0},
            "debug_trace": _trace(
                state,
                reviewer=reviewer,
                status=status,
                review_input=review_input,
                elapsed_ms=round((perf_counter() - started) * 1000),
            ),
        }

    status = ReviewStatus.ERROR
    passed = False
    feedback = "Approval is not available."
    calls = 0
    token_count: int | None = None
    raw_result: object | None = None
    review_messages = _messages(review_input, reviewer)
    estimated_input_tokens = assert_model_messages_within_limit(review_messages)
    trace_sequence = state.get("trace_sequence")
    if not isinstance(trace_sequence, int) or isinstance(trace_sequence, bool):
        trace_sequence = len(state.get("debug_trace", []) or ()) + 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_type = ""
    try:
        calls = 1
        schema: type[BaseModel]
        if reviewer == "safety" or legacy_grounding_compatibility:
            schema = SafetyReviewerOutput
        else:
            schema = GroundingReviewerOutput
        raw_result, calls = await _invoke_with_timeout_counted(
            model,
            schema,
            review_messages,
            runtime,
        )
        input_tokens, output_tokens, total_tokens = _response_usage(raw_result)
        token_count = total_tokens
        result = raw_result if isinstance(raw_result, schema) else schema.model_validate(raw_result)
        if reviewer == "safety" or legacy_grounding_compatibility:
            passed = result.safety_passed is True
            feedback = result.safety_feedback.strip()
        else:
            passed = result.grounding_passed is True
            feedback = result.grounding_feedback.strip()
        if not feedback:
            raise ValueError("reviewer feedback is blank")
        status = ReviewStatus.APPROVE if passed else ReviewStatus.REJECT
    except _ReviewerTimeout as exc:
        status = ReviewStatus.TIMEOUT
        error_type = exc.original_type
        feedback = "Review timed out; approval is not available."
    except _ReviewerInvocationError as exc:
        calls = exc.attempts
        status = ReviewStatus.ERROR
        error_type = exc.original_type
        feedback = f"Review failed ({exc.original_type}); approval is not available."
    except _CallBudgetExceeded as exc:
        calls = 0
        status = ReviewStatus.ERROR
        error_type = type(exc).__name__
        feedback = f"Review call budget exhausted ({exc}); approval is not available."
    except asyncio.TimeoutError as exc:
        status = ReviewStatus.TIMEOUT
        error_type = type(exc).__name__
        feedback = "Review timed out; approval is not available."
    except Exception as exc:
        calls = max(calls, 1)
        status = ReviewStatus.ERROR
        error_type = type(exc).__name__
        feedback = f"Review failed ({type(exc).__name__}); approval is not available."

    trace_event = {
        "agent_name": f"{reviewer}_reviewer",
        "event_type": "agent_output",
        "content": _serialize_model_output(raw_result if raw_result is not None else feedback),
        "model_input": _render_model_messages(review_messages),
        "metadata": {
            "model_call": bool(calls),
            "reviewer": reviewer,
            "status": status.value,
            "attempt": review_input.attempt,
            "review_turn_id": review_input.review_turn_id,
            "elapsed_ms": round((perf_counter() - started) * 1000),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": token_count,
            "estimated_input_tokens": estimated_input_tokens,
            "usage_unavailable_reason": (
                "provider_did_not_report_complete_usage"
                if input_tokens is None or output_tokens is None or token_count is None
                else ""
            ),
            "status": status.value,
            "turn_id": str(
                state.get("trace_turn_id")
                or review_input.review_turn_id
                or state.get("evidence_turn_id")
                or ""
            ),
            "sequence": trace_sequence,
            "error_type": error_type,
            "retry": calls > 1,
        },
    }
    decision = _decision(
        reviewer=reviewer,
        status=status,
        feedback=feedback,
        attempt=review_input.attempt,
        review_turn_id=review_input.review_turn_id,
        model_calls=calls,
        token_count=token_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return {
        "reviewer_decisions": [decision],
        "reviewer_decision_history": [decision],
        "reviewer_call_counts": {reviewer: calls},
        "review_status": status.value,
        "review_passed": passed,
        "reviewer_feedback": feedback,
        "internal_log_events": [trace_event],
        "debug_trace": _trace(
            state,
            reviewer=reviewer,
            status=status,
            review_input=review_input,
            elapsed_ms=round((perf_counter() - started) * 1000),
        ),
    }


async def safety_reviewer_node(
    state: Mapping[str, Any],
    *,
    model: Any,
    runtime: ReviewerRuntimeConfig,
) -> dict[str, Any]:
    return await _run_reviewer(
        state,
        reviewer="safety",
        model=model,
        runtime=runtime,
    )


async def grounding_reviewer_node(
    state: Mapping[str, Any],
    *,
    model: Any,
    runtime: ReviewerRuntimeConfig,
    legacy_grounding_compatibility: bool = False,
) -> dict[str, Any]:
    return await _run_reviewer(
        state,
        reviewer="grounding",
        model=model,
        runtime=runtime,
        legacy_grounding_compatibility=legacy_grounding_compatibility,
    )


async def run_parallel_reviewers(
    state: Mapping[str, Any],
    *,
    safety_model: Any,
    grounding_model: Any,
    safety_runtime: ReviewerRuntimeConfig,
    grounding_runtime: ReviewerRuntimeConfig,
) -> list[dict[str, Any]]:
    review_input = build_review_input(state)
    working_state = {**dict(state), "review_input": review_input}
    return list(
        await asyncio.gather(
            safety_reviewer_node(
                working_state,
                model=safety_model,
                runtime=safety_runtime,
            ),
            grounding_reviewer_node(
                working_state,
                model=grounding_model,
                runtime=grounding_runtime,
            ),
        )
    )


def _decision_field(raw: object, field: str) -> object:
    if isinstance(raw, Mapping):
        return raw.get(field)
    return getattr(raw, field, None)


def _decision_attempt(raw: object) -> int | None:
    value = _decision_field(raw, "attempt")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _decision_reviewer(raw: object) -> str | None:
    value = _decision_field(raw, "reviewer")
    return value if isinstance(value, str) else None


def _decision_status(raw: object) -> ReviewStatus | None:
    value = _decision_field(raw, "status")
    try:
        return value if isinstance(value, ReviewStatus) else ReviewStatus(value)
    except (TypeError, ValueError):
        return None


def _decision_turn(raw: object) -> str | None:
    value = _decision_field(raw, "review_turn_id")
    return str(value) if value is not None and str(value).strip() else None


_REVIEW_JOIN_REASON_ORDER = {
    "review_decision_invalid": 0,
    "review_decision_untagged": 1,
    "review_decision_stale": 2,
    "review_decision_duplicate": 3,
    "review_decision_missing": 4,
    "review_safety_reject": 10,
    "review_safety_error": 10,
    "review_safety_timeout": 10,
    "review_grounding_reject": 11,
    "review_grounding_error": 11,
    "review_grounding_timeout": 11,
    "review_not_approved": 12,
}


def _canonical_review_reason_codes(reasons: list[str]) -> list[str]:
    return sorted(
        dict.fromkeys(reasons),
        key=lambda reason: (_REVIEW_JOIN_REASON_ORDER.get(reason, 100), reason),
    )


def review_join(state: Mapping[str, Any]) -> dict[str, Any]:
    attempt = _current_attempt(state)
    expected_turn = _current_review_turn(state)
    process_gate_severity = state.get("policy_gate_severity")
    if process_gate_severity != GateSeverity.PASS.value:
        clear_review_context(state.get("review_context_id"))
        reasons = ["process_gate_not_passed"]
        return {
            "review_release": False,
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": reasons,
            "debug_trace": _join_trace(state, reasons=reasons, release=False),
        }

    raw_decisions = list(state.get("reviewer_decisions", []) or [])
    if expected_turn:
        # A checkpoint may retain additive audit values from prior turns. Only
        # the current turn can participate in this release decision.
        raw_decisions = [
            raw for raw in raw_decisions if _decision_turn(raw) == expected_turn
        ]

    reasons: list[str] = []
    decisions: list[object] = []
    for raw in raw_decisions:
        reviewer = _decision_reviewer(raw)
        status = _decision_status(raw)
        decision_attempt = _decision_attempt(raw)
        if reviewer not in {"safety", "grounding"} or status is None:
            reasons.append("review_decision_invalid")
        elif decision_attempt is None:
            reasons.append("review_decision_untagged")
        elif decision_attempt != attempt:
            reasons.append("review_decision_stale")
        else:
            decisions.append(raw)

    counts = {reviewer: 0 for reviewer in ("safety", "grounding")}
    for raw in decisions:
        reviewer = _decision_reviewer(raw)
        if reviewer in counts:
            counts[reviewer] += 1
    if len(decisions) != 2:
        reasons.append("review_decision_missing")
    if any(count > 1 for count in counts.values()):
        reasons.append("review_decision_duplicate")
    if any(count != 1 for count in counts.values()):
        reasons.append("review_decision_missing")
    if reasons:
        reasons = _canonical_review_reason_codes(reasons)
        clear_review_context(state.get("review_context_id"))
        return {
            "review_release": False,
            "review_statuses": [
                _decision_status(next(
                    raw for raw in decisions
                    if _decision_reviewer(raw) == reviewer
                )).value
                for reviewer in ("safety", "grounding")
                if any(_decision_reviewer(raw) == reviewer for raw in decisions)
            ],
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": reasons,
            "debug_trace": _join_trace(state, reasons=reasons, release=False),
        }

    by_reviewer = {
        str(_decision_reviewer(raw)): _decision_status(raw)
        for raw in decisions
    }
    ordered_statuses = [
        by_reviewer[reviewer]
        for reviewer in ("safety", "grounding")
    ]
    non_approvals = [
        f"review_{reviewer}_{by_reviewer[reviewer].value}"
        for reviewer in ("safety", "grounding")
        if by_reviewer[reviewer] is not ReviewStatus.APPROVE
    ]
    clear_review_context(state.get("review_context_id"))

    reviewer_model_calls = 0
    reviewer_input_tokens = 0
    reviewer_output_tokens = 0
    reviewer_usage_complete = True
    reviewer_total_usage_complete = True
    for raw in decisions:
        value = _decision_field(raw, "model_calls")
        reviewer_model_calls += value if isinstance(value, int) and value >= 0 else 1
        token_value = _decision_field(raw, "token_count")
        if not (
            isinstance(token_value, int)
            and not isinstance(token_value, bool)
            and token_value >= 0
        ):
            reviewer_total_usage_complete = False
        input_value = _decision_field(raw, "input_tokens")
        output_value = _decision_field(raw, "output_tokens")
        if (
            isinstance(input_value, int)
            and not isinstance(input_value, bool)
            and input_value >= 0
            and isinstance(output_value, int)
            and not isinstance(output_value, bool)
            and output_value >= 0
        ):
            reviewer_input_tokens += input_value
            reviewer_output_tokens += output_value
        else:
            reviewer_usage_complete = False
    current_model_calls = state.get("model_calls", 0)
    model_calls = (
        current_model_calls if isinstance(current_model_calls, int) else 0
    ) + reviewer_model_calls
    current_input_tokens = state.get("input_tokens")
    input_tokens = (
        current_input_tokens + reviewer_input_tokens
        if reviewer_usage_complete
        and isinstance(current_input_tokens, int)
        and not isinstance(current_input_tokens, bool)
        and current_input_tokens >= 0
        else None
    )
    current_output_tokens = state.get("output_tokens")
    output_tokens = (
        current_output_tokens + reviewer_output_tokens
        if reviewer_usage_complete
        and isinstance(current_output_tokens, int)
        and not isinstance(current_output_tokens, bool)
        and current_output_tokens >= 0
        else None
    )
    current_total_tokens = state.get("total_tokens")
    valid_current_total = (
        isinstance(current_total_tokens, int)
        and not isinstance(current_total_tokens, bool)
        and current_total_tokens >= 0
    )
    provider_totals = [
        _decision_field(raw, "token_count")
        for raw in decisions
        if isinstance(_decision_field(raw, "token_count"), int)
        and not isinstance(_decision_field(raw, "token_count"), bool)
        and _decision_field(raw, "token_count") >= 0
    ]
    if reviewer_total_usage_complete and valid_current_total:
        # Each independent reviewer reports its own provider-accounted call
        # total. Preserve that accounting by adding both deltas to the total
        # accumulated before the parallel review fan-out.
        total_tokens = current_total_tokens + sum(provider_totals)
    else:
        total_tokens = None
    ordered_status_values = [status.value for status in ordered_statuses]
    budget = state.get("budget")
    if non_approvals:
        reason_codes = _canonical_review_reason_codes(
            [*non_approvals, "review_not_approved"]
        )
        return {
            "review_release": False,
            "model_calls": model_calls,
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "review_statuses": ordered_status_values,
            "policy_gate_severity": GateSeverity.REPAIRABLE.value,
            "policy_gate_reason_codes": reason_codes,
            "debug_trace": _join_trace(
                state,
                reasons=reason_codes,
                release=False,
            ),
        }
    deadline = getattr(budget, "deadline_at", None)
    if deadline is not None:
        now = state.get("now") or datetime.now(timezone.utc)
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
            or not isinstance(deadline, datetime)
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
        ):
            reason_codes = ["time_unavailable"]
        elif now >= deadline:
            reason_codes = ["deadline_exceeded"]
        else:
            reason_codes = []
        if reason_codes:
            return {
                "review_release": False,
                "model_calls": model_calls,
                "total_tokens": total_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "review_statuses": ordered_status_values,
                "policy_gate_severity": GateSeverity.HARD_STOP.value,
                "policy_gate_reason_codes": reason_codes,
                "debug_trace": _join_trace(
                    state,
                    reasons=reason_codes,
                    release=False,
                ),
            }
    return {
        "review_release": True,
        "model_calls": model_calls,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "review_statuses": ordered_status_values,
        "final_response": state.get("consult_response", ""),
        "user_input": "",
        "debug_trace": _join_trace(state, reasons=(), release=True),
    }


def _join_trace(
    state: Mapping[str, Any],
    *,
    reasons: list[str],
    release: bool,
) -> list[dict[str, Any]]:
    trace = list(state.get("debug_trace", []) or [])
    trace.append(
        {
            "step": len(trace) + 1,
            "node": "review_join",
            "release": release,
            "review_attempt": _current_attempt(state),
            "review_turn_id": _current_review_turn(state),
            "reason_codes": list(reasons),
        }
    )
    return trace


def route_after_review_join(
    state: Mapping[str, Any],
    *,
    policy: Any | None = None,
) -> str:
    from app.ai.repair import route_after_review_join as route_repair

    return route_repair(state, policy=policy)
