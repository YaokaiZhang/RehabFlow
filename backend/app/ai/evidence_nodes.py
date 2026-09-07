"""Conditional evidence fan-out, bounded execution, and the evidence join."""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from time import perf_counter
from typing import Any, Mapping

from langgraph.types import Send
from app.ai.active_turn_context import (
    active_evidence_query,
    active_evidence_results,
    create_active_turn,
    record_evidence_result,
    store_evidence_query,
)

from app.ai.context_integrity import (
    PUBLIC_REHAB_KNOWLEDGE_SCOPE,
    ContextIntegrity,
    context_integrity_from_state,
)
from app.ai.context_integrity import build_context_integrity
from app.ai.evidence_registry import EvidenceAuthorizationError, EvidenceRegistry
from app.ai.evidence_types import EvidenceRequest, EvidenceResult, EvidenceResultMetadata, EvidenceSource
from app.ai.policy_gate import GateSeverity, evaluate_evidence_admission
from app.ai.workflow_policy import WorkflowPolicy


@dataclass(frozen=True)
class _EvidenceExecutionContext:
    context_integrity: ContextIntegrity


EVIDENCE_WORKER_LIMIT = 4
DEFAULT_EVIDENCE_TIMEOUT_MS = 5_000
DEFAULT_EVIDENCE_MAX_ITEMS = 5

_EVIDENCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=EVIDENCE_WORKER_LIMIT,
    thread_name_prefix="rehab-evidence",
)

_SOURCE_QUERY_LABELS = {
    EvidenceSource.CONVERSATION: "recent conversation context",
    EvidenceSource.PATIENT_MEMORY: "patient memory",
    EvidenceSource.CARE_EPISODE: "active Care Episode context",
    EvidenceSource.REHAB_KNOWLEDGE: "authorized rehabilitation knowledge",
}
_EVIDENCE_QUERY_REFERENCE_PREFIX = "evidence_query_ref:"


def _evidence_query_reference(turn_id: str, request_id: str) -> str:
    return f"{_EVIDENCE_QUERY_REFERENCE_PREFIX}{turn_id}:{request_id}"


def _public_rehab_integrity_for_state(state: Mapping[str, Any]) -> ContextIntegrity:
    """Rebuild only the explicit public scope when private manifests are opaque."""
    return build_context_integrity(
        principal_id="rehabflow-public",
        patient_id="rehabflow-public",
        session_id="rehabflow-public",
        patient_memory_visible=False,
        care_episode_authorized=False,
        source_ids_by_source={
            EvidenceSource.REHAB_KNOWLEDGE: [PUBLIC_REHAB_KNOWLEDGE_SCOPE]
        },
        authorized_source_ids=[PUBLIC_REHAB_KNOWLEDGE_SCOPE],
    )


def _has_authenticated_context_boundary(state: Mapping[str, Any]) -> bool:
    authorized_source_ids = state.get("authorized_source_ids")
    base_boundary = (
        bool(state.get("session_id"))
        and bool(state.get("patient_id") or state.get("principal_id"))
        and state.get("principal_authorized") is True
        and state.get("session_authorized") is True
    )
    if not base_boundary:
        return False
    if isinstance(authorized_source_ids, (list, tuple, set, frozenset)):
        return bool(authorized_source_ids)
    return (
        isinstance(authorized_source_ids, Mapping)
        and "__rehabflow_opaque_ref__" in authorized_source_ids
        and isinstance(state.get("source_ids_by_source"), Mapping)
        and any(
            str(key).startswith("__rehabflow_opaque_ref__")
            for key in state["source_ids_by_source"]
        )
    )

