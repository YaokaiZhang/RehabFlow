from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from evals.longitudinal.contract import RUBRIC_DIMENSIONS

REPORT_VERSION = "combined-v2"
RUBRIC_VERSION = "v2"
REPORT_DISCLAIMER = (
    "Engineering diagnostic only; not clinical validation and not a clinical outcome."
)
REAL_MODEL_CASE_COUNT = 8
LONGITUDINAL_SCENARIO_COUNT = 36
_JUDGE_STATUS_COLUMNS = (
    "passed",
    "failed",
    "error",
    "not_configured",
    "not_applicable",
    "not_attempted",
)
_REPORT_ID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)


def _redact(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        result = _REPORT_ID_RE.sub("[REDACTED_ID]", value)
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    if isinstance(value, Mapping):
        return {str(key): _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    return value


def _validate_entries(
    entries: list[Mapping[str, Any]],
    *,
    id_field: str,
    expected_count: int,
    label: str,
) -> tuple[dict[str, Any], ...]:
    if len(entries) != expected_count:
        raise ValueError(
            f"combined report requires exactly {expected_count} {label} entries; "
            f"received {len(entries)}"
        )
    normalized: list[dict[str, Any]] = []
    ids: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise TypeError(f"{label}[{index}] must be an object")
        entry_id = str(entry.get(id_field) or "").strip()
        if not entry_id:
            raise ValueError(f"{label}[{index}] requires {id_field}")
        if entry_id in ids:
            raise ValueError(f"combined report contains duplicate {id_field}: {entry_id}")
        ids.append(entry_id)
        normalized.append(dict(entry))
    return tuple(normalized)


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "-"


def _render_table(
    headers: Sequence[object],
    rows: Sequence[Sequence[object]],
    *,
    alignments: Sequence[str] | None = None,
) -> list[str]:
    cells = [[_markdown_cell(value) for value in headers]]
    cells.extend([[_markdown_cell(value) for value in row] for row in rows])
    width_count = len(cells[0])
    if any(len(row) != width_count for row in cells):
        raise ValueError("Markdown table rows must have the same number of cells")
    alignments = tuple(alignments or ("left",) * width_count)
    if len(alignments) != width_count or any(
        alignment not in {"left", "right"} for alignment in alignments
    ):
        raise ValueError("Markdown table alignments are invalid")
    widths = [
        max(3, max(len(row[index]) for row in cells))
        for index in range(width_count)
    ]

    def render_row(row: Sequence[str]) -> str:
        rendered: list[str] = []
        for index, value in enumerate(row):
            width = widths[index]
            rendered.append(
                value.rjust(width) if alignments[index] == "right" else value.ljust(width)
            )
        return "| " + " | ".join(rendered) + " |"

    separator = []
    for index, width in enumerate(widths):
        if alignments[index] == "right":
            separator.append("-" * max(2, width - 1) + ":")
        else:
            separator.append("-" * width)
    return [
        render_row(cells[0]),
        render_row(separator),
        *(render_row(row) for row in cells[1:]),
    ]


def _score_bar(value: object) -> str:
    if not _is_score(value):
        return "-"
    bounded = max(0.0, min(4.0, float(value)))
    filled = round(bounded / 4.0 * 10)
    return "#" * filled + "-" * (10 - filled)


def _status_values(
    label: str,
    entries: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> list[str]:
    counts = Counter(_judge_status(entry) for entry in entries)
    return [
        label,
        str(len(entries)),
        str(sum(1 for entry in entries if _scored_entry(entry))),
        *(str(counts.get(status, 0)) for status in _JUDGE_STATUS_COLUMNS),
    ]


def _score_summary_values(
    label: str,
    entries: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> list[str]:
    scores = [
        float(entry["rubric_summary"]["weighted_score"])
        for entry in entries
        if _scored_entry(entry)
    ]
    mean = sum(scores) / len(scores) if scores else None
    return [
        label,
        str(len(scores)),
        f"{mean:.2f} / 4.00" if mean is not None else "not available",
        _score_bar(mean),
    ]


def _judge_status(entry: Mapping[str, Any]) -> str:
    return str(entry.get("judge_status") or "unknown")


def _is_score(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scored_entry(entry: Mapping[str, Any]) -> bool:
    summary = entry.get("rubric_summary")
    return (
        _judge_status(entry) in {"passed", "failed"}
        and isinstance(summary, Mapping)
        and _is_score(summary.get("weighted_score"))
    )


def _weighted_score_text(entry: Mapping[str, Any]) -> str:
    summary = entry.get("rubric_summary")
    if not _scored_entry(entry) or not isinstance(summary, Mapping):
        return "not available"
    return f"{float(summary['weighted_score']):.2f} / 4.00"


def _hard_gate_text(entry: Mapping[str, Any]) -> str:
    summary = entry.get("rubric_summary")
    if not isinstance(summary, Mapping):
        return "not available"
    failures = summary.get("hard_gate_failures")
    if not isinstance(failures, list):
        return "not available"
    return ", ".join(str(failure) for failure in failures) if failures else "none"


def _status_row(
    label: str,
    entries: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> str:
    counts = Counter(_judge_status(entry) for entry in entries)
    values = [
        label,
        str(len(entries)),
        str(sum(1 for entry in entries if _scored_entry(entry))),
        *(str(counts.get(status, 0)) for status in _JUDGE_STATUS_COLUMNS),
    ]
    return "| " + " | ".join(_markdown_cell(value) for value in values) + " |"


def _score_summary_row(
    label: str,
    entries: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> str:
    scores = [
        float(entry["rubric_summary"]["weighted_score"])
        for entry in entries
        if _scored_entry(entry)
    ]
    mean = f"{sum(scores) / len(scores):.2f} / 4" if scores else "not available"
    return f"| {_markdown_cell(label)} | {len(scores)} | {mean} |"


def _dimension_scores(
    entries: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    dimension: str,
) -> list[float]:
    scores: list[float] = []
    for entry in entries:
        if not _scored_entry(entry):
            continue
        judge = entry.get("judge")
        criteria = judge.get("criteria") if isinstance(judge, Mapping) else None
        criterion = criteria.get(dimension) if isinstance(criteria, Mapping) else None
        score = criterion.get("score") if isinstance(criterion, Mapping) else None
        if _is_score(score):
            scores.append(float(score))
    return scores


def _dimension_table(
    label: str,
    entries: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> list[str]:
    rows: list[list[str]] = []
    for dimension in RUBRIC_DIMENSIONS:
        scores = _dimension_scores(entries, dimension)
        mean = sum(scores) / len(scores) if scores else None
        rows.append([
            dimension,
            f"{mean:.2f}" if mean is not None else "not available",
            str(len(scores)),
            _score_bar(mean),
        ])
    return [
        f"### {label} judge dimension scores",
        "",
        *_render_table(
            ("Dimension", "Mean score (0-4)", "Scored cases", "Score"),
            rows,
            alignments=("left", "right", "right", "left"),
        ),
    ]


def _case_overview_table(
    real_model_cases: tuple[dict[str, Any], ...],
    longitudinal_scenarios: tuple[dict[str, Any], ...],
) -> list[str]:
    rows: list[list[str]] = []
    for suite, entries in (
        ("real-model", real_model_cases),
        ("longitudinal", longitudinal_scenarios),
    ):
        for entry in entries:
            rows.append([
                suite,
                str(entry.get("case_id") or entry.get("scenario_id") or "unknown"),
                str(entry.get("status") or "unknown"),
                _judge_status(entry),
                _weighted_score_text(entry),
                _hard_gate_text(entry),
            ])
    return [
        "## Case overview",
        "",
        *_render_table(
            (
                "Suite",
                "Case or scenario",
                "Case status",
                "Judge status",
                "Weighted score",
                "Hard-gate failures",
            ),
            rows,
            alignments=("left", "left", "left", "left", "right", "left"),
        ),
    ]


def _message_list(
    entry: Mapping[str, Any],
    key: str,
    *,
    fallback_key: str | None = None,
) -> list[Mapping[str, Any]]:
    values = entry.get(key)
    if values is None and fallback_key:
        values = entry.get(fallback_key)
    if not isinstance(values, (list, tuple)):
        return []
    return [dict(value) for value in values if isinstance(value, Mapping)]


def _message_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, indent=2)
    except TypeError:
        return str(value)


def _text_block(label: str, value: object, *, language: str = "text") -> list[str]:
    text = _message_value(value).replace("~~~", "~~\\~")
    return [
        f"{label}:",
        "",
        f"~~~{language}",
        text or "(empty)",
        "~~~",
    ]


def _empty_message_status(entry: Mapping[str, Any]) -> str:
    status = str(entry.get("message_capture_status") or "")
    if status == "missing":
        return "- unavailable (message capture missing)"
    if status == "unavailable":
        return "- unavailable (message capture unavailable)"
    if status == "captured":
        return "- none (capture verified)"
    if status == "bounded":
        return "- bounded (oversized raw transcript omitted; see capture metadata)"
    return "- none"

def _human_readable_messages(entry: Mapping[str, Any]) -> list[str]:
    internal_messages = _message_list(entry, "ai_internal_messages")
    messages = _message_list(entry, "ai_messages", fallback_key="conversation")
    lines = ["### AI messages", ""]
    capture = entry.get("message_capture")
    if isinstance(capture, Mapping):
        lines.extend(["### Message capture", ""])
        lines.extend(_text_block("Capture metadata", capture, language="json"))
        lines.append("")
    compaction_evidence = entry.get("compaction_evidence")
    if isinstance(compaction_evidence, (list, tuple)):
        lines.extend(["### Compaction evidence", ""])
        lines.extend(_text_block("Evidence", compaction_evidence, language="json"))
        lines.append("")
    if not messages:
        lines.append(_empty_message_status(entry))
    for index, message in enumerate(messages, start=1):
        role = message.get("role") or message.get("sender_role") or "unknown"
        lines.extend(
            [
                f"#### {index}. {_markdown_cell(role)}",
                f"- Session: {_markdown_cell(message.get('session') or 'unknown')}",
                f"- Created: {_markdown_cell(message.get('created_at') or 'unknown')}",
                *_text_block("Content", message.get("content")),
                "",
            ]
        )

    lines.extend(["### AI internal messages", ""])
    if not internal_messages:
        lines.append(_empty_message_status(entry))
    for index, message in enumerate(internal_messages, start=1):
        agent = message.get("agent") or message.get("agent_name") or "unknown"
        event = message.get("event") or message.get("event_type") or "unknown"
        lines.extend(
            [
                f"#### {index}. {_markdown_cell(agent)} / {_markdown_cell(event)}",
                f"- Session: {_markdown_cell(message.get('session') or 'unknown')}",
                f"- Created: {_markdown_cell(message.get('created_at') or 'unknown')}",
                *_text_block("Content", message.get("content")),
                *_text_block("Model input", message.get("model_input"), language="json"),
                *_text_block("Metadata", message.get("metadata") or {}, language="json"),
                "",
            ]
        )
    tool_calls = _message_list(entry, "tool_calls")
    lines.extend(["### Tool calls", ""])
    if not tool_calls:
        lines.append("- none")
    else:
        lines.extend(_text_block("Records", tool_calls, language="json"))
    tool_metrics = entry.get("tool_metrics")
    if isinstance(tool_metrics, Mapping):
        lines.extend(_text_block("Metrics", tool_metrics, language="json"))
    return lines


def _judge_justifications(entry: Mapping[str, Any]) -> list[str]:
    judge = entry.get("judge")
    criteria = judge.get("criteria") if isinstance(judge, Mapping) else None
    lines = ["### Judge justifications", ""]
    if not isinstance(criteria, Mapping) or not criteria:
        lines.append("- none")
        return lines
    for dimension in RUBRIC_DIMENSIONS:
        criterion = criteria.get(dimension)
        if not isinstance(criterion, Mapping):
            continue
        score = criterion.get("score")
        score_text = f"{score} / 4" if _is_score(score) else "not available"
        passed = criterion.get("passed")
        gate_status = (
            "passed" if passed is True
            else "failed" if passed is False
            else "not gated"
        )
        lines.extend(
            [
                f"#### {_markdown_cell(dimension)}: {score_text} "
                f"[{_score_bar(score)}] ({gate_status})",
                f"- Justification: {_markdown_cell(criterion.get('justification') or 'not provided')}",
            ]
        )
        reason_codes = criterion.get("reason_codes")
        reason_text = (
            ", ".join(str(code) for code in reason_codes)
            if isinstance(reason_codes, (list, tuple)) and reason_codes
            else "none"
        )
        lines.append(f"- Reason codes: {_markdown_cell(reason_text)}")
        raw_spans = criterion.get("evidence_spans")
        spans = raw_spans if isinstance(raw_spans, (list, tuple)) else ()
        if not spans:
            lines.append("- Evidence: none")
        else:
            lines.append("- Evidence:")
            for raw_span in spans:
                if not isinstance(raw_span, Mapping):
                    continue
                source = _markdown_cell(raw_span.get("source") or "unknown")
                start = raw_span.get("start", "?")
                end = raw_span.get("end", "?")
                excerpt = raw_span.get("text")
                if isinstance(excerpt, str) and excerpt.strip():
                    evidence_text = _markdown_cell(excerpt)
                else:
                    evidence_text = (
                        f"hash {_markdown_cell(raw_span.get('text_sha256') or 'unknown')}; "
                        f"length {_markdown_cell(raw_span.get('text_length') or 'unknown')}"
                    )
                lines.append(f"  - `{source} [{start}:{end}]`: {evidence_text}")
        lines.append("")
    return lines


_JUDGE_MESSAGE_KEYS = frozenset(
    {
        "ai_messages",
        "ai_internal_messages",
        "conversation",
        "transcript",
        "trace",
        "response",
        "turns",
        "judge_prompt",
        "judge_response",
        "judge_attempts",
        "judge_input",
        "judge_output",
        "generation_prompt",
        "generation_response",
        "result",
        "runtime_events",
        "task_context",
    }
)


def _judge_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in entry.items()
        if str(key) not in _JUDGE_MESSAGE_KEYS
    }


def _detailed_case(
    suite: str,
    entry: Mapping[str, Any],
    *,
    include_messages: bool = True,
) -> list[str]:
    entry_id = entry.get("case_id") or entry.get("scenario_id") or "unknown"
    summary = entry.get("rubric_summary")
    score = summary.get("weighted_score") if isinstance(summary, Mapping) else None
    payload = dict(entry) if include_messages else _judge_entry(entry)
    return [
        f"### {suite}: {entry_id}",
        "",
        f"- Case status: {_markdown_cell(entry.get('status') or 'unknown')}",
        f"- Judge status: {_markdown_cell(_judge_status(entry))}",
        f"- Weighted judge score: {_markdown_cell(_weighted_score_text(entry))}",
        f"- Score: {_markdown_cell(_score_bar(score))}",
        f"- Hard-gate failures: {_markdown_cell(_hard_gate_text(entry))}",
        "",
        *_judge_justifications(entry),
        "",
        *(_human_readable_messages(entry) if include_messages else []),
        "",
        "Full case payload:" if include_messages else "Full judge case payload:",
        "",
        chr(96) * 3 + "json",
        json.dumps(payload, sort_keys=True, indent=2),
        chr(96) * 3,
        "",
    ]


@dataclass(frozen=True)
class CombinedReport:
    generated_at: str
    real_model_cases: tuple[dict[str, Any], ...]
    longitudinal_scenarios: tuple[dict[str, Any], ...]
    run_metadata: dict[str, Any] = field(default_factory=dict)
    redaction_secrets: tuple[str, ...] = field(default=(), repr=False, compare=False)
    report_version: str = REPORT_VERSION
    rubric_version: str = RUBRIC_VERSION
    rubric_dimensions: tuple[str, ...] = RUBRIC_DIMENSIONS
    disclaimer: str = REPORT_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "generated_at": self.generated_at,
            "run_metadata": self.run_metadata,
            "report_version": self.report_version,
            "rubric_version": self.rubric_version,
            "rubric_dimensions": list(self.rubric_dimensions),
            "disclaimer": self.disclaimer,
            "counts": {
                "real_model_cases": len(self.real_model_cases),
                "longitudinal_scenarios": len(self.longitudinal_scenarios),
                "total": len(self.real_model_cases) + len(self.longitudinal_scenarios),
            },
            "real_model_cases": list(self.real_model_cases),
            "longitudinal_scenarios": list(self.longitudinal_scenarios),
        }
        return _redact(payload, self.redaction_secrets)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    def to_markdown(self, *, include_messages: bool = True) -> str:
        payload = self.to_dict()
        real_model_cases = tuple(
            dict(entry) if include_messages else _judge_entry(entry)
            for entry in payload["real_model_cases"]
        )
        longitudinal_scenarios = tuple(
            dict(entry) if include_messages else _judge_entry(entry)
            for entry in payload["longitudinal_scenarios"]
        )
        all_entries = real_model_cases + longitudinal_scenarios
        counts = payload["counts"]
        run_metadata = payload.get("run_metadata")
        if not isinstance(run_metadata, Mapping):
            run_metadata = {}
        backend = run_metadata.get("backend")
        backend = backend if isinstance(backend, Mapping) else {}
        budgets = run_metadata.get("budgets")
        budgets = budgets if isinstance(budgets, Mapping) else {}
        tool_metrics = run_metadata.get("tool_metrics")
        tool_metrics = tool_metrics if isinstance(tool_metrics, Mapping) else {}
        tool_metrics_by_tool = run_metadata.get("tool_metrics_by_tool")
        tool_metrics_by_tool = tool_metrics_by_tool if isinstance(tool_metrics_by_tool, Mapping) else {}
        tool_metrics_by_suite = run_metadata.get("tool_metrics_by_suite")
        tool_metrics_by_suite = tool_metrics_by_suite if isinstance(tool_metrics_by_suite, Mapping) else {}
        fault_profiles = run_metadata.get("fault_profiles")
        fault_profiles = fault_profiles if isinstance(fault_profiles, Mapping) else {}
        database_bootstrap = run_metadata.get("database_bootstrap")
        database_bootstrap = database_bootstrap if isinstance(database_bootstrap, Mapping) else {}
        database_application = database_bootstrap.get("application")
        database_application = database_application if isinstance(database_application, Mapping) else {}
        database_checkpoint = database_bootstrap.get("checkpoint")
        database_checkpoint = database_checkpoint if isinstance(database_checkpoint, Mapping) else {}
        execution_mode = str(run_metadata.get("execution_mode") or "")
        case_concurrency = run_metadata.get("case_concurrency")
        case_concurrency = case_concurrency if isinstance(case_concurrency, Mapping) else {}
        longitudinal_label = "service-backed" if execution_mode == "real-backend-service" else "unavailable"
        tool_by_tool_text = "; ".join(
            f"{name}={metrics.get('execution_success_rate', 'not available')}"
            for name, metrics in sorted(tool_metrics_by_tool.items())
            if isinstance(metrics, Mapping)
        ) or "not available"
        tool_by_suite_text = "; ".join(
            f"{name}={metrics.get('execution_success_rate', 'not available')}"
            for name, metrics in sorted(tool_metrics_by_suite.items())
            if isinstance(metrics, Mapping)
        ) or "not available"
        fault_profile_text = ", ".join(
            f"{name}={count}" for name, count in sorted(fault_profiles.items())
        ) or "not available"

        lines = [
            "# Combined v2 engineering evaluation",
            "",
            f"- Report version: {payload['report_version']}",
            f"- Rubric version: {payload['rubric_version']}",
            f"- Generated at: {payload['generated_at']}",
            f"- Cases: {counts['real_model_cases']} real-model + "
            f"{counts['longitudinal_scenarios']} {longitudinal_label} longitudinal",
            f"- {payload['disclaimer']}",
            "",
            "## Service run overview",
            "",
            f"- Execution mode: {_markdown_cell(run_metadata.get('execution_mode') or 'not available')}",
            f"- Max parallel cases: {_markdown_cell(case_concurrency.get('max_parallel_cases', 'not available'))}",
            f"- Service process per case: {_markdown_cell(case_concurrency.get('service_process_per_case', 'not available'))}",
            f"- Backend: {_markdown_cell(backend.get('service') or 'not available')} "
            f"({_markdown_cell(backend.get('environment') or 'unknown')})",
            f"- Backend revision: {_markdown_cell(backend.get('backend_revision') or 'unknown')}",
            f"- Workflow version: {_markdown_cell(backend.get('workflow_version') or 'unknown')}",
            f"- Evaluator revision: {_markdown_cell(run_metadata.get('evaluator_revision') or 'unknown')}",
            f"- Evaluator SQL ownership: application={_markdown_cell(database_application.get('ownership_state') or 'unknown')}; checkpoint={_markdown_cell(database_checkpoint.get('ownership_state') or 'unknown')}",
            f"- Evaluator SQL readiness: schema={_markdown_cell(database_application.get('schema_revision') or 'unknown')}; checkpoint={_markdown_cell(database_checkpoint.get('checkpoint_ready', False))}",
            f"- Budget compliant: {_markdown_cell(budgets.get('compliant', 'not available'))}; "
            f"noncompliant: {_markdown_cell(budgets.get('noncompliant', 'not available'))}; "
            f"unavailable: {_markdown_cell(budgets.get('unavailable', 'not available'))}",
            f"- Tool attempts: {_markdown_cell(tool_metrics.get('attempted_calls', tool_metrics.get('attempted', 'not available')))}; "
            f"success rate: {_markdown_cell(tool_metrics.get('execution_success_rate', 'not available'))}; "
            f"useful-result rate: {_markdown_cell(tool_metrics.get('useful_result_rate', 'not available'))}",
            f"- Tool outcomes by tool: {_markdown_cell(tool_by_tool_text)}",
            f"- Tool outcomes by suite: {_markdown_cell(tool_by_suite_text)}",
            f"- Fault profiles: {_markdown_cell(fault_profile_text)}",
            "",
            "## Judge score overview",
            "",
            "Scores use the v2 rubric scale from 0 to 4. A score is shown only "
            "when the case has a complete parsed judge result.",
            "",
            "### Judge status",
            "",
            *_render_table(
                (
                    "Scope",
                    "Cases",
                    "Scored",
                    "Passed",
                    "Failed",
                    "Error",
                    "Not configured",
                    "Not applicable",
                    "Not attempted",
                ),
                [
                    _status_values("Combined", all_entries),
                    _status_values("Real-model", real_model_cases),
                    _status_values("Longitudinal", longitudinal_scenarios),
                ],
                alignments=("left",) + ("right",) * 8,
            ),
            "",
            "### Weighted judge scores",
            "",
            *_render_table(
                ("Scope", "Scored cases", "Mean weighted score", "Score"),
                [
                    _score_summary_values("Combined", all_entries),
                    _score_summary_values("Real-model", real_model_cases),
                    _score_summary_values("Longitudinal", longitudinal_scenarios),
                ],
                alignments=("left", "right", "right", "left"),
            ),
            "",
        ]
        lines.extend(_dimension_table("Real-model", real_model_cases))
        lines.extend([""])
        lines.extend(_dimension_table("Longitudinal", longitudinal_scenarios))
        lines.extend([""])
        lines.extend(_case_overview_table(real_model_cases, longitudinal_scenarios))
        lines.extend(["", "## Detailed case reports", ""])
        for suite, entries in (
            ("Real-model", real_model_cases),
            ("Longitudinal", longitudinal_scenarios),
        ):
            lines.extend([f"## {suite} cases", ""])
            for entry in entries:
                lines.extend(_detailed_case(suite, entry, include_messages=include_messages))
        return "\n".join(lines) + "\n"


    def to_messages_markdown(self) -> str:
        payload = self.to_dict()
        lines = [
            "# AI messages and internal messages",
            "",
            f"- Generated at: {payload['generated_at']}",
            f"- {payload['disclaimer']}",
            "",
        ]
        for suite, key in (
            ("Real-model", "real_model_cases"),
            ("Longitudinal", "longitudinal_scenarios"),
        ):
            lines.extend([f"## {suite}", ""])
            for entry in payload[key]:
                entry_id = entry.get("case_id") or entry.get("scenario_id") or "unknown"
                lines.extend([f"### {entry_id}", ""])
                lines.extend(_human_readable_messages(entry))
                lines.append("")
        return "\n".join(lines) + "\n"


def build_combined_report(
    *,
    real_model_cases: list[Mapping[str, Any]],
    longitudinal_cases: list[Mapping[str, Any]],
    generated_at: str | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    redaction_secrets: tuple[str, ...] = (),
) -> CombinedReport:
    return CombinedReport(
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        run_metadata=dict(run_metadata or {}),
        real_model_cases=_validate_entries(
            real_model_cases,
            id_field="case_id",
            expected_count=REAL_MODEL_CASE_COUNT,
            label="real-model cases",
        ),
        longitudinal_scenarios=_validate_entries(
            longitudinal_cases,
            id_field="scenario_id",
            expected_count=LONGITUDINAL_SCENARIO_COUNT,
            label="longitudinal scenarios",
        ),
        redaction_secrets=tuple(secret for secret in redaction_secrets if secret),
    )
