"""Routing policy, clarification projection, and router model invocation."""
from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any, Literal, Mapping

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.ai.structured_output import UsageReportingOutput
from app.ai.graph_dependencies import _qwen_70b
from app.ai.graph_prompt_context import (
    _history_messages,
    _memory_context_prompt,
    _node_context_prompt,
    _router_model_fields,
    _router_retrieval_owned_request,
    _safe_prompt_text,
    _sanitize_clarification_question,
    _without_recent_turns,
)
from app.ai.invocation_support import (
    _RuntimeAdmissionBlocked,
    _RuntimeUsage,
    _blocked_invocation_updates,
    _debug_event,
    _internal_log_events,
    _make_internal_event,
    _model_failure_event,
    _model_trace_event,
    _require_invocation_admission,
    _safe_str,
)
from app.ai.model_input_budget import assert_model_messages_within_limit
from app.ai.policy_gate import GateSeverity
from app.ai.prompt_registry import get_prompt, link_prompt, prompt_text
from app.ai.runtime_dependencies import structured_output_model, with_responses_provider_retry
from app.ai.session_compaction import SessionCompactionError, prepare_active_model_messages
from app.ai.workflow_policy import WorkflowPolicy
from app.ai.workflow_state import RehabGraphState, Route

class ClarificationAnswer(BaseModel):
    "Provider-compatible fixed-shape answer record for checkpoint fields."

    field: str = Field(description="Original clarification field identifier.")
    value: str = Field(description="Exact nonblank user answer for that field.")


class ClarificationField(BaseModel):
    model_config = {"extra": "forbid"}
    field: str = Field(description="Short semantic identifier for one fact needed by the current route.")
    status: Literal["present", "explicitly_absent", "unknown"] = Field(
        description="Fact-ledger status from the complete authorized conversation context."
    )
    evidence_source: Literal["user_conversation", "authorized_context"] = Field(
        description="Source containing the exact phrase that establishes the status or exposes the ambiguity."
    )
    evidence_quote: str = Field(
        description="Exact source phrase that directly names or expresses this same fact dimension."
    )
    reason: str = Field(description="Decision-specific reason; required when status is unknown.")


class RouterOutput(UsageReportingOutput):
    intent_class: Literal["clinical", "non_clinical"] = Field(
        default="clinical",
        description="Whether the current request is clinical or non-clinical.",
    )
    exercise_request: bool = Field(
        default=False,
        description=(
            "Whether the current conversation semantically asks for selecting, performing, "
            "or adapting a rehabilitation exercise or movement, including a clarification "
            "answer to such a request. Recalling, restating, or confirming an exercise plan already "
            "given earlier in this conversation is not an exercise request. Infer this from clinical "
            "intent, not literal keyword matching."
        ),
    )
    # The configured OpenAI-compatible Responses endpoint requires every JSON-schema
    # property to appear in `required`, even when the Pydantic field has a default.
    model_config = {
        "json_schema_extra": {
            "required": [
                "intent_class",
                "exercise_request",
                "routing_decision",
                "if_emergency",
                "emergency_reason",
                "needs_more_info",
                "follow_up_question",
                "clarification_required_fields",
                "clarification_field_reasons",
                "clarification_answered_fields",
                "clarification_answered_values",
                "triage_notes",
            ]
        }
    }
    routing_decision: Literal["urgent_end", "continuation", "ask_clarification"] = Field(
        description="One of: urgent_end (red-flag emergency), continuation (safe to consult), ask_clarification (insufficient details)."
    )
    if_emergency: bool = Field(description="True if the user issue requires immediate medical attention.")
    emergency_reason: str = Field(description="The reason for the emergency classification.")
    needs_more_info: bool = Field(
        description="True if the user provided insufficient information and we need to ask follow-up questions."
    )
    follow_up_question: str = Field(description="A non-empty follow-up question to ask if needs_more_info is True; empty string only when needs_more_info is False.")
    clarification_required_fields: tuple[str, ...] = Field(
        default=(),
        description=(
            "Short semantic field names for unknown facts that are necessary "
            "for the current route. Do not include a treatment, surgery, injury, or rehabilitation "
            "history field unless the current request or authorized context makes it relevant; "
            "the runtime normalizes and deduplicates these identifiers without restricting them to "
            "a fixed vocabulary. Empty when no clarification is required."
        ),
    )
    clarification_field_reasons: list[ClarificationField] = Field(
        default_factory=list,
        description=(
            "One decision-critical reason for each clarification field. Include no field whose "
            "reason is only that more detail would be useful. Do not add a general red-flag screen "
            "merely because the user requested an exercise."
        ),
    )
    clarification_answered_fields: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Names of clarification_required_fields answered by the resumed user message; "
            "empty outside clarification resume."
        ),
    )
    clarification_answered_values: list[ClarificationAnswer] = Field(default_factory=list, description="Structured nonblank answer records using original field identifiers.")
    triage_notes: str = Field(description="Short triage explanation for debugging and observability.")