def _explicit_manifest_integrity(state: Mapping[str, Any]) -> ContextIntegrity | None:
    """Build a downstream evidence manifest from an explicit source map.

    This compatibility path is intentionally below the compiled graph context
    integrity gate. It supports direct evidence-node callers that provide the
    complete allow-list but do not carry HTTP preflight flags; opaque checkpoint
    values are never accepted here.
    """
    authorized_source_ids = state.get("authorized_source_ids")
    source_ids_by_source = state.get("source_ids_by_source")
    if not isinstance(authorized_source_ids, (list, tuple, set, frozenset)):
        return None
    if not authorized_source_ids or not isinstance(source_ids_by_source, Mapping):
        return None
    if any(str(key).startswith("__rehabflow_opaque_ref__") for key in source_ids_by_source):
        return None
    normalized: dict[Any, list[Any]] = {}
    for key, values in source_ids_by_source.items():
        if isinstance(values, (str, bytes)):
            return None
        try:
            normalized[key] = list(values)
        except TypeError:
            return None
    patient_id = str(state.get("patient_id") or state.get("principal_id") or "direct-evidence")
    principal_id = str(state.get("principal_id") or patient_id)
    session_id = str(state.get("session_id") or "direct-evidence-session")
    try:
        return build_context_integrity(
            principal_id=principal_id,
            patient_id=patient_id,
            session_id=session_id,
            care_episode_id=(
                str(state["care_episode_id"])
                if state.get("care_episode_id") is not None
                else None
            ),
            care_episode_authorized=state.get("care_episode_authorized") is True,
            source_ids_by_source=normalized,
            authorized_source_ids=authorized_source_ids,
        )
    except Exception:
        return None


def _integrity_for_state(state: Mapping[str, Any]) -> ContextIntegrity:
    cached_integrity = state.get("context_integrity")
    public_rehab_requested = (
        state.get("rehab_knowledge_requested") is True
        and state.get("rehab_knowledge_scope") == PUBLIC_REHAB_KNOWLEDGE_SCOPE
    )
    source_ids_by_source = state.get("source_ids_by_source")
    if isinstance(source_ids_by_source, Mapping):
        try:
            # Resume commands carry a fresh source manifest. Rebuild from it
            # so a stale checkpointed ContextIntegrity cannot hide new scope.
            return context_integrity_from_state(state)
        except Exception:
            # The protected manifests may be unavailable during a same-command
            # resume. Rebuild from the authenticated allow-list only.
            if public_rehab_requested and _has_authenticated_context_boundary(state):
                return _public_rehab_integrity_for_state(state)
            authorized_source_ids = state.get("authorized_source_ids")
            if (
                isinstance(authorized_source_ids, (list, tuple, set))
                and PUBLIC_REHAB_KNOWLEDGE_SCOPE in authorized_source_ids
            ):
                try:
                    return context_integrity_from_state(
                        {**state, "source_ids_by_source": None, "source_ids": []}
                    )
                except Exception:
                    pass
            explicit_integrity = _explicit_manifest_integrity(state)
            if explicit_integrity is not None:
                return explicit_integrity
            if isinstance(cached_integrity, ContextIntegrity):
                return cached_integrity
            raise
    if isinstance(cached_integrity, ContextIntegrity):
        if public_rehab_requested and _has_authenticated_context_boundary(state):
            allowance = cached_integrity.allowance_for(EvidenceSource.REHAB_KNOWLEDGE)
            if allowance is None or PUBLIC_REHAB_KNOWLEDGE_SCOPE not in allowance.source_ids:
                return _public_rehab_integrity_for_state(state)
        return cached_integrity
    try:
        return context_integrity_from_state(state)
    except Exception:
        if public_rehab_requested and _has_authenticated_context_boundary(state):
            return _public_rehab_integrity_for_state(state)
        explicit_integrity = _explicit_manifest_integrity(state)
        if explicit_integrity is not None:
            return explicit_integrity
        raise


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(maximum, max(minimum, value))


def _query_for(state: Mapping[str, Any], source: EvidenceSource) -> str:
    parts = [str(state.get("user_input") or "").strip()]
    query = " | ".join(part for part in parts if part)
    if not query:
        query = "current rehabilitation turn"
    label = _SOURCE_QUERY_LABELS[source]
    return f"{label}: {query}"[:4096]


