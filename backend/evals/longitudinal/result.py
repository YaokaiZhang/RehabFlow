from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LongitudinalResult:
    scenario_id: str
    runtime: str
    final_response: str | None
    route: str | None
    visited_nodes: tuple[str, ...]
    compatibility_visited_nodes: tuple[str, ...]
    selected_source_ids: tuple[str, ...]
    omitted_source_ids: tuple[str, ...]
    packet_tokens: dict[str, int]
    evidence_source_ids: tuple[str, ...]
    reviewer_outcomes: tuple[str, ...]
    repair_count: int
    checkpoint_outcome: str | None
    persistence_observations: tuple[dict[str, Any], ...]
    persistence_outcomes: tuple[str, ...]
    memory_writes: tuple[dict[str, Any], ...]
    idempotency_outcome: str | None
    catalog_ids: tuple[str, ...]
    failures: tuple[str, ...]
    summary_text: str | None = None
    planner_fallback_used: bool = False
    unknown_planner_source_ids_rejected: tuple[str, ...] = ()
    compacted_source_ids: tuple[str, ...] = ()
    context_candidate_source_ids: tuple[str, ...] = ()
    context_summary_text: str | None = None
    context_summary_mode: str = "production_assembler_session_summary"
    checkpoint_durability: str = "process_local_memory_saver"
    persistence_mode: str = "in_memory_injected_boundary"
    turn_observations: tuple[dict[str, Any], ...] = ()
    raw_visited_nodes: tuple[str, ...] = ()
    elapsed_ms: int | None = None
    elapsed_ms_provenance: str = "unavailable:synthetic_clock"
    model_calls: int | None = None
    tool_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    failure_recovery: bool | None = None
    urgent_route_escapes: int | None = None
    reviewer_disagreement: bool | None = None
    raw_reviewer_outcomes: tuple[str, ...] = ()
    trace: dict[str, Any] | None = None
    runtime_events: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "runtime": self.runtime,
            "final_response": self.final_response,
            "route": self.route,
            "visited_nodes": list(self.visited_nodes),
            "compatibility_visited_nodes": list(self.compatibility_visited_nodes),
            "raw_visited_nodes": list(self.raw_visited_nodes or self.visited_nodes),
            "raw_reviewer_outcomes": list(self.raw_reviewer_outcomes or self.reviewer_outcomes),
            "selected_source_ids": list(self.selected_source_ids),
            "omitted_source_ids": list(self.omitted_source_ids),
            "packet_tokens": dict(self.packet_tokens),
            "evidence_source_ids": list(self.evidence_source_ids),
            "reviewer_outcomes": list(self.reviewer_outcomes),
            "elapsed_ms": self.elapsed_ms,
            "elapsed_ms_provenance": self.elapsed_ms_provenance,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "failure_recovery": self.failure_recovery,
            "urgent_route_escapes": self.urgent_route_escapes,
            "reviewer_disagreement": self.reviewer_disagreement,
            "repair_count": self.repair_count,
            "checkpoint_outcome": self.checkpoint_outcome,
            "persistence_observations": [dict(item) for item in self.persistence_observations],
            "persistence_outcomes": list(self.persistence_outcomes),
            "memory_writes": [dict(item) for item in self.memory_writes],
            "idempotency_outcome": self.idempotency_outcome,
            "catalog_ids": list(self.catalog_ids),
            "failures": list(self.failures),
            "summary_text": self.summary_text,
            "planner_fallback_used": self.planner_fallback_used,
            "unknown_planner_source_ids_rejected": list(self.unknown_planner_source_ids_rejected),
            "compacted_source_ids": list(self.compacted_source_ids),
            "context_candidate_source_ids": list(self.context_candidate_source_ids),
            "context_summary_text": self.context_summary_text,
            "context_summary_mode": self.context_summary_mode,
            "checkpoint_durability": self.checkpoint_durability,
            "persistence_mode": self.persistence_mode,
            "turn_observations": [dict(item) for item in self.turn_observations],
            "trace": self.trace,
            "runtime_events": [dict(item) for item in self.runtime_events],
        }
