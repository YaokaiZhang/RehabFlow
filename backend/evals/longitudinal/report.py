from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping

from evals.longitudinal.contract import RUBRIC_DIMENSIONS
from evals.trace import append_judge_events, redact_trace_value

if TYPE_CHECKING:
    from evals.longitudinal.result import LongitudinalResult
    from evals.longitudinal.contract import ScenarioV2
    from evals.longitudinal.rubric import JudgeResult, RubricSummary

REPORT_DISCLAIMER = (
    "Engineering diagnostic only; not clinical validation and not a clinical outcome."
)
REPORT_VERSION = "longitudinal-v2"
_PRIVACY_SAFE_SOURCE_IDS = frozenset({"response", "rehab_knowledge:public"})
_PRIVACY_SOURCE_PREFIXES = frozenset(
    {
        "conversation",
        "episode",
        "care_episode",
        "memory_item",
        "memory_document",
        "patient_memory",
        "ai_message",
        "triage_summary",
        "retrieved_doc",
        "knowledge_document",
        "rehab_knowledge",
    }
)


def _privacy_safe_source_id(value: object) -> str:
    source_id = str(value or "").strip()
    if not source_id:
        return ""
    if source_id in _PRIVACY_SAFE_SOURCE_IDS:
        return source_id
    prefix, separator, physical_id = source_id.partition(":")
    if separator and prefix in _PRIVACY_SOURCE_PREFIXES and physical_id:
        handle = hashlib.sha256(physical_id.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}:{handle}"
    return f"source:{hashlib.sha256(source_id.encode('utf-8')).hexdigest()[:24]}"


def _privacy_safe_source_values(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return list(dict.fromkeys(_privacy_safe_source_id(item) for item in value))


def _privacy_safe_text(value: str) -> str:
    prefixes = "|".join(
        re.escape(prefix)
        for prefix in (*_PRIVACY_SOURCE_PREFIXES, "source")
    )
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_])(?:{prefixes}):[A-Za-z0-9][A-Za-z0-9_-]*"
    )
    return pattern.sub(
        lambda match: _privacy_safe_source_id(match.group(0)),
        value,
    )


_SOURCE_REPORT_KEYS = frozenset(
    {
        "source",
        "source_id",
        "source_ids",
        "selected_source_ids",
        "evidence_source_ids",
        "required_source_ids",
        "forbidden_source_ids",
        "stale_source_ids",
    }
)


def _privacy_safe_report_value(value: object, *, key: str = "", parent_key: str = "") -> object:
    if isinstance(value, str):
        return _privacy_safe_text(value)
    normalized_key = key.lower()
    if normalized_key in _SOURCE_REPORT_KEYS:
        if isinstance(value, str):
            return _privacy_safe_source_id(value)
        if isinstance(value, (list, tuple, set, frozenset)):
            return _privacy_safe_source_values(value)
    if normalized_key == "id" and "manifest" in parent_key.lower():
        return _privacy_safe_source_id(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): _privacy_safe_report_value(
                child_value,
                key=str(child_key),
                parent_key=normalized_key,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _privacy_safe_report_value(item, parent_key=normalized_key)
            for item in value
        ]
    converted = redact_trace_value(value)
    if converted is not value:
        return _privacy_safe_report_value(converted, key=key, parent_key=parent_key)
    return value


_REVIEWER_ORDER = {
    "safety_reviewer": 0,
    "grounding_reviewer": 1,
    "safety": 0,
    "grounding": 1,
}


def _normalize_reviewer_path(values: object) -> list[str]:
    nodes = [str(value) for value in values] if isinstance(values, (list, tuple)) else []
    for index in range(len(nodes) - 1):
        if nodes[index:index + 2] == ["grounding_reviewer", "safety_reviewer"]:
            nodes[index:index + 2] = ["safety_reviewer", "grounding_reviewer"]
    return nodes


