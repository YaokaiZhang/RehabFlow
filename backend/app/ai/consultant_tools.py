"""Authorized Consultant tool schemas and execution boundary.

The dispatcher admits only names in both the fixed schema set and the typed state's
``authorized_tool_names`` metadata. Memory reads revalidate patient, Care Episode,
and indexed-session ownership before content is returned. Tool results may contain
authorized evidence content; telemetry and requests remain bounded and sanitized.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from time import perf_counter
from typing import Any, Callable, Mapping, Protocol

from langchain_core.messages import ToolMessage

from app.ai.active_turn_context import store_tool_record
from app.ai.policy_gate import GateResult, GateSeverity
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
from app.ai.invocation_support import (
    _CONSULTANT_MAX_RESULTS_PER_QUERY,
    _CONSULTANT_MAX_RETRIEVALS_PER_CALL,
    _RuntimeAdmissionBlocked,
    _RuntimeUsage,
    _TOOL_EXECUTOR,
    _bounded_result_limit,
    _default_reliable_web_search,
    _make_internal_event,
    _query_list,
    _require_invocation_admission,
    _retrieval_tool_message_content,
    _run_async,
    _sanitized_exception_message,
)

_CONSULTANT_MAX_TOOL_CALLS = 8

class ConsultantToolDependencies(Protocol):
    """Only the injected capabilities consumed by authorized tool execution."""

    policy: WorkflowPolicy | None
    rehab_exercise_kb_search: Callable[[str, int], list[dict[str, Any]]] | None
    session_factory: Callable[[], Any] | None
    tool_timeout_seconds: float
    web_search: Callable[[str], Any] | None


CONSULTANT_TOOL_STATE_READ_FIELDS = frozenset(
    {
        "authorized_tool_names", "budget", "care_episode_id", "evidence_turn_id",
        "episode_memory_level1_session_ids", "model_calls", "patient_id", "tool_calls",
        "total_tokens",
    }
)
CONSULTANT_TOOL_CONTENT_FIELDS = frozenset({"query", "queries"})
CONSULTANT_TOOL_ENUM_DOMAINS = {
    "tool_name": frozenset(
        {
            "rehab_exercise_kb_search", "web_search",
            "read_episode_memory_level_2", "search_session_conversation",
        }
    ),
    "status": frozenset(
        {"ok_nonempty", "ok_empty", "failed", "timeout", "blocked", "invalid"}
    ),
}


_CONSULTANT_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "rehab_exercise_kb_search",
            "description": (
                "Use for a concrete exercise or rehabilitation plan, named exercise, "
                "dose, duration, frequency, progression, or evidence-grounded rehab answer. "
                "Do not use for a broad phase or goal explanation that needs no external evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": _CONSULTANT_MAX_RESULTS_PER_QUERY},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Use for fact-checking, current or public clinical guidance, contraindications, "
                "precautions, safety boundaries, recovery expectations, evidence claims, or other "
                "external facts that need authoritative public sources. Do not wait for the user to "
                "request web research explicitly. Named exercises, exercise instructions, and exercise "
                "doses must come from the authorized exercise catalog, never from web_search. Cite only "
                "returned authorized web sources."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "query": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_episode_memory_level_2",
            "description": (
                "Read the detailed Episode Memory entry for exactly one session ID "
                "listed by the active Episode Memory Level 1 index. Do not use this "
                "tool without a Level 1 session ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Use exactly one session ID shown in Episode Memory Level 1.",
                    },
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_session_conversation",
            "description": (
                "Use only after read_episode_memory_level_2 was insufficient for "
                "the target fact. Search AI messages in exactly one authorized prior session. "
                "Use a compact content query that identifies the target fact rather than "
                "conversational or routing language. If the returned messages are related "
                "but do not answer the target fact, call this tool again with a different session "
                "ID from the Episode Memory Level 1 index; do not answer that the fact is "
                "missing until the relevant indexed candidates have been checked. Each "
                "call still searches exactly one selected session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Use exactly the selected prior session identifier.",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Use 2-6 distinctive content terms for the target fact. "
                            "Exclude conversational filler, instructions, negation, "
                            "and descriptions of sessions to omit."
                        ),
                    },
                },
                "required": ["session_id", "query"],
                "additionalProperties": False,
            },
        },
    },
)

_CONSULTANT_TOOL_NAMES = tuple(
    str(schema["function"]["name"])
    for schema in _CONSULTANT_TOOL_SCHEMAS
)

_MEMORY_TOOL_NAMES = frozenset(
    {
        "read_episode_memory_level_2",
        "search_session_conversation",
    }
)


def _episode_memory_level1_session_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    session_ids: list[str] = []
    for raw_value in value:
        text = str(raw_value or "").strip()
        if not text:
            continue
        try:
            text = str(uuid.UUID(text))
        except (TypeError, ValueError):
            continue
        if text not in session_ids:
            session_ids.append(text)
    return session_ids


def _consultant_tool_names_for_level1(session_ids: object) -> list[str]:
    level1_ids = _episode_memory_level1_session_ids(session_ids)
    return [
        name
        for name in _CONSULTANT_TOOL_NAMES
        if name not in _MEMORY_TOOL_NAMES or level1_ids
    ]


def _consultant_tool_schemas_for_state(
    state: RehabGraphState,
) -> tuple[dict[str, Any], ...]:
    authorized = set(str(name) for name in state.get("authorized_tool_names", ()))
    if not _episode_memory_level1_session_ids(
        state.get("episode_memory_level1_session_ids")
    ):
        authorized.difference_update(_MEMORY_TOOL_NAMES)
    return tuple(
        schema
        for schema in _CONSULTANT_TOOL_SCHEMAS
        if str(schema["function"]["name"]) in authorized
    )


def _level2_entry_for_session(value: object, session_id: str) -> tuple[str, str]:
    """Read only explicit v2 state; legacy prose is conservatively unknown."""
    try:
        payload = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "unknown", ""
    if not isinstance(payload, Mapping) or payload.get("version") != 2:
        return "unknown", ""
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return "unknown", ""
    for entry in entries:
        if not isinstance(entry, Mapping) or str(entry.get("session_id") or "") != session_id:
            continue
        state = str(entry.get("detail_state") or "")
        summary = str(entry.get("summary") or "").strip()
        if state == "substantive" and summary:
            return state, summary
        return state if state in {"unavailable", "empty"} else "unknown", ""
    return "unknown", ""


def _safe_tool_call_request(arguments: Mapping[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {}
    raw_queries = arguments.get("queries")
    if isinstance(raw_queries, str):
        raw_queries = [raw_queries]
    if isinstance(raw_queries, (list, tuple)):
        queries = [
            str(value).strip()[:2000]
            for value in raw_queries
            if str(value).strip()
        ]
        if queries:
            request["queries"] = list(dict.fromkeys(queries))[:_CONSULTANT_MAX_RETRIEVALS_PER_CALL]
    else:
        query = str(arguments.get("query") or "").strip()
        if query:
            request["query"] = query[:2000]
    if "session_id" in arguments:
        request["session_id"] = str(arguments.get("session_id") or "").strip()[:128]
    if "limit" in arguments:
        request["limit"] = _bounded_result_limit(arguments.get("limit"))
    return request


def _search_adapter_result(
    search: Any,
    query: str,
    limit: int | None = None,
    *,
    timeout_seconds: float = 10.0,
) -> Any:
    import inspect

    if not callable(search):
        return {"error": "search adapter is unavailable"}
    timeout = max(0.1, min(float(timeout_seconds), 60.0))
    call = (lambda: search(query)) if limit is None else (lambda: search(query, limit))
    future = _TOOL_EXECUTOR.submit(call)
    try:
        result = future.result(timeout=timeout)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError("tool adapter timed out") from exc
    if inspect.isawaitable(result):
        async def await_result() -> Any:
            return await asyncio.wait_for(result, timeout=timeout)

        return _run_async(await_result())
    return result


def _combine_compressed_results(
    tool_name: str,
    results: list[CompressedToolResult],
) -> CompressedToolResult:
    nonempty = [result for result in results if result.status == "ok_nonempty"]
    rendered = "\n\n".join(result.rendered for result in results)
    if not rendered:
        rendered = f"{tool_name}: no evidence was found."
    refs: list[Mapping[str, str]] = []
    items: list[Mapping[str, Any]] = []
    seen_refs: set[str] = set()
    for result in results:
        for ref in result.source_refs:
            url = str(ref.get("url") or "")
            if url and url not in seen_refs:
                seen_refs.add(url)
                refs.append(dict(ref))
        items.extend(result.items)
    status = "ok_nonempty" if nonempty else next(
        (
            result.status
            for result in results
            if result.status in {"failed", "timeout", "blocked", "invalid"}
        ),
        "ok_empty",
    )
    return CompressedToolResult(
        tool_name=tool_name,
        status=status,
        rendered=rendered,
        items=tuple(items),
        source_refs=tuple(refs),
        result_count=sum(result.result_count for result in results),
        error=next((result.error for result in results if result.error), ""),
    )


def _run_memory_context_tool(
    *,
    name: str,
    arguments: Mapping[str, Any],
    state: RehabGraphState,
    deps: ConsultantToolDependencies,
) -> CompressedToolResult:
    session_factory = deps.session_factory
    if not callable(session_factory):
        return compressed_tool_failure(name, "failed", "session factory unavailable")
    try:
        patient_id = uuid.UUID(str(state.get("patient_id") or ""))
        episode_id = uuid.UUID(str(state.get("care_episode_id") or ""))
    except (TypeError, ValueError):
        return compressed_tool_failure(name, "invalid")

    db = session_factory()
    try:
        level1_session_ids = set(
            _episode_memory_level1_session_ids(
                state.get("episode_memory_level1_session_ids")
            )
        )
        try:
            selected_session_id = uuid.UUID(str(arguments.get("session_id") or ""))
        except (TypeError, ValueError):
            return compressed_tool_failure(name, "invalid")
        if str(selected_session_id) not in level1_session_ids:
            return CompressedToolResult(
                tool_name=name,
                status="blocked",
                rendered=(
                    "No authorized Episode Memory Level 1 entry matches that session."
                ),
            )

        selected_session = db.get(AISession, selected_session_id)
        if (
            selected_session is None
            or str(getattr(selected_session, "patient_id", "")) != str(patient_id)
            or str(getattr(selected_session, "care_episode_id", "")) != str(episode_id)
        ):
            return CompressedToolResult(
                tool_name=name,
                status="blocked",
                rendered="The selected Episode Memory session is no longer available.",
            )

        if name == "read_episode_memory_level_2":
            from sqlalchemy import select
            from app.db.models import CareEpisode, MemoryDocument

            episode = db.get(CareEpisode, episode_id)
            if (
                episode is None
                or episode.patient_id != patient_id
                or str(getattr(episode, "status", "active")) != "active"
            ):
                return compressed_tool_failure(name, "blocked")

            document = db.execute(
                select(MemoryDocument).where(
                    MemoryDocument.scope == "episode",
                    MemoryDocument.patient_id == patient_id,
                    MemoryDocument.care_episode_id == episode_id,
                    MemoryDocument.status == "active",
                )
            ).scalars().first()
            content = str(getattr(document, "level2_summary", "") or "").strip()
            detail_state, selected_content = _level2_entry_for_session(content, str(selected_session_id))
            if detail_state != "substantive":
                return CompressedToolResult(
                    tool_name=name,
                    status="ok_empty",
                    rendered="No substantive Level 2 entry is available for the selected session.",
                )
            return CompressedToolResult(
                tool_name=name,
                status="ok_nonempty",
                rendered=selected_content,
                result_count=1,
            )

        from app.services.session_conversation_search import (
            SessionConversationSearchError,
            search_session_conversation_for_tool,
        )

        messages = search_session_conversation_for_tool(
            db,
            patient_id=patient_id,
            care_episode_id=episode_id,
            session_id=selected_session_id,
            query=str(arguments.get("query") or ""),
        )
        rendered = "\n".join(
            f"{item.get('role', '')}: {item.get('content', '')}"
            for item in messages
            if str(item.get("content") or "").strip()
        )
        return CompressedToolResult(
            tool_name=name,
            status="ok_nonempty" if rendered else "ok_empty",
            rendered=rendered or "No matching AI messages were found in that session.",
            result_count=len(messages),
        )
    except SessionConversationSearchError as exc:
        if str(exc) == "session_scope_invalid":
            return CompressedToolResult(
                tool_name=name,
                status="failed",
                rendered="The selected session is outside the active Care Episode.",
                error=_sanitized_exception_message(exc),
            )
        if str(exc) == "query_not_searchable":
            return compressed_tool_failure(name, "failed", _sanitized_exception_message(exc))
        return compressed_tool_failure(name, "failed", _sanitized_exception_message(exc))
    except Exception as exc:
        return compressed_tool_failure(name, "failed", _sanitized_exception_message(exc))
    finally:
        db.close()



def _dispatch_consultant_tool(
    *,
    name: str,
    arguments: Mapping[str, Any],
    state: RehabGraphState,
    deps: ConsultantToolDependencies,
    query: str,
    usage: _RuntimeUsage,
    attempt: int,
    call_id: str,
    invocation_admitted: bool = False,
) -> tuple[ToolMessage, dict[str, Any], list[dict[str, Any]], list[Mapping[str, str]], dict[str, Any]]:
    allowed = {str(value) for value in state.get("authorized_tool_names", ())}
    if name not in _CONSULTANT_TOOL_NAMES or name not in allowed:
        raise _RuntimeAdmissionBlocked(
            GateResult(
                severity=GateSeverity.HARD_STOP,
                reason_codes=["tool_name_forbidden"],
            ),
            tool_call_names=[name],
        )
    if not invocation_admitted:
        try:
            _require_invocation_admission(
                state,
                usage,
                policy=deps.policy or WorkflowPolicy(),
                invocation="tool",
            )
        except _RuntimeAdmissionBlocked as blocked:
            blocked.tool_call_names = [name]
            raise
        usage.record_tool_invocation()
    started = perf_counter()
    stable_call_id = hashlib.sha256(
        f"{state.get('evidence_turn_id', 'unknown-turn')}:{attempt}:{call_id}".encode("utf-8")
    ).hexdigest()[:24]
    selected_queries = _query_list(arguments, query)
    if name == "web_search":
        selected_queries = selected_queries[:1]
    try:
        if name in {"read_episode_memory_level_2", "search_session_conversation"}:
            compressed = _run_memory_context_tool(
                name=name,
                arguments=arguments,
                state=state,
                deps=deps,
            )
            tool_request = _safe_tool_call_request(arguments)
            event = {
                "call_id": stable_call_id,
                "tool_name": name,
                "attempt": attempt,
                "outcome": compressed.status,
                "latency_ms": round((perf_counter() - started) * 1000),
                "result_count": compressed.result_count,
                "request": tool_request,
                "timeout_ms": round(deps.tool_timeout_seconds * 1000),
            }
            if compressed.status in {"failed", "timeout", "blocked", "invalid"}:
                event["failure_category"] = compressed.status
                event["failure_error"] = _sanitized_exception_message(
                    RuntimeError(compressed.error or f"{compressed.status}: memory retrieval failed")
                )
            internal_event = _make_internal_event(
                "consultant",
                "tool_result",
                compressed.rendered,
                attempt=attempt,
                tool_name=name,
                tool_arguments=tool_request,
                outcome=compressed.status,
                result_count=compressed.result_count,
                timeout_ms=round(deps.tool_timeout_seconds * 1000),
                exception_message=event.get("failure_error", ""),
            )
            return (
                ToolMessage(tool_call_id=call_id, content=_retrieval_tool_message_content(compressed)),
                event,
                [],
                [],
                internal_event,
            )

        fault_profile = os.getenv("REHAB_EVAL_FAULT_PROFILE", "").strip().lower()
        if evaluator_mode_enabled() and fault_profile == "tool_failure":
            raise RuntimeError("evaluator tool fault profile")
        if evaluator_mode_enabled() and fault_profile == "timeout":
            raise TimeoutError("evaluator timeout fault profile")
        if evaluator_mode_enabled() and fault_profile == "evidence_unavailable":
            compressed = compressed_tool_failure(name, "failed")
            raw_docs: list[dict[str, Any]] = []
            source_refs: list[Mapping[str, str]] = []
        elif name == "rehab_exercise_kb_search":
            batches = [
                _search_adapter_result(
                    deps.rehab_exercise_kb_search,
                    selected_query,
                    _bounded_result_limit(arguments.get("limit", _CONSULTANT_MAX_RESULTS_PER_QUERY)),
                    timeout_seconds=deps.tool_timeout_seconds,
                )
                for selected_query in selected_queries
            ]
            compressed = _combine_compressed_results(
                name,
                [compress_kb_results(batch) for batch in batches],
            )
            raw_docs = []
            for batch in batches:
                if isinstance(batch, (list, tuple)):
                    raw_docs.extend(
                        dict(item) for item in batch if isinstance(item, Mapping)
                    )
                    continue
                if not isinstance(batch, Mapping) or batch.get("error"):
                    continue
                for key in ("results", "documents", "items", "data"):
                    records = batch.get(key)
                    if isinstance(records, (list, tuple)):
                        raw_docs.extend(
                            dict(item) for item in records if isinstance(item, Mapping)
                        )
                        break
                else:
                    raw_docs.append(dict(batch))
            source_refs = []
        else:
            raw_results = [
                _search_adapter_result(
                    deps.web_search or _default_reliable_web_search,
                    selected_query,
                    timeout_seconds=deps.tool_timeout_seconds,
                )
                for selected_query in selected_queries
            ]
            compressed = _combine_compressed_results(
                name,
                [compress_web_results(result) for result in raw_results],
            )
            # A transient web failure gets one deterministic narrower retry. The
            # evaluator fault profile is handled above and must remain a failure.
            if compressed.status == "failed" and name == "web_search" and selected_queries:
                narrowed_query = " ".join(selected_queries[0].split()[:6]).strip()
                if narrowed_query and narrowed_query != selected_queries[0]:
                    retry_result = _search_adapter_result(
                        deps.web_search or _default_reliable_web_search,
                        narrowed_query,
                        timeout_seconds=deps.tool_timeout_seconds,
                    )
                    compressed = compress_web_results(retry_result)
                    selected_queries = [narrowed_query]
            raw_docs = []
            source_refs = list(compressed.source_refs)
    except TimeoutError as exc:
        compressed = compressed_tool_failure(name, "timeout", _sanitized_exception_message(exc))
        raw_docs = []
        source_refs = []
    except PermissionError as exc:
        compressed = compressed_tool_failure(name, "blocked", _sanitized_exception_message(exc))
        raw_docs = []
        source_refs = []
    except (TypeError, ValueError) as exc:
        compressed = compressed_tool_failure(name, "invalid", _sanitized_exception_message(exc))
        raw_docs = []
        source_refs = []
    except Exception as exc:
        compressed = compressed_tool_failure(name, "failed", _sanitized_exception_message(exc))
        raw_docs = []
        source_refs = []

    tool_request = _safe_tool_call_request(arguments)
    event = {
        "call_id": stable_call_id,
        "tool_name": name,
        "attempt": attempt,
        "outcome": compressed.status,
        "latency_ms": round((perf_counter() - started) * 1000),
        "result_count": compressed.result_count,
        "query_count": len(selected_queries),
        "query_budget": _CONSULTANT_MAX_RETRIEVALS_PER_CALL,
        "result_budget": _CONSULTANT_MAX_RESULTS_PER_QUERY,
        "call_budget": _CONSULTANT_MAX_TOOL_CALLS,
        "timeout_ms": round(deps.tool_timeout_seconds * 1000),
        "request": tool_request,
    }
    if compressed.status in {"failed", "timeout", "blocked", "invalid"}:
        event["failure_category"] = compressed.status
        event["failure_error"] = _sanitized_exception_message(
            RuntimeError(compressed.error or f"{compressed.status}: provider result failed validation")
        )
    tool_record = {
        "tool_name": name,
        "status": compressed.status,
        "result_count": compressed.result_count,
        "request": tool_request,
        "rendered": (
            f"Consultant tool call {name} request: "
            f"{json.dumps(tool_request, ensure_ascii=False, sort_keys=True)}\n"
            f"Retrieval state: {compressed.status}. "
            f"Compressed {name} result:\n{compressed.rendered}"
        ),
    }
    turn_id = str(state.get("evidence_turn_id") or "")
    if turn_id:
        try:
            store_tool_record(turn_id, tool_record)
        except KeyError:
            pass
    internal_event = _make_internal_event(
        "consultant",
        "tool_result",
        compressed.rendered,
        attempt=attempt,
        tool_name=name,
        tool_arguments=tool_request,
        outcome=compressed.status,
        result_count=compressed.result_count,
        query_count=len(selected_queries),
        query_budget=_CONSULTANT_MAX_RETRIEVALS_PER_CALL,
        result_budget=_CONSULTANT_MAX_RESULTS_PER_QUERY,
        call_budget=_CONSULTANT_MAX_TOOL_CALLS,
        timeout_ms=round(deps.tool_timeout_seconds * 1000),
        exception_message=event.get("failure_error", ""),
    )
    return (
        ToolMessage(tool_call_id=call_id, content=_retrieval_tool_message_content(compressed)),
        event,
        raw_docs,
        source_refs,
        internal_event,
    )