def build_evidence_requests(state: Mapping[str, Any]) -> list[EvidenceRequest]:
    """Build only conditional, source-authorized requests for this turn."""
    if state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value:
        return []

    try:
        integrity = _integrity_for_state(state)
    except Exception:
        return []

    max_items = _bounded_int(
        state.get("evidence_max_items"),
        default=DEFAULT_EVIDENCE_MAX_ITEMS,
        minimum=1,
        maximum=50,
    )
    timeout_ms = _bounded_int(
        state.get("evidence_timeout_ms"),
        default=DEFAULT_EVIDENCE_TIMEOUT_MS,
        minimum=1,
        maximum=30_000,
    )
    requests: list[EvidenceRequest] = []

    for source in (
        EvidenceSource.CONVERSATION,
        EvidenceSource.PATIENT_MEMORY,
        EvidenceSource.CARE_EPISODE,
        EvidenceSource.REHAB_KNOWLEDGE,
    ):
        allowance = integrity.allowance_for(source)
        care_episode_id = integrity.identity.care_episode_id
        if source is EvidenceSource.CARE_EPISODE and care_episode_id:
            # Keep an unauthorized episode request visible to the join so it
            # becomes a deterministic forbidden hard stop rather than silently
            # looking like an optional omission.
            source_ids = list(allowance.source_ids) if allowance is not None else []
            marker = f"care_episode:{care_episode_id}"
            if marker not in source_ids:
                source_ids.insert(0, marker)
        elif allowance is None or not allowance.visible:
            continue
        else:
            source_ids = list(allowance.source_ids)

        if allowance is None and source is not EvidenceSource.CARE_EPISODE:
            continue
        if not source_ids and source is EvidenceSource.REHAB_KNOWLEDGE:
            # Public Rehab Knowledge is authorized by an explicit scope marker;
            # an empty allowance remains fail-closed.
            if (
                state.get("rehab_knowledge_requested") is not True
                or allowance is None
                or PUBLIC_REHAB_KNOWLEDGE_SCOPE not in allowance.source_ids
            ):
                continue
        elif not source_ids and source is not EvidenceSource.CARE_EPISODE:
            continue

        requests.append(
            EvidenceRequest(
                request_id=f"evidence:{source.value}",
                source=source,
                query=_query_for(state, source),
                source_ids=source_ids,
                max_items=max_items,
                timeout_ms=timeout_ms,
            )
        )

    budget = state.get("budget")
    max_tool_calls = getattr(budget, "max_tool_calls", None)
    current_tool_calls = state.get("tool_calls", 0)
    if isinstance(max_tool_calls, int) and isinstance(current_tool_calls, int):
        available = max_tool_calls - current_tool_calls
        if available <= 0:
            return []
        critical_episode = next(
            (
                request
                for request in requests
                if request.source is EvidenceSource.CARE_EPISODE
            ),
            None,
        )
        selected = requests[:available]
        if critical_episode is not None and critical_episode not in selected:
            selected = [critical_episode, *selected][:available]
        requests = selected
    return requests