def _normalize_reviewer_outcomes(values: object) -> list[str]:
    outcomes = [str(value) for value in values] if isinstance(values, (list, tuple)) else []
    indexed = list(enumerate(outcomes))
    indexed.sort(
        key=lambda item: (
            _REVIEWER_ORDER.get(item[1].partition(":")[0], len(_REVIEWER_ORDER)),
            item[0],
        )
    )
    return [value for _index, value in indexed]


def _normalized_turn_observation(observation: object) -> dict[str, Any]:
    normalized = dict(observation) if isinstance(observation, dict) else {}
    normalized["normalized_visited_nodes"] = _normalize_reviewer_path(
        normalized.pop("visited_nodes", [])
    )
    normalized["normalized_reviewer_outcomes"] = _normalize_reviewer_outcomes(
        normalized.pop("reviewer_outcomes", [])
    )
    for key in ("selected_source_ids", "evidence_source_ids", "omitted_source_ids", "unknown_planner_source_ids_rejected", "compacted_source_ids", "context_candidate_source_ids"):
        if key in normalized:
            normalized[key] = _privacy_safe_source_values(normalized[key])
    source_map = normalized.get("source_ids_by_source")
    if isinstance(source_map, dict):
        normalized["source_ids_by_source"] = {
            str(source): _privacy_safe_source_values(source_ids)
            for source, source_ids in source_map.items()
        }
    return normalized


def _report_result_dict(result: LongitudinalResult) -> dict[str, Any]:
    serialized = result.to_dict()
    serialized.pop("raw_visited_nodes", None)
    serialized.pop("raw_reviewer_outcomes", None)
    serialized["normalized_visited_nodes"] = _normalize_reviewer_path(
        serialized.pop("visited_nodes")
    )
    serialized["normalized_reviewer_outcomes"] = _normalize_reviewer_outcomes(
        serialized.pop("reviewer_outcomes")
    )
    for key in ("selected_source_ids", "evidence_source_ids", "omitted_source_ids", "unknown_planner_source_ids_rejected", "compacted_source_ids", "context_candidate_source_ids"):
        if key in serialized:
            serialized[key] = _privacy_safe_source_values(serialized[key])
    serialized["turn_observations"] = [
        _normalized_turn_observation(observation)
        for observation in serialized["turn_observations"]
    ]
    if serialized.get("trace") is not None:
        serialized["trace"] = _privacy_safe_report_value(serialized["trace"])
    return serialized



@dataclass(frozen=True)
class LongitudinalCaseReport:
    scenario_id: str
    status: str
    rubric_applicability: dict[str, str]
    result: LongitudinalResult
    judge_result: JudgeResult | None = None
    rubric_summary: RubricSummary | None = None
    judge_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status,
            "rubric_applicability": dict(self.rubric_applicability),
            "result": _report_result_dict(self.result),
            "judge": (
                _privacy_safe_report_value(self.judge_result.to_dict())
                if self.judge_result
                else None
            ),
            "rubric_summary": (
                _privacy_safe_report_value(self.rubric_summary.to_dict())
                if self.rubric_summary
                else None
            ),
            "judge_error": _privacy_safe_text(self.judge_error) if self.judge_error else None,
        }


@dataclass(frozen=True)
class LongitudinalReport:
    generated_at: str
    report_version: str
    rubric_version: str
    rubric_dimensions: tuple[str, ...]
    cases: tuple[LongitudinalCaseReport, ...]
    disclaimer: str = REPORT_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "report_version": self.report_version,
            "rubric_version": self.rubric_version,
            "rubric_dimensions": list(self.rubric_dimensions),
            "disclaimer": self.disclaimer,
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    def to_markdown(self) -> str:
        lines = [
            "# Longitudinal v2 engineering evaluation",
            "",
            f"- Report version: {self.report_version}",
            f"- Rubric version: {self.rubric_version}",
            f"- {self.disclaimer}",
            "",
            "| Scenario | Status | Route | Judge |",
            "| --- | --- | --- | --- |",
        ]
        for case in self.cases:
            judge_status = (
                "error"
                if case.judge_error is not None
                else "not configured"
                if case.judge_result is None
                else "error"
                if case.rubric_summary is None
                else "passed"
                if case.rubric_summary.passed
                else "failed"
            )
            lines.append(
                f"| {case.scenario_id} | {case.status} | "
                f"{case.result.route or '-'} | {judge_status} |"
            )
        lines.extend(["", "## Rubric dimensions", ""])
        lines.append(", ".join(self.rubric_dimensions))
        for case in self.cases:
            lines.extend(["", f"## {case.scenario_id}", ""])
            lines.append(
                "Applicability: "
                + ", ".join(
                    f"{dimension}={case.rubric_applicability[dimension]}"
                    for dimension in self.rubric_dimensions
                )
            )
            lines.extend(["", chr(96) * 3 + "json"])
            lines.append(json.dumps(case.to_dict(), sort_keys=True, indent=2))
            lines.append(chr(96) * 3)
        return "\n".join(lines) + "\n"


