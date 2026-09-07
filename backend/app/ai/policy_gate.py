"""Pure deterministic workflow admission and release policy gates."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.ai.workflow_policy import WorkflowPolicy
from app.ai.workflow_state import RehabGraphState, ReviewStatus, Route, StrEnum


class GateSeverity(StrEnum):
    PASS = "pass"
    REPAIRABLE = "repairable"
    HARD_STOP = "hard_stop"


class GateResult(BaseModel):
    """Machine-readable result; natural-language clinical content stays outside this schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: GateSeverity
    reason_codes: list[str]


_ALLOWED_TRANSITIONS: dict[Route | None, frozenset[Route]] = {
    None: frozenset(Route),
    Route.URGENT: frozenset({Route.URGENT}),
    Route.CLARIFY: frozenset({Route.CLARIFY}),
    Route.CONSULTANT: frozenset({Route.CONSULTANT}),
    Route.UNSUPPORTED: frozenset({Route.UNSUPPORTED}),
}


def _values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (str(value),)
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return (str(value),)


def _parse_route(value: Any) -> tuple[Route | None, bool]:
    if value is None:
        return None, True
    if isinstance(value, Route):
        return value, True
    try:
        return Route(str(value)), True
    except ValueError:
        return None, False


def _parse_review_statuses(value: Any) -> tuple[set[ReviewStatus], bool]:
    statuses: set[ReviewStatus] = set()
    for raw_status in _values(value):
        try:
            statuses.add(ReviewStatus(raw_status))
        except ValueError:
            return set(), False
    return statuses, True


def _result(severity: GateSeverity, reasons: list[str]) -> GateResult:
    return GateResult(severity=severity, reason_codes=list(dict.fromkeys(reasons)))


def _within_budget(value: object, maximum: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= 0
        and not isinstance(maximum, bool)
        and isinstance(maximum, int)
        and value <= maximum
    )


def evaluate_policy_gate(
    control: Mapping[str, Any],
    *,
    policy: WorkflowPolicy,
) -> GateResult:
    """Evaluate supplied machine-control fields without models, prose, clocks, or storage."""
    hard: list[str] = []
    repairable: list[str] = []
    passed: list[str] = []

    for field, pass_reason, fail_reason in (
        ("principal_authorized", "principal_authorized", "principal_unauthorized"),
        ("session_authorized", "session_authorized", "session_unauthorized"),
        ("care_episode_authorized", "care_episode_authorized", "care_episode_unauthorized"),
    ):
        if control.get(field) is True:
            passed.append(pass_reason)
        else:
            hard.append(fail_reason)

    if policy.supports_workflow_version(control.get("workflow_version")):
        passed.append("workflow_version_supported")
    else:
        hard.append("workflow_version_unsupported")

    current_route, current_route_valid = _parse_route(control.get("current_route"))
    requested_route, requested_route_valid = _parse_route(control.get("requested_route"))
    if not current_route_valid:
        hard.append("current_route_invalid")
    if not requested_route_valid or requested_route is None:
        hard.append("requested_route_invalid")
    elif current_route_valid and requested_route in _ALLOWED_TRANSITIONS[current_route]:
        passed.append("route_transition_allowed")
    elif current_route_valid:
        hard.append("route_transition_forbidden")

    for field, pass_reason, fail_reason, allowed_key in (
        ("source_ids", "source_ids_authorized", "source_id_forbidden", "authorized_source_ids"),
        ("tool_names", "tool_names_authorized", "tool_name_forbidden", "authorized_tool_names"),
        ("catalog_ids", "catalog_ids_authorized", "catalog_id_forbidden", "allowed_catalog_ids"),
    ):
        values = set(_values(control.get(field)))
        allowed = set(_values(control.get(allowed_key)))
        if values <= allowed:
            passed.append(pass_reason)
        else:
            hard.append(fail_reason)

    budget = control.get("budget")
    if budget is None:
        hard.append("budget_missing")
    else:
        for counter, limit, pass_reason, fail_reason in (
            ("model_calls", "max_model_calls", "model_budget_within_limit", "model_budget_exceeded"),
            ("tool_calls", "max_tool_calls", "tool_budget_within_limit", "tool_budget_exceeded"),
        ):
            if _within_budget(control.get(counter, 0), getattr(budget, limit, None)):
                passed.append(pass_reason)
            else:
                hard.append(fail_reason)
        total_tokens = control.get("total_tokens", 0)
        max_total_tokens = getattr(budget, "max_total_tokens", None)
        if total_tokens is None:
            passed.append("token_usage_unavailable")
        elif _within_budget(total_tokens, max_total_tokens):
            passed.append("token_budget_within_limit")
        else:
            passed.append("token_budget_exceeded")

        now = control.get("now")
        deadline = getattr(budget, "deadline_at", None)
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
            or not isinstance(deadline, datetime)
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
        ):
            hard.append("time_unavailable")
        elif now > deadline:
            hard.append("deadline_exceeded")
        else:
            passed.append("deadline_within_limit")

    required_nodes = set(_values(control.get("required_nodes")))
    outcomes = control.get("required_outcomes")
    if isinstance(outcomes, Mapping) and all(outcomes.get(node) is not None for node in required_nodes):
        passed.append("required_node_outcomes_present")
    else:
        hard.append("required_node_outcome_missing")

    idempotency_key = control.get("idempotency_key")
    if idempotency_key is None:
        passed.append("idempotency_not_requested")
    elif not str(idempotency_key).strip():
        hard.append("idempotency_key_missing")
    else:
        request_hash = control.get("request_hash")
        prior_request_hash = control.get("prior_request_hash")
        request_hash_valid = isinstance(request_hash, str) and bool(request_hash.strip())
        prior_request_hash_valid = isinstance(prior_request_hash, str) and bool(prior_request_hash.strip())
        if not request_hash_valid:
            hard.append("request_hash_missing")
        if not prior_request_hash_valid:
            hard.append("prior_request_hash_missing")
        if request_hash_valid and prior_request_hash_valid:
            if request_hash == prior_request_hash:
                passed.append("idempotency_key_consistent")
            else:
                hard.append("idempotency_key_reused")

    repair_count = control.get("repair_count", 0)
    if (
        isinstance(repair_count, bool)
        or not isinstance(repair_count, int)
        or repair_count < 0
        or repair_count > policy.max_repairs
    ):
        hard.append("repair_count_invalid")

    statuses, statuses_valid = _parse_review_statuses(control.get("review_statuses"))
    if not statuses_valid:
        hard.append("review_status_invalid")
    elif statuses:
        if statuses == {ReviewStatus.APPROVE}:
            passed.append("review_approved")
        else:
            if ReviewStatus.TIMEOUT in statuses:
                repairable.append("review_timeout")
            if ReviewStatus.ERROR in statuses:
                repairable.append("review_error")
            if ReviewStatus.REJECT in statuses:
                repairable.append("review_rejected")
            repairable.append("review_not_approved")
            if repair_count == policy.max_repairs:
                hard.append("repair_budget_exhausted")

    if hard:
        return _result(GateSeverity.HARD_STOP, hard)
    if repairable:
        return _result(GateSeverity.REPAIRABLE, repairable)
    return _result(GateSeverity.PASS, passed)


