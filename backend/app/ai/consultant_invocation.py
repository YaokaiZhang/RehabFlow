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
import re
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace
from time import perf_counter
from typing import Any, Callable, Literal, Mapping, Protocol

from langchain_core.messages import AIMessage as LangChainAIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from app.ai.structured_output import UsageReportingOutput
from app.ai.active_turn_context import store_consultant_snapshot, store_tool_record, store_web_sources
from app.ai.model_input_budget import model_messages_estimated_tokens
from app.ai.policy_gate import GateResult, GateSeverity
from app.ai.prompt_registry import get_prompt, link_prompt, prompt_text
from app.ai.turn_progress import report_progress, report_tools_started
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
from app.services.catalog_exercise_suggestions import suggestions_from_search_documents
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
_CATALOG_SUGGESTION_LIMIT = 10

# A model with no tools bound sometimes writes the call it wanted as text,
# e.g. "<invoke:read_episode_memory_level_2><parameter name=...>". That text
# must never be treated as the patient-facing answer.
_TOOL_MARKUP_PATTERN = re.compile(
    r"</?\s*(?:[a-z_]+:)?(?:invoke|function_calls|tool_use|tool_call|parameter)\b"
    r"|<invoke:[\w-]+",
    re.IGNORECASE,
)
_TOOL_MARKUP_BLOCK_PATTERN = re.compile(
    r"<\s*(?:[a-z_]+:)?(?:function_calls|invoke|tool_use|tool_call)\b.*?"
    r"(?:</\s*(?:[a-z_]+:)?(?:function_calls|invoke|tool_use|tool_call)\s*>|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _looks_like_tool_markup(text: str) -> bool:
    return bool(_TOOL_MARKUP_PATTERN.search(text or ""))


def _strip_tool_markup(text: str) -> str:
    stripped = _TOOL_MARKUP_BLOCK_PATTERN.sub("", text or "")
    stripped = re.sub(r"</?\s*(?:[a-z_]+:)?parameter\b[^>]*>", "", stripped, flags=re.IGNORECASE)
    return stripped.strip()


def _tool_calls_from_markup(
    text: str,
    tool_schemas: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    step: int,
) -> list[dict[str, Any]]:
    """Recover calls to authorized tools whose required arguments are all written out."""
    calls: list[dict[str, Any]] = []
    for schema in tool_schemas:
        function = schema.get("function") or {}
        name = str(function.get("name") or "")
        if not name or not re.search(rf"\b{re.escape(name)}\b", text):
            continue
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") or {}
        arguments: dict[str, Any] = {}
        for argument in properties:
            match = re.search(
                rf"<\s*(?:[a-z_]+:)?parameter\s+name\s*=\s*[\"']{re.escape(argument)}[\"']\s*>"
                rf"(.*?)</\s*(?:[a-z_]+:)?parameter\s*>",
                text,
                re.IGNORECASE | re.DOTALL,
            ) or re.search(
                rf"[\"']{re.escape(argument)}[\"']\s*:\s*[\"']([^\"']*)[\"']",
                text,
            )
            if not match or not match.group(1).strip():
                continue
            value: Any = match.group(1).strip()
            if (properties.get(argument) or {}).get("type") == "array":
                try:
                    parsed = json.loads(value)
                except ValueError:
                    parsed = [value]
                value = parsed if isinstance(parsed, list) else [str(parsed)]
            arguments[argument] = value
        if all(required in arguments for required in parameters.get("required") or ()):
            calls.append(
                {
                    "id": f"consultant-markup-{step}-{len(calls) + 1}",
                    "name": name,
                    "args": arguments,
                }
            )
    return calls[:_CONSULTANT_MAX_TOOL_CALLS]


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


class ConsultantRetrievalPlan(UsageReportingOutput):
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
        " " + prompt_text("rehab-consultant-retrieval-plan-level3", session_id=required_level3_session_id)
        if required_level3_session_id else ""
    )
    required_catalog = (
        " " + prompt_text("rehab-consultant-retrieval-plan-catalog")
        + (
            " " + prompt_text("rehab-consultant-retrieval-plan-catalog-web")
            if "web_search" in authorized_names else ""
        )
        if required_catalog_query and "rehab_exercise_kb_search" in authorized_names else ""
    )
    planner_prompt = get_prompt(
        "rehab-consultant-retrieval-plan",
        authorized_tools=json.dumps(authorized_tools, ensure_ascii=False, sort_keys=True),
    )
    planning_messages = [
        *messages,
        HumanMessage(content=planner_prompt.text + required_followup + required_catalog),
    ]
    factory = getattr(model, "with_structured_output", None)
    if callable(factory):
        response = link_prompt(
            structured_output_model(model, ConsultantRetrievalPlan), planner_prompt
        ).invoke(planning_messages)
        plan = response if isinstance(response, ConsultantRetrievalPlan) else ConsultantRetrievalPlan.model_validate(response)
    else:
        binder = getattr(model, "bind_tools", None)
        if not callable(binder):
            raise TypeError("consultant model does not support required structured retrieval planning")
        try:
            planner = binder([_CONSULTANT_RETRIEVAL_PLAN_TOOL], tool_choice="required")
        except TypeError:
            planner = binder([_CONSULTANT_RETRIEVAL_PLAN_TOOL])
        response = link_prompt(with_responses_provider_retry(planner), planner_prompt).invoke(planning_messages)
        calls = list(getattr(response, "tool_calls", None) or [])
        if len(calls) != 1 or _tool_call_name(calls[0]) != "consultant_retrieval_plan":
            raise ValueError("Consultant retrieval planner did not return its required plan call")
        plan = ConsultantRetrievalPlan.model_validate(_tool_arguments(calls[0]))
    # The reason is trace-only; a blank one must not fail the patient's turn.
    if not plan.reason.strip():
        plan.reason = (
            "Retrieval planned without a stated reason."
            if plan.action == "retrieve"
            else "No further retrieval needed before drafting."
        )
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
            call.reason = f"Planned {call.tool_name} call."
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
        initial_context += "\n\n" + prompt_text(
            "rehab-consultant-context-planner-unavailable", planner_error=planner_error
        )
    disclosure_trace: list[dict[str, Any]] = [
        {"event": "initial_memory_context"},
    ]
    safety_instruction = _safety_revision_instruction(state, attempt)
    consultant_prompt = get_prompt(
        "rehab-consultant",
        patient_response_style=prompt_text("rehab-patient-response-style"),
    )
    messages: list[Any] = [
        SystemMessage(content=consultant_prompt.text),
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
    answer_model = link_prompt(with_responses_provider_retry(model), consultant_prompt)

    tool_events: list[dict[str, Any]] = []
    internal_events: list[dict[str, Any]] = []
    tool_names: list[str] = []
    retrieved_docs: list[dict[str, Any]] = []
    web_sources: list[Mapping[str, str]] = []
    rendered_tool_results: list[str] = []
    catalog_suggestions: list[dict[str, Any]] = []
    catalog_search_docs: list[dict[str, Any]] = []
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
    markup_retry_message: HumanMessage | None = None

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
            searches_complete = bool(tool_schemas) and all(
                retrieval_outcomes.get(str(schema["function"]["name"])) == "substantive"
                for schema in tool_schemas
            )
            # An exercise-selection request is answered by the catalog; once it has
            # returned usable records, draft instead of planning more retrieval.
            catalog_covers_request = (
                state.get("exercise_request") is True
                and retrieval_outcomes.get("rehab_exercise_kb_search") == "substantive"
            )
            searches_complete = searches_complete or catalog_covers_request
            if deps.use_tool_calling and tool_schemas and not searches_complete:
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
                    report_progress("drafting")
                    response = answer_model.invoke(prepared_messages)
            else:
                consultant_boundary = "consultant.answer"
                report_progress("drafting")
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
        if (
            response_is_model_output
            and not tool_calls
            and _looks_like_tool_markup(response_content)
        ):
            recovered_calls = (
                _tool_calls_from_markup(response_content, tool_schemas, step=step)
                if deps.use_tool_calling
                else []
            )
            internal_events.append(
                _make_internal_event(
                    "consultant",
                    "tool_markup",
                    "The Consultant wrote tool-call markup instead of an answer.",
                    attempt=attempt,
                    decision_step=step,
                    action="run_recovered_calls" if recovered_calls else "request_plain_answer",
                    tool_calls=[call["name"] for call in recovered_calls],
                )
            )
            response_content = ""
            if recovered_calls:
                response = LangChainAIMessage(content="", tool_calls=recovered_calls)
                tool_calls = list(recovered_calls)
            else:
                markup_retry_message = HumanMessage(
                    content=prompt_text("rehab-consultant-tool-markup-retry")
                )
                # Keep the markup out of the transcript reviewers see; only the
                # retry instruction goes to the final synthesis call.
                response = None
        if response is not None:
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
        report_tools_started([_tool_call_name(call) for call in tool_calls])
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
            if name == "rehab_exercise_kb_search":
                catalog_search_docs.extend(docs)
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
            synthesis = answer_model.invoke(
                [*messages, markup_retry_message] if markup_retry_message is not None else messages
            )
            usage.record_model_response(synthesis)
            response_content = _message_content(synthesis).strip()
            if _looks_like_tool_markup(response_content):
                response_content = _strip_tool_markup(response_content)
                internal_events.append(
                    _make_internal_event(
                        "consultant",
                        "tool_markup",
                        "The Consultant's final synthesis contained tool-call markup; it was removed.",
                        attempt=attempt,
                        action="strip_markup",
                    )
                )
                synthesis = LangChainAIMessage(content=response_content)
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
    # Only catalog-validated rows become suggestions; raw KB payloads are never trusted.
    catalog_suggestions = [
        suggestion.to_state_dict()
        for suggestion in suggestions_from_search_documents(
            catalog_search_docs, limit=_CATALOG_SUGGESTION_LIMIT
        )
    ]
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