def observed_evidence_manifest(runtime_events: object) -> list[dict[str, Any]]:
    """Expose only response and content observed from the live service."""
    manifest: list[dict[str, Any]] = [
        {
            "id": "response",
            "authorized": True,
            "provenance": "live_service_response",
            "content_available": True,
        }
    ]
    if not isinstance(runtime_events, (list, tuple)):
        return manifest
    seen_sequences: set[int] = set()
    seen_sources: set[tuple[str, str]] = set()
    for raw_event in runtime_events:
        if not isinstance(raw_event, Mapping):
            continue
        sequence = raw_event.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or sequence in seen_sequences
        ):
            continue
        seen_sequences.add(sequence)
        event_type = str(raw_event.get("event_type") or "")
        result = raw_event.get("result")
        content_available = (
            event_type in {"tool_result", "context_observed"}
            and isinstance(result, Mapping)
            and isinstance(result.get("content"), str)
            and bool(result["content"].strip())
        )
        source_id = str(raw_event.get("source_id") or "").strip()
        provenance = (
            "live_service_context"
            if str(raw_event.get("source") or "") == "live_service_context"
            else "live_service_runtime"
        )
        manifest_id = source_id or f"runtime_event:{sequence}"
        source_key = (manifest_id, event_type)
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        manifest.append(
            {
                "id": manifest_id,
                "authorized": True,
                "provenance": provenance,
                "event_type": event_type,
                "content_available": content_available,
                **(
                    {"tool_name": str(raw_event.get("tool_name") or "unknown_tool")}
                    if event_type in {"tool_call", "tool_result"}
                    else {}
                ),
            }
        )
    return manifest


def _authorized_manifest(
    scenario: ScenarioV2,
    result: LongitudinalResult,
) -> list[dict[str, Any]]:
    """Build judge evidence strictly from the persisted live runtime trace."""
    del scenario
    return observed_evidence_manifest(result.runtime_events)

def _judge_task_context(
    scenario: ScenarioV2,
    result: LongitudinalResult,
) -> dict[str, Any]:
    if isinstance(result.trace, Mapping) and isinstance(result.trace.get("task_context"), Mapping):
        return dict(result.trace["task_context"])
    return {
        "scenario_id": scenario.scenario_id,
        "user_goal": scenario.oracles.get("user_goal"),
        "case_condition": scenario.oracles.get("case_condition"),
        "information_need": scenario.oracles.get("information_need"),
        "target_boundary": scenario.oracles.get("target_boundary"),
        "timeline": [
            {
                "operation": str(step.get("op") or ""),
                "message": str(step.get("message") or ""),
            }
            for step in scenario.timeline
            if step.get("op") in {"turn", "resume"}
        ],
    }