def shared_control(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the non-packet control fields copied into each Send payload."""
    control_keys = (
        "session_id",
        "patient_id",
        "care_episode_id",
        "trace_turn_id",
        "workflow_version",
        "evidence_turn_id",
        "rehab_knowledge_requested",
        "rehab_knowledge_scope",
        "principal_authorized",
        "session_authorized",
        "care_episode_authorized",
        "authorized_source_ids",
        "source_ids",
        "source_ids_by_source",
        "evidence_timeout_ms",
        "evidence_max_items",
        "budget",
        "model_calls",
        "tool_calls",
        "total_tokens",
        "policy_gate_severity",
        "policy_gate_reason_codes",
        "now",
    )
    control = {key: state[key] for key in control_keys if key in state}
    control.setdefault("evidence_turn_id", state.get("evidence_turn_id") or create_active_turn(str(state.get("session_id") or "") or None))
    if "rehab_knowledge_requested" in state:
        control["rehab_knowledge_requested"] = state["rehab_knowledge_requested"]
    try:
        control["context_integrity"] = _integrity_for_state(state)
    except Exception:
        pass
    return control


def dispatch_evidence(state: Mapping[str, Any]) -> list[Send]:
    """Fan out authorized requests with LangGraph Send.

    An empty plan goes directly to the join. A single request remains a single
    branch; the graph does not manufacture a parallel task for it.
    """
    turn_id = state.get("evidence_turn_id") or create_active_turn(str(state.get("session_id") or "") or None)
    working_state = {**dict(state), "evidence_turn_id": turn_id}
    requests = build_evidence_requests(working_state)
    control = shared_control(working_state)
    if not requests:
        return [Send("evidence_join", control)]

    # EvidenceRequest.query is needed by the retrieval adapter, but raw patient
    # text must not cross the graph branch/checkpoint boundary. Keep the query
    # in the bounded active-turn store and dispatch only its request reference.
    branch_requests: list[EvidenceRequest] = []
    for request in requests:
        store_evidence_query(turn_id, request.request_id, request.query)
        branch_requests.append(
            request.model_copy(
                update={
                    "query": _evidence_query_reference(str(turn_id), request.request_id)
                }
            )
        )
    return [
        Send(
            "gather_evidence",
            {"evidence_request": request, **control},
        )
        for request in branch_requests
    ]


def _status_result(
    request: EvidenceRequest,
    status: str,
    started: float,
    *,
    reason_code: str | None = None,
) -> EvidenceResult:
    return EvidenceResult(
        request_id=request.request_id,
        source=request.source,
        status=status,  # type: ignore[arg-type]
        packets=[],
        elapsed_ms=max(0, round((perf_counter() - started) * 1000)),
        reason_code=reason_code,
    )


def _run_blocking_request(
    request: EvidenceRequest,
    *,
    registry: EvidenceRegistry,
    integrity: ContextIntegrity,
    catalog_validator: Any | None,
) -> EvidenceResult:
    started = perf_counter()
    context = _EvidenceExecutionContext(integrity)
    try:
        registry.plan([request], integrity)
    except EvidenceAuthorizationError:
        return _status_result(request, "forbidden", started)
    except Exception:
        return _status_result(request, "error", started)

    try:
        adapter = registry.adapter_for(request.source)
    except EvidenceAuthorizationError:
        return _status_result(request, "forbidden", started)
    except Exception:
        return _status_result(request, "error", started)

    try:
        result = adapter.fetch(request, context=context)
        result = result if isinstance(result, EvidenceResult) else EvidenceResult.model_validate(result)
        if result.request_id != request.request_id or result.source != request.source:
            return _status_result(request, "error", started)

        return result
    except TimeoutError:
        return _status_result(request, "timeout", started)
    except Exception:
        return _status_result(request, "error", started)


async def gather_evidence(
    state: Mapping[str, Any],
    *,
    registry: EvidenceRegistry | None = None,
    catalog_validator: Any | None = None,
) -> EvidenceResult:
    """Execute one branch without sharing blocking resources with siblings."""
    raw_request = state.get("evidence_request")
    started = perf_counter()
    try:
        request = raw_request if isinstance(raw_request, EvidenceRequest) else EvidenceRequest.model_validate(raw_request)
    except Exception:
        return EvidenceResult(
            request_id="invalid-evidence-request",
            source=EvidenceSource.CONVERSATION,
            status="error",
            packets=[],
            elapsed_ms=max(0, round((perf_counter() - started) * 1000)),
        )

    query_reference = request.query
    if query_reference.startswith(_EVIDENCE_QUERY_REFERENCE_PREFIX):
        reference = query_reference[len(_EVIDENCE_QUERY_REFERENCE_PREFIX):]
        reference_turn_id, separator, reference_request_id = reference.partition(":")
        if not separator or reference_request_id != request.request_id:
            return _status_result(
                request,
                "error",
                started,
                reason_code="evidence_query_unavailable",
            )
        query = active_evidence_query(
            reference_turn_id,
            reference_request_id,
        )
        if not isinstance(query, str) or not query.strip():
            return _status_result(
                request,
                "error",
                started,
                reason_code="evidence_query_unavailable",
            )
        request = request.model_copy(update={"query": query})

    try:
        integrity = _integrity_for_state(state)
    except Exception:
        return _status_result(request, "forbidden", started)

    active_registry = registry or state.get("evidence_registry")
    if not isinstance(active_registry, EvidenceRegistry):
        return _status_result(request, "error", started)

    policy = state.get("policy")
    active_policy = policy if isinstance(policy, WorkflowPolicy) else WorkflowPolicy()
    admission = evaluate_evidence_admission(
        {
            **dict(state),
            "now": state.get("now") or datetime.now(timezone.utc),
        },
        policy=active_policy,
    )
    if admission.severity is GateSeverity.HARD_STOP:
        return _status_result(
            request,
            "error",
            started,
            reason_code=admission.reason_codes[0],
        )

    timeout_ms = request.timeout_ms
    loop = asyncio.get_running_loop()
    blocking_call = partial(
        _run_blocking_request,
        request,
        registry=active_registry,
        integrity=integrity,
        catalog_validator=catalog_validator,
    )
    future = loop.run_in_executor(_EVIDENCE_EXECUTOR, blocking_call)
    try:
        return await asyncio.wait_for(future, timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        future.cancel()
        return _status_result(request, "timeout", started)
    except Exception:
        return _status_result(request, "error", started)


async def execute_evidence_requests(
    state: Mapping[str, Any],
    requests: list[EvidenceRequest],
    *,
    registry: EvidenceRegistry,
    catalog_validator: Any | None = None,
) -> list[EvidenceResult]:
    """Run one request directly and multiple requests concurrently."""
    if not requests:
        return []
    payloads = [
        {"evidence_request": request, **shared_control(state)}
        for request in requests
    ]
    if len(payloads) == 1:
        return [
            await gather_evidence(
                payloads[0],
                registry=registry,
                catalog_validator=catalog_validator,
            )
        ]
    return list(
        await asyncio.gather(
            *(
                gather_evidence(
                    payload,
                    registry=registry,
                    catalog_validator=catalog_validator,
                )
                for payload in payloads
            )
        )
    )


def _trace(state: Mapping[str, Any], details: Mapping[str, Any]) -> list[dict[str, Any]]:
    trace = list(state.get("debug_trace", []) or [])
    trace.append({"step": len(trace) + 1, "node": "evidence_join", **dict(details)})
    return trace


def join_evidence_results(state: Mapping[str, Any]) -> dict[str, Any]:
    """Require the exact dispatched result set without retaining packet bodies."""
    turn_id = state.get("evidence_turn_id")
    raw_results = active_evidence_results(turn_id)
    if not raw_results and not turn_id:
        # Narrow compatibility seam for pre-graph unit callers. Production
        # graph state carries metadata only and reads bodies from active memory.
        raw_results = [
            value
            for value in list(state.get("evidence_results", []) or [])
            if isinstance(value, EvidenceResult)
        ]

    results: list[EvidenceResult] = []
    for raw_result in raw_results:
        try:
            results.append(
                raw_result
                if isinstance(raw_result, EvidenceResult)
                else EvidenceResult.model_validate(raw_result)
            )
        except Exception:
            return {
                "policy_gate_severity": GateSeverity.HARD_STOP.value,
                "policy_gate_reason_codes": ["evidence_result_invalid"],
                "debug_trace": _trace(
                    state,
                    {"hard_stop": True, "reason": "evidence_result_invalid"},
                ),
            }

    expected = {request.request_id: request for request in build_evidence_requests(state)}
    received_ids = [result.request_id for result in results]
    received_counts = Counter(received_ids)
    duplicate_requests = sorted(
        request_id for request_id, count in received_counts.items() if count > 1
    )
    unexpected_requests = sorted(set(received_ids) - set(expected))
    source_mismatches = sorted(
        result.request_id
        for result in results
        if result.request_id in expected
        and result.source is not expected[result.request_id].source
    )
    received = {result.request_id: result for result in results}
    missing = sorted(set(expected) - set(received))
    forbidden = sorted(
        result.request_id for result in results if result.status == "forbidden"
    )
    episode_request_ids = {
        request.request_id
        for request in expected.values()
        if request.source is EvidenceSource.CARE_EPISODE
    }
    episode_unavailable = sorted(
        result.request_id
        for result in results
        if result.request_id in episode_request_ids
        and result.status in {"empty", "error", "timeout", "forbidden"}
    )
    hard_stop_reasons: list[str] = []
    budget = state.get("budget")
    max_tool_calls = getattr(budget, "max_tool_calls", None)
    current_tool_calls = state.get("tool_calls", 0)
    if (
        isinstance(max_tool_calls, int)
        and isinstance(current_tool_calls, int)
        and current_tool_calls >= max_tool_calls
    ):
        hard_stop_reasons.append("tool_budget_exceeded")
    if missing:
        hard_stop_reasons.append("evidence_branch_missing")
    if episode_request_ids.intersection(missing) or episode_unavailable:
        hard_stop_reasons.append("evidence_episode_unavailable")
    if unexpected_requests:
        hard_stop_reasons.append("evidence_result_unexpected")
    if duplicate_requests:
        hard_stop_reasons.append("evidence_result_duplicate")
    if source_mismatches:
        hard_stop_reasons.append("evidence_result_source_mismatch")
    if forbidden:
        hard_stop_reasons.append("evidence_source_forbidden")

    statuses = {
        result.request_id: {
            "source": result.source.value,
            "status": result.status,
            "packet_count": len(result.packets),
            **({"reason_code": result.reason_code} if result.reason_code else {}),
        }
        for result in sorted(results, key=lambda item: (item.request_id, item.source.value))
    }
    budget_reason_codes = sorted(
        {
            result.reason_code
            for result in results
            if result.reason_code
            in {
                "deadline_exceeded",
                "model_budget_exceeded",
                "tool_budget_exceeded",
                "time_unavailable",
                "budget_missing",
            }
        }
    )
    hard_stop_reasons.extend(f"evidence_{reason}" for reason in budget_reason_codes)
    evidence_tool_calls = sum(
        1 for result in results if result.reason_code is None
    )
    if hard_stop_reasons:
        return {
            "tool_calls": int(state.get("tool_calls", 0) or 0) + evidence_tool_calls,
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": sorted(set(hard_stop_reasons)),
            "debug_trace": _trace(
                state,
                {
                    "hard_stop": True,
                    "missing_requests": missing,
                    "unexpected_requests": unexpected_requests,
                    "duplicate_requests": duplicate_requests,
                    "source_mismatches": source_mismatches,
                    "episode_unavailable": episode_unavailable,
                    "forbidden_requests": forbidden,
                    "statuses": statuses,
                },
            ),
        }

    return {
        "tool_calls": int(state.get("tool_calls", 0) or 0) + evidence_tool_calls,
        "debug_trace": _trace(
            state,
            {
                "hard_stop": False,
                "statuses": statuses,
                "optional_unavailable": sorted(
                    f"{result.source.value}:{result.status}"
                    for result in results
                    if result.status in {"empty", "timeout", "error"}
                ),
            },
        ),
    }


def route_after_evidence_join(state: Mapping[str, Any]) -> str:
    if state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value:
        return "unsafe_fallback"
    return "context_triage"
