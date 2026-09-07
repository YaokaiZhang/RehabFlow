from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from typing import Any


_REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_-]{6,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|password|secret|credential)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\b(?:secret|credential|token)[_-][A-Za-z0-9._-]{3,}\b"),
)
_REAL_ID_PATTERNS = (
    re.compile(r"(?i)\bpatient[_ -]?\d+\b"),
    re.compile(r"(?i)\breal[_ -]?patient[_ -]?[A-Za-z0-9-]+\b"),
    re.compile(r"(?i)\b(?:mrn|medical[_ -]?record)\s*[:#-]?\s*[A-Za-z0-9-]+\b"),
)


def _primitive(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_primitive(item) for item in value]
    if is_dataclass(value):
        return _primitive(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _primitive(model_dump(mode="json"))
        except TypeError:
            return _primitive(model_dump())
    content = getattr(value, "content", None)
    if content is not None:
        return {"content": _primitive(content)}
    return str(value)


def redact_trace_value(value: object, *, secrets: Sequence[str] = ()) -> object:
    if isinstance(value, str):
        result = value
        for secret in sorted((str(item) for item in secrets if str(item)), key=len, reverse=True):
            result = result.replace(secret, _REDACTED)
        for pattern in (*_SECRET_PATTERNS, *_REAL_ID_PATTERNS):
            result = pattern.sub(_REDACTED, result)
        return result
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if re.search(r"(?i)(api[_-]?key|password|secret|credential|authorization)", normalized_key):
                output[normalized_key] = _REDACTED
            else:
                output[normalized_key] = redact_trace_value(item, secrets=secrets)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_trace_value(item, secrets=secrets) for item in value]
    converted = _primitive(value)
    if converted is value:
        return converted
    return redact_trace_value(converted, secrets=secrets)