def evaluate_invocation_admission(
    control: Mapping[str, Any],
    *,
    policy: WorkflowPolicy,
    invocation: str,
) -> GateResult:
    """Fail closed before a model or admitted tool consumes the next budget slot."""
    del policy  # The authoritative per-turn budget is carried in control state.
    budget = control.get("budget")
    if budget is None:
        return _result(GateSeverity.HARD_STOP, ["budget_missing"])

    counters = {
        "model": ("model_calls", "max_model_calls", "model_budget_exceeded"),
        "tool": ("tool_calls", "max_tool_calls", "tool_budget_exceeded"),
    }
    if invocation not in counters:
        return _result(GateSeverity.HARD_STOP, ["invocation_kind_invalid"])

    counter, limit, exceeded_reason = counters[invocation]
    current = control.get(counter, 0)
    maximum = getattr(budget, limit, None)
    if not _within_budget(current, maximum) or not _within_budget(current + 1, maximum):
        return _result(GateSeverity.HARD_STOP, [exceeded_reason])
    # Provider token usage is optional telemetry. Model/tool slots and the
    # deadline remain authoritative when usage is missing or over the legacy cap.

    now = control.get("now")
    deadline = getattr(budget, "deadline_at", None)
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or not isinstance(deadline, datetime)
        or deadline.tzinfo is None
        or deadline.utcoffset() is None
    ):
        return _result(GateSeverity.HARD_STOP, ["time_unavailable"])
    if now > deadline:
        return _result(GateSeverity.HARD_STOP, ["deadline_exceeded"])
    return _result(GateSeverity.PASS, [f"{invocation}_invocation_admitted"])


def evaluate_evidence_admission(
    control: Mapping[str, Any],
    *,
    policy: WorkflowPolicy,
) -> GateResult:
    """Apply the complete Task 2 budget before an evidence adapter can run."""
    budget = control.get("budget")
    if budget is None:
        return _result(GateSeverity.HARD_STOP, ["budget_missing"])

    model_calls = control.get("model_calls", 0)
    max_model_calls = getattr(budget, "max_model_calls", None)
    if (
        not _within_budget(model_calls, max_model_calls)
        or model_calls >= max_model_calls
    ):
        return _result(GateSeverity.HARD_STOP, ["model_budget_exceeded"])

    tool_result = evaluate_invocation_admission(
        control,
        policy=policy,
        invocation="tool",
    )
    if tool_result.severity is GateSeverity.HARD_STOP:
        return tool_result

    now = control.get("now")
    deadline = getattr(budget, "deadline_at", None)
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or not isinstance(deadline, datetime)
        or deadline.tzinfo is None
        or deadline.utcoffset() is None
    ):
        return _result(GateSeverity.HARD_STOP, ["time_unavailable"])
    if now >= deadline:
        return _result(GateSeverity.HARD_STOP, ["deadline_exceeded"])
    return _result(GateSeverity.PASS, ["evidence_invocation_admitted"])


