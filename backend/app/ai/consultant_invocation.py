"""Consultant retrieval planning, invocation, and response projection.

The public ``consultant_agent`` boundary reads the typed Consultant fields described
in ``backend/README.md`` and returns a partial ``RehabGraphState`` update. Existing
serialization, authorization, and content handling remain delegated to the same
runtime collaborators used before extraction.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace
from time import perf_counter
from typing import Any, Callable, Literal, Mapping, Protocol

from langchain_core.messages import AIMessage as LangChainAIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from app.ai.active_turn_context import store_consultant_snapshot, store_tool_record, store_web_sources
from app.ai.model_input_budget import model_messages_estimated_tokens
from app.ai.policy_gate import GateResult, GateSeverity
from app.ai.runtime_dependencies import structured_output_model, with_responses_provider_retry
from app.ai.session_compaction import SessionCompactionError, prepare_active_model_messages
from app.ai.tool_result_compression import (
    CompressedToolResult,
    compress_kb_results,
    compress_web_results,
    compressed_tool_failure,
)
from app.ai.workflow_policy import WorkflowPolicy
from app.ai.workflow_state import RehabGraphState
from app.core.config import evaluator_mode_enabled
from app.db.models import AISession, CareEpisode
from app.ai.fallback import unsafe_fallback_agent
from app.ai.graph_prompt_context import (
    _history_messages,
    _node_context_prompt,
    _safety_revision_instruction,
    _without_recent_turns,
)
from app.ai.invocation_support import (
    _CONSULTANT_BATCH_EXECUTOR,
    _CONSULTANT_MAX_RESULTS_PER_QUERY,
    _CONSULTANT_MAX_RETRIEVALS_PER_CALL,
    _RuntimeAdmissionBlocked,
    _RuntimeUsage,
    _TOOL_EXECUTOR,
    _blocked_invocation_updates,
    _bounded_result_limit,
    _debug_event,
    _default_reliable_web_search,
    _internal_log_events,
    _make_internal_event,
    _message_content,
    _model_failure_event,
    _model_trace_event,
    _query_list,
    _require_invocation_admission,
    _retrieval_tool_message_content,
    _run_async,
    _safe_str,
    _sanitized_exception_message,
    _serialize_model_messages,
    _tool_arguments,
    _tool_call_id,
    _tool_call_name,
)

class ConsultantDependencies(Protocol):
    """Only the injected capabilities consumed by Consultant invocation."""

    consultant_model: Any
    policy: WorkflowPolicy | None
    session_compaction_model: Any | None
    session_compaction_timeout_seconds: float
    session_factory: Callable[[], Any] | None
    use_tool_calling: bool | None


CONSULTANT_STATE_READ_FIELDS = frozenset(
    {
        "authorized_tool_names",
        "budget",
        "consult_attempts",
        "context_packets",
        "context_planner_error",
        "conversation_history",
        "episode_memory_level1_session_ids",
        "exercise_request",
        "evidence_turn_id",
        "input_tokens",
        "internal_log_events",
        "model_calls",
        "output_tokens",
        "policy_gate_severity",
        "session_id",
        "tool_calls",
        "total_tokens",
        "user_input",
    }
)
CONSULTANT_STATE_WRITE_FIELDS = frozenset(
    {
        "allowed_catalog_ids", "catalog_exercise_suggestions", "consult_attempts",
        "consult_response", "consultant_context_snapshot", "context_disclosure_trace",
        "debug_trace", "input_tokens", "internal_log_events", "model_calls",
        "output_tokens", "retrieval_errors", "retrieval_outcomes", "retrieval_status",
        "retrieved_docs", "tool_call_names", "tool_calling_error", "tool_calling_used",
        "tool_calls", "tool_events", "total_tokens", "web_search_summary", "web_sources",
    }
)
CONSULTANT_CONTENT_FIELDS = frozenset(
    {
        "context_packets", "conversation_history", "internal_log_events", "user_input",
        "catalog_exercise_suggestions", "consult_response", "consultant_context_snapshot",
        "retrieved_docs", "web_search_summary", "web_sources",
    }
)
CONSULTANT_ENUM_DOMAINS = {
    "policy_gate_severity": frozenset({"pass", "repairable", "hard_stop"}),
    "retrieval_outcomes": frozenset({"failed", "empty", "substantive"}),
}


# Frozen delivery: one injected Consultant decision loop.
_CONSULTANT_MAX_DECISION_STEPS = 4


class ConsultantRetrievalArguments(BaseModel):
    model_config = {"extra": "forbid"}
    query: str
    queries: list[str]
    limit: int
    session_id: str


class ConsultantRetrievalCall(BaseModel):
    model_config = {"extra": "forbid"}
    tool_name: Literal[
        "rehab_exercise_kb_search", "web_search",
        "read_episode_memory_level_2", "search_session_conversation",
    ]
    arguments: ConsultantRetrievalArguments
    reason: str


class ConsultantRetrievalPlan(BaseModel):
    """One provider-validated batch of independent retrieval calls."""

    model_config = {"extra": "forbid"}
    action: Literal["none", "retrieve"]
    calls: list[ConsultantRetrievalCall]
    reason: str


_CONSULTANT_RETRIEVAL_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "consultant_retrieval_plan",
        "description": (
            "Return one structured Consultant retrieval plan. Put all independent authorized "
            "calls in calls so they can execute as one batch. Defer any call that depends on a "
            "result to the next plan. This call never answers the patient."
        ),
        "parameters": ConsultantRetrievalPlan.model_json_schema(),
        "strict": True,
    },
}

from app.ai.consultant_tools import (
    _CONSULTANT_MAX_TOOL_CALLS,
    _CONSULTANT_TOOL_NAMES,
    _CONSULTANT_TOOL_SCHEMAS,
    _MEMORY_TOOL_NAMES,
    _combine_compressed_results,
    _consultant_tool_names_for_level1,
    _consultant_tool_schemas_for_state,
    _dispatch_consultant_tool,
    _episode_memory_level1_session_ids,
    _level2_entry_for_session,
    _run_memory_context_tool,
    _safe_tool_call_request,
    _search_adapter_result,
)

def _retrieval_call_arguments(call: ConsultantRetrievalCall) -> dict[str, Any]:
    arguments = call.arguments
    query = arguments.query.strip()
    queries = [value.strip() for value in arguments.queries if value.strip()]
    session_id = arguments.session_id.strip()
    if call.tool_name in {"rehab_exercise_kb_search", "web_search"}:
        if session_id or not (query or queries):
            raise ValueError("search retrieval plan has invalid arguments")
        return {
            **({"queries": queries} if queries else {"query": query}),
            **({"limit": arguments.limit} if call.tool_name == "rehab_exercise_kb_search" else {}),
        }
    if call.tool_name == "read_episode_memory_level_2":
        if not session_id or query or queries:
            raise ValueError("Level 2 retrieval plan has invalid arguments")
        return {"session_id": session_id}
    if query and queries:
        if len(queries) != 1 or queries[0] != query:
            raise ValueError("selected-session retrieval plan has conflicting queries")
    elif not query:
        if len(queries) != 1:
            raise ValueError("selected-session retrieval plan requires exactly one query")
        query = queries[0]
    if not session_id:
        raise ValueError("selected-session retrieval plan has invalid arguments")
    return {"session_id": session_id, "query": query}


def _consultant_retrieval_plan(
    model: Any,
    messages: list[Any],
    *,
    authorized_tools: tuple[dict[str, Any], ...],
    required_level3_session_id: str = "",
    required_level3_query: str = "",
    required_catalog_query: str = "",
) -> tuple[ConsultantRetrievalPlan, Any]:
    authorized_names = [str(schema["function"]["name"]) for schema in authorized_tools]
    required_followup = (
        " The prior Level 2 result had no substantive entry. You must select "
        f"search_session_conversation for session {required_level3_session_id}; action=none is invalid."
        if required_level3_session_id else ""
    )
    required_catalog = (
        " The Router marked this conversation as an exercise-selection request. Include "
        "rehab_exercise_kb_search in this retrieval plan before drafting, using the supplied "
        "conversation context as its query; the catalog call is mandatory."
        if required_catalog_query and "rehab_exercise_kb_search" in authorized_names else ""
    )
    planning_messages = [*messages, HumanMessage(content=(
        "Plan the next retrieval step before drafting any patient-facing answer. "
        "Return action=retrieve with every independent authorized call in one calls list when "
        "evidence is needed; calls that need another result must wait for the next plan. "
        "Otherwise return action=none with an empty calls list. Never repeat an unchanged call that already "
        "returned a failed result; use materially different structured arguments only when the available "
        "context supports them, or return action=none. All argument fields are required: use empty strings "
        "and an empty queries list for fields that do not apply. The authorized tools are:\n"
        + json.dumps(authorized_tools, ensure_ascii=False, sort_keys=True) + required_followup + required_catalog
    ))]
    factory = getattr(model, "with_structured_output", None)
    if callable(factory):
        response = structured_output_model(model, ConsultantRetrievalPlan).invoke(planning_messages)
        plan = response if isinstance(response, ConsultantRetrievalPlan) else ConsultantRetrievalPlan.model_validate(response)
    else:
        binder = getattr(model, "bind_tools", None)
        if not callable(binder):
            raise TypeError("consultant model does not support required structured retrieval planning")
        try:
            planner = binder([_CONSULTANT_RETRIEVAL_PLAN_TOOL], tool_choice="required")
        except TypeError:
            planner = binder([_CONSULTANT_RETRIEVAL_PLAN_TOOL])
        response = with_responses_provider_retry(planner).invoke(planning_messages)
        calls = list(getattr(response, "tool_calls", None) or [])
        if len(calls) != 1 or _tool_call_name(calls[0]) != "consultant_retrieval_plan":
            raise ValueError("Consultant retrieval planner did not return its required plan call")
        plan = ConsultantRetrievalPlan.model_validate(_tool_arguments(calls[0]))
    if not plan.reason.strip():
        raise ValueError("Consultant retrieval plan requires a reason")
    if required_catalog_query and "rehab_exercise_kb_search" in authorized_names and not any(
        call.tool_name == "rehab_exercise_kb_search" for call in plan.calls
    ):
        catalog_call = ConsultantRetrievalCall(
            tool_name="rehab_exercise_kb_search",
            arguments=ConsultantRetrievalArguments(
                query=required_catalog_query,
                queries=[],
                limit=_CONSULTANT_MAX_RESULTS_PER_QUERY,
                session_id="",
            ),
            reason="The Router identified a semantic exercise-selection request; authorize the catalog record before drafting.",
        )
        plan.action = "retrieve"
        plan.calls = [*plan.calls, catalog_call]
        if len(plan.calls) > _CONSULTANT_MAX_TOOL_CALLS:
            plan.calls = [
                call for call in plan.calls
                if call.tool_name != "rehab_exercise_kb_search"
            ][:_CONSULTANT_MAX_TOOL_CALLS - 1] + [catalog_call]
    if plan.action == "none":
        if plan.calls:
            raise ValueError("no-retrieval Consultant plan must have no calls")
        if required_level3_session_id:
            raise ValueError("Consultant must search the selected session after insufficient Level 2")
        return plan, response
    if not plan.calls or len(plan.calls) > _CONSULTANT_MAX_TOOL_CALLS:
        raise ValueError("retrieval plan must contain a bounded nonempty calls list")
    call_arguments: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    for call in plan.calls:
        if not call.reason.strip():
            raise ValueError("each Consultant retrieval call requires a reason")
        if call.tool_name not in authorized_names:
            raise _RuntimeAdmissionBlocked(
                GateResult(
                    severity=GateSeverity.HARD_STOP,
                    reason_codes=["tool_name_forbidden"],
                ),
                tool_call_names=[call.tool_name],
            )
        if (
            required_level3_session_id
            and call.tool_name == "search_session_conversation"
            and call.arguments.session_id.strip() == required_level3_session_id
            and not call.arguments.query.strip()
            and not any(value.strip() for value in call.arguments.queries)
        ):
            fallback_query = " ".join(required_level3_query.split())[:2000]
            if not fallback_query:
                raise ValueError("selected-session retrieval plan requires exactly one query")
            call.arguments.query = fallback_query
        arguments = _retrieval_call_arguments(call)
        identity = f"{call.tool_name}:{json.dumps(arguments, sort_keys=True)}"
        if identity in seen_calls:
            raise ValueError("retrieval plan contains a duplicate call")
        seen_calls.add(identity)
        call_arguments.append(arguments)
    level3_calls = [
        (call, arguments)
        for call, arguments in zip(plan.calls, call_arguments)
        if call.tool_name == "search_session_conversation"
    ]
    if required_level3_session_id:
        if len(plan.calls) != 1 or len(level3_calls) != 1 or level3_calls[0][1].get("session_id") != required_level3_session_id:
            raise ValueError("Consultant must defer and then search the selected session after insufficient Level 2")
    elif level3_calls and any(call.tool_name == "read_episode_memory_level_2" for call in plan.calls):
        # A Level-3 search depends on the result of Level-2; keep only the
        # prerequisite and unrelated independent calls for this execution round.
        plan.calls = [
            call for call in plan.calls
            if call.tool_name != "search_session_conversation"
        ]
        if not plan.calls:
            raise ValueError("dependent retrieval call was deferred without a prerequisite")
    elif level3_calls:
        raise ValueError("selected-session search must wait for a prior Level 2 result")
    return plan, response


def _consultant_v2_agent(state: RehabGraphState, deps: ConsultantDependencies) -> RehabGraphState:
    if state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value:
        return unsafe_fallback_agent(state)

    model = deps.consultant_model
    if model is None:
        raise RuntimeError("consultant_model_required")

    attempt = int(state.get("consult_attempts", 0) or 0) + 1
    query = str(state.get("user_input") or "")
    usage = _RuntimeUsage.from_state(state)
    history_messages = _history_messages(state)
    initial_context = _node_context_prompt(state, "consultant")
    if history_messages:
        initial_context = _without_recent_turns(initial_context)
    planner_error = str(state.get("context_planner_error") or "").strip()
    if planner_error:
        initial_context += "\n\nContext assembly used deterministic source order because the planner was unavailable (" + planner_error + "). Preserve authorized goals and preferences, but do not invent an exercise protocol."
    disclosure_trace: list[dict[str, Any]] = [
        {"event": "initial_memory_context"},
    ]
    safety_instruction = _safety_revision_instruction(state, attempt)
    messages: list[Any] = [
        SystemMessage(
            content=(
                "You are the RehabFlow Consultant. Return only the patient-facing answer. "
                "The initial context contains Patient Memory, Episode Memory Level 1, and the "
                "current session conversation. Previous-session conversations are not injected "
                "wholesale. First use the current session conversation when it directly contains "
                "the requested fact. Human and assistant messages immediately before the current "
                "request are current-session evidence; if they already answer the request, "
                "restate the exact plan or fact instead of saying it is missing. Episode Memory "
                "Level 1 is an index, not the detailed fact; "
                "it may list the exact session IDs that its detailed entries come from. If "
                "Episode Memory Level 1 is absent or lists no session ID, do not call either "
                "memory tool because there is no authorized Level 2 target. Never use Level 2 "
                "to look up Patient Memory. Only call read_episode_memory_level_2 when a "
                "relevant Level 1 session ID is shown and the requested detail is not already "
                "in the initial context, passing exactly one of those listed session IDs. "
                "Treat a request for details from prior interactions as recall when the current "
                "session does not contain the answer. Level 1 keywords and broad summaries are "
                "navigation aids, not substitutes for the requested detail. Select a relevant "
                "authorized session and read its Level 2 entry. If Level 2 omits the detail or "
                "points to the conversation as its source, search that same session with 2-6 "
                "distinctive content terms. If a result is related but insufficient, continue "
                "through other relevant Level 1 candidates one session per call. Conclude that "
                "the detail is unavailable only after the relevant authorized candidates are "
                "exhausted. Never search an unlisted ID or broaden one call across sessions. "
                "Never call search_session_conversation before Level 2 or when Level 2 already "
                "contains the target fact. "
                "Build search queries from the fact's distinguishing content, not the user's "
                "conversational instructions. Use only returned messages as evidence for a "
                "selected-session fact and preserve material qualifiers. Do not emit control "
                "markers or invent missing context. "
                "Choose retrieval by information need rather than request wording. Use the "
                "authorized exercise catalog for every named exercise, exercise instruction, dose, "
                "frequency, progression, rehabilitation plan, or catalog-backed recommendation. "
                "If the user asks which exercise or movement from a plan fits a described body area "
                "or restriction, call rehab_exercise_kb_search for the authorized catalog record even "
                "when the prior plan text is not in the current context. "
                "Retrieved catalog and web results are internal evidence, not a checklist of content "
                "that must appear in the patient-facing answer. Distinguish web contents from user facts. Before naming or describing an exercise, "
                "decide whether that result actually answers the user's question and is applicable to "
                "the stated symptoms, restrictions, rehabilitation stage, and goal. "
                "A catalog match can describe an option but cannot by "
                "itself establish that the patient should exercise today; if the context does not support "
                "that decision, omit the exercises completely in the response and ask for the "
                "missing care-plan or symptom detail. "
                "If a catalog result is empty or not applicable to the patient's body area, symptom "
                "behavior, rehabilitation stage, or restrictions, do not recommend it. Continue with "
                "materially different catalog queries while the retrieval budget permits and the "
                "conversation supports a useful reformulation; do not repeat an unchanged query. "
                "Never select or introduce an exercise from web_search. Use web_search as a normal "
                "fact-checking and public-evidence tool for current guidance, contraindications, "
                "precautions, safety boundaries, recovery expectations, evidence claims, and other "
                "external clinical facts. Do not wait for the user to request web research explicitly. "
                "When independent catalog and web evidence are both relevant, request them in one "
                "retrieval batch. Web evidence may support external facts around an exercise, but the "
                "exercise itself and its instructions must remain catalog-sourced. Honor an explicit "
                "request to retrieve or consult an authorized source. Do not draft "
                "patient-specific clinical guidance from general model knowledge while required "
                "evidence is absent. General education may cover high-level phases, goals, "
                "exercise categories, and warning signs without retrieval, but label it as "
                "general education and do not turn it into personalized clearance, dose, or "
                "progression. When the user explicitly asks for general education, preserve "
                "that bounded scope through review and repair; do not replace it with inability "
                "solely because patient-specific evidence is absent. Apply the latest user-provided timing and symptoms in multi-turn "
                "answers and distinguish current facts from conditional escalation boundaries. "
                "If a retrieval call returns failed, say that the provider was unavailable; "
                "if it returns ok_empty, say that the search completed without usable evidence. "
                "Neither status supports a clinical claim. Preserve the known user goal, "
                "symptoms, and constraints, and ask only for the narrow information or existing "
                "care-plan detail needed for the next grounded decision. Do not replace either "
                "state with a generic warning-sign checklist, a named exercise, a dose, or a "
                "progression rule. "
                "After a failed web call, if retrying, use one materially narrower query rather "
                "than a synonym. Treat deletion and archival as lifecycle state: do not reuse "
                "inactive content as current evidence. Preserve all relevant allowances, goals, "
                "restrictions, symptom boundaries, and source qualifiers from authorized context; "
                "When an allowance is paired with a restriction or deferral, retain that material limit whenever the allowance is used. Keep the answer within the current request scope and do not expand unrelated conditions mentioned only as background. "
                "When only an authorized durable goal or bounded preference is available and no "
                "exercise protocol is present, restate that goal or preference and give only its "
                "narrow focus or constraint. Do not introduce quantitative parameters, such as duration, "
                "frequency, repetitions, intensity, or progression, unless they are explicitly supported by "
                "the current authorized context or the user's current request. Do not invent named exercises, doses, repetitions, "
                "progression thresholds, or clearance. "
                "do not add unsupported care details or rationale. If a named planning or retrieval "
                "capability is unavailable, disclose that limitation and do not simulate its output; "
                "use any still-authorized preference or constraint only within its stated scope. "
                "Ask a concise direct question when information required for safe, grounded guidance "
                "is missing. On resumed clarification, incorporate the new answer instead of "
                "repeating resolved questions. If prior request content is unavailable, ask the user "
                "to restate the relevant care request rather than asking for internal identifiers. "
                "Do not mention internal agents, tools, or prompts."
            )
        ),
        HumanMessage(
            content=(
                f"{initial_context}\n\n"
                f"Current user request: {query}\n"
                f"{safety_instruction}"
            )
        ),
    ]
    messages[1:1] = history_messages

    tool_schemas = _consultant_tool_schemas_for_state(state)
    answer_model = with_responses_provider_retry(model)

    tool_events: list[dict[str, Any]] = []
    internal_events: list[dict[str, Any]] = []
    tool_names: list[str] = []
    retrieved_docs: list[dict[str, Any]] = []
    web_sources: list[Mapping[str, str]] = []
    rendered_tool_results: list[str] = []
    catalog_suggestions: list[dict[str, Any]] = []
    retrieval_status: dict[str, str] = {}
    # Compatibility keeps legacy ok_empty/ok_nonempty values; this parallel
    # projection exposes the frozen failed|empty|substantive contract.
    retrieval_outcomes: dict[str, str] = {}
    retrieval_errors: dict[str, str] = {}
    prior_internal_events = [
        event
        for event in (state.get("internal_log_events") or ())
        if isinstance(event, Mapping)
    ]
    response_content = ""
    started = perf_counter()
    tool_budget_exhausted = False
    estimated_input_tokens = 0
    required_level3_session_id = ""
    failed_call_identities: set[str] = set()

    for step in range(1, _CONSULTANT_MAX_DECISION_STEPS + 1):
        _require_invocation_admission(
            state,
            usage,
            policy=deps.policy or WorkflowPolicy(),
            invocation="model",
        )
        model_started = perf_counter()
        try:
            prepared_messages, estimated_input_tokens = prepare_active_model_messages(
                messages,
                model=deps.session_compaction_model,
                timeout_seconds=deps.session_compaction_timeout_seconds,
                session_factory=deps.session_factory,
                session_id=state.get("session_id"),
                internal_events=[*prior_internal_events, *internal_events],
            )
        except SessionCompactionError as exc:
            usage.record_failed_model_invocation()
            internal_events.append(
                _model_failure_event(
                    "consultant",
                    messages,
                    exc,
                    started=model_started,
                    state=state,
                    failing_boundary="consultant.session_compaction",
                    model_call_attempt_count=usage.model_calls,
                )
            )
            return {
                **usage.updates(),
                "policy_gate_severity": GateSeverity.HARD_STOP.value,
                "policy_gate_reason_codes": ["session_compaction_failed"],
                "consult_attempts": attempt,
                "retrieved_docs": retrieved_docs,
                "retrieval_status": retrieval_status,
                "retrieval_outcomes": retrieval_outcomes,
                "retrieval_errors": retrieval_errors,
                "web_sources": [dict(source) for source in web_sources],
                "consult_response": "",
                "tool_calling_used": bool(tool_names),
                "tool_call_names": tool_names,
                "tool_events": tool_events,
                "context_disclosure_trace": disclosure_trace,
                "internal_log_events": _internal_log_events(state, *internal_events),
            }
        messages = prepared_messages
        response_is_model_output = True
        consultant_boundary = "consultant.retrieval_planning"
        try:
            if deps.use_tool_calling and tool_schemas:
                exercise_query = "\n".join(
                    str(item.get("content") or "").strip()
                    for item in (state.get("conversation_history") or [])
                    if isinstance(item, Mapping)
                    and str(item.get("role") or "").lower() == "user"
                    and str(item.get("content") or "").strip()
                )[-4000:]
                if query.strip() and query.strip() not in exercise_query:
                    exercise_query = f"{exercise_query}\n{query.strip()}"[-4000:]
                plan, planning_response = _consultant_retrieval_plan(
                    model, prepared_messages, authorized_tools=tool_schemas,
                    required_level3_session_id=required_level3_session_id,
                    required_level3_query=query,
                    required_catalog_query=(
                        exercise_query
                        if state.get("exercise_request") is True
                        and "rehab_exercise_kb_search" not in tool_names
                        else ""
                    ),
                )
                usage.record_model_response(planning_response)
                internal_events.append(_make_internal_event(
                    "consultant", "retrieval_plan", plan.reason, attempt=attempt,
                    decision_step=step, action=plan.action,
                    tool_calls=[call.tool_name for call in plan.calls],
                ))
                if plan.action == "retrieve":
                    response_is_model_output = False
                    response = LangChainAIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": f"consultant-plan-{step}-{index}",
                                "name": call.tool_name,
                                "args": _retrieval_call_arguments(call),
                            }
                            for index, call in enumerate(plan.calls, start=1)
                        ],
                    )
                else:
                    consultant_boundary = "consultant.answer"
                    response = answer_model.invoke(prepared_messages)
            else:
                consultant_boundary = "consultant.answer"
                response = answer_model.invoke(prepared_messages)
        except _RuntimeAdmissionBlocked as blocked:
            blocked_updates = _blocked_invocation_updates(state, usage, blocked)
            return {
                **blocked_updates,
                "consult_attempts": attempt,
                "retrieved_docs": retrieved_docs,
                "retrieval_status": retrieval_status,
                "retrieval_outcomes": retrieval_outcomes,
                "retrieval_errors": retrieval_errors,
                "web_sources": [dict(source) for source in web_sources],
                "consult_response": "",
                "tool_calling_used": bool(blocked.tool_call_names),
                "tool_call_names": list(blocked.tool_call_names),
                "tool_events": [
                    *tool_events,
                    *blocked_updates.get("tool_events", []),
                ],
                "context_disclosure_trace": disclosure_trace,
                "internal_log_events": _internal_log_events(
                    state, *internal_events
                ),
            }
        except Exception as exc:
            usage.record_failed_model_invocation()
            internal_events.append(
                _model_failure_event(
                    "consultant",
                    prepared_messages,
                    exc,
                    started=model_started,
                    state=state,
                    failing_boundary=consultant_boundary,
                    model_call_attempt_count=usage.model_calls,
                )
            )
            return {
                **usage.updates(),
                "policy_gate_severity": GateSeverity.HARD_STOP.value,
                "policy_gate_reason_codes": ["model_invocation_failed"],
                "consult_attempts": attempt,
                "retrieved_docs": retrieved_docs,
                "retrieval_status": retrieval_status,
                "retrieval_outcomes": retrieval_outcomes,
                "retrieval_errors": retrieval_errors,
                "consult_response": "",
                "tool_calling_used": bool(tool_names),
                "tool_call_names": tool_names,
                "tool_events": tool_events,
                "context_disclosure_trace": disclosure_trace,
                "internal_log_events": _internal_log_events(state, *internal_events),
            }

        if response_is_model_output:
            usage.record_model_response(response)
        response_content = _message_content(response).strip()
        tool_calls = list(getattr(response, "tool_calls", None) or [])
        internal_events.append(
            _model_trace_event(
                "consultant",
                prepared_messages,
                response,
                started=model_started,
                state=state,
                attempt=attempt,
                decision_step=step,
                tool_calls=[_tool_call_name(call) for call in tool_calls],
                estimated_input_tokens=estimated_input_tokens,
            )
        )
        messages.append(response)

        fresh_tool_calls: list[object] = []
        for call in tool_calls:
            call_identity = (
                f"{_tool_call_name(call)}:"
                f"{json.dumps(_tool_arguments(call), sort_keys=True)}"
            )
            if call_identity in failed_call_identities:
                internal_events.append(
                    _make_internal_event(
                        "consultant",
                        "retrieval_plan",
                        "An unchanged failed retrieval call was deferred.",
                        attempt=attempt,
                        decision_step=step,
                        action="defer_duplicate_failed_call",
                        tool_name=_tool_call_name(call),
                    )
                )
                continue
            fresh_tool_calls.append(call)
        tool_calls = fresh_tool_calls

        remaining_tool_budget = _CONSULTANT_MAX_TOOL_CALLS - len(tool_events)
        if len(tool_calls) > remaining_tool_budget:
            tool_budget_exhausted = True
            tool_calls = tool_calls[:remaining_tool_budget]
        batch_usage = replace(usage)
        try:
            for call in tool_calls:
                _require_invocation_admission(
                    state,
                    batch_usage,
                    policy=deps.policy or WorkflowPolicy(),
                    invocation="tool",
                )
                batch_usage.record_tool_invocation()
        except _RuntimeAdmissionBlocked as blocked:
            blocked.tool_call_names = [_tool_call_name(call)]
            blocked_updates = _blocked_invocation_updates(state, usage, blocked)
            return {
                **blocked_updates,
                "consult_attempts": attempt,
                "retrieved_docs": retrieved_docs,
                "retrieval_status": retrieval_status,
                "retrieval_outcomes": retrieval_outcomes,
                "retrieval_errors": retrieval_errors,
                "web_sources": [dict(source) for source in web_sources],
                "consult_response": "",
                "tool_calling_used": True,
                "tool_call_names": [*tool_names, _tool_call_name(call)],
                "tool_events": [*tool_events, *blocked_updates.get("tool_events", [])],
                "context_disclosure_trace": disclosure_trace,
                "internal_log_events": _internal_log_events(state, *internal_events),
            }
        usage.tool_calls = batch_usage.tool_calls
        batch_futures = [
            _CONSULTANT_BATCH_EXECUTOR.submit(
                _dispatch_consultant_tool,
                name=_tool_call_name(call), arguments=_tool_arguments(call), state=state,
                deps=deps, query=query, usage=usage, attempt=attempt,
                call_id=_tool_call_id(call, index), invocation_admitted=True,
            )
            for index, call in enumerate(tool_calls, start=1)
        ]
        batch_results = [future.result() for future in batch_futures]

        for index, (call, batch_result) in enumerate(zip(tool_calls, batch_results), start=1):
            if len(tool_events) >= _CONSULTANT_MAX_TOOL_CALLS:
                tool_budget_exhausted = True
                break
            name = _tool_call_name(call)
            call_id = _tool_call_id(call, index)
            arguments = _tool_arguments(call)
            try:
                (
                    tool_message,
                    event,
                    docs,
                    refs,
                    internal_event,
                ) = batch_result
            except _RuntimeAdmissionBlocked as blocked:
                blocked_names = [
                    *tool_names,
                    *(blocked.tool_call_names or [name]),
                ]
                blocked.tool_call_names = list(dict.fromkeys(blocked_names))
                blocked_updates = _blocked_invocation_updates(state, usage, blocked)
                return {
                    **blocked_updates,
                    "consult_attempts": attempt,
                    "retrieved_docs": retrieved_docs,
                    "retrieval_status": retrieval_status,
                    "retrieval_outcomes": retrieval_outcomes,
                    "retrieval_errors": retrieval_errors,
                    "web_sources": [dict(source) for source in web_sources],
                    "consult_response": "",
                    "tool_calling_used": bool(blocked.tool_call_names),
                    "tool_call_names": blocked.tool_call_names,
                    "tool_events": [
                        *tool_events,
                        *blocked_updates.get("tool_events", []),
                    ],
                    "context_disclosure_trace": disclosure_trace,
                    "internal_log_events": _internal_log_events(state, *internal_events),
                }

            messages.append(tool_message)
            tool_events.append(event)
            retrieval_status[name] = str(event.get("outcome") or "")
            raw_outcome = retrieval_status[name]
            retrieval_outcomes[name] = (
                "failed" if raw_outcome in {"failed", "timeout", "blocked", "invalid"}
                else "empty" if raw_outcome == "ok_empty"
                else "substantive" if raw_outcome == "ok_nonempty"
                else raw_outcome
            )
            if retrieval_outcomes[name] == "failed":
                retrieval_errors[name] = str(event.get("failure_error") or "retrieval failed")[:500]
                failed_call_identities.add(
                    f"{name}:{json.dumps(arguments, sort_keys=True)}"
                )
            else:
                retrieval_errors.pop(name, None)
            if name == "read_episode_memory_level_2":
                required_level3_session_id = (
                    str(arguments.get("session_id") or "")
                    if event.get("outcome") == "ok_empty"
                    and "search_session_conversation" in {str(schema["function"]["name"]) for schema in tool_schemas}
                    else ""
                )
            elif name == "search_session_conversation" and str(arguments.get("session_id") or "") == required_level3_session_id:
                required_level3_session_id = ""
            internal_events.append(internal_event)
            retrieved_docs.extend(docs)
            web_sources.extend(refs)
            tool_names.append(name)
            safe_request = _safe_tool_call_request(arguments)
            disclosure_trace.append(
                {
                    "event": "tool_call",
                    "tool_name": name,
                    "request": safe_request,
                    "outcome": event.get("outcome"),
                }
            )
            rendered_tool_results.append(
                f"Consultant tool call {name} request: "
                f"{json.dumps(safe_request, ensure_ascii=False, sort_keys=True)}\n"
                f"Compressed {name} result:\n{tool_message.content}"
            )

        if tool_budget_exhausted or not tool_calls:
            break

    if not response_content and not required_level3_session_id:
        synthesis_started = perf_counter()
        try:
            _require_invocation_admission(
                state, usage, policy=deps.policy or WorkflowPolicy(), invocation="model"
            )
            synthesis = answer_model.invoke(messages)
            usage.record_model_response(synthesis)
            response_content = _message_content(synthesis).strip()
            messages.append(synthesis)
        except _RuntimeAdmissionBlocked as blocked:
            blocked_updates = _blocked_invocation_updates(state, usage, blocked)
            return {
                **blocked_updates,
                "consult_attempts": attempt,
                "retrieved_docs": retrieved_docs,
                "retrieval_status": retrieval_status,
                "retrieval_outcomes": retrieval_outcomes,
                "retrieval_errors": retrieval_errors,
                "web_sources": [dict(source) for source in web_sources],
                "consult_response": "",
                "tool_calling_used": bool(tool_names),
                "tool_call_names": tool_names,
                "tool_events": tool_events,
                "context_disclosure_trace": disclosure_trace,
                "internal_log_events": _internal_log_events(state, *internal_events),
            }
        except Exception as exc:
            usage.record_failed_model_invocation()
            internal_events.append(
                _model_failure_event(
                    "consultant", messages, exc, started=synthesis_started, state=state,
                    failing_boundary="consultant.final_synthesis",
                    model_call_attempt_count=usage.model_calls,
                )
            )
            return {
                **usage.updates(),
                "policy_gate_severity": GateSeverity.HARD_STOP.value,
                "policy_gate_reason_codes": ["model_invocation_failed"],
                "consult_attempts": attempt,
                "retrieved_docs": retrieved_docs,
                "retrieval_status": retrieval_status,
                "retrieval_outcomes": retrieval_outcomes,
                "retrieval_errors": retrieval_errors,
                "consult_response": "",
                "tool_calling_used": bool(tool_names),
                "tool_call_names": tool_names,
                "tool_events": tool_events,
                "context_disclosure_trace": disclosure_trace,
                "internal_log_events": _internal_log_events(state, *internal_events),
            }

    aggregate_input_tokens = model_messages_estimated_tokens(messages)
    if not response_content and not required_level3_session_id:
        raise RuntimeError("consultant_empty_response")
    final_response = response_content

    unique_web_sources: list[Mapping[str, str]] = []
    seen_web_urls: set[str] = set()
    for source in web_sources:
        url = str(source.get("url") or "")
        if url and url not in seen_web_urls:
            seen_web_urls.add(url)
            unique_web_sources.append(dict(source))
    web_sources = unique_web_sources
    consultant_visible_messages = [
        message
        for message in messages
        if str(getattr(message, "type", "")).lower() in {"human", "user", "ai", "assistant"}
    ]
    visible_message_snapshot = _serialize_model_messages(consultant_visible_messages)
    consultant_snapshot = "\n\n".join(
        part
        for part in (
            "Initial Consultant context:\n" + initial_context,
            "Consultant-visible session conversation:\n" + visible_message_snapshot,
            f"Current user request: {query}",
            *rendered_tool_results,
        )
        if part.strip()
    )
    turn_id = str(state.get("evidence_turn_id") or "")
    if turn_id:
        try:
            store_consultant_snapshot(turn_id, consultant_snapshot)
            store_web_sources(turn_id, [dict(source) for source in web_sources])
        except KeyError:
            pass

    internal_events.append(
        _make_internal_event(
            "consultant",
            "agent_output",
            final_response,
            attempt=attempt,
            mode="consultant-tool-loop",
            decision_steps=step,
            tool_calls=tool_names,
            tool_events=tool_events,
            retrieval_outcomes=retrieval_outcomes,
            retrieval_errors=retrieval_errors,
            estimated_input_tokens=estimated_input_tokens,
            aggregate_input_tokens=aggregate_input_tokens,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
        )
    )
    return {
        **usage.updates(),
        "consult_attempts": attempt,
        "retrieved_docs": retrieved_docs,
        "web_search_summary": "\n\n".join(rendered_tool_results),
        "retrieval_status": retrieval_status,
        "retrieval_outcomes": retrieval_outcomes,
        "retrieval_errors": retrieval_errors,
        "catalog_exercise_suggestions": catalog_suggestions,
        "allowed_catalog_ids": [
            str(item.get("exercise_id"))
            for item in catalog_suggestions
            if item.get("exercise_id")
        ],
        "consult_response": final_response,
        "tool_calling_used": bool(tool_names),
        "tool_calling_error": "consultant_tool_budget_exhausted" if tool_budget_exhausted else "",
        "tool_events": tool_events,
        "tool_call_names": tool_names,
        "web_sources": [dict(source) for source in web_sources],
        "consultant_context_snapshot": consultant_snapshot,
        "context_disclosure_trace": disclosure_trace,
        "internal_log_events": _internal_log_events(state, *internal_events),
        "debug_trace": _debug_event(
            state,
            "consultant",
            {
                "attempt": attempt,
                "mode": "consultant-tool-loop",
                "decision_steps": step,
                "tool_calls": tool_names,
                "response_preview": _safe_str(final_response, 500),
            },
        ),
    }


# The compiled graph resolves this global at invocation time.
consultant_agent = _consultant_v2_agent
