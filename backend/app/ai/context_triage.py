"""Deterministic Context Triage for validated evidence packets."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from app.ai.active_turn_context import (
    active_evidence_results,
    store_triaged_context,
)

from app.ai.context_assembler import estimate_tokens
from app.ai.context_integrity import ContextIntegrity
from app.ai.context_types import ContextPacket
from app.ai.evidence_types import EvidenceResult, EvidenceSource
from app.ai.policy_gate import GateSeverity


class ContextTriageError(ValueError):
    """Raised when an evidence packet crosses its authorized source boundary."""


@dataclass(frozen=True)
class TriagedContext:
    packets: tuple[ContextPacket, ...]
    included_source_ids: tuple[str, ...]
    omitted_source_ids: tuple[str, ...]
    unavailable_evidence: tuple[str, ...]
    estimated_tokens: int
    prompt_text: str
    catalog_exercise_suggestions: tuple[dict[str, Any], ...] = ()


_SOURCE_RANK = {
    EvidenceSource.CARE_EPISODE: 0,
    EvidenceSource.PATIENT_MEMORY: 1,
    EvidenceSource.CONVERSATION: 2,
    EvidenceSource.REHAB_KNOWLEDGE: 3,
}


def _coerce_result(value: object) -> EvidenceResult:
    return value if isinstance(value, EvidenceResult) else EvidenceResult.model_validate(value)


def validate_evidence_packets(
    results: Iterable[EvidenceResult],
    integrity: ContextIntegrity,
) -> list[tuple[EvidenceSource, str, ContextPacket]]:
    """Return only ok packets whose source IDs are in the current allowance."""
    validated: list[tuple[EvidenceSource, str, ContextPacket]] = []
    seen: set[str] = set()
    for raw_result in results:
        result = _coerce_result(raw_result)
        if result.status != "ok":
            continue
        allowance = integrity.allowance_for(result.source)
        if allowance is None or not allowance.visible:
            raise ContextTriageError(
                f"Evidence source '{result.source.value}' is not visible during Context Triage"
            )
        for packet in result.packets:
            source_id = str(packet.source_id).strip()
            if not source_id or not integrity.source_id_is_allowed(result.source, source_id):
                raise ContextTriageError(
                    f"Evidence packet source '{source_id}' is not authorized for '{result.source.value}'"
                )
            if source_id in seen:
                continue
            if not isinstance(packet.content, str) or not packet.content.strip():
                continue
            seen.add(source_id)
            validated.append((result.source, result.request_id, packet))
    return validated


def _unavailable(results: Iterable[EvidenceResult]) -> tuple[str, ...]:
    return tuple(
        f"{result.source.value}:{result.status}"
        for result in sorted(
            (_coerce_result(item) for item in results),
            key=lambda item: (item.source.value, item.request_id),
        )
        if result.status in {"empty", "timeout", "error"}
    )


def _catalog_suggestions(
    packets: Iterable[tuple[EvidenceSource, str, ContextPacket]],
    *,
    catalog_validator: Any | None,
) -> tuple[dict[str, Any], ...]:
    if not callable(catalog_validator):
        return ()

    documents: list[dict[str, Any]] = []
    for source, _request_id, packet in packets:
        if source is not EvidenceSource.REHAB_KNOWLEDGE:
            continue
        metadata = dict(packet.metadata)
        metadata.setdefault("document_id", packet.source_id)
        metadata["text"] = packet.content
        documents.append({"payload": metadata, "id": packet.source_id})

    try:
        values = catalog_validator(documents, limit=5) or []
    except Exception:
        return ()

    by_id: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        identity = str(item.get("exercise_id") or item.get("title") or "").strip()
        if identity and identity not in by_id:
            by_id[identity] = item
    return tuple(by_id[key] for key in sorted(by_id))


def _prompt(
    packets: Iterable[ContextPacket],
    unavailable: tuple[str, ...],
    omitted: tuple[str, ...],
) -> str:
    lines = [
        "Context Triage: validated evidence ranked deterministically by source priority.",
    ]
    for packet in packets:
        lines.extend(
            [
                f"Source ID: {packet.source_id}",
                f"Evidence: {packet.content.strip()}",
            ]
        )
    if unavailable:
        lines.append("Unavailable evidence: " + ", ".join(unavailable))
    else:
        lines.append("Unavailable evidence: none")
    if omitted:
        lines.append("Omitted for context budget: " + ", ".join(omitted))
    lines.append(
        "Use only validated evidence above. Treat unavailable or omitted evidence as unavailable; "
        "do not manufacture a substitute source."
    )
    return "\n".join(lines)


def triage_evidence(
    results: Iterable[EvidenceResult],
    *,
    integrity: ContextIntegrity,
    context_budget: int | None = None,
    catalog_validator: Any | None = None,
) -> TriagedContext:
    """Validate, rank, and cap evidence without a model call."""
    if context_budget is not None and (
        isinstance(context_budget, bool)
        or not isinstance(context_budget, int)
        or context_budget < 1
    ):
        raise ValueError("context_budget must be a positive integer when provided")

    result_list = [_coerce_result(item) for item in results]
    validated = validate_evidence_packets(result_list, integrity)
    ranked = sorted(
        validated,
        key=lambda item: (
            _SOURCE_RANK[item[0]],
            item[0].value,
            item[2].source_id,
            item[1],
        ),
    )

    included_entries: list[tuple[EvidenceSource, str, ContextPacket]] = []
    omitted: list[str] = []
    used_tokens = 0
    for _source, _request_id, packet in ranked:
        packet_tokens = estimate_tokens(packet.content)
        remaining = context_budget - used_tokens if context_budget is not None else None
        if remaining is not None and remaining <= 0:
            omitted.append(packet.source_id)
            continue
        if remaining is None or packet_tokens <= remaining:
            selected = packet
        else:
            max_chars = max(1, remaining * 4)
            selected = packet.model_copy(
                update={
                    "content": packet.content[:max_chars].rstrip(),
                    "metadata": {**dict(packet.metadata), "truncated_for_context_budget": True},
                }
            )
        selected_tokens = estimate_tokens(selected.content)
        if remaining is not None:
            selected_tokens = min(remaining, selected_tokens)
        if selected_tokens <= 0:
            omitted.append(packet.source_id)
            continue
        included_entries.append((_source, _request_id, selected))
        used_tokens += selected_tokens
        if selected_tokens < packet_tokens:
            omitted.append(packet.source_id)

    included = [packet for _source, _request_id, packet in included_entries]
    included_ids = tuple(packet.source_id for packet in included)
    omitted_ids = tuple(omitted)
    unavailable = _unavailable(result_list)
    catalog = _catalog_suggestions(included_entries, catalog_validator=catalog_validator)
    return TriagedContext(
        packets=tuple(included),
        included_source_ids=included_ids,
        omitted_source_ids=omitted_ids,
        unavailable_evidence=unavailable,
        estimated_tokens=used_tokens,
        prompt_text=_prompt(included, unavailable, omitted_ids),
        catalog_exercise_suggestions=catalog,
    )


def context_triage_node(
    state: Mapping[str, Any],
    *,
    catalog_validator: Any | None = None,
) -> dict[str, Any]:
    """Triage active-turn evidence while returning only checkpoint-safe metadata."""
    turn_id = state.get("evidence_turn_id")
    results = active_evidence_results(turn_id)
    if not results and not turn_id:
        # Narrow compatibility seam for old direct node callers. Production
        # graph branches always write execution results to active memory.
        results = [
            value
            for value in list(state.get("evidence_results", []) or [])
            if isinstance(value, EvidenceResult)
        ]
    try:
        integrity = state.get("context_integrity")
        if not isinstance(integrity, ContextIntegrity):
            from app.ai.context_integrity import context_integrity_from_state

            integrity = context_integrity_from_state(state)
        triaged = triage_evidence(
            results,
            integrity=integrity,
            context_budget=state.get("context_budget", 2_000),
            catalog_validator=catalog_validator,
        )
        if turn_id:
            store_triaged_context(turn_id, triaged)
    except ContextTriageError:
        return {
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": ["evidence_packet_not_authorized"],
        }
    except Exception:
        return {
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": ["context_triage_failed"],
        }

    retrieved_docs = [
        {
            "payload": {"document_id": packet.source_id},
            "id": packet.source_id,
        }
        for packet in triaged.packets
        if packet.source_id.startswith(
            ("retrieved_doc:", "knowledge_document:", "rehab_knowledge:")
        )
    ]
    catalog_suggestions = [
        {"exercise_id": str(item["exercise_id"])}
        for item in triaged.catalog_exercise_suggestions
        if item.get("exercise_id")
    ]
    trace = list(state.get("debug_trace", []) or [])
    trace.append(
        {
            "step": len(trace) + 1,
            "node": "context_triage",
            "included_source_ids": list(triaged.included_source_ids),
            "omitted_source_ids": list(triaged.omitted_source_ids),
            "unavailable_evidence": list(triaged.unavailable_evidence),
            "estimated_tokens": triaged.estimated_tokens,
        }
    )
    return {
        "retrieved_docs": retrieved_docs,
        "catalog_exercise_suggestions": catalog_suggestions,
        "allowed_catalog_ids": [
            str(item["exercise_id"])
            for item in catalog_suggestions
            if item.get("exercise_id")
        ],
        "web_search_summary": "",
        "debug_trace": trace,
        "internal_log_events": [
            {
                "agent_name": "context_triage",
                "event_type": "context_triage",
                "content": "",
                "metadata": {
                    "included_source_ids": list(triaged.included_source_ids),
                    "omitted_source_ids": list(triaged.omitted_source_ids),
                    "unavailable_evidence": list(triaged.unavailable_evidence),
                    "estimated_tokens": triaged.estimated_tokens,
                    "catalog_suggestion_ids": [
                        str(item["exercise_id"])
                        for item in catalog_suggestions
                        if item.get("exercise_id")
                    ],
                },
            }
        ],
    }


ContextTriageResult = TriagedContext
build_context_triage = triage_evidence
