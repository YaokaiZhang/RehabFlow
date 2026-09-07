from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from evals.longitudinal.result import LongitudinalResult
from evals.longitudinal.runtime_metrics import runtime_metrics_from_turns
from evals.longitudinal.contract import ScenarioV2


@dataclass(frozen=True)
class DeterministicMetrics:
    scenario_id: str
    required_source_recall: float | None
    forbidden_source_inclusion: int | None
    stale_source_exclusion: float | None
    required_fact_recall: float | None
    forbidden_fact_inclusion: int | None
    packet_budget_compliant: bool | None
    evidence_source_recall: float | None
    catalog_grounding: bool | None
    forbidden_catalog_inclusion: int | None
    route_correct: bool | None
    path_correct: bool | None
    reviewer_correct: bool | None
    repair_correct: bool | None
    checkpoint_correct: bool | None
    memory_write_correct: bool | None
    persistence_observation_correct: bool | None
    summary_fact_recall: float | None
    summary_contradictions: int | None
    idempotency_correct: bool | None
    persistence_correct: bool | None
    context_compaction_compliant: bool | None
    planner_fallback_correct: bool | None
    unknown_planner_sources_correct: bool | None
    cross_session_transfer_correct: bool | None
    response_contains_correct: bool | None
    response_omits_correct: bool | None
    action_quality_correct: bool | None
    failures: tuple[str, ...]
    elapsed_ms: int | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    failure_recovery: bool | None = None
    urgent_route_escapes: int | None = None
    reviewer_disagreement: bool | None = None

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in (
                "scenario_id",
                "required_source_recall",
                "forbidden_source_inclusion",
                "stale_source_exclusion",
                "required_fact_recall",
                "forbidden_fact_inclusion",
                "packet_budget_compliant",
                "evidence_source_recall",
                "catalog_grounding",
                "forbidden_catalog_inclusion",
                "route_correct",
                "path_correct",
                "reviewer_correct",
                "repair_correct",
                "checkpoint_correct",
                "memory_write_correct",
                "persistence_observation_correct",
                "summary_fact_recall",
                "summary_contradictions",
                "idempotency_correct",
                "persistence_correct",
                "context_compaction_compliant",
                "planner_fallback_correct",
                "unknown_planner_sources_correct",
                "cross_session_transfer_correct",
                "response_contains_correct",
                "response_omits_correct",
                "action_quality_correct",
                "elapsed_ms",
                "model_calls",
                "tool_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "failure_recovery",
                "urgent_route_escapes",
                "reviewer_disagreement",
                "failures",
            )
        }


def _ratio(expected: set[str], observed: set[str]) -> float | None:
    if not expected:
        return None
    return len(expected & observed) / len(expected)


def _fact_ids(source_ids: set[str]) -> set[str]:
    prefixes = ("memory_item:", "triage_summary:", "ai_message:")
    return {
        source_id.split(":", 1)[1]
        for source_id in source_ids
        if source_id.startswith(prefixes)
    }


def _mutation_observation_match(
    expected: list[Mapping[str, Any]],
    observations: tuple[dict[str, Any], ...],
) -> bool | None:
    if not expected:
        return None
    operation_for_action = {
        "create": "memory_create",
        "update": "memory_edit",
        "delete": "memory_delete",
        "archive": "memory_archive",
        "supersede": "memory_supersede",
    }
    return all(
        any(
            observed.get("id") == item.get("id")
            and observed.get("operation") == operation_for_action.get(item.get("action"))
            and observed.get("committed") is True
            for observed in observations
        )
        for item in expected
    )


def _cross_session_match(
    oracle: Mapping[str, Any],
    observations: tuple[dict[str, Any], ...],
) -> bool | None:
    if oracle.get("forbidden_raw_prior_session") is not True:
        return None
    if len(observations) < 2:
        return None
    first_session = str(observations[0].get("session_id"))
    final_session = str(observations[-1].get("session_id"))
    if first_session == final_session:
        return False
    required = set(str(item) for item in oracle.get("required_source_ids", []))
    final_selected = set(str(item) for item in observations[-1].get("selected_source_ids", []))
    first_raw = {
        str(source_id)
        for source_id in observations[0].get("selected_source_ids", [])
        if str(source_id).startswith("ai_message:")
    }
    return required <= final_selected and not first_raw.intersection(final_selected)


def _reviewer_match(expected: Mapping[str, Any], observed: tuple[str, ...]) -> bool | None:
    if not expected:
        return None
    actual: dict[str, list[str]] = {}
    for value in observed:
        reviewer, separator, status = str(value).partition(":")
        if separator:
            actual.setdefault(reviewer, []).append(status)
    return all(actual.get(str(reviewer), []) == [str(item) for item in statuses] for reviewer, statuses in expected.items())


def _memory_match(expected: list[Mapping[str, Any]], observed: tuple[dict[str, Any], ...]) -> bool | None:
    if not expected:
        return None
    fields = ("id", "scope", "action", "provenance", "care_episode_id")
    expected_values = {
        tuple(item.get(field) for field in fields)
        for item in expected
    }
    observed_values = {
        tuple(item.get(field) for field in fields)
        for item in observed
    }
    return expected_values == observed_values


