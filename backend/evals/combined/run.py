from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import signal
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock, Semaphore
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from sqlalchemy.engine import make_url

from app.ai.workflow_state import WORKFLOW_VERSION
from app.core.config import backend_source_revision, redact_exception
from evals.corpus import DEFAULT_CASES_PATH, load_longitudinal_cases
from evals.longitudinal.contract import RUBRIC_DIMENSIONS, ScenarioV2
from evals.longitudinal.judge import evaluate_with_repair
from evals.trace import TraceRecorder, redact_trace_value
from evals.real_model.run import (
    _OpenAICompatibleProvider,
    _content,
    _conversation_messages,
    _metadata,
    _tokens,
    build_generation_prompt,
)
from evals.combined.report import (
    LONGITUDINAL_SCENARIO_COUNT,
    REAL_MODEL_CASE_COUNT,
    CombinedReport,
    build_combined_report,
)
from evals.combined.service import BackendHealth, ServiceHarness, ServiceHarnessConfig, validate_backend_health
from evals.combined.service_runner import ServiceCaseRunner, _real_model_fixture_scenario, judge_service_result, provision_service_identity
from evals.combined.tool_metrics import ToolCallRecord, aggregate_tool_metrics, group_tool_metrics
from evals.combined.progress import LedgerRejected, ProgressLedger, configuration_fingerprint, run_checkpointed_staged_cases, validate_manifest_ownership

DEFAULT_CASES = Path("evals/real_model/cases/v2.json")
DEFAULT_SCENARIO_DIR = DEFAULT_CASES_PATH
DEFAULT_ARTIFACT_DIR = Path("evals/artifacts/combined-v2")
REAL_MODEL_APPLICABILITY = {dimension: "primary" for dimension in RUBRIC_DIMENSIONS}

_DATABASE_CREDENTIAL_RE = re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+@")


def _privacy_safe_error_message(
    error: BaseException,
    *,
    secrets: tuple[str, ...] = (),
) -> str:
    value = redact_exception(error)
    value = _DATABASE_CREDENTIAL_RE.sub(r"\1[REDACTED]@", value)
    return str(redact_trace_value(value, secrets=secrets))