def build_case_report(
    scenario: ScenarioV2,
    result: LongitudinalResult,
    *,
    judge: object | None = None,
) -> LongitudinalCaseReport:
    from evals.longitudinal.judge import (
        evaluate_with_repair,
        invoke_configured_judge,
    )
    judge_result: JudgeResult | None = None
    rubric_summary: RubricSummary | None = None
    judge_error: str | None = None
    judge_trace: dict[str, Any] | None = None
    judge_attempts: tuple[Any, ...] = ()
    if judge is not None:
        try:
            manifest = _authorized_manifest(scenario, result)
            evaluation = evaluate_with_repair(
                scenario=scenario,
                response=result.final_response or "",
                evidence_manifest=manifest,
                invoke=lambda payload: invoke_configured_judge(judge, payload),
                runtime_events=getattr(result, "runtime_events", ()),
                task_context=_judge_task_context(scenario, result),
            )
            judge_attempts = evaluation.attempts
            judge_result = evaluation.result
            rubric_summary = evaluation.summary
            judge_error = evaluation.error
            if judge_result is not None and judge_attempts:
                final_attempt = judge_attempts[-1]
                judge_trace = {
                    "input": final_attempt.payload,
                    "raw_output": final_attempt.raw_output,
                    "parsed_scores": judge_result.to_dict(),
                    "rubric_summary": rubric_summary.to_dict() if rubric_summary else None,
                    "rationale": {
                        dimension: {
                            "reason_codes": list(item.reason_codes),
                            "evidence_spans": [
                                {
                                    "source": span.source,
                                    "start": span.start,
                                    "end": span.end,
                                    "text": span.text,
                                }
                                for span in item.evidence_spans
                            ],
                        }
                        for dimension, item in judge_result.criteria.items()
                    },
                }
        except Exception as exc:
            judge_error = f"{type(exc).__name__}: {exc}"
    trace = result.trace
    if trace is not None:
        trace = dict(trace)
        trace["unavailable_reasons"] = dict(trace.get("unavailable_reasons", {}))
        for attempt_index, attempt in enumerate(judge_attempts):
            is_final_success = (
                judge_result is not None and attempt_index == len(judge_attempts) - 1
            )
            trace = append_judge_events(
                trace,
                request=attempt.payload,
                raw_response=attempt.raw_output,
                structured_output=judge_result.to_dict() if is_final_success else None,
                rationale=judge_trace["rationale"] if is_final_success and judge_trace else None,
                usage_metadata=attempt.usage_metadata,
                error=(
                    {
                        "error_type": "judge_failure",
                        "message": attempt.error,
                        "provider_error": dict(attempt.error_details or {}),
                    }
                    if attempt.error is not None
                    else None
                ),
            )
        if judge_attempts:
            trace["judge_input"] = judge_attempts[0].payload
            trace["judge_output"] = (
                redact_trace_value(judge_attempts[-1].raw_output)
                if judge_attempts[-1].raw_output is not None else None
            )
        if judge_trace is not None:
            trace["judge"] = redact_trace_value(judge_trace)
            trace["final"]["judge"] = redact_trace_value(judge_trace)
            trace["unavailable_reasons"].pop("judge", None)
        elif judge_error is not None:
            trace["unavailable_reasons"]["judge"] = "judge_failed:" + judge_error
        result = replace(result, trace=trace)
    if judge is None:
        status = "completed_unjudged"
    elif judge_error is not None:
        status = "completed_with_judge_error"
    elif rubric_summary is not None and not rubric_summary.passed:
        status = "failed"
    elif rubric_summary is not None:
        status = "passed"
    else:
        status = "completed_with_judge_error"
        judge_error = judge_error or "judge_result_missing"
    return LongitudinalCaseReport(
        scenario_id=scenario.scenario_id,
        status=status,
        rubric_applicability=dict(scenario.rubric_applicability),
        result=result,
        judge_result=judge_result,
        rubric_summary=rubric_summary,
        judge_error=judge_error,
    )


def build_report(
    cases: list[LongitudinalCaseReport],
    *,
    generated_at: str | None = None,
) -> LongitudinalReport:
    # Provider-free v2 reports are replay artifacts. Callers that need an
    # observed generation time can pass it explicitly.
    report_generated_at = (
        generated_at if generated_at is not None else "not_provided"
    )
    return LongitudinalReport(
        generated_at=report_generated_at,
        report_version=REPORT_VERSION,
        rubric_version="v2",
        rubric_dimensions=RUBRIC_DIMENSIONS,
        cases=tuple(cases),
    )