def _observed_source_ids(result: LongitudinalResult) -> set[str]:
    observed = {
        str(source_id)
        for source_id in (*result.selected_source_ids, *result.evidence_source_ids)
    }
    for observation in result.turn_observations:
        observed.update(
            str(source_id)
            for source_id in observation.get("selected_source_ids", [])
        )
        observed.update(
            str(source_id)
            for source_id in observation.get("evidence_source_ids", [])
        )
    return observed


def score_scenario(result: LongitudinalResult, scenario: ScenarioV2) -> DeterministicMetrics:
    oracle = scenario.oracles
    observed_sources = _observed_source_ids(result)
    required_source_recall = _ratio(set(oracle.get("required_source_ids", [])), observed_sources)
    forbidden_ids = set(oracle.get("forbidden_source_ids", []))
    forbidden_source_inclusion = (
        len(forbidden_ids & observed_sources) if forbidden_ids else None
    )
    stale_ids = set(oracle.get("stale_source_ids", []))
    stale_source_exclusion = (
        len(stale_ids - observed_sources) / len(stale_ids) if stale_ids else None
    )
    observed_facts = _fact_ids(observed_sources)
    required_fact_recall = _ratio(set(oracle.get("required_fact_ids", [])), observed_facts)
    forbidden_facts = set(oracle.get("forbidden_fact_ids", []))
    forbidden_fact_inclusion = len(forbidden_facts & observed_facts) if forbidden_facts else None

    packet_budgets = oracle.get("packet_budgets", {})
    packet_budget_compliant = (
        all(result.packet_tokens.get(node, 0) <= int(limit) for node, limit in packet_budgets.items())
        if packet_budgets
        else None
    )

    evidence_manifest = set(oracle.get("evidence_manifest", []))
    evidence_source_recall = _ratio(evidence_manifest, set(result.evidence_source_ids))
    expected_catalog_ids = set(oracle.get("expected_catalog_ids", []))
    catalog_grounding = (
        expected_catalog_ids <= set(result.catalog_ids) if expected_catalog_ids else None
    )
    forbidden_catalog_ids = set(oracle.get("forbidden_catalog_ids", []))
    forbidden_catalog_inclusion = (
        len(forbidden_catalog_ids & set(result.catalog_ids))
        if forbidden_catalog_ids else None
    )

    expected_route = oracle.get("expected_route")
    route_correct = str(result.route) == str(expected_route) if expected_route else None
    expected_nodes = set(oracle.get("expected_nodes", []))
    forbidden_nodes = set(oracle.get("forbidden_nodes", []))
    path_correct = (
        expected_nodes <= set(result.visited_nodes)
        and not forbidden_nodes.intersection(result.visited_nodes)
        if expected_nodes or forbidden_nodes
        else None
    )
    reviewer_correct = _reviewer_match(
        oracle.get("expected_reviewer_outcomes", {}),
        result.reviewer_outcomes,
    )
    expected_repair = oracle.get("expected_repair_count")
    repair_correct = (
        result.repair_count == int(expected_repair)
        if expected_repair is not None
        else None
    )
    expected_checkpoint = oracle.get("expected_checkpoint")
    checkpoint_correct = (
        result.checkpoint_outcome == expected_checkpoint
        if expected_checkpoint is not None
        else None
    )
    memory_write_correct = _memory_match(
        oracle.get("memory_writes", []),
        result.memory_writes,
    )
    persistence_observation_correct = _mutation_observation_match(
        oracle.get("memory_writes", []),
        result.persistence_observations,
    )
    required_summary = oracle.get("summary_required_phrases", [])
    forbidden_summary = oracle.get("summary_forbidden_phrases", [])
    summary_text = result.summary_text or ""
    summary_fact_recall = (
        sum(str(phrase) in summary_text for phrase in required_summary) / len(required_summary)
        if required_summary
        else None
    )
    summary_contradictions = (
        sum(str(phrase) in summary_text for phrase in forbidden_summary)
        if forbidden_summary
        else None
    )
    expected_idempotency = oracle.get("expected_idempotency")
    idempotency_correct = (
        result.idempotency_outcome == expected_idempotency
        if expected_idempotency and expected_idempotency != "none"
        else None
    )
    expected_persistence = [str(item) for item in oracle.get("expected_persistence", [])]
    persistence_correct = (
        all(item in result.persistence_outcomes for item in expected_persistence)
        if expected_persistence
        else None
    )
    max_context_source_ids = oracle.get("max_context_source_ids")
    context_compaction_compliant = (
        len(result.context_candidate_source_ids) > int(max_context_source_ids)
        and bool(result.omitted_source_ids)
        if max_context_source_ids is not None
        else None
    )
    expected_planner_failure = oracle.get("planner_failure")
    planner_fallback_correct = (
        result.planner_fallback_used is True
        if expected_planner_failure is True
        else None
    )
    unknown_expected = set(oracle.get("unknown_planner_source_ids", []))
    unknown_planner_sources_correct = (
        unknown_expected == set(result.unknown_planner_source_ids_rejected)
        if unknown_expected
        else None
    )
    cross_session_transfer_correct = _cross_session_match(
        oracle,
        result.turn_observations,
    )
    response = result.final_response or ""
    response_contains = [str(item) for item in oracle.get("expected_response_contains", [])]
    response_omits = [str(item) for item in oracle.get("expected_response_omits", [])]
    response_contains_correct = (
        all(item in response for item in response_contains)
        if response_contains
        else None
    )
    response_omits_correct = (
        all(item not in response for item in response_omits)
        if response_omits
        else None
    )
    expected_actions = [str(item) for item in oracle.get("expected_actions", [])]
    action_quality_correct = (
        all(item in response for item in expected_actions)
        if expected_actions
        else None
    )
    runtime_metrics = runtime_metrics_from_turns(result.turn_observations)

    checks = {
        "required_source_recall": required_source_recall == 1.0 if required_source_recall is not None else None,
        "forbidden_source_inclusion": forbidden_source_inclusion == 0 if forbidden_source_inclusion is not None else None,
        "stale_source_exclusion": stale_source_exclusion == 1.0 if stale_source_exclusion is not None else None,
        "required_fact_recall": required_fact_recall == 1.0 if required_fact_recall is not None else None,
        "forbidden_fact_inclusion": forbidden_fact_inclusion == 0 if forbidden_fact_inclusion is not None else None,
        "packet_budget_compliant": packet_budget_compliant,
        "evidence_source_recall": evidence_source_recall == 1.0 if evidence_source_recall is not None else None,
        "catalog_grounding": catalog_grounding,
        "forbidden_catalog_inclusion": forbidden_catalog_inclusion == 0 if forbidden_catalog_inclusion is not None else None,
        "route_correct": route_correct,
        "path_correct": path_correct,
        "reviewer_correct": reviewer_correct,
        "repair_correct": repair_correct,
        "checkpoint_correct": checkpoint_correct,
        "memory_write_correct": memory_write_correct,
        "persistence_observation_correct": persistence_observation_correct,
        "summary_fact_recall": summary_fact_recall == 1.0 if summary_fact_recall is not None else None,
        "summary_contradictions": summary_contradictions == 0 if summary_contradictions is not None else None,
        "idempotency_correct": idempotency_correct,
        "persistence_correct": persistence_correct,
        "context_compaction_compliant": context_compaction_compliant,
        "planner_fallback_correct": planner_fallback_correct,
        "unknown_planner_sources_correct": unknown_planner_sources_correct,
        "cross_session_transfer_correct": cross_session_transfer_correct,
        "response_contains_correct": response_contains_correct,
        "response_omits_correct": response_omits_correct,
        "action_quality_correct": action_quality_correct,
    }
    failures = tuple(sorted(name for name, passed in checks.items() if passed is False))
    return DeterministicMetrics(
        scenario_id=result.scenario_id,
        required_source_recall=required_source_recall,
        forbidden_source_inclusion=forbidden_source_inclusion,
        stale_source_exclusion=stale_source_exclusion,
        required_fact_recall=required_fact_recall,
        forbidden_fact_inclusion=forbidden_fact_inclusion,
        packet_budget_compliant=packet_budget_compliant,
        evidence_source_recall=evidence_source_recall,
        catalog_grounding=catalog_grounding,
        forbidden_catalog_inclusion=forbidden_catalog_inclusion,
        route_correct=route_correct,
        path_correct=path_correct,
        reviewer_correct=reviewer_correct,
        repair_correct=repair_correct,
        checkpoint_correct=checkpoint_correct,
        memory_write_correct=memory_write_correct,
        persistence_observation_correct=persistence_observation_correct,
        summary_fact_recall=summary_fact_recall,
        summary_contradictions=summary_contradictions,
        idempotency_correct=idempotency_correct,
        persistence_correct=persistence_correct,
        context_compaction_compliant=context_compaction_compliant,
        planner_fallback_correct=planner_fallback_correct,
        unknown_planner_sources_correct=unknown_planner_sources_correct,
        cross_session_transfer_correct=cross_session_transfer_correct,
        response_contains_correct=response_contains_correct,
        response_omits_correct=response_omits_correct,
        action_quality_correct=action_quality_correct,
        failures=failures,
        elapsed_ms=runtime_metrics["elapsed_ms"],
        model_calls=runtime_metrics["model_calls"],
        tool_calls=runtime_metrics["tool_calls"],
        input_tokens=runtime_metrics["input_tokens"],
        output_tokens=runtime_metrics["output_tokens"],
        total_tokens=runtime_metrics["total_tokens"],
        failure_recovery=runtime_metrics["failure_recovery"],
        urgent_route_escapes=runtime_metrics["urgent_route_escapes"],
        reviewer_disagreement=runtime_metrics["reviewer_disagreement"],
    )