def _derive_application_schema_revision(backend_dir: Path) -> str:
    """Resolve the repository's one current Alembic head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_dir = backend_dir.resolve().parent
    config = Config(str(repo_dir / "alembic.ini"))
    heads = tuple(ScriptDirectory.from_config(config).get_heads())
    if len(heads) != 1:
        raise RuntimeError(
            "evaluator requires exactly one Alembic head; found: "
            + ", ".join(heads)
        )
    return heads[0]


def evaluation_completed(reports: list[Mapping[str, Any]]) -> bool:
    """Return whether the complete 8+36 case set produced terminal reports."""
    return len(reports) == REAL_MODEL_CASE_COUNT + LONGITUDINAL_SCENARIO_COUNT


def evaluation_quality_passed(reports: list[Mapping[str, Any]]) -> bool:
    """Return whether every completed report passed its judge gate."""
    return evaluation_completed(reports) and all(
        entry.get("status") == "passed"
        and entry.get("judge_status") in {"passed", "not_applicable"}
        for entry in reports
    )


_MATRIX_CASE_CONDITIONS = frozenset({"no-call-appropriate", "retrieval-needed"})
_MATRIX_CASE_COUNT = 10


def _live_service_runtime_events(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        raw_event
        for raw_event in value
        if isinstance(raw_event, Mapping)
        and raw_event.get("source") == "live_service_runtime"
    ]


def _ordered_live_runtime_events(value: object) -> bool:
    live_events = _live_service_runtime_events(value)
    if not live_events:
        return False
    previous_sequence = 0
    for raw_event in live_events:
        sequence = raw_event.get("sequence")
        if not isinstance(sequence, int) or sequence <= previous_sequence:
            return False
        if raw_event.get("event_type") not in {"tool_call", "tool_result"}:
            return False
        previous_sequence = sequence
    return True


def evaluation_verification_diagnostics(
    reports: list[Mapping[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    if not evaluation_completed(reports):
        failures.append(
            f"case_count:{len(reports)}_expected:{REAL_MODEL_CASE_COUNT + LONGITUDINAL_SCENARIO_COUNT}"
        )
    for index, entry in enumerate(reports):
        case_id = str(entry.get("scenario_id") or entry.get("case_id") or index)
        if (
            entry.get("execution_mode") != "real-backend-service"
            or entry.get("provider_free") is not False
        ):
            failures.append(f"{case_id}:not_real_backend_service")
        if entry.get("judge_status") not in {"passed", "failed", "not_applicable"}:
            failures.append(f"{case_id}:judge_not_terminal")
        if entry.get("judge_error") or entry.get("judge_error_type"):
            failures.append(f"{case_id}:judge_error")
        if not isinstance(entry.get("response"), str) or not entry["response"].strip():
            failures.append(f"{case_id}:response_missing")
        if not isinstance(entry.get("runtime_events"), list):
            failures.append(f"{case_id}:runtime_events_missing")
        fixture_setup = entry.get("fixture_setup")
        if isinstance(fixture_setup, Mapping) and fixture_setup.get("failures"):
            failures.append(f"{case_id}:fixture_setup_failed")
        for failure_key in ("failures", "report_failures"):
            raw_failures = entry.get(failure_key)
            if isinstance(raw_failures, (list, tuple)):
                if any(not str(failure).strip().startswith("budget") for failure in raw_failures):
                    failures.append(f"{case_id}:{failure_key}_contains_execution_failure")
        if entry.get("resource_manifest") is None:
            failures.append(f"{case_id}:resource_manifest_missing")

    matrix_entries = [
        entry
        for entry in reports
        if str(
            (entry.get("task_context") or {}).get("case_condition")
            if isinstance(entry.get("task_context"), Mapping)
            else ""
        ) in _MATRIX_CASE_CONDITIONS
    ]
    retrieval_entries = [
        entry for entry in matrix_entries
        if entry.get("task_context", {}).get("case_condition") == "retrieval-needed"
    ]
    no_call_entries = [
        entry for entry in matrix_entries
        if entry.get("task_context", {}).get("case_condition") == "no-call-appropriate"
    ]
    retrieval_with_events = sum(
        1 for entry in retrieval_entries
        if _ordered_live_runtime_events(entry.get("runtime_events"))
    )
    no_call_with_empty_trace = sum(
        1 for entry in no_call_entries
        if not _live_service_runtime_events(entry.get("runtime_events"))
    )
    if len(matrix_entries) != _MATRIX_CASE_COUNT:
        failures.append(
            f"matrix_case_count:{len(matrix_entries)}_expected:{_MATRIX_CASE_COUNT}"
        )
    if not retrieval_entries:
        failures.append("matrix_retrieval_needed_cases_missing")
    if retrieval_with_events == 0:
        failures.append(
            "matrix_retrieval_live_evidence_missing:"
            f"{len(retrieval_entries)}_retrieval_cases"
        )
    if not no_call_entries:
        failures.append("matrix_no_call_cases_missing")
    if no_call_with_empty_trace == 0:
        failures.append("matrix_no_call_empty_trace_missing")

    return {
        "completed": evaluation_completed(reports),
        "passed": not failures,
        "matrix_case_count": len(matrix_entries),
        "retrieval_needed_case_count": len(retrieval_entries),
        "retrieval_needed_with_ordered_live_events": retrieval_with_events,
        "no_call_appropriate_case_count": len(no_call_entries),
        "no_call_with_empty_runtime_events": no_call_with_empty_trace,
        "failures": list(dict.fromkeys(failures)),
    }


def evaluation_verification_passed(reports: list[Mapping[str, Any]]) -> bool:
    """Require live execution and truthful ten-case matrix evidence."""
    return bool(evaluation_verification_diagnostics(reports)["passed"])


def budget_status_summary(reports: list[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"compliant": 0, "noncompliant": 0, "unavailable": 0}
    for report in reports:
        comparison = report.get("budget_comparison")
        if (
            not isinstance(comparison, Mapping)
            or comparison.get("unavailable") is True
            or comparison.get("status") == "unavailable"
        ):
            counts["unavailable"] += 1
        elif comparison.get("compliant") is True and comparison.get("status") != "noncompliant":
            counts["compliant"] += 1
        else:
            counts["noncompliant"] += 1
    counts["total"] = len(reports)
    return counts


class DiagnosticProvider(Protocol):
    def invoke(self, prompt: str, *, model: str) -> object: ...


@dataclass(frozen=True)
class _RealModelScenario:
    scenario_id: str
    rubric_applicability: dict[str, str]
    oracles: dict[str, Any]


def _response_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if "content" in value or "response" in value:
            return _content(value.get("content", value.get("response", "")))
        return json.dumps(dict(value), sort_keys=True)
    content = _content(value)
    if content:
        return content
    return str(value or "")


def _invoke_judge(provider: object, prompt: str, *, model: str) -> object:
    if callable(provider):
        return provider(prompt)
    for method_name in ("judge", "invoke"):
        method = getattr(provider, method_name, None)
        if callable(method):
            return method(prompt, model=model) if method_name == "invoke" else method(prompt)
    raise TypeError("configured judge must be callable or expose judge(payload) or invoke(prompt, model=)")


def _transcript_message(
    *,
    role: str,
    stage: str,
    content: str,
) -> dict[str, str]:
    return {"role": role, "stage": stage, "content": content}


def _real_model_case(
    case: Mapping[str, Any],
    *,
    generator: DiagnosticProvider,
    judge: object | None,
    generation_model: str,
    judge_model: str,
) -> dict[str, Any]:
    case_id = str(case.get("case_id") or "synthetic-case")
    input_dialog = _conversation_messages(dict(case))
    transcript = [
        _transcript_message(
            role=message["role"],
            stage=f"synthetic_turn_{index}",
            content=message["content"],
        )
        for index, message in enumerate(input_dialog, start=1)
    ]
    recorder = TraceRecorder(
        case_id,
        run_configuration={
            "suite": "real_model_v2",
            "runner_mode": "real-model-provider",
            "source_revision": "79436e06495a730aafe99393a765d66ff66d7474",
            "prompt_version": "real-model-diagnostic-prompt-v4",
            "rubric_version": "v2",
            "provider": "configured-provider",
            "generation_model": generation_model,
            "judge_model": judge_model,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "effective_budgets": {
                "model_calls": 2,
                "tool_calls": 0,
                "repairs": 1,
            },
        },
    )
    recorder.start_turn(
        f"{case_id}:turn-1",
        input_dialog=input_dialog,
        payload={"case_id": case_id},
    )
    generation_prompt = ""
    generation_response = ""
    judge_prompt = ""
    judge_response = ""
    generated_metadata: dict[str, Any] = {}
    generation_error: str | None = None

    try:
        generation_prompt = build_generation_prompt(dict(case))
        recorder.record(
            "model_request",
            {
                "agent": "generation",
                "model": generation_model,
                "messages": input_dialog,
                "prompt": generation_prompt,
            },
        )
        transcript.append(
            _transcript_message(
                role="user",
                stage="generation_prompt",
                content=generation_prompt,
            )
        )
        generated = generator.invoke(generation_prompt, model=generation_model)
        generation_response = _content(generated)
        generated_metadata = _metadata(generated)
        input_tokens, output_tokens = _tokens(generated)
        recorder.record(
            "model_response",
            {
                "agent": "generation",
                "model": generation_model,
                "response": generation_response,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "metadata": generated_metadata,
            },
        )
        transcript.append(
            _transcript_message(
                role="assistant",
                stage="generation_response",
                content=generation_response,
            )
        )
        for index, outcome in enumerate(generated_metadata.get("review_outcomes", ()) or (), start=1):
            recorder.record(
                "review_finished",
                {"stage": "generation", "review_index": index, "outcome": str(outcome)},
            )
        for index in range(int(generated_metadata.get("repair_count", 0) or 0)):
            recorder.record(
                "repair",
                {"stage": "generation", "repair_index": index + 1, "source": "provider_metadata"},
            )
    except Exception as exc:
        generation_error = f"{type(exc).__name__}: {exc}"

    if generation_error is not None:
        return {
            "suite": "real_model",
            "case_id": case_id,
            "execution_mode": "real-model-provider",
            "provider_free": False,
            "status": "generation_error",
            "judge_status": "not_attempted",
            "judge_error": generation_error,
            "generation_model": generation_model,
            "judge_model": judge_model,
            "rubric_applicability": dict(REAL_MODEL_APPLICABILITY),
            "generation_prompt": generation_prompt,
            "generation_response": generation_response,
            "judge_prompt": judge_prompt,
            "judge_response": judge_response,
            "conversation": transcript,
            "trace": recorder.finalize(
                summary={
                    "model_calls": 1,
                    "tool_calls": 0,
                    "repair_count": 0,
                    "total_tokens": 0,
                    "elapsed_ms": None,
                },
                final_dialog=transcript,
                unavailable_reasons={
                    "judge": "generation_failed_before_judge",
                    "graph_transitions": "real_model_provider_contract_exposes_text_generation_only",
                    "tool_invocations": "real_model_provider_contract_exposes_no_product_tool_calls",
                    "elapsed_ms": "combined_case_wrapper_does_not_measure_provider_latency",
            "model_retries": "provider_response_exposes_no_retry_trace",
                },
            ),
        }

    judge_result: Any = None
    rubric_summary: Any = None
    judge_status = "not_configured" if judge is None else "error"
    judge_error: str | None = None
    judge_input_tokens: int | None = None
    judge_output_tokens: int | None = None
    judge_attempts: tuple[Any, ...] = ()
    if judge is not None:
        try:
            scenario = _RealModelScenario(
                scenario_id=case_id,
                rubric_applicability=dict(REAL_MODEL_APPLICABILITY),
                oracles={},
            )
            manifest = [
                {"id": str(source_id), "authorized": True}
                for source_id in case.get("authorized_evidence", []) or []
            ]
            manifest.append({"id": "response", "content": generation_response, "authorized": True})
            evaluation = evaluate_with_repair(
                scenario=scenario,
                response=generation_response,
                evidence_manifest=manifest,
                invoke=lambda payload: _invoke_judge(
                    judge,
                    payload,
                    model=judge_model,
                ),
            )
            judge_attempts = evaluation.attempts
            for attempt_index, attempt in enumerate(judge_attempts):
                attempt_response = _response_text(attempt.raw_output)
                stage_suffix = "" if attempt_index == 0 else "_repair"
                recorder.record(
                    "judge_request",
                    {
                        "attempt": attempt_index + 1,
                        "model": judge_model,
                        "prompt": attempt.payload,
                        "authorized_evidence_manifest": manifest,
                    },
                )
                transcript.append(
                    _transcript_message(
                        role="user",
                        stage=f"judge{stage_suffix}_prompt",
                        content=attempt.payload,
                    )
                )
                judge_response_payload: dict[str, Any] = {
                    "attempt": attempt_index + 1,
                    "model": judge_model,
                    "raw_output": attempt.raw_output,
                    "response": attempt_response,
                }
                if attempt.error is not None:
                    judge_response_payload["error"] = attempt.error
                if attempt.usage_metadata:
                    judge_response_payload.update(
                        {
                            key: attempt.usage_metadata[key]
                            for key in ("input_tokens", "output_tokens")
                            if isinstance(attempt.usage_metadata.get(key), int)
                        }
                    )
                recorder.record("judge_response", judge_response_payload)
                transcript.append(
                    _transcript_message(
                        role="assistant",
                        stage=f"judge{stage_suffix}_response",
                        content=attempt_response,
                    )
                )
            if judge_attempts:
                final_attempt = judge_attempts[-1]
                judge_prompt = final_attempt.payload
                judge_response = _response_text(final_attempt.raw_output)
                judge_input_tokens, judge_output_tokens = _tokens(final_attempt.raw_output)
            judge_result = evaluation.result
            rubric_summary = evaluation.summary
            judge_error = evaluation.error
            if rubric_summary is not None:
                judge_status = "passed" if rubric_summary.passed else "failed"
        except Exception as exc:
            judge_status = "error"
            judge_error = f"{type(exc).__name__}: {exc}"

    if judge_status == "not_configured":
        status = "completed_unjudged"
    elif judge_status == "error":
        status = "completed_with_judge_error"
    else:
        status = "passed" if judge_status == "passed" else "failed"

    generated_input_tokens, generated_output_tokens = _tokens(generated)
    judge_trace = None
    if judge_result is not None:
        judge_trace = {
            "input": judge_prompt,
            "raw_output": judge_response,
            "parsed_scores": judge_result.to_dict(),
            "rubric_summary": rubric_summary.to_dict() if rubric_summary is not None else None,
            "rationale": judge_result.to_dict(),
        }
    trace = recorder.finalize(
        summary={
            "model_calls": 1 + len(judge_attempts) if judge is not None else 1,
            "tool_calls": 0,
            "repair_count": int(generated_metadata.get("repair_count", 0) or 0),
            "total_tokens": (
                sum(
                    token
                    for token in (
                        generated_input_tokens,
                        generated_output_tokens,
                        *((
                            judge_input_tokens,
                            judge_output_tokens,
                        ) if judge is not None else ()),
                    )
                    if token is not None
                )
                if all(
                    token is not None
                    for token in (
                        generated_input_tokens,
                        generated_output_tokens,
                        *((
                            judge_input_tokens,
                            judge_output_tokens,
                        ) if judge is not None else ()),
                    )
                )
                else None
            ),
            "elapsed_ms": None,
        },
        final_dialog=transcript,
        judge=judge_trace,
        unavailable_reasons={
            "graph_transitions": "real_model_provider_contract_exposes_text_generation_only",
            "tool_invocations": "real_model_provider_contract_exposes_no_product_tool_calls",
            "elapsed_ms": "combined_case_wrapper_does_not_measure_provider_latency",
            **({"judge": "judge_not_configured"} if judge is None else {}),
        },
    )
    return {
        "suite": "real_model",
        "case_id": case_id,
        "execution_mode": "real-model-provider",
        "provider_free": False,
        "status": status,
        "judge_status": judge_status,
        "judge_error": judge_error,
        "generation_model": generation_model,
        "judge_model": judge_model,
        "rubric_applicability": dict(REAL_MODEL_APPLICABILITY),
        "generation_prompt": generation_prompt,
        "generation_response": generation_response,
        "judge_prompt": judge_prompt,
        "judge_response": judge_response,
        "conversation": transcript,
        "route": str(generated_metadata.get("route") or "") or None,
        "review_outcomes": [
            str(item) for item in generated_metadata.get("review_outcomes", ()) or ()
        ],
        "repair_count": int(generated_metadata.get("repair_count", 0) or 0),
        "judge_repair_count": max(0, len(judge_attempts) - 1),
        "input_tokens": generated_input_tokens,
        "output_tokens": generated_output_tokens,
        "judge": judge_result.to_dict() if judge_result is not None else None,
        "rubric_summary": rubric_summary.to_dict() if rubric_summary is not None else None,
        "trace": trace,
    }


def _load_real_model_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("real-model v2 fixture must contain a list")
    if not all(isinstance(item, Mapping) for item in payload):
        raise ValueError("real-model v2 fixture entries must be objects")
    cases = [dict(item) for item in payload]
    if len(cases) != REAL_MODEL_CASE_COUNT:
        raise RuntimeError(
            f"real-model v2 fixture requires exactly {REAL_MODEL_CASE_COUNT} cases; "
            f"received {len(cases)}"
        )
    ids = [str(case.get("case_id") or "") for case in cases]
    if any(not case_id for case_id in ids) or len(set(ids)) != len(ids):
        raise RuntimeError("real-model v2 fixture contains missing or duplicate case IDs")
    return cases


def _load_longitudinal_scenarios() -> list[ScenarioV2]:
    scenarios = load_longitudinal_cases(DEFAULT_SCENARIO_DIR)
    if len(scenarios) != LONGITUDINAL_SCENARIO_COUNT:
        raise RuntimeError(
            f"longitudinal v2 fixture requires exactly {LONGITUDINAL_SCENARIO_COUNT} scenarios; "
            f"received {len(scenarios)}"
        )
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(set(ids)) != len(ids):
        raise RuntimeError("longitudinal v2 fixture contains duplicate scenario IDs")
    return scenarios


def run_combined(
    real_model_cases: list[dict[str, Any]],
    longitudinal_scenarios: list[ScenarioV2],
    *,
    generator: DiagnosticProvider,
    judge: object | None,
    generation_model: str,
    judge_model: str,
    include_conversation: bool = True,
    max_parallel_cases: int = 1,
    generated_at: str | None = None,
    redaction_secrets: tuple[str, ...] = (),
) -> CombinedReport:
    raise RuntimeError("provider-free combined execution is retired; use the service-backed evaluator entrypoint")
    if len(real_model_cases) != REAL_MODEL_CASE_COUNT:
        raise RuntimeError("combined runner received an incomplete real-model suite")
    if len(longitudinal_scenarios) != LONGITUDINAL_SCENARIO_COUNT:
        raise RuntimeError("combined runner received an incomplete longitudinal suite")
    if not isinstance(max_parallel_cases, int) or isinstance(max_parallel_cases, bool):
        raise ValueError("max_parallel_cases must be a positive integer")
    if max_parallel_cases < 1:
        raise ValueError("max_parallel_cases must be a positive integer")

    def execute_real(case: dict[str, Any]) -> dict[str, Any]:
        return _real_model_case(
            case,
            generator=generator,
            judge=judge,
            generation_model=generation_model,
            judge_model=judge_model,
        )

    def execute_longitudinal(scenario: ScenarioV2) -> dict[str, Any]:
        return _longitudinal_case(scenario, judge=judge, judge_model=judge_model)

    if max_parallel_cases == 1:
        real_reports = [execute_real(case) for case in real_model_cases]
        longitudinal_reports = [
            execute_longitudinal(scenario)
            for scenario in longitudinal_scenarios
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_parallel_cases, thread_name_prefix="rehab-eval") as executor:
            real_futures = [executor.submit(execute_real, case) for case in real_model_cases]
            longitudinal_futures = [
                executor.submit(execute_longitudinal, scenario)
                for scenario in longitudinal_scenarios
            ]
            real_reports = [future.result() for future in real_futures]
            longitudinal_reports = [future.result() for future in longitudinal_futures]

    if not include_conversation:
        for report in real_reports:
            report["conversation"] = None
    return build_combined_report(
        real_model_cases=real_reports,
        longitudinal_cases=longitudinal_reports,
        generated_at=generated_at,
        redaction_secrets=redaction_secrets,
    )


def readable_artifact_paths(markdown_path: Path) -> tuple[Path, Path]:
    return (
        markdown_path.with_name(f"{markdown_path.stem}-judge.md"),
        markdown_path.with_name(f"{markdown_path.stem}-messages.md"),
    )


def write_readable_artifacts(
    report: CombinedReport,
    markdown_path: Path,
) -> tuple[Path, Path]:
    judge_path, messages_path = readable_artifact_paths(markdown_path)
    judge_path.write_text(report.to_markdown(include_messages=False), encoding="utf-8")
    messages_path.write_text(report.to_messages_markdown(), encoding="utf-8")
    return judge_path, messages_path


def write_artifacts(
    report: CombinedReport,
    artifact_dir: Path,
) -> tuple[Path, Path]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = artifact_dir / f"combined-{stamp}.json"
    markdown_path = artifact_dir / f"combined-{stamp}.md"
    json_path.write_text(report.to_json() + "\n", encoding="utf-8")
    markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    write_readable_artifacts(report, markdown_path)
    return json_path, markdown_path


def write_diagnostic_artifacts(
    *,
    artifact_dir: Path,
    phase: str,
    error: BaseException,
    run_metadata: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    artifact_paths: tuple[Path, Path] | None = None,
    redaction_secrets: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    """Write a privacy-safe failure artifact without requiring complete suite counts."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if artifact_paths is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact_paths = (
            artifact_dir / f"combined-diagnostic-{stamp}.json",
            artifact_dir / f"combined-diagnostic-{stamp}.md",
        )
    json_path, markdown_path = artifact_paths
    error_message = _privacy_safe_error_message(error, secrets=redaction_secrets)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_version": "combined-v2-diagnostic",
        "status": "failed",
        "diagnostic": {
            "phase": phase,
            "error_type": type(error).__name__,
            "error_message": error_message,
        },
        "counts": {
            "real_model_cases": 0,
            "longitudinal_scenarios": 0,
            "total": 0,
        },
        "run_metadata": dict(run_metadata),
        "cleanup": dict(cleanup),
    }
    rendered_json = json.dumps(payload, sort_keys=True, indent=2)
    rendered_markdown = (
        "# Combined v2 diagnostic artifact\n\n"
        f"- Status: {payload['status']}\n"
        f"- Failure phase: {phase}\n"
        f"- Failure type: {type(error).__name__}\n"
        f"- Failure message: {error_message}\n"
        "- No complete 8 + 26 case report was available.\n"
        f"- Cleanup status: {cleanup.get('status', 'unknown')}\n"
        f"- Cleanup failures: {', '.join(str(item) for item in cleanup.get('failures', [])) or 'none'}\n\n"
        "JSON payload:\n\n"
        f"{rendered_json}\n"
    )
    json_path.write_text(rendered_json + "\n", encoding="utf-8")
    markdown_path.write_text(rendered_markdown, encoding="utf-8")
    return json_path, markdown_path


