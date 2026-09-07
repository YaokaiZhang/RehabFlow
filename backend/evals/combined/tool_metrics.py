"""Privacy-safe tool-call records and evaluator metrics."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import isfinite
import re
from typing import Iterable, Mapping


TOOL_OUTCOMES = (
    "ok_nonempty",
    "ok_empty",
    "blocked",
    "invalid",
    "timeout",
    "error",
)
_OUTCOME_SET = frozenset(TOOL_OUTCOMES)
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_FORBIDDEN_KEY = re.compile(
    r"(?:prompt|argument|result|content|response)",
    re.IGNORECASE,
)


def _safe_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not _SAFE_TEXT.fullmatch(normalized):
        raise ValueError(f"{field_name} must be privacy-safe metadata")
    return normalized


def _safe_metadata(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be a mapping")
    result: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if _FORBIDDEN_KEY.search(key):
            raise ValueError(f"forbidden tool metadata field: {key}")
        if isinstance(raw_value, bool):
            result[key] = raw_value
        elif isinstance(raw_value, int) and not isinstance(raw_value, bool) and raw_value >= 0:
            result[key] = raw_value
        elif isinstance(raw_value, float) and isfinite(raw_value) and raw_value >= 0:
            result[key] = raw_value
        elif isinstance(raw_value, str):
            result[key] = _safe_text(raw_value, field_name=f"metadata[{key}]")
        else:
            raise ValueError("tool metadata value is not scalar")
    return result


@dataclass(frozen=True)
class ToolCallRecord:
    """One terminal model-requested tool attempt without clinical payload."""

    call_id: str
    turn_correlation: str
    tool_name: str
    attempt: int
    outcome: str
    latency_ms: int | None = None
    result_count: int | None = None
    failure_category: str | None = None
    retry_of: str | None = None
    retry_count: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "call_id", _safe_text(self.call_id, field_name="call_id"))
        object.__setattr__(
            self,
            "turn_correlation",
            _safe_text(self.turn_correlation, field_name="turn_correlation"),
        )
        object.__setattr__(self, "tool_name", _safe_text(self.tool_name, field_name="tool_name"))
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if self.outcome not in _OUTCOME_SET:
            raise ValueError(f"outcome must be one of {sorted(_OUTCOME_SET)}")
        for field_name in ("latency_ms", "result_count", "retry_count"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.result_count is not None and self.outcome not in {"ok_nonempty", "ok_empty"}:
            raise ValueError("result_count is only valid for successful tool outcomes")
        if self.failure_category is not None:
            object.__setattr__(
                self,
                "failure_category",
                _safe_text(self.failure_category, field_name="failure_category"),
            )
        if self.retry_of is not None:
            object.__setattr__(
                self,
                "retry_of",
                _safe_text(self.retry_of, field_name="retry_of"),
            )
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        for field_name in (
            "call_id",
            "turn_correlation",
            "tool_name",
            "attempt",
            "outcome",
            "latency_ms",
            "result_count",
            "failure_category",
            "retry_of",
            "retry_count",
        ):
            value = getattr(self, field_name)
            if value is not None:
                payload[field_name] = value
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def _as_record(record: ToolCallRecord | Mapping[str, object]) -> ToolCallRecord:
    return record if isinstance(record, ToolCallRecord) else ToolCallRecord(**dict(record))


def aggregate_tool_metrics(
    records: Iterable[ToolCallRecord | Mapping[str, object]],
) -> dict[str, object]:
    normalized = [_as_record(record) for record in records]
    counts = Counter(record.outcome for record in normalized)
    attempted = len(normalized)
    executed = sum(record.outcome not in {"blocked", "invalid"} for record in normalized)
    retried = sum(record.retry_of is not None or record.attempt > 1 for record in normalized)
    recovered = 0
    by_call: dict[str, list[ToolCallRecord]] = defaultdict(list)
    for record in normalized:
        by_call[record.retry_of or record.call_id].append(record)
    for attempts in by_call.values():
        if len(attempts) < 2:
            continue
        if attempts[-1].outcome in {"ok_nonempty", "ok_empty"} and any(
            item.outcome not in {"ok_nonempty", "ok_empty"} for item in attempts[:-1]
        ):
            recovered += 1
    successful = counts["ok_nonempty"] + counts["ok_empty"]
    return {
        "attempted_calls": attempted,
        "executed_calls": executed,
        "ok_nonempty_calls": counts["ok_nonempty"],
        "attempted": attempted,
        "executed": executed,
        "failed_calls": counts["timeout"] + counts["error"],
        "rejected_calls": counts["blocked"] + counts["invalid"],
        "ok_empty_calls": counts["ok_empty"],
        "blocked_calls": counts["blocked"],
        "invalid_calls": counts["invalid"],
        "timeout_calls": counts["timeout"],
        "error_calls": counts["error"],
        "retried_calls": retried,
        "calls_recovered_after_retry": recovered,
        "execution_success_rate": successful / attempted if attempted else None,
        "useful_result_rate": counts["ok_nonempty"] / attempted if attempted else None,
    }


def tool_expectation_failures(
    records: Iterable[ToolCallRecord | Mapping[str, object]],
    expectations: Mapping[str, object] | None,
) -> list[str]:
    if not expectations:
        return []
    normalized = [_as_record(record) for record in records]
    names = {record.tool_name for record in normalized}
    failures: list[str] = []

    def strings(value: object) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, (list, tuple, set, frozenset)):
            return {str(item) for item in value}
        return set()

    required = strings(
        expectations.get("required_tools", expectations.get("required_tool_names"))
    )
    forbidden = strings(
        expectations.get("forbidden_tools", expectations.get("forbidden_tool_names"))
    )
    failures.extend(
        f"required_tool_missing:{name}" for name in sorted(required - names)
    )
    failures.extend(
        f"forbidden_tool_called:{name}" for name in sorted(forbidden & names)
    )
    integer_rules = {
        "minimum_attempts": "attempted_calls",
        "min_attempts": "attempted_calls",
        "minimum_successful_calls": "successful_calls",
        "min_successful_calls": "successful_calls",
        "max_blocked": "blocked_calls",
        "max_invalid": "invalid_calls",
        "max_timeout": "timeout_calls",
        "max_error": "error_calls",
    }
    metrics = aggregate_tool_metrics(normalized)
    metrics["successful_calls"] = metrics["ok_nonempty_calls"] + metrics["ok_empty_calls"]
    for key, metric_key in integer_rules.items():
        if key not in expectations:
            continue
        expected = expectations[key]
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            failures.append(f"invalid_tool_expectation:{key}")
        elif key.startswith("max_") and int(metrics[metric_key]) > expected:
            failures.append(f"{key}_exceeded")
        elif key.startswith(("min_", "minimum_")) and int(metrics[metric_key]) < expected:
            failures.append(f"{key}_not_met")
    per_tool = expectations.get("per_tool")
    if isinstance(per_tool, Mapping):
        for raw_name, raw_expectation in per_tool.items():
            if not isinstance(raw_expectation, Mapping):
                failures.append(f"invalid_tool_expectation:{raw_name}")
                continue
            tool_name = str(raw_name)
            scoped = [record for record in normalized if record.tool_name == tool_name]
            scoped_failures = tool_expectation_failures(scoped, raw_expectation)
            failures.extend(f"{tool_name}:{failure}" for failure in scoped_failures)
    return list(dict.fromkeys(failures))


def group_tool_metrics(
    records: Iterable[ToolCallRecord | Mapping[str, object]],
    *,
    key: str,
) -> dict[str, dict[str, object]]:
    if key not in {"tool_name", "turn_correlation", "suite", "case_id"}:
        raise ValueError(f"unsupported tool metric group: {key}")
    grouped: dict[str, list[ToolCallRecord]] = defaultdict(list)
    for record in (_as_record(item) for item in records):
        value = record.tool_name if key == "tool_name" else record.turn_correlation
        grouped[value].append(record)
    return {name: aggregate_tool_metrics(items) for name, items in sorted(grouped.items())}


def select_tool_telemetry_records(telemetry: Mapping[str, object]) -> list[object]:
    """Prefer structured terminal events over name-only tool-call hints."""
    raw_calls = telemetry.get("tool_calls")
    raw_events = telemetry.get("tool_events")
    if isinstance(raw_calls, (list, tuple)):
        has_terminal_data = any(
            isinstance(item, Mapping)
            and any(
                key in item
                for key in (
                    "outcome",
                    "status",
                    "result_count",
                    "count",
                    "failure_category",
                )
            )
            for item in raw_calls
        )
        if has_terminal_data or not isinstance(raw_events, (list, tuple)) or not raw_events:
            return list(raw_calls)
    if isinstance(raw_events, (list, tuple)):
        return list(raw_events)
    return []


def records_from_backend_telemetry(
    telemetry: Mapping[str, object],
    *,
    turn_correlation: str,
) -> list[ToolCallRecord]:
    raw_records = select_tool_telemetry_records(telemetry)
    if not isinstance(raw_records, (list, tuple)):
        return []
    records: list[ToolCallRecord] = []
    for index, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, Mapping):
            continue
        outcome = str(raw.get("outcome") or raw.get("status") or "error").lower()
        result_count = raw.get("result_count", raw.get("count"))
        if outcome in {"ok", "success", "completed"}:
            outcome = "ok_nonempty" if isinstance(result_count, int) and result_count > 0 else "ok_empty"
        elif outcome in {"empty", "no_results"}:
            outcome = "ok_empty"
        elif outcome in {"reject", "rejected", "policy_rejection"}:
            outcome = "blocked"
        elif outcome in {"invalid", "invalid_request"}:
            outcome = "invalid"
        elif outcome in {"timeout", "timed_out"}:
            outcome = "timeout"
        elif outcome not in _OUTCOME_SET:
            outcome = "error"
        name = str(raw.get("tool_name") or raw.get("name") or "unknown_tool")
        call_id = str(raw.get("call_id") or f"call-{index}")
        attempt = raw.get("attempt", index)
        records.append(
            ToolCallRecord(
                call_id=call_id,
                turn_correlation=turn_correlation,
                tool_name=name,
                attempt=attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else index,
                outcome=outcome,
                latency_ms=raw.get("latency_ms") if isinstance(raw.get("latency_ms"), int) else None,
                result_count=result_count if isinstance(result_count, int) and result_count >= 0 and outcome in {"ok_nonempty", "ok_empty"} else None,
                failure_category=(
                    str(raw.get("failure_category"))
                    if raw.get("failure_category") is not None
                    else None
                ),
                retry_of=str(raw["retry_of"]) if raw.get("retry_of") is not None else None,
                retry_count=raw.get("retry_count") if isinstance(raw.get("retry_count"), int) else None,
            )
        )
    return records