def _build_count_cross_checks(
    events: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    model_requests = sum(
        item.get("event_type") in {"model_request", "judge_request"}
        for item in events
    )
    tool_calls = sum(item.get("event_type") == "tool_call" for item in events)
    repairs = sum(
        item.get("event_type") in {"repair_started", "repair", "repair_completed"}
        for item in events
    )
    response_payloads = [
        item.get("payload")
        for item in events
        if item.get("event_type") in {"model_response", "judge_response"}
        and isinstance(item.get("payload"), Mapping)
    ]
    if not response_payloads:
        trace_total_tokens: int | None = 0
    elif all(
        isinstance(payload.get("input_tokens"), int)
        and isinstance(payload.get("output_tokens"), int)
        for payload in response_payloads
    ):
        trace_total_tokens = sum(
            int(payload["input_tokens"]) + int(payload["output_tokens"])
            for payload in response_payloads
        )
    else:
        trace_total_tokens = None
    derived = {
        "model_calls": model_requests,
        "tool_calls": tool_calls,
        "repair_count": repairs,
        "total_tokens": trace_total_tokens,
        "elapsed_ms": summary.get("elapsed_ms"),
    }
    checks: dict[str, dict[str, Any]] = {}
    for key, trace_value in derived.items():
        summary_value = summary.get(key)
        unavailable_reason = None
        if key == "elapsed_ms" and not isinstance(trace_value, int):
            unavailable_reason = "elapsed_not_measurable_by_trace"
        elif key == "total_tokens" and trace_value is None:
            unavailable_reason = "token_usage_unavailable_for_one_or_more_model_responses"
        elif key not in summary:
            unavailable_reason = f"summary_{key}_not_provided"
        matches = (
            summary_value == trace_value
            if unavailable_reason is None
            else None
        )
        checks[key] = {
            "summary": summary_value,
            "trace_derived": trace_value,
            "matches": matches,
            "unavailable_reason": unavailable_reason,
        }
    return checks


def refresh_trace_counts(
    trace: Mapping[str, Any],
    *,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    refreshed = deepcopy(dict(trace))
    final = dict(refreshed.get("final") or {})
    summary_value = dict(summary or final.get("summary") or {})
    final["summary"] = summary_value
    refreshed["final"] = final
    checks = _build_count_cross_checks(
        refreshed.get("events") or [],
        summary_value,
    )
    refreshed["count_cross_checks"] = checks
    failed_metrics = sorted(
        key for key, check in checks.items() if check.get("matches") is False
    )
    refreshed["acceptance"] = {
        "passed": not failed_metrics,
        "failed_metrics": failed_metrics,
        "unavailable_metrics": sorted(
            key
            for key, check in checks.items()
            if check.get("matches") is None
        ),
    }
    return refreshed


def append_judge_events(
    trace: Mapping[str, Any],
    *,
    request: object,
    raw_response: object = None,
    structured_output: object = None,
    rationale: object = None,
    model: str | None = None,
    error: Mapping[str, Any] | None = None,
    usage_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    refreshed = deepcopy(dict(trace))
    events = list(refreshed.get("events") or [])
    turns = refreshed.get("turns") or []
    turn_id = (
        str(turns[-1].get("turn_id"))
        if turns and isinstance(turns[-1], Mapping)
        else f"{refreshed.get('case_id', 'case')}:judge"
    )
    correlation_id = f"{refreshed.get('case_id', 'case')}:judge"
    next_sequence = max(
        (int(item.get("sequence", -1)) for item in events if isinstance(item, Mapping)),
        default=-1,
    ) + 1

    def add_event(event_type: str, payload: object) -> None:
        nonlocal next_sequence
        events.append(
            {
                "sequence": next_sequence,
                "event_type": event_type,
                "correlation_id": correlation_id,
                "turn_id": turn_id,
                "payload": redact_trace_value(payload),
            }
        )
        next_sequence += 1

    request_payload: dict[str, Any] = {"input": request}
    if model is not None:
        request_payload["model"] = model
    add_event("judge_request", request_payload)
    response_payload: dict[str, Any] = {
        "raw_output": raw_response,
        "structured_output": structured_output,
        "rubric_rationale": rationale,
    }
    if error is not None:
        response_payload["error"] = dict(error)
    if model is not None:
        response_payload["model"] = model
    if usage_metadata is not None:
        for key in ("input_tokens", "output_tokens"):
            value = usage_metadata.get(key)
            if isinstance(value, int) and value >= 0:
                response_payload[key] = value
    add_event("judge_response", response_payload)
    refreshed["events"] = events
    judge_payload = {
        "input": request,
        "raw_output": raw_response,
        "structured_output": structured_output,
        "rubric_rationale": rationale,
    }
    if error is not None:
        judge_payload["error"] = dict(error)
    refreshed["judge"] = redact_trace_value(judge_payload)
    final = dict(refreshed.get("final") or {})
    final["judge"] = redact_trace_value(judge_payload)
    summary = dict(final.get("summary") or {})
    summary["model_calls"] = int(summary.get("model_calls", 0) or 0) + 1
    if (
        usage_metadata is not None
        and isinstance(usage_metadata.get("input_tokens"), int)
        and isinstance(usage_metadata.get("output_tokens"), int)
        and isinstance(summary.get("total_tokens"), int)
    ):
        summary["total_tokens"] = (
            int(summary["total_tokens"])
            + int(usage_metadata["input_tokens"])
            + int(usage_metadata["output_tokens"])
        )
    else:
        summary["total_tokens"] = None
        reasons = dict(refreshed.get("unavailable_reasons") or {})
        reasons["judge_token_usage"] = "judge_provider_did_not_expose_token_usage"
        refreshed["unavailable_reasons"] = reasons
    final["summary"] = summary
    refreshed["final"] = final
    return refresh_trace_counts(refreshed)


class TraceRecorder:
    def __init__(
        self,
        case_id: str,
        *,
        secrets: Sequence[str] = (),
        run_configuration: Mapping[str, Any] | None = None,
    ) -> None:
        self.case_id = str(case_id)
        self.secrets = tuple(str(secret) for secret in secrets if str(secret))
        self.run_configuration = dict(run_configuration or {})
        self.events: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self._current_turn_id: str | None = None
        self._sequence = 0

    def redact(self, value: object) -> object:
        return redact_trace_value(value, secrets=self.secrets)

    def start_turn(
        self,
        turn_id: str,
        *,
        input_dialog: Sequence[Mapping[str, Any]] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self._current_turn_id = str(turn_id)
        turn = {
            "turn_id": self._current_turn_id,
            "input_dialog": self.redact(list(input_dialog or [])),
            "payload": self.redact(dict(payload or {})),
        }
        self.turns.append(turn)
        self.record("turn_input", turn)

    def record(
        self,
        event_type: str,
        payload: object = None,
        *,
        turn_id: str | None = None,
        unavailable_reason: str | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "sequence": self._sequence,
            "event_type": str(event_type),
            "correlation_id": str(turn_id or self._current_turn_id or f"{self.case_id}:run"),
            "turn_id": str(turn_id or self._current_turn_id or ""),
            "payload": self.redact(payload),
        }
        if unavailable_reason:
            event["unavailable_reason"] = str(unavailable_reason)
        self._sequence += 1
        self.events.append(event)
        return event

    def record_agent_event(self, event: object) -> dict[str, Any]:
        payload = self.redact(_primitive(event))
        if isinstance(payload, Mapping):
            payload = dict(payload)
            payload["trace_id"] = f"{self.case_id}:trace"
            payload["turn_id"] = self._current_turn_id or f"{self.case_id}:run"
            payload["session_hash"] = f"{self.case_id}:session"
            if "elapsed_ms" in payload:
                payload["elapsed_ms"] = 0
                payload["elapsed_ms_provenance"] = (
                    "unavailable:wall_clock_normalized_for_deterministic_replay"
                )
        event_type = (
            str(payload.get("event_type") or "graph_event")
            if isinstance(payload, Mapping)
            else "graph_event"
        )
        if event_type == "tool_finished" and isinstance(payload, Mapping):
            tool_name = str(payload.get("tool_name") or "unknown_tool")
            is_rehab_scripted_tool = tool_name == "rehab_exercise_kb_search"
            already_projected = any(
                item["event_type"] == "tool_call"
                and isinstance(item.get("payload"), Mapping)
                and item["payload"].get("_graph_event_index") == payload.get("event_index")
                and item.get("turn_id") == (self._current_turn_id or "")
                for item in self.events
            )
            if not is_rehab_scripted_tool and not already_projected:
                self.record(
                    "tool_call",
                    {
                        "tool_name": tool_name,
                        "_graph_event_index": payload.get("event_index"),
                        "arguments": {},
                        "arguments_unavailable_reason": (
                            "production_graph_trace_exposes_tool_name_but_not_arguments"
                        ),
                    },
                )
                self.record(
                    "tool_result",
                    {
                        "tool_name": tool_name,
                        "result": dict(payload),
                    },
                )
        return self.record(event_type, payload)

    def finalize(
        self,
        *,
        summary: Mapping[str, Any] | None = None,
        final_dialog: Sequence[Mapping[str, Any]] | None = None,
        unavailable_reasons: Mapping[str, str] | None = None,
        judge: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        summary_dict = dict(summary or {})
        final_payload = {
            "summary": self.redact(summary_dict),
            "dialog": self.redact(list(final_dialog or [])),
        }
        if judge is not None:
            final_payload["judge"] = self.redact(dict(judge))
        model_requests = sum(item["event_type"] in {"model_request", "judge_request"} for item in self.events)
        tool_calls = sum(item["event_type"] == "tool_call" for item in self.events)
        repairs = sum(
            item["event_type"] in {"repair_started", "repair", "repair_completed"}
            for item in self.events
        )
        model_response_payloads = [
            item["payload"]
            for item in self.events
            if item["event_type"] in {"model_response", "judge_response"}
            and isinstance(item.get("payload"), Mapping)
        ]
        token_usage_complete = bool(model_response_payloads) and all(
            isinstance(payload.get("input_tokens"), int)
            and isinstance(payload.get("output_tokens"), int)
            for payload in model_response_payloads
        )
        if not model_response_payloads:
            derived_total_tokens: int | None = 0
        elif token_usage_complete:
            derived_total_tokens = sum(
                int(payload["input_tokens"]) + int(payload["output_tokens"])
                for payload in model_response_payloads
            )
        else:
            derived_total_tokens = None
        derived = {
            "model_calls": model_requests,
            "tool_calls": tool_calls,
            "repair_count": repairs,
            "total_tokens": derived_total_tokens,
            "elapsed_ms": summary_dict.get("elapsed_ms"),
        }
        checks: dict[str, Any] = {}
        for key, trace_value in derived.items():
            summary_value = summary_dict.get(key)
            unavailable_reason = None
            if key == "elapsed_ms" and not isinstance(trace_value, int):
                unavailable_reason = "elapsed_not_measurable_by_trace"
            elif key == "total_tokens" and trace_value is None:
                unavailable_reason = "token_usage_unavailable_for_one_or_more_model_responses"
            elif key not in summary_dict:
                unavailable_reason = f"summary_{key}_not_provided"
            matches = (
                summary_value == trace_value
                if unavailable_reason is None
                else None
            )
            checks[key] = {
                "summary": summary_value,
                "trace_derived": trace_value,
                "matches": matches,
                "unavailable_reason": unavailable_reason,
            }
        reasons = dict(unavailable_reasons or {})
        if judge is None and "judge" not in reasons:
            reasons["judge"] = "not_configured"
        trace = {
            "schema_version": "agent-loop-trace-v1",
            "case_id": self.case_id,
            "run_configuration": self.redact(self.run_configuration),
            "turns": deepcopy(self.turns),
            "events": deepcopy(self.events),
            "final": final_payload,
            "count_cross_checks": checks,
            "unavailable_reasons": reasons,
            "judge": self.redact(dict(judge)) if judge is not None else None,
        }
        return refresh_trace_counts(trace)

