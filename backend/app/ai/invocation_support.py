"""Low-level invocation, trace-event, and tool-call support contracts.

This module is intentionally independent from graph composition and owner nodes.
It contains shared mechanics used by Consultant invocation and tool dispatch.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Mapping

from pydantic import BaseModel

from app.ai.graph_tracing import _trace_attempt
from app.ai.model_input_budget import model_messages_estimated_tokens
from app.ai.policy_gate import GateSeverity, evaluate_invocation_admission
from app.ai.reliable_web_search import default_reliable_web_search
from app.ai.tool_result_compression import CompressedToolResult
from app.ai.workflow_policy import WorkflowPolicy
from app.ai.workflow_state import RehabGraphState

logger = logging.getLogger(__name__)
_ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rehab-tool")
_CONSULTANT_BATCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="rehab-tool-batch",
)
DEBUG_GRAPH = (
    _ENVIRONMENT not in {"prod", "production"}
    and os.getenv("REHAB_GRAPH_DEBUG", "false").lower()
    in {"1", "true", "yes", "on"}
)

_CONSULTANT_MAX_RETRIEVALS_PER_CALL = 4
_CONSULTANT_MAX_RESULTS_PER_QUERY = 5


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return str(value)


def _valid_counter(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _response_usage(
    response: object,
) -> tuple[int | None, int | None, int | None]:
    """Read provider usage without guessing input/output labels from totals."""
    metadata_candidates = (
        getattr(response, "usage_metadata", None),
        getattr(response, "response_metadata", None),
        getattr(response, "usage", None),
    )
    for metadata in metadata_candidates:
        if not isinstance(metadata, Mapping):
            continue
        for usage in (metadata, metadata.get("token_usage"), metadata.get("usage")):
            if not isinstance(usage, Mapping):
                continue
            input_tokens = _valid_counter(usage.get("input_tokens"))
            if input_tokens is None:
                input_tokens = _valid_counter(usage.get("prompt_tokens"))
            output_tokens = _valid_counter(usage.get("output_tokens"))
            if output_tokens is None:
                output_tokens = _valid_counter(usage.get("completion_tokens"))
            total = _valid_counter(usage.get("total_tokens"))
            if total is not None:
                return input_tokens, output_tokens, total
            if input_tokens is not None or output_tokens is not None:
                return (
                    input_tokens,
                    output_tokens,
                    (input_tokens or 0) + (output_tokens or 0),
                )
    return None, None, None


def _estimated_response_tokens(response: object) -> int | None:
    """Estimate structured-output size when a wrapper strips usage metadata."""
    try:
        if isinstance(response, BaseModel):
            text = response.model_dump_json()
        else:
            text = getattr(response, "content", "")
        compact = " ".join(str(text or "").split())
        return max(1, (len(compact) + 3) // 4) if compact else None
    except Exception:
        return None


def _response_total_tokens(response: object) -> int | None:
    return _response_usage(response)[2]


@dataclass
class _RuntimeUsage:
    model_calls: int | None
    tool_calls: int | None
    total_tokens: int | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    _pending_reserved_model_calls: int = field(default=0, init=False, repr=False)

    @classmethod
    def from_state(cls, state: RehabGraphState) -> "_RuntimeUsage":
        return cls(
            model_calls=_valid_counter(state.get("model_calls", 0)),
            tool_calls=_valid_counter(state.get("tool_calls", 0)),
            total_tokens=_valid_counter(state.get("total_tokens", 0)),
            input_tokens=_valid_counter(state.get("input_tokens")),
            output_tokens=_valid_counter(state.get("output_tokens")),
        )

    def record_model_response(self, response: object) -> None:
        self._record_model_response(response, reserved=False)

    def _record_model_response(self, response: object, *, reserved: bool) -> None:
        input_tokens, output_tokens, response_tokens = _response_usage(response)
        if response_tokens is None:
            response_tokens = _estimated_response_tokens(response)
        completed_before = (
            None
            if self.model_calls is None
            else max(0, self.model_calls - self._pending_reserved_model_calls)
        )
        if input_tokens is None:
            self.input_tokens = None
        elif completed_before == 0 and self.input_tokens is None:
            self.input_tokens = input_tokens
        elif self.input_tokens is not None:
            self.input_tokens += input_tokens
        if output_tokens is None:
            self.output_tokens = None
        elif completed_before == 0 and self.output_tokens is None:
            self.output_tokens = output_tokens
        elif self.output_tokens is not None:
            self.output_tokens += output_tokens
        if reserved:
            self._pending_reserved_model_calls = max(
                0, self._pending_reserved_model_calls - 1
            )
        else:
            self.model_calls = (
                None if self.model_calls is None else self.model_calls + 1
            )
        self.total_tokens = (
            None
            if self.total_tokens is None or response_tokens is None
            else self.total_tokens + response_tokens
        )

    def record_failed_model_invocation(self, *, reserved: bool = False) -> None:
        if reserved:
            self._pending_reserved_model_calls = max(
                0, self._pending_reserved_model_calls - 1
            )
        else:
            self.model_calls = (
                None if self.model_calls is None else self.model_calls + 1
            )
        self.total_tokens = None
        self.input_tokens = None
        self.output_tokens = None

    def reserve_model_invocation(self) -> None:
        self.model_calls = None if self.model_calls is None else self.model_calls + 1
        self._pending_reserved_model_calls += 1

    def record_reserved_model_response(self, response: object) -> None:
        self._record_model_response(response, reserved=True)

    def record_tool_invocation(self) -> None:
        self.tool_calls = None if self.tool_calls is None else self.tool_calls + 1

    def updates(self) -> dict[str, int | None]:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class _RuntimeAdmissionBlocked(RuntimeError):
    def __init__(
        self,
        result: object,
        *,
        tool_call_names: list[str] | None = None,
    ) -> None:
        self.result = result
        self.tool_call_names = tool_call_names
        super().__init__("runtime invocation admission blocked")


def _require_invocation_admission(
    state: RehabGraphState,
    usage: _RuntimeUsage,
    *,
    policy: WorkflowPolicy,
    invocation: str,
) -> None:
    result = evaluate_invocation_admission(
        {
            "budget": state.get("budget"),
            "model_calls": usage.model_calls,
            "tool_calls": usage.tool_calls,
            "total_tokens": usage.total_tokens,
            "now": datetime.now(timezone.utc),
        },
        policy=policy,
        invocation=invocation,
    )
    if result.severity is GateSeverity.HARD_STOP:
        raise _RuntimeAdmissionBlocked(result)


def _blocked_invocation_updates(
    state: RehabGraphState,
    usage: _RuntimeUsage,
    blocked: _RuntimeAdmissionBlocked,
) -> RehabGraphState:
    result = blocked.result
    for _ in blocked.tool_call_names or ():
        usage.record_tool_invocation()
    return {
        **usage.updates(),
        "tool_call_names": list(blocked.tool_call_names or ()),
        "tool_events": [
            {
                "call_id": hashlib.sha256(
                    f"{state.get('evidence_turn_id', 'unknown-turn')}:{index}:{name}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:24],
                "tool_name": str(name),
                "attempt": max(1, _trace_attempt(state)),
                "outcome": "blocked",
                "failure_category": "policy_rejection",
            }
            for index, name in enumerate(blocked.tool_call_names or (), start=1)
        ],
        "policy_gate_severity": result.severity.value,
        "policy_gate_reason_codes": result.reason_codes,
        "debug_trace": _debug_event(
            state,
            "runtime_admission",
            {
                "severity": result.severity.value,
                "reason_codes": result.reason_codes,
            },
        ),
    }


def _append_debug_event(
    state: RehabGraphState,
    node: str,
    details: dict[str, Any],
) -> list[dict[str, Any]]:
    trace = list(state.get("debug_trace", []))
    safe_details: dict[str, Any] = {}
    dropped_failure = False
    dropped_keys = {
        "content", "error", "feedback", "last_feedback", "patient_text",
        "prompt", "reason", "response_preview", "tool_calling_error",
        "session_id", "patient_id", "care_episode_id", "principal_id",
        "owner_id", "user_id",
    }
    for raw_key, value in details.items():
        key = str(raw_key)
        if key in dropped_keys:
            dropped_failure = dropped_failure or key in {
                "error", "reason", "tool_calling_error"
            }
            continue
        if isinstance(value, bool) or (
            isinstance(value, int) and not isinstance(value, bool)
        ):
            safe_details[key] = value
        elif key == "context_packet_tokens" and isinstance(value, Mapping):
            safe_details[key] = {
                str(raw_node)[:64]: raw_tokens
                for raw_node, raw_tokens in value.items()
                if (
                    str(raw_node).strip()
                    and all(
                        char.isalnum() or char in "._:/-"
                        for char in str(raw_node).strip()
                    )
                    and isinstance(raw_tokens, int)
                    and not isinstance(raw_tokens, bool)
                    and raw_tokens >= 0
                )
            }
        elif isinstance(value, (list, tuple, set)):
            safe_details[key] = [
                str(item)[:128]
                for item in value
                if isinstance(item, str)
                and item
                and all(char.isalnum() or char in "._:/-" for char in item)
            ]
        elif (
            isinstance(value, str)
            and len(value) <= 128
            and all(char.isalnum() or char in "._:/-" for char in value)
        ):
            safe_details[key] = value
    if dropped_failure and "failure_category" not in safe_details:
        safe_details["failure_category"] = "runtime_error"
    trace.append(
        {
            "step": len(trace) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node": node,
            **safe_details,
        }
    )
    return trace


def _safe_str(value: Any, max_len: int = 400) -> str:
    text = str(value)
    return text if len(text) <= max_len else f"{text[:max_len]}..."


def _debug_event(
    state: RehabGraphState,
    node: str,
    details: dict[str, Any],
) -> list[dict[str, Any]]:
    trace = _append_debug_event(state, node, details)
    if DEBUG_GRAPH:
        logger.info("[%s] debug_event_emitted", node)
    return trace


def _internal_log_events(
    state: RehabGraphState,
    *events: dict[str, Any],
) -> list[dict[str, Any]]:
    del state
    return list(events)


def _serialize_trace_value(value: Any) -> str:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        value = value.model_dump()
    elif hasattr(value, "content"):
        value = getattr(value, "content")
    try:
        return json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except Exception:
        return str(value)


def _serialize_model_messages(messages: list[Any]) -> str:
    payload = [
        {
            "role": str(
                getattr(message, "type", None) or message.__class__.__name__
            ),
            "content": _json_safe(getattr(message, "content", message)),
        }
        for message in messages
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _sanitized_exception_message(exc: BaseException) -> str:
    from app.services.ai_turn_persistence import redact_internal_text

    normalized = " ".join(str(exc).split()) or type(exc).__name__
    return redact_internal_text(normalized)[:500]


def _model_failure_event(
    agent_name: str,
    messages: list[Any],
    exc: Exception,
    *,
    started: float,
    state: Mapping[str, Any] | None = None,
    failing_boundary: str | None = None,
    model_call_attempt_count: int | None = None,
) -> dict[str, Any]:
    serialized_input = _serialize_model_messages(messages)
    exception_message = _sanitized_exception_message(exc)
    provider_status = getattr(exc, "status_code", None)
    if provider_status is None:
        provider_status = getattr(exc, "status", None)
    timeout_status = isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()
    trace_metadata: dict[str, Any] = {
        "model_call": True,
        "status": "error",
        "error_type": type(exc).__name__,
        "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        "usage_unavailable_reason": "provider_did_not_report_complete_usage",
        "exception_message": exception_message,
        "serialized_provider_input_estimate": {
            "chars": len(serialized_input),
            "estimated_tokens": model_messages_estimated_tokens(messages),
        },
        "model_call_attempt_count": (
            model_call_attempt_count
            if model_call_attempt_count is not None
            else (
                int(state.get("model_calls", 0) or 0) + 1
                if state is not None
                else 1
            )
        ),
        "failing_boundary": failing_boundary or agent_name,
        "timeout_status": timeout_status,
    }
    if state is not None:
        turn_id = (
            state.get("trace_turn_id")
            or state.get("evidence_turn_id")
            or state.get("review_turn_id")
        )
        if turn_id:
            trace_metadata["turn_id"] = str(turn_id)
        trace_metadata["sequence"] = len(state.get("debug_trace", []) or ()) + 1
    if provider_status is not None:
        trace_metadata["provider_status"] = str(provider_status)[:64]
    return _make_internal_event(
        agent_name,
        "agent_output",
        _serialize_trace_value(
            {"status": "error", "error_type": type(exc).__name__}
        ),
        model_input=serialized_input,
        **trace_metadata,
    )


def _model_trace_event(
    agent_name: str,
    messages: list[Any],
    response: Any,
    *,
    started: float,
    state: Mapping[str, Any] | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    input_text = _serialize_model_messages(messages)
    output_text = _serialize_trace_value(response)
    input_tokens, output_tokens, total_tokens = _response_usage(response)
    trace_metadata = {"status": "completed", **dict(metadata)}
    if state is not None:
        turn_id = (
            state.get("trace_turn_id")
            or state.get("evidence_turn_id")
            or state.get("review_turn_id")
        )
        if turn_id:
            trace_metadata.setdefault("turn_id", str(turn_id))
        trace_metadata.setdefault(
            "sequence", len(state.get("debug_trace", []) or ()) + 1
        )
    if input_tokens is None or output_tokens is None or total_tokens is None:
        trace_metadata.setdefault(
            "usage_unavailable_reason",
            "provider_did_not_report_complete_usage",
        )
    return _make_internal_event(
        agent_name,
        "agent_output",
        output_text,
        model_input=input_text,
        model_call=True,
        elapsed_ms=round((perf_counter() - started) * 1000, 2),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        **trace_metadata,
    )


def _make_internal_event(
    agent_name: str,
    event_type: str,
    content: str = "",
    *,
    model_input: str = "",
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "agent_name": agent_name,
        "event_type": event_type,
        "content": str(content or ""),
        "model_input": str(model_input or ""),
        "metadata": dict(metadata),
    }


def _default_reliable_web_search(query: str) -> Any:
    return default_reliable_web_search(query)


def _query_list(arguments: Mapping[str, Any], fallback: str) -> list[str]:
    raw_queries = arguments.get("queries")
    if isinstance(raw_queries, str):
        raw_queries = [raw_queries]
    if not isinstance(raw_queries, (list, tuple)):
        raw_queries = [arguments.get("query") or fallback]
    queries = [str(value).strip() for value in raw_queries if str(value).strip()]
    return (
        list(dict.fromkeys(queries))[:_CONSULTANT_MAX_RETRIEVALS_PER_CALL]
        or [fallback]
    )


def _tool_arguments(call: Any) -> dict[str, Any]:
    if isinstance(call, Mapping):
        function = call.get("function")
        arguments = call.get("args")
        if arguments is None and isinstance(function, Mapping):
            arguments = function.get("arguments")
    else:
        arguments = getattr(call, "args", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    return dict(arguments) if isinstance(arguments, Mapping) else {}


def _tool_call_name(call: Any) -> str:
    if isinstance(call, Mapping):
        function = call.get("function")
        function_name = function.get("name") if isinstance(function, Mapping) else ""
        return str(call.get("name") or function_name or "")
    return str(getattr(call, "name", "") or "")


def _tool_call_id(call: Any, index: int) -> str:
    if isinstance(call, Mapping):
        return str(call.get("id") or f"consultant-tool-{index}")
    return str(getattr(call, "id", "") or f"consultant-tool-{index}")


def _message_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content

    def text_parts(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, Mapping):
            text = value.get("text")
            if isinstance(text, str) and text.strip():
                return [text]
            nested = value.get("content")
            return text_parts(nested) if nested is not None else []
        if isinstance(value, (list, tuple)):
            parts: list[str] = []
            for item in value:
                parts.extend(text_parts(item))
            return parts
        return []

    text = "\n".join(part for part in text_parts(content) if part.strip())
    if text:
        return text
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


def _run_async(coroutine: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _bounded_result_limit(value: Any) -> int:
    try:
        return max(1, min(int(value), _CONSULTANT_MAX_RESULTS_PER_QUERY))
    except (TypeError, ValueError):
        return _CONSULTANT_MAX_RESULTS_PER_QUERY


def _retrieval_tool_message_content(compressed: CompressedToolResult) -> str:
    return (
        f"Retrieval state: {compressed.status}. A failed state means the provider "
        "errored; ok_empty means the provider returned successfully without usable "
        "evidence. Neither state is evidence.\n"
        f"{compressed.rendered}"
    )