def _finalize_failed_run(
    *,
    artifact_dir: Path,
    run_manifest: Any,
    backend_revision: str,
    application_database_url: str,
    checkpoint_database_url: str,
    qdrant_url: str,
    qdrant_api_key: str | None,
    qdrant_path: str | None = None,
    qdrant_owner_identity: str | None = None,
    database_bootstrap: Any | None,
    phase: str,
    error: BaseException,
    cleanup_fn: Callable[..., Any],
    bootstrap_cleanup: Any | None = None,
    redaction_secrets: tuple[str, ...] = (),
) -> tuple[Path, Path, Any]:
    """Persist a safe diagnostic before exact cleanup and rewrite it with cleanup status."""
    run_metadata = {
        "execution_mode": "real-backend-service",
        "evaluator_revision": "unversioned",
        "evaluator_dirty_state": "not-applicable",
        "backend_revision": backend_revision,
        "manifest": run_manifest.privacy_safe(),
        "bootstrap_cleanup": (
            bootstrap_cleanup.to_dict()
            if bootstrap_cleanup is not None and hasattr(bootstrap_cleanup, "to_dict")
            else None
        ),
    }
    diagnostic_secrets = tuple(
        str(secret)
        for secret in (*redaction_secrets, qdrant_api_key)
        if secret
    )
    safe_error_message = _privacy_safe_error_message(error, secrets=diagnostic_secrets)
    if phase == "backend_startup" and str(error).startswith("backend service exited with code"):
        run_metadata["startup_failure_detail"] = safe_error_message
    pending_cleanup = {"status": "pending", "failures": []}
    json_path, markdown_path = write_diagnostic_artifacts(
        artifact_dir=artifact_dir,
        phase=phase,
        error=error,
        run_metadata={**run_metadata, "cleanup": pending_cleanup},
        cleanup=pending_cleanup,
        redaction_secrets=diagnostic_secrets,
    )

    def no_op_cleanup(_manifest: Any) -> tuple[int, int]:
        return 0, 0

    cleanup = cleanup_fn(
        run_manifest,
        application_database_url=application_database_url,
        checkpoint_database_url=checkpoint_database_url,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        qdrant_path=qdrant_path,
        qdrant_owner_identity=qdrant_owner_identity,
        database_bootstrap=database_bootstrap,
        application_cleanup=None if application_database_url else no_op_cleanup,
        checkpoint_cleanup=None if checkpoint_database_url else no_op_cleanup,
        qdrant_cleanup=None if qdrant_url or qdrant_path else no_op_cleanup,
    )
    if bootstrap_cleanup is not None and not getattr(bootstrap_cleanup, "ok", True):
        bootstrap_failures = list(getattr(bootstrap_cleanup, "failures", ()))
        cleanup.failures.extend(
            f"bootstrap_{failure}" for failure in bootstrap_failures
        )
        cleanup.status = "failed"
    cleanup_payload = cleanup.to_dict()
    write_diagnostic_artifacts(
        artifact_dir=artifact_dir,
        phase=phase,
        error=error,
        run_metadata={**run_metadata, "cleanup": cleanup_payload},
        cleanup=cleanup_payload,
        artifact_paths=(json_path, markdown_path),
        redaction_secrets=diagnostic_secrets,
    )
    return json_path, markdown_path, cleanup


def _enabled() -> bool:
    return os.getenv("REHAB_REAL_MODEL_ENABLED", "").strip().lower() in {"1", "true", "yes"}


class _EvaluatorInterrupted(RuntimeError):
    def __init__(self, signal_number: int) -> None:
        super().__init__(f"evaluator interrupted by signal {signal_number}")
        self.signal_number = int(signal_number)


class _EvaluatorChildExit(RuntimeError):
    def __init__(self, exit_code: int = 137) -> None:
        super().__init__(f"evaluator child exited with code {exit_code}")
        self.exit_code = int(exit_code)


def _case_key(suite: str, identifier: str) -> str:
    return f"{suite}:{identifier}"


