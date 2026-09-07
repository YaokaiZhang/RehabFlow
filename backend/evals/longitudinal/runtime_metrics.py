from __future__ import annotations

from typing import Any, Mapping


def _observed_sum(turns: list[Mapping[str, Any]], key: str) -> int | None:
    values = [turn.get(key) for turn in turns]
    if not values or any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        return None
    return sum(values)


def _reviewer_disagreement(turns: list[Mapping[str, Any]]) -> bool | None:
    observed_fanout = False
    disagreement = False
    for turn in turns:
        outcomes = turn.get("reviewer_outcomes")
        if not isinstance(outcomes, (list, tuple)):
            continue
        first_status_by_reviewer: dict[str, str] = {}
        for raw_outcome in outcomes:
            reviewer, separator, status = str(raw_outcome).partition(":")
            if separator and reviewer and status and reviewer not in first_status_by_reviewer:
                first_status_by_reviewer[reviewer] = status
        if len(first_status_by_reviewer) < 2:
            continue
        observed_fanout = True
        if len(set(first_status_by_reviewer.values())) > 1:
            disagreement = True
    return disagreement if observed_fanout else None


def runtime_metrics_from_turns(
    turn_observations: tuple[dict[str, Any], ...] | list[Mapping[str, Any]],
) -> dict[str, int | bool | None]:
    turns = [turn for turn in turn_observations if isinstance(turn, Mapping)]
    statuses = [turn.get("status") for turn in turns]
    failure_indexes = [index for index, status in enumerate(statuses) if status == "failed"]
    failure_recovery: bool | None = None
    if failure_indexes:
        last_failure = max(failure_indexes)
        failure_recovery = any(
            status in {"completed", "clarification_required"}
            for status in statuses[last_failure + 1:]
        )

    urgent_turns = [
        turn for turn in turns
        if turn.get("route") == "urgent"
    ]
    urgent_route_escapes: int | None = None
    if urgent_turns:
        urgent_route_escapes = sum(
            "consultant" in {
                str(node) for node in turn.get("visited_nodes", [])
            }
            or any(
                str(node).endswith("_reviewer")
                for node in turn.get("visited_nodes", [])
            )
            for turn in urgent_turns
        )

    return {
        "elapsed_ms": _observed_sum(turns, "elapsed_ms"),
        "model_calls": _observed_sum(turns, "model_calls"),
        "tool_calls": _observed_sum(turns, "tool_calls"),
        "input_tokens": _observed_sum(turns, "input_tokens"),
        "output_tokens": _observed_sum(turns, "output_tokens"),
        "total_tokens": _observed_sum(turns, "total_tokens"),
        "failure_recovery": failure_recovery,
        "urgent_route_escapes": urgent_route_escapes,
        "reviewer_disagreement": _reviewer_disagreement(turns),
    }