def _control_route(
    routing_decision: str,
    *,
    if_emergency: bool,
    needs_more_info: bool,
) -> Route:
    if if_emergency or routing_decision == "urgent_end":
        return Route.URGENT
    if needs_more_info or routing_decision == "ask_clarification":
        return Route.CLARIFY
    if routing_decision == "continuation":
        return Route.CONSULTANT
    return Route.UNSUPPORTED


def _router_context_text(state: RehabGraphState) -> str:
    parts = [str(state.get("user_input") or "")]
    active_router_packet = _node_context_prompt(state, "router")
    if active_router_packet != "Assembled Context: None":
        parts.append(active_router_packet)
    else:
        packets = state.get("context_packets")
        if isinstance(packets, Mapping):
            router_packet = packets.get("router")
            if router_packet:
                parts.append(str(router_packet))
    return " ".join(parts).strip().lower()


def _is_greeting_only(value: object) -> bool:
    return str(value or "").strip().lower() in {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"}


def router_agent(
    state: RehabGraphState,
    model: Any | None = None,
    *,
    policy: WorkflowPolicy | None = None,
    compaction_model: Any | None = None,
    compaction_timeout_seconds: float = 120.0,
    session_factory: Callable[[], Any] | None = None,
) -> RehabGraphState:
    usage = _RuntimeUsage.from_state(state)
    try:
        _require_invocation_admission(
            state,
            usage,
            policy=policy or WorkflowPolicy(),
            invocation="model",
        )
    except _RuntimeAdmissionBlocked as blocked:
        return _blocked_invocation_updates(state, usage, blocked)

    router_prompt = get_prompt("rehab-router")
    structured_llm = link_prompt(
        structured_output_model(model or _qwen_70b(), RouterOutput),
        router_prompt,
    )
    sys_prompt = router_prompt.text
    history_messages = _history_messages(state)
    router_context = _node_context_prompt(state, "router")
    if history_messages:
        router_context = _without_recent_turns(router_context)
    clarification_checkpoint = state.get("clarification_checkpoint")
    checkpoint_prompt = ""
    if isinstance(clarification_checkpoint, Mapping):
        required_fields = [
            str(value).strip()
            for value in (clarification_checkpoint.get("required_fields") or ())
            if str(value).strip()
        ]
        answered_fields = clarification_checkpoint.get("answered_fields")
        answered_fields = answered_fields if isinstance(answered_fields, Mapping) else {}
        remaining_fields = [
            field for field in required_fields if field not in answered_fields
        ]
        checkpoint_prompt = "\n" + prompt_text(
            "rehab-router-checkpoint",
            checkpoint_json=json.dumps(dict(clarification_checkpoint), ensure_ascii=False, sort_keys=True),
            remaining_fields_json=json.dumps(remaining_fields),
        )
    user_prompt = (
        f"{router_context}\n"
        f"User Input: {state.get('user_input', '')}"
        f"{checkpoint_prompt}"
    )
    router_messages = [
        SystemMessage(content=sys_prompt),
        *history_messages,
        HumanMessage(content=user_prompt),
    ]
    router_started = perf_counter()
    try:
        router_messages, estimated_input_tokens = prepare_active_model_messages(
            router_messages,
            model=compaction_model,
            timeout_seconds=compaction_timeout_seconds,
            session_factory=session_factory,
            session_id=state.get("session_id"),
            internal_events=state.get("internal_log_events"),
        )
    except SessionCompactionError as exc:
        usage.record_failed_model_invocation()
        return {
            **usage.updates(),
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": ["session_compaction_failed"],
            "debug_trace": _debug_event(
                state,
                "router",
                {"status": "error", "failure_category": "session_compaction_failed"},
            ),
            "internal_log_events": _internal_log_events(
                state,
                _model_failure_event(
                    "router",
                    router_messages,
                    exc,
                    started=router_started,
                    state=state,
                    failing_boundary="router.session_compaction",
                    model_call_attempt_count=usage.model_calls,
                ),
            ),
        }
    try:
        result: RouterOutput = structured_llm.invoke(router_messages)
    except Exception as exc:
        usage.record_failed_model_invocation()
        return {
            **usage.updates(),
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": ["model_invocation_failed"],
            "debug_trace": _debug_event(
                state,
                "router",
                {"status": "error", "error_type": type(exc).__name__},
            ),
            "internal_log_events": _internal_log_events(
                state,
                _model_failure_event(
                    "router",
                    router_messages,
                    exc,
                    started=router_started,
                    state=state,
                    failing_boundary="router.invoke",
                    model_call_attempt_count=usage.model_calls,
                ),
            ),
        }

    usage.record_model_response(result)

    routing_decision = result.routing_decision
    # Routing and urgent-risk interpretation are owned by structured model output.
    if_emergency = result.if_emergency
    needs_more_info = result.needs_more_info
    raw_model_required_fields = tuple(
        str(value).strip()
        for value in result.clarification_required_fields
        if str(value).strip()
    )
    model_required_fields = (
        ()
        if result.intent_class == "non_clinical"
        else _router_model_fields(
            state,
            raw_model_required_fields,
        )
    )
    basis_by_field: dict[str, ClarificationField] = {}
    for item in result.clarification_field_reasons:
        normalized = _router_model_fields(state, (item.field,))
        if normalized:
            basis_by_field[normalized[0]] = item
    user_evidence = " ".join(
        [str(state.get("user_input") or "")]
        + [
            str(message.get("content") or "")
            for message in (state.get("conversation_history") or ())
            if isinstance(message, Mapping)
            and str(message.get("role") or "").strip().lower() == "user"
        ]
    )
    authorized_evidence = _router_context_text(state)

    def evidence_is_present(item: ClarificationField) -> bool:
        quote = " ".join(str(item.evidence_quote).lower().split())
        source = user_evidence if item.evidence_source == "user_conversation" else authorized_evidence
        return bool(quote) and quote in " ".join(source.lower().split())

    unknown_basis_fields = {
        field for field, item in basis_by_field.items() if item.status == "unknown"
    }
    resolved_model_fields = tuple(
        field for field in model_required_fields if field in unknown_basis_fields
    )
    clarification_declared = needs_more_info or routing_decision == "ask_clarification"
    invalid_basis = clarification_declared and (
        not raw_model_required_fields
        or not basis_by_field
        or (
            bool(unknown_basis_fields)
            and (
                not model_required_fields
                or any(field not in basis_by_field for field in model_required_fields)
                or any(
                    not str(item.reason).strip() or not evidence_is_present(item)
                    for field, item in basis_by_field.items()
                    if field in set(resolved_model_fields)
                )
            )
        )
    )
    model_required_fields = resolved_model_fields
    explicit_answered_fields = {str(field).strip() for field in result.clarification_answered_fields if str(field).strip() in set(model_required_fields)}
    follow_up_question = _sanitize_clarification_question(
        result.follow_up_question,
        model_required_fields,
    )
    route = _control_route(
        routing_decision,
        if_emergency=if_emergency,
        needs_more_info=needs_more_info,
    )
    # Do not checkpoint a nonblank field list that contains no usable
    # identifier after normalization.
    if invalid_basis and not if_emergency:
        routing_decision = "unsupported"
        needs_more_info = False
        follow_up_question = ""
        route = Route.UNSUPPORTED
    elif route is Route.CLARIFY and not model_required_fields and not if_emergency:
        routing_decision = "continuation"
        needs_more_info = False
        follow_up_question = ""
        route = Route.CONSULTANT
    if route is Route.CLARIFY and _router_retrieval_owned_request(
        state, model_required_fields
    ):
        routing_decision = "continuation"
        needs_more_info = False
        follow_up_question = ""
        model_required_fields = ()
        route = Route.CONSULTANT
    if route is Route.CLARIFY and model_required_fields and explicit_answered_fields and not isinstance(clarification_checkpoint, Mapping):
        remaining_model_fields = tuple(
            field for field in model_required_fields
            if field not in explicit_answered_fields
        )
        if not remaining_model_fields:
            routing_decision = "continuation"
            needs_more_info = False
            follow_up_question = ""
            model_required_fields = ()
            route = Route.CONSULTANT
        model_required_fields = remaining_model_fields
    answered_checkpoint: dict[str, object] | None = None
    if isinstance(clarification_checkpoint, Mapping):
        answered_checkpoint = dict(clarification_checkpoint)
        required_fields = tuple(
            str(value).strip()
            for value in (clarification_checkpoint.get("required_fields") or ())
            if str(value).strip()
        )
        required_field_set = set(required_fields)
        prior_answered_fields = clarification_checkpoint.get("answered_fields")
        prior_answered_values = {
            str(key).strip(): value
            for key, value in (prior_answered_fields.items() if isinstance(prior_answered_fields, Mapping) else ())
            if str(key).strip() in required_field_set
            and isinstance(value, str)
            and value.strip()
        }
        model_answer_values = {str(answer.field).strip(): answer.value for answer in result.clarification_answered_values if str(answer.field).strip() in required_field_set and isinstance(answer.value, str) and answer.value.strip()}
        answered_fields = dict(prior_answered_values)
        answered_fields.update(model_answer_values)

        # Only structured, nonblank values for the persisted checkpoint fields
        # can satisfy the checkpoint.
        answered_checkpoint["answered_fields"] = answered_fields
        checkpoint_satisfied = bool(required_fields) and all(
            field in answered_fields for field in required_fields
        )
        answered_checkpoint["status"] = "satisfied" if checkpoint_satisfied else "answered"
        if not if_emergency and checkpoint_satisfied:
            routing_decision = "continuation"
            needs_more_info = False
            follow_up_question = ""
            route = Route.CONSULTANT
        elif not if_emergency and required_fields:
            prior_question = str(clarification_checkpoint.get("question") or "").strip()
            # The Router owns the patient-facing question. A resumed checkpoint
            # must not overwrite a newer, narrower Router question with stale text.
            candidate_source = follow_up_question or prior_question
            candidate_question = _sanitize_clarification_question(
                candidate_source,
                tuple(
                    field for field in required_fields
                    if field not in answered_fields
                ),
            )
            answered_checkpoint["question"] = candidate_question
            routing_decision = "ask_clarification"
            needs_more_info = True
            follow_up_question = candidate_question
            route = Route.CLARIFY


    updates = {
        **usage.updates(),
        "routing_decision": routing_decision,
        "intent_class": result.intent_class,
        "exercise_request": bool(result.exercise_request or state.get("exercise_request")),
        "route": route,
        "if_emergency": if_emergency,
        "emergency_reason": result.emergency_reason,
        "needs_more_info": needs_more_info,
        "follow_up_question": follow_up_question,
        "clarification_question": follow_up_question,
        "clarification_required_fields": list(
            required_fields if answered_checkpoint is not None else model_required_fields
        ),
        "clarification_field_reasons": (
            dict(answered_checkpoint.get("field_reasons") or {})
            if answered_checkpoint is not None
            else ({field: basis_by_field[field].reason for field in model_required_fields} if model_required_fields and not invalid_basis else {})
        ),
        "debug_trace": _debug_event(
            state,
            "router",
            {
                "decision": result.routing_decision,
                "if_emergency": result.if_emergency,
                "needs_more_info": result.needs_more_info,
                "intent_class": result.intent_class,
                "triage_notes": result.triage_notes,
            },
        ),
        "internal_log_events": _internal_log_events(
            state,
            _model_trace_event(
                "router",
                router_messages,
                result,
                started=router_started,
                state=state,
                decision=result.routing_decision,
                if_emergency=result.if_emergency,
                emergency_reason=result.emergency_reason,
                needs_more_info=result.needs_more_info,
                follow_up_question=result.follow_up_question,
                triage_notes=result.triage_notes,
            ),
        ),
    }

    if answered_checkpoint is not None:
        updates["clarification_checkpoint"] = answered_checkpoint

    if invalid_basis and not if_emergency:
        updates["policy_gate_severity"] = GateSeverity.HARD_STOP.value
        updates["policy_gate_reason_codes"] = ["clarification_basis_invalid"]
        updates["final_response"] = ""

    if routing_decision == "urgent_end" or if_emergency:
        emergency_reason = _safe_str(result.emergency_reason, 500).strip().rstrip(".")
        updates["final_response"] = (
            f"This needs urgent medical attention. {emergency_reason}. "
            "Please get medical care now rather than following a rehabilitation plan."
        )
    elif routing_decision == "ask_clarification" or needs_more_info:
        if re.search(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", follow_up_question):
            updates["policy_gate_severity"] = GateSeverity.HARD_STOP.value
            updates["policy_gate_reason_codes"] = ["clarification_question_invalid"]
            updates["final_response"] = ""
        else:
            updates["final_response"] = follow_up_question
        # Pending clarification checkpoints keep control state, not the raw user message.
        updates["user_input"] = ""
    elif answered_checkpoint is not None:
        updates["clarification_answered"] = True

    return updates