def _fixture_file_digests(paths: tuple[Path, ...]) -> list[dict[str, str]]:
    digests: list[dict[str, str]] = []
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(item for item in path.rglob("*") if item.is_file())
        else:
            digests.append({"path": str(path), "sha256": "missing"})
    for path in sorted(set(files), key=str):
        digests.append({
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return digests


def _fixture_payload_digest(value: object) -> str:
    if hasattr(value, "__dataclass_fields__"):
        value = {
            key: getattr(value, key)
            for key in getattr(value, "__dataclass_fields__", {})
        }
    canonical = json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_configuration_fingerprint(
    args: argparse.Namespace,
    *,
    judge_key: str,
    real_model_cases: list[dict[str, Any]],
    longitudinal_scenarios: list[ScenarioV2],
) -> str:
    # Resource paths and auto-selected ports belong to the manifest, not the
    # evaluator configuration. They change when a direct invocation resumes a
    # run and must not make a valid ledger look mismatched.
    def argument(name: str, default: Any = "") -> Any:
        return getattr(args, name, default)

    configuration = {
        "backend_host": str(argument("backend_host")),
        "startup_timeout_seconds": float(argument("startup_timeout_seconds", 90)),
        "service_environment": str(argument("service_environment")),
        "backend_revision": str(argument("backend_revision")),
        "application_schema_revision": str(argument("application_schema_revision")),
        "workflow_version": str(argument("workflow_version")),
        "embedding_provider": str(argument("embedding_provider")),
        "backend_generation_provider": str(argument("backend_generation_provider")),
        "generation_model": str(argument("generation_model")),
        "judge_model": str(argument("judge_model")),
        "judge_thinking_level": str(argument("judge_thinking_level")),
        "include_conversation": bool(argument("include_conversation", False)),
        "evaluator_sql_namespace": str(argument("evaluator_sql_namespace")),
        "evaluator_application_database_name": str(argument("evaluator_application_database_name")),
        "evaluator_checkpoint_database_name": str(argument("evaluator_checkpoint_database_name")),
        "qdrant_url": str(argument("qdrant_url")).strip() or None,
        "qdrant_owner_identity": str(argument("qdrant_owner_identity")),
        "qdrant_allowlist": str(argument("qdrant_allowlist")),
        "judge_key": judge_key,
        "backend_generation_key_present": bool(str(argument("backend_generation_key")).strip()),
        "qdrant_api_key_present": bool(str(argument("qdrant_api_key")).strip()),
    }
    configuration.update(
        {
            "judge_key": judge_key,
            "real_model_case_ids": [str(item["case_id"]) for item in real_model_cases],
            "longitudinal_scenario_ids": [scenario.scenario_id for scenario in longitudinal_scenarios],
            "workflow_version": args.workflow_version,
            "fixture_file_digests": _fixture_file_digests(
                (Path(args.cases), Path(DEFAULT_SCENARIO_DIR))
            ),
            "real_model_fixture_payload_sha256": _fixture_payload_digest(real_model_cases),
            "longitudinal_fixture_payload_sha256": _fixture_payload_digest(longitudinal_scenarios),
        }
    )
    return configuration_fingerprint(configuration)

def _raise_on_child_exit_137(harness: ServiceHarness, error: BaseException) -> None:
    if _child_exit_code(harness, error) == 137:
        raise _EvaluatorChildExit(137) from error


def _child_exit_code(harness: ServiceHarness, error: BaseException | None = None) -> int | None:
    return_code = getattr(harness, "process_returncode", None)
    if not isinstance(return_code, int) and error is not None:
        match = re.search(r"code\s+(-?\d+)", str(error))
        if match:
            return_code = int(match.group(1))
    if isinstance(return_code, int):
        return 128 + signal.SIGKILL if return_code == -signal.SIGKILL else return_code
    return None


def _merge_database_ownership(current: Any, recorded: Mapping[str, Any] | None) -> Any:
    """Keep live bootstrap ownership immutable; ledger data is never authoritative."""
    del recorded
    return current


def _incomplete_ledger_paths(artifact_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(
        artifact_dir.glob("evaluator-run-*.ledger.json"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    ):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            paths.append(path)
            continue
        if not isinstance(state, Mapping) or not bool(state.get("terminal")):
            paths.append(path)
    return paths


def _assert_database_identity_matches_manifest(
    database_bootstrap: Any,
    manifest: Any,
    *,
    admin_url: str,
) -> None:
    recorded = getattr(manifest, "database_bootstrap", None)
    if not isinstance(recorded, Mapping):
        return
    application_record = recorded.get("application")
    checkpoint_record = recorded.get("checkpoint")
    if not isinstance(application_record, Mapping) or not isinstance(checkpoint_record, Mapping):
        raise LedgerRejected("matching evaluator ledger database ownership is unverifiable")
    current = database_bootstrap.manifest_record()
    for role in ("application", "checkpoint"):
        current_record = current.get(role)
        expected_name = str(recorded[role].get("name") or "")
        if not isinstance(current_record, Mapping) or str(current_record.get("name") or "") != expected_name:
            raise LedgerRejected("matching evaluator ledger database identity does not match requested configuration")
    recorded_host = str(recorded.get("server_host") or "").strip().lower()
    recorded_port = int(recorded.get("server_port") or 0)
    current_host = str(getattr(database_bootstrap, "server_host", "") or "").strip().lower()
    current_port = int(getattr(database_bootstrap, "server_port", 0) or 0)
    if (recorded_host or recorded_port) and (
        recorded_host != current_host or recorded_port != current_port
    ):
        raise LedgerRejected("matching evaluator ledger SQL server identity does not match requested configuration")


def _catalog_runtime_parent(catalog_dir: Path) -> Path | None:
    resolved = catalog_dir.resolve()
    if resolved.name != "catalog" or not resolved.parent.name.startswith("run."):
        return None
    return resolved.parent.parent


def _qdrant_runtime_parent(qdrant_path: Path) -> Path | None:
    resolved = qdrant_path.resolve()
    if resolved.name != "qdrant" or not resolved.parent.name.startswith("run."):
        return None
    return resolved.parent.parent


def _retarget_sql_url(
    raw_url: str,
    *,
    host: str,
    port: int,
    database: str | None = None,
) -> str:
    try:
        parsed = make_url(raw_url)
        if str(parsed.host or "").lower() != host.lower():
            raise LedgerRejected("resumed evaluator SQL host does not match the recorded owner")
        return parsed.set(
            host=host,
            port=port,
            database=database if database is not None else parsed.database,
        ).render_as_string(hide_password=False)
    except LedgerRejected:
        raise
    except Exception as exc:
        raise LedgerRejected("resumed evaluator SQL identity is invalid") from exc


def _rebind_resumed_resources(args: argparse.Namespace, manifest: Any) -> None:
    """Use only the exact resource identities recorded by a matching ledger."""
    if manifest is None:
        raise LedgerRejected("matching evaluator ledger has no cleanup manifest")
    catalog_roots = {
        Path(case.catalog_path).resolve().parent
        for case in manifest.cases.values()
        if case.catalog_path is not None
    }
    if len(catalog_roots) > 1:
        raise LedgerRejected("matching evaluator ledger has multiple catalog roots")
    if catalog_roots:
        prior_catalog_dir = next(iter(catalog_roots))
        current_runtime = _catalog_runtime_parent(Path(args.catalog_dir).resolve())
        prior_runtime = _catalog_runtime_parent(prior_catalog_dir)
        if prior_runtime is None or current_runtime is None or prior_runtime != current_runtime:
            raise LedgerRejected("matching evaluator ledger catalog is outside the evaluator runtime")
        args.catalog_dir = prior_catalog_dir
    if manifest.qdrant_path is not None:
        prior_qdrant_path = Path(manifest.qdrant_path).resolve()
        prior_runtime = _qdrant_runtime_parent(prior_qdrant_path)
        current_runtime = (
            _qdrant_runtime_parent(Path(args.qdrant_path).resolve())
            if args.qdrant_path is not None
            else None
        )
        if prior_runtime is None or (current_runtime is not None and prior_runtime != current_runtime):
            raise LedgerRejected("matching evaluator ledger Qdrant path is outside the evaluator runtime")
        args.qdrant_path = prior_qdrant_path
        args.qdrant_url = ""
    record = manifest.database_bootstrap
    if not isinstance(record, Mapping):
        return
    host = str(record.get("server_host") or "").strip()
    try:
        port = int(record.get("server_port") or 0)
    except (TypeError, ValueError) as exc:
        raise LedgerRejected("matching evaluator ledger SQL server identity is invalid") from exc
    if not host or port <= 0:
        raise LedgerRejected("matching evaluator ledger SQL server identity is invalid")
    args.evaluator_sql_admin_url = _retarget_sql_url(
        args.evaluator_sql_admin_url,
        host=host,
        port=port,
    )
    for role, argument_name in (
        ("application", "database_url"),
        ("checkpoint", "checkpoint_database_url"),
    ):
        raw = str(getattr(args, argument_name) or "").strip()
        if not raw:
            continue
        raw_record = record.get(role)
        if not isinstance(raw_record, Mapping):
            raise LedgerRejected("matching evaluator ledger SQL database manifest is incomplete")
        setattr(
            args,
            argument_name,
            _retarget_sql_url(
                raw,
                host=host,
                port=port,
                database=str(raw_record.get("name") or ""),
            ),
        )


def _load_recovery_ledger(path: Path, *, catalog_dir: Path) -> ProgressLedger:
    ledger = ProgressLedger.load_unmatched(path, catalog_dir=None)
    manifest = ledger.manifest
    if manifest is None:
        raise LedgerRejected("prior evaluator ledger has no cleanup manifest")
    catalog_roots = {
        Path(case.catalog_path).resolve().parent
        for case in manifest.cases.values()
        if case.catalog_path is not None
    }
    if len(catalog_roots) > 1:
        raise LedgerRejected("prior evaluator ledger has multiple catalog roots")
    if catalog_roots:
        prior_catalog_dir = next(iter(catalog_roots))
        current_catalog_dir = catalog_dir.resolve()
        if prior_catalog_dir != current_catalog_dir:
            if (
                _catalog_runtime_parent(prior_catalog_dir)
                is None
                or _catalog_runtime_parent(current_catalog_dir) is None
                or _catalog_runtime_parent(prior_catalog_dir)
                != _catalog_runtime_parent(current_catalog_dir)
            ):
                raise LedgerRejected("prior evaluator ledger catalog is outside evaluator runtime")
        validate_manifest_ownership(manifest, catalog_dir=prior_catalog_dir)
    return ledger
def _load_recovery_ledgers(
    paths: list[Path],
    *,
    fingerprint: str,
    expected_case_ids: tuple[str, ...],
    catalog_dir: Path,
) -> list[ProgressLedger]:
    recovered: list[ProgressLedger] = []
    for path in paths:
        ledger = _load_recovery_ledger(path, catalog_dir=catalog_dir)
        if (
            ledger.fingerprint == fingerprint
            and ledger.expected_case_ids == expected_case_ids
        ):
            raise LedgerRejected("matching evaluator ledger could not be verified")
        recovered.append(ledger)
    return recovered


def _prior_managed_local_runtime_is_absent(
    manifest: Any,
    *,
    current_runtime_dir: Path,
) -> bool:
    current_runtime = current_runtime_dir.resolve()
    if not current_runtime.is_dir() or not current_runtime.name.startswith("run."):
        return False

    prior_runtime_dirs: set[Path] = set()
    for case in manifest.cases.values():
        if case.catalog_path is None:
            continue
        catalog_dir = Path(case.catalog_path).resolve().parent
        if catalog_dir.name != "catalog" or not catalog_dir.parent.name.startswith("run."):
            return False
        prior_runtime_dirs.add(catalog_dir.parent)
    if manifest.qdrant_path is not None:
        qdrant_path = Path(manifest.qdrant_path).resolve()
        if qdrant_path.name != "qdrant" or not qdrant_path.parent.name.startswith("run."):
            return False
        prior_runtime_dirs.add(qdrant_path.parent)
    if len(prior_runtime_dirs) != 1:
        return False

    prior_runtime = next(iter(prior_runtime_dirs))
    if (
        prior_runtime == current_runtime
        or prior_runtime.parent != current_runtime.parent
        or prior_runtime.exists()
    ):
        return False

    record = manifest.database_bootstrap
    if not isinstance(record, Mapping):
        return False
    if str(record.get("run_id") or "") != str(manifest.run_id):
        return False
    if str(record.get("server_host") or "").strip().lower() not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return False
    namespace = str(record.get("namespace") or "").strip()
    for role in ("application", "checkpoint"):
        resource = record.get(role)
        if not isinstance(resource, Mapping):
            return False
        owner = str(resource.get("owner_role") or "").strip()
        expected_marker = (
            f"rehabflow-evaluator:r13:{namespace}:{manifest.run_id}:{role}:{owner}"
        )
        if (
            resource.get("created_by_run") is not True
            or resource.get("live_verified") is not True
            or not owner
            or str(resource.get("ownership_marker") or "") != expected_marker
        ):
            return False
    return True


def _mark_empty_recovery_ledger_cleaned(progress: ProgressLedger) -> None:
    progress.record_recovery_cleanup({
        "status": "passed",
        "failures": [],
        "deleted_counts": {
            "application": 0,
            "checkpoint": 0,
            "qdrant": 0,
            "catalog": 0,
        },
        "verification_counts": {
            "application": 0,
            "checkpoint": 0,
            "qdrant": 0,
            "catalog": 0,
        },
    })
    progress.mark_terminal()


def main(argv: list[str] | None = None) -> int:
    from evals.combined.cleanup import cleanup_exact_manifest, preserve_test_data
    from evals.combined.database_bootstrap import (
        DatabaseBootstrapError,
        EvaluatorDatabaseConfig,
        bootstrap_evaluator_databases,
        recover_evaluator_databases_from_manifest,
    )
    from evals.combined.fixture_store import FixtureStore
    from evals.combined.preflight import (
        PreflightConfig,
        PreflightResult,
        validate_preflight_config,
        verify_application_schema,
        verify_checkpoint_tables,
        verify_qdrant_capabilities,
        writable_probe,
    )
    from evals.combined.resource_manifest import RunManifest, privacy_hash, verify_qdrant_endpoint_binding
    from evals.combined.service import FAULT_PROFILES

    parser = argparse.ArgumentParser(
        description="Run the 44-case service-backed real-model and longitudinal v2 evaluator."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--backend-dir", type=Path, default=Path(os.getenv("REHAB_EVAL_BACKEND_DIR", "backend")))
    parser.add_argument("--python-bin", type=Path, default=Path(os.getenv("REHAB_EVAL_PYTHON", os.sys.executable)))
    parser.add_argument("--backend-host", default=os.getenv("REHAB_EVAL_BACKEND_HOST", "127.0.0.1"))
    parser.add_argument("--backend-port", type=int, default=int(os.getenv("REHAB_EVAL_BACKEND_PORT", "0")))
    parser.add_argument(
        "--startup-timeout-seconds",
        type=float,
        default=float(os.getenv("REHAB_EVAL_STARTUP_TIMEOUT_SECONDS", "90")),
        help="Maximum seconds to wait for each evaluator backend process to become ready.",
    )
    parser.add_argument("--service-environment", default=os.getenv("REHAB_EVAL_SERVICE_ENVIRONMENT", "evaluation"))
    parser.add_argument("--backend-revision", default=os.getenv("REHAB_EVAL_BACKEND_REVISION", ""))
    parser.add_argument("--application-schema-revision", default=os.getenv("REHAB_EVAL_SCHEMA_REVISION", ""))
    parser.add_argument("--workflow-version", default=os.getenv("REHAB_EVAL_WORKFLOW_VERSION", WORKFLOW_VERSION))
    parser.add_argument("--evaluator-sql-admin-url", default=os.getenv("REHAB_EVAL_SQL_ADMIN_URL", ""))
    parser.add_argument("--evaluator-sql-namespace", default=os.getenv("REHAB_EVAL_SQL_NAMESPACE", ""))
    parser.add_argument("--evaluator-application-database-name", default=os.getenv("REHAB_EVAL_APPLICATION_DATABASE_NAME", ""))
    parser.add_argument("--evaluator-checkpoint-database-name", default=os.getenv("REHAB_EVAL_CHECKPOINT_DATABASE_NAME", ""))
    parser.add_argument("--evaluator-sql-allowlist", default=os.getenv("REHAB_EVAL_SQL_SERVER_ALLOWLIST", ""))
    parser.add_argument("--evaluator-sql-owner-role", default=os.getenv("REHAB_EVAL_SQL_OWNER_ROLE", ""))
    parser.add_argument("--database-url", default=os.getenv("REHAB_EVAL_DATABASE_URL", ""))
    parser.add_argument("--checkpoint-database-url", default=os.getenv("REHAB_EVAL_CHECKPOINT_DATABASE_URL", ""))
    parser.add_argument("--qdrant-url", default=os.getenv("REHAB_EVAL_QDRANT_URL", ""))
    parser.add_argument("--qdrant-path", type=Path, default=Path(os.getenv("REHAB_EVAL_QDRANT_PATH")) if os.getenv("REHAB_EVAL_QDRANT_PATH") else None)
    parser.add_argument("--qdrant-owner-identity", default=os.getenv("REHAB_EVAL_QDRANT_OWNER_IDENTITY", ""))
    parser.add_argument("--qdrant-allowlist", default=os.getenv("REHAB_EVAL_QDRANT_ALLOWLIST", ""))
    parser.add_argument("--qdrant-api-key", default=os.getenv("REHAB_EVAL_QDRANT_API_KEY", ""))
    parser.add_argument("--catalog-dir", type=Path, default=Path(os.getenv("REHAB_EVAL_CATALOG_DIR", "")))
    parser.add_argument("--embedding-provider", default=os.getenv("REHAB_EVAL_EMBEDDING_PROVIDER", os.getenv("REHAB_EMBEDDING_PROVIDER", "hash")))
    parser.add_argument("--backend-generation-provider", default=os.getenv("REHAB_EVAL_BACKEND_GENERATION_PROVIDER", "openai"))
    parser.add_argument("--backend-generation-key", default=os.getenv("OPENAI_API_KEY", ""))
    parser.add_argument("--baseline", type=Path, help="Accepted for wrapper compatibility; ignored by combined-v2.")
    parser.add_argument("--generation-model", default=os.getenv("REAL_MODEL_GENERATION_MODEL", "gpt-5.4-mini"))
    parser.add_argument(
        "--judge-thinking-level",
        default=os.getenv("REAL_MODEL_JUDGE_THINKING_LEVEL", "high"),
        help="OpenAI judge reasoning effort, for example minimal, low, medium, high, or none.",
    )
    parser.add_argument("--judge-model", default=os.getenv("REAL_MODEL_JUDGE_MODEL", "gpt-5.6-luna"))
    parser.add_argument("--include-conversation", action="store_true", default=True)
    parser.add_argument("--no-conversation", action="store_false", dest="include_conversation")
    parser.add_argument(
        "--max-parallel-cases",
        type=int,
        default=int(os.getenv("REHAB_EVAL_MAX_PARALLEL_CASES", "2")),
        help="Maximum number of evaluator cases with live backend processes at once.",
    )
    parser.add_argument(
        "--max-parallel-judge-calls",
        type=int,
        default=int(os.getenv("REHAB_EVAL_MAX_PARALLEL_JUDGE_CALLS", "1")),
        help="Maximum concurrent judge postprocess calls; keep at 1 for provider frequency limits.",
    )
    args = parser.parse_args(argv)
    args.backend_revision = args.backend_revision.strip() or backend_source_revision(args.backend_dir)
    args.workflow_version = args.workflow_version.strip() or WORKFLOW_VERSION
    if not args.application_schema_revision.strip():
        try:
            args.application_schema_revision = _derive_application_schema_revision(args.backend_dir)
        except Exception as exc:
            parser.error(f"could not derive latest Alembic revision: {type(exc).__name__}")
    if args.max_parallel_cases < 1:
        parser.error("--max-parallel-cases must be a positive integer")
    if args.max_parallel_judge_calls < 1:
        parser.error("--max-parallel-judge-calls must be a positive integer")
    if args.startup_timeout_seconds <= 0:
        parser.error("--startup-timeout-seconds must be positive")
    keep_completed_data = os.getenv("REHAB_EVAL_KEEP_TEST_DATA", "1").strip().lower() not in {
        "0", "false", "no",
    }

    _ = args.baseline
    _ = args.generation_model
    if not _enabled():
        parser.error("set REHAB_REAL_MODEL_ENABLED=1 explicitly to run service-backed evaluation")
    judge_key = (os.getenv("OPENAI_JUDGE_API_KEY") or os.getenv("OPENAI_API_KEY", "")).strip()
    if not judge_key:
        parser.error("OPENAI_JUDGE_API_KEY or OPENAI_API_KEY is required for judging")
    if not args.evaluator_sql_admin_url.strip():
        parser.error("separate evaluator SQL administrator connection is required")
    if not args.evaluator_sql_namespace.strip():
        parser.error("exact evaluator SQL namespace is required")
    if not args.evaluator_application_database_name.strip() or not args.evaluator_checkpoint_database_name.strip():
        parser.error("exact evaluator application and checkpoint database names are required")
    if not args.qdrant_url.strip() and args.qdrant_path is None:
        parser.error("REHAB_EVAL_QDRANT_URL, REHAB_EVAL_QDRANT_PATH, or an evaluator local resource is required")
    if args.qdrant_path and not args.qdrant_path.is_absolute():
        parser.error("REHAB_EVAL_QDRANT_PATH or --qdrant-path must be absolute")
    if not args.catalog_dir.is_absolute():
        parser.error("REHAB_EVAL_CATALOG_DIR or --catalog-dir must be absolute")

    real_model_cases = _load_real_model_cases(args.cases)
    longitudinal_scenarios = _load_longitudinal_scenarios()
    expected_case_ids = tuple(
        [
            *(_case_key("real_model", str(case["case_id"])) for case in real_model_cases),
            *(_case_key("longitudinal", scenario.scenario_id) for scenario in longitudinal_scenarios),
        ]
    )
    run_fingerprint = _run_configuration_fingerprint(
        args,
        judge_key=judge_key,
        real_model_cases=real_model_cases,
        longitudinal_scenarios=longitudinal_scenarios,
    )
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    incomplete_ledgers = _incomplete_ledger_paths(args.artifact_dir)
    progress: ProgressLedger | None = None
    prior_progresses: list[ProgressLedger] = []
    resume_existing = False
    if incomplete_ledgers:
        try:
            progress = ProgressLedger.discover(
                args.artifact_dir,
                fingerprint=run_fingerprint,
                expected_case_ids=expected_case_ids,
                catalog_dir=args.catalog_dir,
            )
        except LedgerRejected as exc:
            if not str(exc).startswith("no matching incomplete evaluator ledger found"):
                print(json.dumps({
                    "status": "ledger_rejected",
                    "reason": type(exc).__name__,
                }, sort_keys=True))
                return 2
            try:
                prior_progresses = _load_recovery_ledgers(
                    incomplete_ledgers,
                    fingerprint=run_fingerprint,
                    expected_case_ids=expected_case_ids,
                    catalog_dir=args.catalog_dir,
                )
            except LedgerRejected as recovery_exc:
                print(json.dumps({
                    "status": "ledger_rejected",
                    "reason": type(recovery_exc).__name__,
                    "detail": str(recovery_exc),
                }, sort_keys=True))
                return 2
            if not prior_progresses:
                print(json.dumps({
                    "status": "ledger_rejected",
                    "reason": "LedgerRejected",
                }, sort_keys=True))
                return 2
    if progress is None:
        # A replacement run must have a durable manifest before prior recovery
        # or any new evaluator database/resource creation begins.
        progress = ProgressLedger.create(
            args.artifact_dir,
            fingerprint=run_fingerprint,
            expected_case_ids=expected_case_ids,
        )
        run_manifest = RunManifest(run_id=progress.run_id)
        progress.attach_manifest(run_manifest)
    else:
        run_manifest = progress.manifest
        resume_existing = True
        if run_manifest is None:
            print(json.dumps({
                "status": "ledger_rejected",
                "reason": "LedgerRejected",
            }, sort_keys=True))
            return 2
    database_bootstrap = None
    application_database_url = ""
    checkpoint_database_url = ""
    managed_runtime_value = os.getenv("REHAB_EVAL_MANAGED_LOCAL_RUNTIME_DIR", "").strip()
    managed_runtime_dir = (
        Path(managed_runtime_value).resolve()
        if os.getenv("REHAB_EVAL_LOCAL_POSTGRES_MANAGED", "").strip() == "1"
        and managed_runtime_value
        else None
    )
    try:
        if resume_existing:
            _rebind_resumed_resources(args, run_manifest)
        if prior_progresses:
            for prior_progress in prior_progresses:
                prior_manifest = prior_progress.manifest
                if prior_manifest is None:
                    raise LedgerRejected("prior evaluator ledger has no cleanup manifest")
                if prior_manifest.database_bootstrap is None and not prior_manifest.cases:
                    _mark_empty_recovery_ledger_cleaned(prior_progress)
                    continue
                if (
                    managed_runtime_dir is not None
                    and _prior_managed_local_runtime_is_absent(
                        prior_manifest,
                        current_runtime_dir=managed_runtime_dir,
                    )
                ):
                    _mark_empty_recovery_ledger_cleaned(prior_progress)
                    continue
                try:
                    recovery_bootstrap = recover_evaluator_databases_from_manifest(
                        prior_manifest.database_bootstrap,
                        admin_url=args.evaluator_sql_admin_url,
                        run_id=prior_progress.run_id,
                        allowlisted_hosts=tuple(
                            host.strip() for host in args.evaluator_sql_allowlist.split(",") if host.strip()
                        ),
                    )
                except DatabaseBootstrapError as exc:
                    raise LedgerRejected("prior evaluator database ownership could not be proven") from exc
                recovery_qdrant_path = (
                    str(prior_manifest.qdrant_path)
                    if prior_manifest.qdrant_path is not None
                    else str(args.qdrant_path) if args.qdrant_path else None
                )
                recovery_cleanup = cleanup_exact_manifest(
                    prior_manifest,
                    application_database_url=(
                        recovery_bootstrap.application.url
                        if recovery_bootstrap.application.live_verified
                        else ""
                    ),
                    checkpoint_database_url=(
                        recovery_bootstrap.checkpoint.url
                        if recovery_bootstrap.checkpoint.live_verified
                        else ""
                    ),
                    qdrant_url=args.qdrant_url,
                    qdrant_api_key=args.qdrant_api_key or None,
                    qdrant_path=recovery_qdrant_path,
                    qdrant_owner_identity=args.qdrant_owner_identity,
                    database_bootstrap=recovery_bootstrap,
                    application_cleanup=(
                        None
                        if recovery_bootstrap.application.live_verified
                        else lambda _manifest: (0, 0)
                    ),
                    checkpoint_cleanup=(
                        None
                        if recovery_bootstrap.checkpoint.live_verified
                        else lambda _manifest: (0, 0)
                    ),
                )
                prior_progress.record_recovery_cleanup(recovery_cleanup.to_dict())
                if not recovery_cleanup.ok:
                    raise RuntimeError("exact recovery cleanup failed")
                prior_progress.mark_terminal()

        recorded_database_bootstrap = run_manifest.database_bootstrap
        if isinstance(recorded_database_bootstrap, Mapping):
            for role in ("application", "checkpoint"):
                record = recorded_database_bootstrap.get(role)
                if (
                    isinstance(record, Mapping)
                    and record.get("created_by_run") is True
                    and record.get("live_verified") is not True
                ):
                    raise LedgerRejected(
                        f"recorded {role} database ownership proof is incomplete"
                    )
        database_bootstrap = bootstrap_evaluator_databases(
            EvaluatorDatabaseConfig(
                admin_url=args.evaluator_sql_admin_url,
                namespace=args.evaluator_sql_namespace,
                application_database_name=args.evaluator_application_database_name,
                checkpoint_database_name=args.evaluator_checkpoint_database_name,
                expected_application_schema_revision=args.application_schema_revision,
                repo_root=Path(args.backend_dir).resolve().parent,
                run_id=run_manifest.run_id,
                application_url=args.database_url.strip() or None,
                checkpoint_url=args.checkpoint_database_url.strip() or None,
                owner_role=args.evaluator_sql_owner_role.strip() or None,
                allowlisted_hosts=tuple(
                    host.strip() for host in args.evaluator_sql_allowlist.split(",") if host.strip()
                ),
            ),
            resource_hook=run_manifest.record_database_bootstrap,
        )
        if recorded_database_bootstrap is not None:
            _assert_database_identity_matches_manifest(
                database_bootstrap,
                RunManifest(
                    run_id=run_manifest.run_id,
                    database_bootstrap=recorded_database_bootstrap,
                ),
                admin_url=args.evaluator_sql_admin_url,
            )
        run_manifest.record_database_bootstrap(database_bootstrap.manifest_record())
        progress.persist_manifest()
        application_database_url = database_bootstrap.application.url
        checkpoint_database_url = database_bootstrap.checkpoint.url
        application_schema_revision = (
            database_bootstrap.application.schema_revision or args.application_schema_revision
        )
        preflight = validate_preflight_config(PreflightConfig(
            application_database_url=application_database_url,
            checkpoint_database_url=checkpoint_database_url,
            qdrant_url=args.qdrant_url,
            qdrant_path=args.qdrant_path,
            catalog_dir=args.catalog_dir,
            backend_revision=args.backend_revision,
            application_schema_revision=application_schema_revision,
            workflow_version=args.workflow_version,
            backend_generation_provider=args.backend_generation_provider,
            backend_generation_key=args.backend_generation_key,
            judge_key=judge_key,
            environment=args.service_environment,
            evaluator_mode=True,
            embedding_provider=args.embedding_provider,
            qdrant_owner_identity=args.qdrant_owner_identity,
            qdrant_allowlisted_hosts=tuple(
                host.strip() for host in args.qdrant_allowlist.split(",") if host.strip()
            ),
        ))
        writable_probe(args.catalog_dir)
        verify_application_schema(
            application_database_url,
            expected_revision=application_schema_revision,
        )
        if run_manifest.qdrant_endpoint_binding is None:
            if run_manifest.exact_qdrant_collection_names():
                verify_qdrant_endpoint_binding(
                    run_manifest,
                    qdrant_url=args.qdrant_url,
                    qdrant_path=args.qdrant_path,
                    owner_identity=args.qdrant_owner_identity,
                )
            run_manifest.record_qdrant_endpoint_binding(
                qdrant_url=args.qdrant_url,
                qdrant_path=args.qdrant_path,
                owner_identity=args.qdrant_owner_identity,
            )
        verify_qdrant_endpoint_binding(
            run_manifest,
            qdrant_url=args.qdrant_url,
            qdrant_path=args.qdrant_path,
            owner_identity=args.qdrant_owner_identity,
        )
        from qdrant_client import QdrantClient
        qdrant_client = (
            QdrantClient(path=str(args.qdrant_path), force_disable_check_same_thread=True)
            if args.qdrant_path
            else QdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key or None)
        )
        try:
            verify_qdrant_capabilities(
                qdrant_client,
                probe_collection=f"rehab_eval_preflight_{privacy_hash(run_manifest.run_id)}",
                ownership_verified=True,
            )
        finally:
            qdrant_client.close()
        progress.persist_manifest()
        from langgraph.checkpoint.postgres import PostgresSaver
        with PostgresSaver.from_conn_string(checkpoint_database_url) as saver:
            verify_checkpoint_tables(saver)
        preflight = PreflightResult(
            status=preflight.status,
            checks={
                **dict(preflight.checks),
                "database_bootstrap": "passed",
                "application_schema": "passed",
                "checkpoint_tables": "passed",
            },
            safe_identity={
                **dict(preflight.safe_identity),
                "evaluator_sql_server": privacy_hash(
                    f"{database_bootstrap.server_host}:{database_bootstrap.server_port}"
                ),
                "application_schema_revision": application_schema_revision,
            },
        )
        from evals.combined.progress import validate_manifest_ownership
        validate_manifest_ownership(run_manifest, catalog_dir=args.catalog_dir)
        for case_key in progress.missing_case_ids:
            _, raw_case_id = case_key.split(":", 1)
            if raw_case_id not in run_manifest.cases:
                continue
            stale_case = RunManifest(
                run_id=run_manifest.run_id,
                qdrant_endpoint_binding=run_manifest.qdrant_endpoint_binding,
                cases={raw_case_id: run_manifest.cases[raw_case_id]},
            )
            stale_cleanup = cleanup_exact_manifest(
                stale_case,
                application_database_url=application_database_url,
                checkpoint_database_url=checkpoint_database_url,
                qdrant_url=args.qdrant_url,
                qdrant_api_key=args.qdrant_api_key or None,
                qdrant_path=str(args.qdrant_path) if args.qdrant_path else None,
                qdrant_owner_identity=args.qdrant_owner_identity,
            )
            if not stale_cleanup.ok:
                raise RuntimeError("incomplete case recovery cleanup failed")
            del run_manifest.cases[raw_case_id]
            progress.persist_manifest()
        progress.mark_phase("case_setup")
    except LedgerRejected as exc:
        print(json.dumps({
            "status": "ledger_rejected",
            "reason": type(exc).__name__,
            "detail": str(exc),
        }, sort_keys=True))
        return 2
    except Exception as exc:
        bootstrap_failure = getattr(exc, "partial_result", None)
        bootstrap_cleanup = getattr(exc, "cleanup_result", None)
        if bootstrap_failure is not None:
            database_bootstrap = bootstrap_failure
            run_manifest.record_database_bootstrap(
                bootstrap_failure.manifest_record()
            )
            # The bootstrap may have created only one database or may not have
            # applied its schema. Retry exact database cleanup without opening
            # incomplete application/checkpoint phases.
            application_database_url = ""
            checkpoint_database_url = ""
        json_path, markdown_path, cleanup = _finalize_failed_run(
            artifact_dir=args.artifact_dir,
            run_manifest=run_manifest,
            backend_revision=args.backend_revision,
            application_database_url=application_database_url,
            checkpoint_database_url=checkpoint_database_url,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key or None,
            qdrant_path=str(args.qdrant_path) if args.qdrant_path else None,
            qdrant_owner_identity=args.qdrant_owner_identity,
            database_bootstrap=database_bootstrap,
            phase="preflight",
            error=exc,
            cleanup_fn=cleanup_exact_manifest,
            bootstrap_cleanup=bootstrap_cleanup,
            redaction_secrets=(judge_key, args.backend_generation_key),
        )
        if progress is None:
            print(json.dumps({
                "status": "blocked" if cleanup.ok else "failed_cleanup",
                "json_artifact": str(json_path),
                "markdown_artifact": str(markdown_path),
                "cleanup": cleanup.to_dict(),
            }, sort_keys=True))
            return 2 if cleanup.ok else 1
        progress.record_cleanup(cleanup.to_dict())
        progress.mark_terminal()
        print(json.dumps({
            "status": "blocked" if cleanup.ok else "failed_cleanup",
            "blocker": type(exc).__name__,
            "json_artifact": str(json_path),
            "markdown_artifact": str(markdown_path),
            "cleanup": cleanup.to_dict(),
        }, sort_keys=True))
        return 2 if cleanup.ok else 1

    judge = _OpenAICompatibleProvider(
        reasoning_effort=args.judge_thinking_level,
        api_key=judge_key,
        base_url=os.getenv(
            "OPENAI_JUDGE_API_BASE",
            os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"),
        ),
        model_name=args.judge_model,
    )
    config = ServiceHarnessConfig(
        backend_dir=args.backend_dir,
        python_bin=args.python_bin,
        host=args.backend_host,
        port=args.backend_port,
        environment=args.service_environment,
        backend_launcher=args.backend_dir.resolve().parent / "scripts" / "dev-backend.mjs",
        expected_revision=args.backend_revision,
        database_url=application_database_url,
        checkpoint_database_url=checkpoint_database_url,
        qdrant_url=args.qdrant_url or None,
        qdrant_api_key=args.qdrant_api_key or None,
        qdrant_path=args.qdrant_path,
        catalog_dir=args.catalog_dir,
        embedding_provider=args.embedding_provider,
        backend_generation_provider=args.backend_generation_provider,
        backend_generation_key=args.backend_generation_key or None,
        startup_timeout_seconds=args.startup_timeout_seconds,
    )
    if args.qdrant_path:
        os.environ["QDRANT_PATH"] = str(args.qdrant_path)
        os.environ.pop("QDRANT_URL", None)
    else:
        os.environ["QDRANT_URL"] = args.qdrant_url
        os.environ.pop("QDRANT_PATH", None)
    if args.qdrant_api_key:
        os.environ["QDRANT_API_KEY"] = args.qdrant_api_key
    else:
        os.environ.pop("QDRANT_API_KEY", None)
    os.environ["REHAB_EMBEDDING_PROVIDER"] = args.embedding_provider
    harness = ServiceHarness(config)
    fixture_store = FixtureStore(
        harness=harness,
        manifest=run_manifest,
        database_url=application_database_url,
        catalog_dir=args.catalog_dir,
        qdrant_path=args.qdrant_path,
        progress_hook=progress.persist_manifest,
        activate_case_resources=False,
    )
    progress.mark_phase("service_execution")
    health: BackendHealth | None = None
    all_reports: list[dict[str, Any]] = []
    provisioning_lock = RLock()

    def declared_fault(source: Mapping[str, Any]) -> str | None:
        explicit = str(source.get("fault_profile") or "").strip().lower()
        if explicit:
            return explicit
        for profile in FAULT_PROFILES:
            if source.get(profile) is True:
                return profile
        return None

    def run_case(
        execute: Callable[[ServiceCaseRunner, Any], Mapping[str, Any]],
        *,
        scenario: ScenarioV2 | None,
        suite: str,
        id_field: str,
        identifier: str,
        fault_profile: str | None,
    ) -> dict[str, Any]:
        profile = fault_profile or "none"
        result: dict[str, Any] | None = None
        case_harness: ServiceHarness | None = None
        try:
            with provisioning_lock:
                identity = provision_service_identity(
                    harness,
                    identifier,
                    scenario,
                    fixture_store=fixture_store,
                )
            case_harness = ServiceHarness(replace(config, port=0))
            if identity.case_resources is not None:
                case_harness.activate_case_resources(identity.case_resources)
            if profile != "none":
                case_harness.set_fault_profile(profile)
            case_health = case_harness.start()
            validate_backend_health(
                case_health,
                expected_revision=args.backend_revision,
                expected_workflow_version=args.workflow_version,
                require_restart=True,
            )
            case_runner = ServiceCaseRunner(
                case_harness,
                lambda _active_harness, _case_id, _scenario: identity,
            )
            result = dict(execute(case_runner, identity))
            if _child_exit_code(case_harness) == 137:
                raise _EvaluatorChildExit(137)
            result["fault_profile"] = profile
        except _EvaluatorInterrupted:
            raise
        except Exception as exc:
            _raise_on_child_exit_137(case_harness or harness, exc)
            result = {
                "suite": suite,
                id_field: identifier,
                "execution_mode": "real-backend-service",
                "provider_free": False,
                "status": "failed",
                "judge_status": "not_attempted",
                "fault_profile": profile,
                "failures": [type(exc).__name__],
            }
        finally:
            if case_harness is not None:
                try:
                    case_harness.stop()
                except Exception as exc:
                    result = result or {
                        "suite": suite,
                        id_field: identifier,
                        "execution_mode": "real-backend-service",
                        "provider_free": False,
                        "status": "failed",
                        "judge_status": "not_attempted",
                        "fault_profile": profile,
                        "failures": [],
                    }
                    result.setdefault("failures", []).append(
                        f"case_service_stop_{type(exc).__name__}"
                    )
                    result["status"] = "failed"
        return result or {
            "suite": suite,
            id_field: identifier,
            "execution_mode": "real-backend-service",
            "provider_free": False,
            "status": "failed",
            "judge_status": "not_attempted",
            "fault_profile": profile,
            "failures": ["case_result_missing"],
        }

    service_failure: Exception | None = None
    failure_phase = "backend_startup"
    interruption: _EvaluatorInterrupted | None = None
    child_interruption: _EvaluatorChildExit | None = None
    previous_signal_handlers = {
        signal_number: signal.getsignal(signal_number)
        for signal_number in (signal.SIGINT, signal.SIGTERM)
    }

    def handle_signal(signal_number: int, _frame: Any) -> None:
        raise _EvaluatorInterrupted(signal_number)

    for signal_number in previous_signal_handlers:
        signal.signal(signal_number, handle_signal)
    try:
        health = harness.start()
        validate_backend_health(
            health,
            expected_revision=args.backend_revision,
            expected_workflow_version=args.workflow_version,
            require_restart=True,
        )
        failure_phase = "service_execution"
        case_calls: list[tuple[str, Callable[[], Mapping[str, Any]]]] = []
        for case in real_model_cases:
            identifier = str(case["case_id"])
            case_calls.append((
                _case_key("real_model", identifier),
                lambda case=case, identifier=identifier: run_case(
                    lambda runner, identity: runner.run_real_model_case(
                        case,
                        judge=None,
                        judge_model=args.judge_model,
                        workflow_version=health.workflow_version,
                        identity=identity,
                    ),
                    scenario=_real_model_fixture_scenario(case),
                    suite="real_model",
                    id_field="case_id",
                    identifier=identifier,
                    fault_profile=declared_fault(case),
                ),
            ))
        for scenario in longitudinal_scenarios:
            identifier = scenario.scenario_id
            case_calls.append((
                _case_key("longitudinal", identifier),
                lambda scenario=scenario, identifier=identifier: run_case(
                    lambda runner, identity: runner.run_longitudinal_case(
                        scenario,
                        judge=None,
                        judge_model=args.judge_model,
                        workflow_version=health.workflow_version,
                        identity=identity,
                    ),
                    scenario=scenario,
                    suite="longitudinal",
                    id_field="scenario_id",
                    identifier=identifier,
                    fault_profile=declared_fault(scenario.oracles),
                ),
            ))
        real_cases_by_id = {
            str(case["case_id"]): case
            for case in real_model_cases
        }
        scenarios_by_id = {
            scenario.scenario_id: scenario
            for scenario in longitudinal_scenarios
        }

        def postprocess_case(
            case_key: str,
            service_result: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            suite, _, identifier = case_key.partition(":")
            if suite == "real_model":
                return judge_service_result(
                    service_result,
                    case=real_cases_by_id[identifier],
                    judge=judge,
                    judge_model=args.judge_model,
                )
            if suite == "longitudinal":
                return judge_service_result(
                    service_result,
                    scenario=scenarios_by_id[identifier],
                    judge=judge,
                    judge_model=args.judge_model,
                )
            return dict(service_result)

        run_checkpointed_staged_cases(
            progress,
            case_calls,
            postprocess=postprocess_case,
            max_parallel_cases=args.max_parallel_cases,
            max_parallel_postprocess=args.max_parallel_judge_calls,
            execution_gate=Semaphore(args.max_parallel_cases),
        )
        all_reports = progress.load_case_results()
        for report in all_reports:
            identifier = str(report.get("case_id") or report.get("scenario_id") or "")
            if "ai_messages" in report and "ai_internal_messages" in report:
                report.setdefault("message_capture_status", "captured")
                continue
            case_manifest = run_manifest.cases.get(identifier)
            if case_manifest is None:
                report["message_capture_status"] = "missing"
                report["report_failures"] = [
                    "case_manifest_missing_for_message_hydration"
                ]
                continue
            try:
                persisted_messages = fixture_store.load_persisted_messages(case_manifest)
            except Exception as exc:
                report["message_capture_status"] = "missing"
                report["report_failures"] = [
                    f"persisted_messages_{type(exc).__name__}"
                ]
                continue
            report["ai_messages"] = list(
                persisted_messages.get("ai_messages", []) or []
            )
            report["ai_internal_messages"] = list(
                persisted_messages.get("ai_internal_messages", []) or []
            )
            if report["ai_messages"] or report["ai_internal_messages"]:
                report["message_capture_status"] = "legacy_database"
            elif str(report.get("response") or "").strip():
                report["message_capture_status"] = "missing"
                report.setdefault("report_failures", []).append(
                    "message_capture_missing"
                )
            else:
                report["message_capture_status"] = "unavailable"
    except _EvaluatorInterrupted as exc:
        interruption = exc
    except _EvaluatorChildExit as exc:
        child_interruption = exc
    except Exception as exc:
        service_failure = exc
    finally:
        try:
            harness.stop()
        except Exception as exc:
            service_failure = service_failure or exc
            failure_phase = "backend_stop"

    if interruption is not None:
        try:
            fixture_store.close()
        finally:
            progress.write_interrupted_diagnostic(
                reason=f"signal_{interruption.signal_number}",
                exit_code=128 + interruption.signal_number,
            )
        print(json.dumps({
            "status": "interrupted",
            "signal": interruption.signal_number,
            "ledger": str(progress.ledger_path),
            "completed_case_count": len(progress.completed_case_ids),
        }, sort_keys=True))
        return 128 + interruption.signal_number

    if child_interruption is not None:
        try:
            fixture_store.close()
        finally:
            progress.write_interrupted_diagnostic(
                reason="child_exit_137",
                exit_code=child_interruption.exit_code,
            )
        print(json.dumps({
            "status": "interrupted",
            "reason": "child_exit_137",
            "ledger": str(progress.ledger_path),
            "completed_case_count": len(progress.completed_case_ids),
        }, sort_keys=True))
        return child_interruption.exit_code

    child_exit_code = _child_exit_code(harness, service_failure)
    if child_exit_code == 137:
        try:
            fixture_store.close()
        finally:
            progress.write_interrupted_diagnostic(reason="child_exit_137", exit_code=137)
        print(json.dumps({
            "status": "interrupted",
            "reason": "child_exit_137",
            "ledger": str(progress.ledger_path),
            "completed_case_count": len(progress.completed_case_ids),
        }, sort_keys=True))
        return 137

    if service_failure is not None:
        try:
            fixture_store.close()
        except Exception as exc:
            service_failure = exc
            failure_phase = "fixture_store_close"
        json_path, markdown_path, cleanup = _finalize_failed_run(
            artifact_dir=args.artifact_dir,
            run_manifest=run_manifest,
            backend_revision=args.backend_revision,
            application_database_url=application_database_url,
            checkpoint_database_url=checkpoint_database_url,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key or None,
            qdrant_path=str(args.qdrant_path) if args.qdrant_path else None,
            qdrant_owner_identity=args.qdrant_owner_identity,
            database_bootstrap=database_bootstrap,
            phase=failure_phase,
            error=service_failure,
            cleanup_fn=cleanup_exact_manifest,
            redaction_secrets=(judge_key, args.backend_generation_key),
        )
        progress.record_cleanup(cleanup.to_dict())
        progress.mark_terminal()
        print(json.dumps({
            "status": "failed_cleanup" if not cleanup.ok else "failed",
            "json_artifact": str(json_path),
            "markdown_artifact": str(markdown_path),
            "cleanup": cleanup.to_dict(),
        }, sort_keys=True))
        return 1

    if not args.include_conversation:
        for report in all_reports:
            report["conversation"] = None
            report["transcript"] = None
    judge_status_counts: dict[str, int] = {}
    for report in all_reports:
        status = str(report.get("judge_status") or "unknown")
        judge_status_counts[status] = judge_status_counts.get(status, 0) + 1
    required_judge_failures = sum(
        1
        for report in all_reports
        if report.get("judge_status") not in {"passed", "not_applicable"}
    )
    normalized_records: list[ToolCallRecord] = []
    records_by_suite: dict[str, list[ToolCallRecord]] = {}
    for entry in all_reports:
        suite_records: list[ToolCallRecord] = []
        for raw_record in entry.get("tool_calls", []) or []:
            if not isinstance(raw_record, Mapping):
                continue
            try:
                record = ToolCallRecord(**dict(raw_record))
            except (TypeError, ValueError):
                continue
            normalized_records.append(record)
            suite_records.append(record)
        records_by_suite.setdefault(str(entry.get("suite") or "unknown"), []).extend(suite_records)
    tool_metrics = aggregate_tool_metrics(normalized_records)
    tool_metrics_by_tool = group_tool_metrics(normalized_records, key="tool_name")
    tool_metrics_by_suite = {
        suite: aggregate_tool_metrics(records)
        for suite, records in sorted(records_by_suite.items())
    }
    budget_summary = budget_status_summary(all_reports)
    fault_profiles: dict[str, int] = {}
    for entry in all_reports:
        profile = str(entry.get("fault_profile") or "none")
        fault_profiles[profile] = fault_profiles.get(profile, 0) + 1
    run_metadata = {
        "execution_mode": "real-backend-service",
        "case_concurrency": {
            "max_parallel_cases": args.max_parallel_cases,
            "service_process_per_case": True,
        },
        "evaluator_revision": "unversioned",
        "evaluator_dirty_state": "not-applicable",
        "backend_revision": args.backend_revision,
        "backend": health.sanitized() if health is not None else {},
        "preflight": preflight.to_dict(),
        "manifest": run_manifest.privacy_safe(),
        "database_bootstrap": run_manifest.privacy_safe().get("database_bootstrap"),
        "judge": {
            "status_counts": judge_status_counts,
            "required_failures": required_judge_failures,
        },
        "tool_metrics": tool_metrics,
        "tool_metrics_by_tool": tool_metrics_by_tool,
        "tool_metrics_by_suite": tool_metrics_by_suite,
        "budgets": budget_summary,
        "fault_profiles": fault_profiles,
        "cleanup": {"status": "pending"},
    }
    report = build_combined_report(
        real_model_cases=[item for item in all_reports if item.get("suite") == "real_model"],
        longitudinal_cases=[item for item in all_reports if item.get("suite") == "longitudinal"],
        redaction_secrets=(judge_key, args.backend_generation_key),
        run_metadata=run_metadata,
    )
    json_path, markdown_path = write_artifacts(report, args.artifact_dir)
    evaluation_finished = evaluation_completed(all_reports)
    evaluation_quality_ok = evaluation_quality_passed(all_reports)
    verification = evaluation_verification_diagnostics(all_reports)
    evaluation_verification_ok = bool(verification["passed"])
    run_metadata["quality"] = {
        "passed": evaluation_quality_ok,
        "substantive_rubric_failures": sum(
            1
            for entry in all_reports
            if entry.get("status") == "failed" and not entry.get("judge_error")
        ),
    }
    run_metadata["verification"] = {
        **verification,
        "failure_policy": "provider_judge_resource_or_service_failure_is_not_a_completion_fallback",
    }
    if not (keep_completed_data and evaluation_finished and evaluation_verification_ok):
        progress.mark_cleanup_pending()
    harness.stop()
    fixture_store.close()
    if keep_completed_data and evaluation_finished and evaluation_verification_ok:
        cleanup = preserve_test_data(run_manifest)
    else:
        cleanup = cleanup_exact_manifest(
            run_manifest,
            application_database_url=application_database_url,
            checkpoint_database_url=checkpoint_database_url,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key or None,
            qdrant_path=str(args.qdrant_path) if args.qdrant_path else None,
            qdrant_owner_identity=args.qdrant_owner_identity,
            database_bootstrap=database_bootstrap,
        )
    run_metadata["cleanup"] = cleanup.to_dict()
    run_metadata["manifest"] = run_manifest.privacy_safe()
    report = build_combined_report(
        real_model_cases=[item for item in all_reports if item.get("suite") == "real_model"],
        longitudinal_cases=[item for item in all_reports if item.get("suite") == "longitudinal"],
        redaction_secrets=(judge_key, args.backend_generation_key),
        run_metadata=run_metadata,
    )
    json_path.write_text(report.to_json() + "\n", encoding="utf-8")
    markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    judge_path, messages_path = write_readable_artifacts(report, markdown_path)
    payload = report.to_dict()
    progress.record_cleanup(cleanup.to_dict())
    progress.mark_terminal()
    cleanup_accepted = cleanup.ok or cleanup.status == "preserved"
    print(json.dumps({
        "status": "combined" if cleanup_accepted and evaluation_verification_ok else "failed_verification" if cleanup_accepted else "failed_cleanup",
        "judge_markdown_artifact": str(judge_path),
        "messages_markdown_artifact": str(messages_path),
        "json_artifact": str(json_path),
        "markdown_artifact": str(markdown_path),
        "real_model_case_count": payload["counts"]["real_model_cases"],
        "longitudinal_scenario_count": payload["counts"]["longitudinal_scenarios"],
        "backend_revision": health.backend_revision if health else None,
        "evaluation_completed": evaluation_finished,
        "evaluation_quality_passed": evaluation_quality_ok,
        "evaluation_verification_passed": evaluation_verification_ok,
        "verification": run_metadata["verification"],
        "budgets": run_metadata["budgets"],
        "cleanup": cleanup.to_dict(),
    }, sort_keys=True))
    return 0 if (
        cleanup_accepted
        and evaluation_finished
        and evaluation_verification_ok
    ) else 1

if __name__ == "__main__":
    raise SystemExit(main())