preflight_gate = evaluate_policy_gate
process_gate = evaluate_policy_gate


def process_policy_gate(
    state: RehabGraphState,
    *,
    policy: WorkflowPolicy | None = None,
    now: datetime | None = None,
) -> RehabGraphState:
    """Block machine-control violations after drafting and before clinical review."""
    from app.ai.invocation_support import _debug_event

    if state.get("policy_gate_severity") == GateSeverity.HARD_STOP.value:
        return {
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": list(state.get("policy_gate_reason_codes") or ()),
            "debug_trace": _debug_event(
                state,
                "policy_gate",
                {
                    "severity": GateSeverity.HARD_STOP.value,
                    "reason_codes": list(state.get("policy_gate_reason_codes") or ()),
                },
            ),
        }
    required_non_null_fields = (
        "principal_authorized", "session_authorized", "care_episode_authorized",
        "authorized_source_ids", "source_ids", "authorized_tool_names",
        "allowed_catalog_ids", "tool_call_names", "catalog_exercise_suggestions",
        "model_calls", "tool_calls", "budget", "repair_count",
    )
    missing_fields = [
        field for field in required_non_null_fields
        if field not in state or state.get(field) is None
    ]
    if "idempotency_key" not in state:
        missing_fields.append("idempotency_key")
    if "prior_request_hash" not in state:
        missing_fields.append("prior_request_hash")
    request_hash = state.get("request_hash")
    if not isinstance(request_hash, str) or not request_hash.strip():
        missing_fields.append("request_hash")
    if state.get("route") is None and state.get("current_route") is None:
        missing_fields.append("current_route")
    if missing_fields:
        reason_codes = list(
            dict.fromkeys(f"{field}_missing" for field in missing_fields)
        )
        return {
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": reason_codes,
            "debug_trace": _debug_event(
                state,
                "policy_gate",
                {
                    "severity": GateSeverity.HARD_STOP.value,
                    "reason_codes": reason_codes,
                },
            ),
        }

    active_policy = policy or WorkflowPolicy()
    result = evaluate_policy_gate(
        {
            "principal_authorized": state.get("principal_authorized") is True,
            "session_authorized": state.get("session_authorized") is True,
            "care_episode_authorized": state.get("care_episode_authorized") is True,
            "workflow_version": state.get("workflow_version"),
            "current_route": state.get("route", state.get("current_route")),
            "requested_route": state.get("requested_route"),
            "authorized_source_ids": state.get("authorized_source_ids"),
            "source_ids": state.get("source_ids"),
            "authorized_tool_names": state.get("authorized_tool_names"),
            "tool_names": state.get("tool_call_names"),
            "allowed_catalog_ids": state.get("allowed_catalog_ids"),
            "catalog_ids": tuple(
                str(item.get("exercise_id"))
                for item in state.get("catalog_exercise_suggestions") or ()
                if isinstance(item, dict) and item.get("exercise_id")
            ),
            "model_calls": state.get("model_calls"),
            "tool_calls": state.get("tool_calls"),
            "total_tokens": (
                0
                if isinstance(state.get("repair_count"), int)
                and not isinstance(state.get("repair_count"), bool)
                and state.get("repair_count", 0) > 0
                else state.get("total_tokens")
            ),
            "budget": state.get("budget"),
            "required_nodes": {"consultant"},
            "required_outcomes": (
                {"consultant": state.get("consult_response")}
                if state.get("consult_response") is not None else {}
            ),
            "idempotency_key": state.get("idempotency_key"),
            "request_hash": state.get("request_hash"),
            "prior_request_hash": state.get("prior_request_hash"),
            "repair_count": state.get("repair_count"),
            "now": now or datetime.now(timezone.utc),
        },
        policy=active_policy,
    )
    reason_codes = list(result.reason_codes)
    severity = result.severity
    current_model_calls = state.get("model_calls")
    max_model_calls = getattr(state.get("budget"), "max_model_calls", None)
    if (
        severity is GateSeverity.PASS
        and isinstance(current_model_calls, int)
        and not isinstance(current_model_calls, bool)
        and isinstance(max_model_calls, int)
        and current_model_calls + 4 > max_model_calls
    ):
        severity = GateSeverity.HARD_STOP
        reason_codes.append("model_budget_exceeded")
    return {
        "policy_gate_severity": severity.value,
        "policy_gate_reason_codes": list(dict.fromkeys(reason_codes)),
        "debug_trace": _debug_event(
            state,
            "policy_gate",
            {
                "severity": severity.value,
                "reason_codes": list(dict.fromkeys(reason_codes)),
            },
        ),
    }
