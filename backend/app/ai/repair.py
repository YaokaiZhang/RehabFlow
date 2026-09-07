"""Bounded natural-language repair for consultant drafts."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from time import perf_counter
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage

from app.ai.policy_gate import GateResult, GateSeverity, evaluate_invocation_admission
from app.ai.runtime_dependencies import with_responses_provider_retry
from app.ai.workflow_policy import WorkflowPolicy
from app.ai.workflow_state import ReviewStatus, ReviewerDecision, replace_reviewer_decisions


logger = logging.getLogger(__name__)

REPAIR_NODE = "consultant_repair"
REVALIDATE_NODE = "repair_revalidate_context"
MAX_REPAIR_INSTRUCTION_CHARS = 12_000
_RESERVED_REVIEWER_CALLS = 4
_HARD_POLICY_REASONS = frozenset(
    {
        "principal_unauthorized",
        "session_unauthorized",
        "care_episode_unauthorized",
        "workflow_version_unsupported",
        "source_id_forbidden",
        "tool_name_forbidden",
        "catalog_id_forbidden",
        "model_budget_exceeded",
        "tool_budget_exceeded",
        "deadline_exceeded",
        "repair_budget_exhausted",
    }
)
_TOKEN_TELEMETRY_REASONS = frozenset(
    {"token_budget_exceeded", "token_usage_unavailable"}
)


def is_token_telemetry_only_hard_stop(state: Mapping[str, Any]) -> bool:
    """Treat legacy token telemetry reasons as non-blocking control metadata."""
    if state.get("policy_gate_severity") != GateSeverity.HARD_STOP.value:
        return False
    reasons = {str(value) for value in state.get("policy_gate_reason_codes", ()) or ()}
    return bool(reasons) and reasons <= _TOKEN_TELEMETRY_REASONS


_REQUIRED_CONTEXT_IDENTITY_FIELDS = (
    "principal_id",
    "patient_id",
    "session_id",
    "workflow_version",
)
_REQUIRED_FRESHNESS_SNAPSHOT_FIELDS = (
    "context_version",
    "context_fingerprint",
    "source_versions",
    "allowance_versions",
)


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _valid_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _current_attempt(decisions: Sequence[object]) -> int | None:
    attempts = [
        _field(value, "attempt")
        for value in decisions
        if _valid_nonnegative_int(_field(value, "attempt"))
    ]
    return max(attempts) if attempts else None


def build_repair_instruction(
    draft: str,
    decisions: Sequence[ReviewerDecision],
) -> str:
    """Combine current failed reviewer feedback into a bounded prose prompt."""
    current_attempt = _current_attempt(decisions)
    feedback: list[tuple[str, str]] = []
    for decision in decisions:
        attempt = _field(decision, "attempt")
        if current_attempt is not None and attempt != current_attempt:
            continue
        status = _field(decision, "status")
        try:
            status = status if isinstance(status, ReviewStatus) else ReviewStatus(status)
        except (TypeError, ValueError):
            continue
        if status is ReviewStatus.APPROVE:
            continue
        reviewer = str(_field(decision, "reviewer") or "reviewer").strip()
        text = " ".join(str(_field(decision, "feedback") or "").split())
        if text:
            feedback.append((reviewer, text[:3_000]))

    draft_text = " ".join(str(draft or "").split())[:8_000]
    feedback_text = " ".join(
        f"The {reviewer} reviewer said: {text}"
        for reviewer, text in feedback
    )
    if not feedback_text:
        feedback_text = "The reviewers did not approve the draft; reassess it conservatively."
    return (
        "Revise the consultant answer below into a new standalone rehabilitation response. "
        "Address the reviewer feedback directly, use only the authorized context already supplied, "
        "and keep the answer safe, appropriately uncertain, and natural language. "
        f"Previous consultant answer: {draft_text or 'none'}. "
        f"Reviewer feedback: {feedback_text}"
    )[:MAX_REPAIR_INSTRUCTION_CHARS]


def _stable_version(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode()).hexdigest()


def _effective_context_identity(state: Mapping[str, Any]) -> Mapping[str, object]:
    identity = state.get("context_identity")
    if (
        isinstance(identity, Mapping)
        and _complete_context_identity(identity, state.get("workflow_version"))
    ):
        return identity
    integrity = state.get("context_integrity")
    integrity_identity = getattr(integrity, "identity", None)
    if integrity_identity is not None:
        model_dump = getattr(integrity_identity, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return dumped
    workflow_version = state.get("workflow_version")
    principal_id = state.get("principal_id") or state.get("patient_id")
    patient_id = state.get("patient_id") or principal_id
    session_id = state.get("session_id")
    if all(_nonblank_string(value) for value in (principal_id, patient_id, session_id, workflow_version)):
        return {
            "principal_id": str(principal_id),
            "patient_id": str(patient_id),
            "session_id": str(session_id),
            "care_episode_id": state.get("care_episode_id"),
            "workflow_version": str(workflow_version),
        }
    return {}


def _effective_source_allowances(state: Mapping[str, Any]) -> Mapping[str, object]:
    allowances = state.get("source_allowances")
    if (
        isinstance(allowances, Mapping)
        and _complete_source_allowances(allowances) is not None
    ):
        return allowances
    integrity = state.get("context_integrity")
    allowance_metadata = getattr(integrity, "allowance_metadata", None)
    if callable(allowance_metadata):
        dumped = allowance_metadata()
        if isinstance(dumped, Mapping) and dumped:
            return dumped
    source_map = state.get("source_ids_by_source")
    if not isinstance(source_map, Mapping):
        return {}
    source_versions = state.get("evidence_source_versions")
    scopes = {
        "conversation": "session",
        "patient_memory": "patient",
        "care_episode": "episode",
        "rehab_knowledge": "public_knowledge",
    }
    rebuilt: dict[str, object] = {}
    for raw_source, raw_ids in source_map.items():
        source = str(getattr(raw_source, "value", raw_source))
        if source not in scopes or not isinstance(raw_ids, (list, tuple, set)):
            continue
        ids = [str(value).strip() for value in raw_ids if str(value).strip()]
        version = (
            source_versions.get(source)
            if isinstance(source_versions, Mapping)
            else None
        ) or f"{source}:v1"
        rebuilt[source] = {
            "source_ids": ids,
            "scope": scopes[source],
            "version": str(version),
            "visible": True,
        }
    return rebuilt


def _source_versions(state: Mapping[str, Any]) -> dict[str, str]:
    explicit = state.get("evidence_source_versions") or state.get("evidence_versions")
    if isinstance(explicit, Mapping) and explicit:
        return {
            str(key): str(value)
            for key, value in explicit.items()
            if str(key).strip() and str(value).strip()
        }
    allowances = _effective_source_allowances(state)
    if not isinstance(allowances, Mapping):
        return {}
    return {
        str(source): str(metadata.get("version"))
        for source, metadata in allowances.items()
        if isinstance(metadata, Mapping) and metadata.get("version") is not None
    }


def _allowance_versions(state: Mapping[str, Any]) -> dict[str, str]:
    allowances = _effective_source_allowances(state)
    if not isinstance(allowances, Mapping):
        return {}
    return {
        str(source): str(metadata.get("version"))
        for source, metadata in allowances.items()
        if isinstance(metadata, Mapping) and metadata.get("version") is not None
    }


def _nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _complete_version_map(
    value: object,
    *,
    required_keys: set[str] | None = None,
) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    normalized: dict[str, str] = {}
    for raw_key, raw_version in value.items():
        if not _nonblank_string(raw_key) or not _nonblank_string(raw_version):
            return None
        normalized[raw_key] = raw_version
    if required_keys is not None and set(normalized) != required_keys:
        return None
    return normalized


def _complete_context_identity(value: object, workflow_version: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if any(not _nonblank_string(value.get(field)) for field in _REQUIRED_CONTEXT_IDENTITY_FIELDS):
        return False
    return value.get("workflow_version") == workflow_version


def _complete_source_allowances(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    versions: dict[str, str] = {}
    for raw_source, metadata in value.items():
        if not _nonblank_string(raw_source) or not isinstance(metadata, Mapping):
            return None
        source_ids = metadata.get("source_ids")
        if (
            not isinstance(source_ids, list)
            or any(not _nonblank_string(source_id) for source_id in source_ids)
            or not _nonblank_string(metadata.get("scope"))
            or not _nonblank_string(metadata.get("version"))
            or not isinstance(metadata.get("visible"), bool)
        ):
            return None
        versions[raw_source] = metadata["version"]
    return versions


def _current_freshness_metadata(state: Mapping[str, Any]) -> dict[str, object] | None:
    workflow_version = state.get("workflow_version")
    identity = _effective_context_identity(state)
    allowances = _effective_source_allowances(state)
    allowance_versions = _complete_source_allowances(allowances)
    context_version = state.get("context_version")
    if not _nonblank_string(context_version):
        context_version = context_version_snapshot(state)["context_version"]
    if (
        not _nonblank_string(workflow_version)
        or not _complete_context_identity(identity, workflow_version)
        or not _nonblank_string(context_version)
        or allowance_versions is None
    ):
        return None
    source_versions = _complete_version_map(
        state.get("evidence_source_versions"),
        required_keys=set(allowance_versions),
    )
    if source_versions is None:
        return None
    return {
        "context_version": context_version,
        "source_versions": source_versions,
        "allowance_versions": allowance_versions,
    }


def context_version_snapshot(state: Mapping[str, Any]) -> dict[str, object]:
    """Return identifier/version metadata only; packet bodies are never copied."""
    identity = _effective_context_identity(state)
    allowances = _effective_source_allowances(state)
    context_fingerprint = _stable_version(
        {
            "identity": identity,
            "allowances": allowances,
            "workflow_version": state.get("workflow_version"),
        }
    )
    explicit_context_version = state.get("context_version")
    context_version = (
        str(explicit_context_version)
        if explicit_context_version is not None
        else context_fingerprint
    )
    return {
        "context_version": context_version,
        "context_fingerprint": context_fingerprint,
        "source_versions": _source_versions(state),
        "allowance_versions": _allowance_versions(state),
    }


def context_versions_current(state: Mapping[str, Any]) -> bool:
    """Check whether the evidence snapshot can safely be reused for repair."""
    if state.get("force_revalidate_context") is True:
        return False
    current_metadata = _current_freshness_metadata(state)
    if current_metadata is None:
        return False
    snapshot = state.get("repair_context_snapshot") or state.get("evidence_context_snapshot")
    if not isinstance(snapshot, Mapping):
        return False
    if any(field not in snapshot for field in _REQUIRED_FRESHNESS_SNAPSHOT_FIELDS):
        return False
    current = context_version_snapshot(state)
    expected_context = snapshot.get("context_version")
    if not _nonblank_string(expected_context):
        return False
    expected_fingerprint = snapshot.get("context_fingerprint")
    if not _nonblank_string(expected_fingerprint):
        return False
    expected_sources = _complete_version_map(
        snapshot.get("source_versions"),
        required_keys=set(current_metadata["source_versions"]),
    )
    expected_allowance_versions = _complete_version_map(
        snapshot.get("allowance_versions"),
        required_keys=set(current_metadata["allowance_versions"]),
    )
    if expected_sources is None or expected_allowance_versions is None:
        return False
    if str(expected_context) != str(current["context_version"]):
        return False
    if str(expected_fingerprint) != str(current["context_fingerprint"]):
        return False
    if expected_sources != dict(current["source_versions"]):
        return False
    if expected_allowance_versions != dict(current["allowance_versions"]):
        return False
    return True


def evaluate_repair_admission(
    state: Mapping[str, Any],
    *,
    policy: WorkflowPolicy,
) -> GateResult:
    """Fail closed before the single repair model call consumes budget."""
    policy_reasons = set(state.get("policy_gate_reason_codes") or ())
    if (
        (
            state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value
            and not is_token_telemetry_only_hard_stop(state)
        )
        or policy_reasons & _HARD_POLICY_REASONS
    ):
        return GateResult(
            severity=GateSeverity.HARD_STOP,
            reason_codes=list(state.get("policy_gate_reason_codes") or ()) or ["policy_hard_stop"],
        )
    repair_count = state.get("repair_count", 0)
    if not _valid_nonnegative_int(repair_count):
        return GateResult(severity=GateSeverity.HARD_STOP, reason_codes=["repair_count_invalid"])
    if not policy.repair_is_allowed(repair_count):
        return GateResult(severity=GateSeverity.HARD_STOP, reason_codes=["repair_budget_exhausted"])
    budget_result = evaluate_invocation_admission(
        {
            "budget": state.get("budget"),
            "model_calls": state.get("model_calls", 0),
            "tool_calls": state.get("tool_calls", 0),
            # Provider usage is optional for reviewers. Repair admission is
            # bounded by model/tool slots and deadline; unknown token
            # telemetry must not discard the reviewer feedback.
            "total_tokens": 0,
            "now": state.get("now") or datetime.now(timezone.utc),
        },
        policy=policy,
        invocation="model",
    )
    if budget_result.severity is GateSeverity.HARD_STOP:
        return budget_result
    budget = state.get("budget")
    current_tool_calls = state.get("tool_calls", 0)
    max_tool_calls = getattr(budget, "max_tool_calls", None)
    if (
        not _valid_nonnegative_int(current_tool_calls)
        or not isinstance(max_tool_calls, int)
        or isinstance(max_tool_calls, bool)
        or current_tool_calls >= max_tool_calls
    ):
        return GateResult(
            severity=GateSeverity.HARD_STOP,
            reason_codes=["tool_budget_exceeded"],
        )
    current_model_calls = state.get("model_calls", 0)
    max_model_calls = getattr(budget, "max_model_calls", None)
    if (
        not _valid_nonnegative_int(current_model_calls)
        or not isinstance(max_model_calls, int)
        or isinstance(max_model_calls, bool)
        or current_model_calls + 1 + _RESERVED_REVIEWER_CALLS > max_model_calls
    ):
        return GateResult(
            severity=GateSeverity.HARD_STOP,
            reason_codes=["model_budget_exceeded"],
        )
    return GateResult(severity=GateSeverity.PASS, reason_codes=["repair_invocation_admitted"])


def _response_content(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        return " ".join(
            str(item.get("text") if isinstance(item, Mapping) else item)
            for item in content
        ).strip()
    return str(content or "").strip()


def _response_tokens(response: object) -> int | None:
    for metadata in (
        getattr(response, "usage_metadata", None),
        getattr(response, "response_metadata", None),
        getattr(response, "usage", None),
    ):
        if not isinstance(metadata, Mapping):
            continue
        candidates = (metadata, metadata.get("token_usage"), metadata.get("usage"))
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                value = candidate.get("total_tokens")
                if _valid_nonnegative_int(value):
                    return value
    return None


def _trace(state: Mapping[str, Any], node: str, **details: object) -> list[dict[str, object]]:
    trace = list(state.get("debug_trace", []) or ())
    trace.append({"step": len(trace) + 1, "node": node, **details})
    return trace


def _render_model_messages(messages: Sequence[object]) -> str:
    return json.dumps(
        [
            {
                "role": str(getattr(message, "type", None) or message.__class__.__name__),
                "content": str(getattr(message, "content", message)),
            }
            for message in messages
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _internal_event(
    state: Mapping[str, Any],
    response: str,
    *,
    model_input: str = "",
    metadata: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    trace_metadata = {
        "model_call": True,
        "status": "completed",
        "repair_count": state.get("repair_count", 0) + 1,
        **dict(metadata or {}),
    }
    turn_id = state.get("trace_turn_id") or state.get("evidence_turn_id") or state.get("review_turn_id")
    if turn_id:
        trace_metadata.setdefault("turn_id", str(turn_id))
    trace_metadata.setdefault("sequence", len(state.get("debug_trace", []) or ()) + 1)
    if any(trace_metadata.get(key) is None for key in ("input_tokens", "output_tokens", "total_tokens")):
        trace_metadata.setdefault(
            "usage_unavailable_reason",
            "provider_did_not_report_complete_usage",
        )
    return [
        {
            "agent_name": REPAIR_NODE,
            "event_type": "agent_output",
            "content": response,
            "model_input": model_input,
            "metadata": trace_metadata,
        }
    ]


def consultant_repair_node(state: Mapping[str, Any], deps: Any) -> dict[str, Any]:
    """Perform exactly one natural-language consultant repair, without tools."""
    policy = deps.policy if isinstance(getattr(deps, "policy", None), WorkflowPolicy) else WorkflowPolicy()
    admission = evaluate_repair_admission(state, policy=policy)
    if admission.severity is GateSeverity.HARD_STOP:
        return {
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": admission.reason_codes,
            "reviewer_decisions": replace_reviewer_decisions(),
            "debug_trace": _trace(
                state,
                REPAIR_NODE,
                action="bypassed",
                reason_codes=admission.reason_codes,
            ),
        }

    if not context_versions_current(state):
        return {
            "repair_requires_revalidation": True,
            "reviewer_decisions": replace_reviewer_decisions(),
            "debug_trace": _trace(state, REPAIR_NODE, action="revalidate_context"),
        }

    draft = str(state.get("consult_response") or state.get("consultant_draft") or "")
    instruction = build_repair_instruction(draft, list(state.get("reviewer_decisions", []) or ()))
    authorized_context = str(state.get("consultant_context_snapshot") or "").strip()
    if authorized_context:
        instruction = (
            "Frozen authorized Consultant-visible context; use it only as evidence and do not add causal interpretations:\n"
            f"{authorized_context}\n\n{instruction}"
        )
    returned_tool_results = str(state.get("web_search_summary") or "").strip()
    if returned_tool_results:
        instruction = (
            f"{instruction}\n\nReturned tool results from the reviewed Consultant attempt; "
            "preserve their evidence boundary during repair:\n"
            f"{returned_tool_results}"
        )
    repair_messages = [
        SystemMessage(
            content=(
                "You are the RehabFlow consultant repair action. Return only a revised "
                "natural-language rehabilitation response. Do not return JSON, issue codes, "
                "or a typed review list. Revise against the current reviewer feedback using "
                "only the frozen authorized context and returned tool results supplied in the "
                "repair request. Preserve supported patient facts and material qualifiers. "
                "Revise clause by clause: retain every supported fact and narrow next step, and "
                "delete or qualify only the clauses identified as unsupported. Treat feedback as "
                "a deletion or qualification constraint, not an invitation to add new clinical "
                "content. Do not introduce a new diagnosis, rationale, action, dose, progression, "
                "or escalation criterion during repair. Never replace a supported narrow boundary "
                "with broader generic care advice. If evidence required for personalized "
                "guidance is missing, state the limitation and ask only for the information or "
                "existing care plan needed to answer; clearly labeled general education may remain high-level and non-individualized. If reviewer feedback identifies unsupported patient-specific clauses in a mixed-scope draft, retain the clearly labeled general-education content and remove or qualify only those clauses. Preserve material restrictions or deferrals identified in reviewer feedback when retaining the associated allowance, and remove unrelated scope drift rather than replacing the supported draft. Conditional deference to the user's clinician "
                "or therapist is allowed only without inventing their instructions. "
                "If an authorized durable goal or bounded preference is present and the reviewer rejected unsupported "
                "protocol detail, preserve a direct restatement of that goal or preference and its "
                "narrow focus or constraint instead of replacing it with generic inability. Do not "
                "claim that an exercise is already approved or already in the user's plan unless "
                "that wording is explicitly supported by the frozen context. If exact exercise "
                "selection or dosing is unavailable, say only that it cannot be provided from the "
                "current evidence. "
                "If an authorized capability is unavailable, do not simulate its output. Respect active memory "
                "lifecycle state."
            )
        ),
        HumanMessage(content=instruction),
    ]
    repair_started = perf_counter()
    try:
        repair_model = with_responses_provider_retry(deps.consultant_model)
        response = repair_model.invoke(repair_messages)
        repaired_draft = _response_content(response)
        if not repaired_draft:
            raise ValueError("repair model returned blank content")
    except Exception as exc:
        return {
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": ["repair_model_error"],
            "reviewer_decisions": replace_reviewer_decisions(),
            "debug_trace": _trace(
                state,
                REPAIR_NODE,
                action="failed",
                reason="repair_model_error",
                error=type(exc).__name__,
            ),
            "internal_log_events": _internal_event(
                state,
                json.dumps(
                    {"status": "error", "error_type": type(exc).__name__},
                    sort_keys=True,
                ),
                model_input=_render_model_messages(repair_messages),
                metadata={
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "elapsed_ms": round((perf_counter() - repair_started) * 1000, 2),
                },
            ),
        }

    current_model_calls = state.get("model_calls", 0)
    current_total_tokens = state.get("total_tokens", 0)
    tokens = _response_tokens(response)
    total_tokens = (
        current_total_tokens + tokens
        if _valid_nonnegative_int(current_total_tokens) and tokens is not None
        else None
    )
    current_attempt = state.get("review_attempt")
    if not _valid_nonnegative_int(current_attempt):
        decision_attempt = _current_attempt(list(state.get("reviewer_decisions", []) or ()))
        current_attempt = decision_attempt if decision_attempt is not None else state.get("consult_attempts", 0)
    next_attempt = current_attempt + 1 if _valid_nonnegative_int(current_attempt) else 1
    next_repair_count = state.get("repair_count", 0) + 1
    return {
        "consult_response": repaired_draft,
        "web_search_summary": state.get("web_search_summary", ""),
        "retrieved_docs": list(state.get("retrieved_docs", ()) or ()),
        "tool_events": list(state.get("tool_events", ()) or ()),
        "consult_attempts": max(int(state.get("consult_attempts", 0) or 0) + 1, next_repair_count),
        "repair_count": next_repair_count,
        "repair_action_count": int(state.get("repair_action_count", 0) or 0) + 1,
        "repair_requires_revalidation": False,
        "review_attempt": next_attempt,
        "review_turn_id": str(uuid4()),
        "reviewer_decisions": replace_reviewer_decisions(),
        "model_calls": current_model_calls + 1 if _valid_nonnegative_int(current_model_calls) else None,
        "tool_calls": state.get("tool_calls", 0),
        "total_tokens": total_tokens,
        "tool_calling_used": False,
        "tool_calling_error": "",
        "debug_trace": _trace(
            state,
            REPAIR_NODE,
            action="completed",
            repair_count=next_repair_count,
            review_attempt=next_attempt,
            model_calls=1,
            tool_calls=0,
            token_count=tokens,
        ),
        "internal_log_events": _internal_event(
            state,
            repaired_draft,
            model_input=_render_model_messages(repair_messages),
            metadata={
                "elapsed_ms": round((perf_counter() - repair_started) * 1000, 2),
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": tokens,
            },
        ),
    }


def repair_revalidate_context_node(state: Mapping[str, Any]) -> dict[str, Any]:
    """Re-run Context Integrity before evidence planning when versions changed."""
    from app.ai.context_integrity import context_integrity

    updates = dict(context_integrity(state))
    current_attempt = state.get("review_attempt")
    if not _valid_nonnegative_int(current_attempt):
        decision_attempt = _current_attempt(list(state.get("reviewer_decisions", []) or ()))
        current_attempt = decision_attempt if decision_attempt is not None else state.get("consult_attempts", 0)
    next_attempt = current_attempt + 1 if _valid_nonnegative_int(current_attempt) else 1
    updates.update(
        {
            "repair_requires_revalidation": False,
            "force_revalidate_context": False,
            "discard_previous_evidence": True,
            "review_attempt": next_attempt,
            "review_turn_id": str(uuid4()),
            "reviewer_decisions": replace_reviewer_decisions(),
            "debug_trace": _trace(
                {**dict(state), **updates},
                REVALIDATE_NODE,
                action="context_integrity_revalidated",
                review_attempt=next_attempt,
            ),
        }
    )
    return updates


def route_after_review_join(
    state: Mapping[str, Any],
    *,
    policy: WorkflowPolicy | None = None,
) -> str:
    process_gate_severity = state.get("policy_gate_severity")
    token_telemetry_only = is_token_telemetry_only_hard_stop(state)
    if (
        process_gate_severity != GateSeverity.PASS.value
        and not token_telemetry_only
        and state.get("review_release") is True
    ):
        return "unsafe_fallback"
    if state.get("review_release") is True:
        return "safe_end"
    if (
        process_gate_severity == GateSeverity.HARD_STOP.value
        and not token_telemetry_only
    ):
        return "unsafe_fallback"
    active_policy = policy or WorkflowPolicy()
    admission = evaluate_repair_admission(state, policy=active_policy)
    if admission.severity is not GateSeverity.PASS:
        logger.warning(
            "repair admission rejected route: reasons=%s repair_count=%s "
            "model_calls=%s tool_calls=%s context_current=%s",
            admission.reason_codes,
            state.get("repair_count"),
            state.get("model_calls"),
            state.get("tool_calls"),
            context_versions_current(state),
        )
        return "unsafe_fallback"
    if not context_versions_current(state):
        snapshot = state.get("repair_context_snapshot") or state.get("evidence_context_snapshot")
        current = context_version_snapshot(state)
        logger.warning(
            "repair context requires revalidation: snapshot_keys=%s "
            "expected_context=%s current_context=%s expected_fingerprint=%s "
            "current_fingerprint=%s expected_sources=%s current_sources=%s "
            "expected_allowances=%s current_allowances=%s",
            sorted(snapshot.keys()) if isinstance(snapshot, Mapping) else [],
            snapshot.get("context_version") if isinstance(snapshot, Mapping) else None,
            current.get("context_version"),
            snapshot.get("context_fingerprint") if isinstance(snapshot, Mapping) else None,
            current.get("context_fingerprint"),
            sorted((snapshot.get("source_versions") or {}).keys()) if isinstance(snapshot, Mapping) and isinstance(snapshot.get("source_versions"), Mapping) else [],
            sorted((current.get("source_versions") or {}).keys()),
            sorted((snapshot.get("allowance_versions") or {}).keys()) if isinstance(snapshot, Mapping) and isinstance(snapshot.get("allowance_versions"), Mapping) else [],
            sorted((current.get("allowance_versions") or {}).keys()),
        )
        return REVALIDATE_NODE
    return REPAIR_NODE


def route_after_repair(state: Mapping[str, Any]) -> str:
    if (
        state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value
        and not is_token_telemetry_only_hard_stop(state)
    ):
        return "unsafe_fallback"
    if state.get("repair_requires_revalidation") is True:
        return REVALIDATE_NODE
    return "policy_gate" if state.get("consult_response") else "unsafe_fallback"
