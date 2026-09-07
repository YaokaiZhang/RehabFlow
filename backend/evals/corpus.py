from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

import yaml

from evals.longitudinal.contract import ScenarioV2


DEFAULT_CASES_PATH = Path(__file__).with_name("cases.yaml")
SUITES = ("longitudinal_v2",)
MATRIX_CASE_IDS = frozenset({
    "sufficient-session-no-call",
    "level2-memory-disclosure",
    "level3-selected-session-search",
    "empty-retrieval-adaptive-recovery",
    "compaction-critical-fact",
    "patient-memory-scope-no-call",
    "catalog-retrieval-grounding",
    "web-retrieval-grounding",
    "failed-retrieval-honest-fallback",
    "compaction-selected-session-recall",
})
CANONICAL_LONGITUDINAL_CASE_COUNT = 36
_METADATA_KEYS = {
    "scenario_id",
    "title",
    "category",
    "tags",
    "notes",
    "script_repeat_reasons",
    "case",
    "scenario",
}


def _schema_path(suite: str) -> Path:
    if suite == "longitudinal_v2":
        return Path(__file__).parent / "schema" / "longitudinal.schema.json"
    raise ValueError(f"unknown evaluator suite: {suite}")


def _validate_suite(suite: str) -> str:
    if suite not in SUITES:
        raise ValueError(f"unknown evaluator suite: {suite}")
    return suite


def _yaml_entries(source: Path, suite: str) -> list[dict[str, Any]]:
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"could not parse canonical YAML {source}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ValueError(f"canonical YAML must be an object: {source}")
    raw_entries = document.get(suite)
    if not isinstance(raw_entries, list):
        raise ValueError(f"canonical YAML suite {suite!r} must be a list")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"{suite}[{index}] case must be an object")
        scenario_id = str(raw_entry.get("scenario_id") or "").strip()
        raw_case = raw_entry.get("case", raw_entry.get("scenario"))
        if not isinstance(raw_case, Mapping):
            raw_case = {
                key: value
                for key, value in raw_entry.items()
                if key not in _METADATA_KEYS
            }
        case = copy.deepcopy(dict(raw_case))
        case_id = str(case.get("scenario_id") or scenario_id).strip()
        label = scenario_id or case_id or "<missing-id>"
        if not scenario_id:
            raise ValueError(f"{suite}[{index}] {label}: missing scenario_id")
        if case_id != scenario_id:
            raise ValueError(
                f"{suite}[{index}] {label}: metadata scenario_id does not match case scenario_id"
            )
        if scenario_id in seen:
            raise ValueError(f"{suite}[{index}] {scenario_id}: duplicate scenario_id")
        seen.add(scenario_id)
        title = str(raw_entry.get("title") or "").strip()
        category = str(raw_entry.get("category") or "").strip()
        if not title:
            raise ValueError(f"{suite}[{index}] {scenario_id}: missing title")
        if not category:
            raise ValueError(f"{suite}[{index}] {scenario_id}: missing category")
        reasons = raw_entry.get("script_repeat_reasons", {})
        if reasons is None:
            reasons = {}
        if not isinstance(reasons, Mapping):
            raise ValueError(f"{suite}[{index}] {scenario_id}: script_repeat_reasons must be an object")
        entries.append(
            {
                "scenario_id": scenario_id,
                "title": title,
                "category": category,
                "tags": list(raw_entry.get("tags", []) or []),
                "notes": str(raw_entry.get("notes") or ""),
                "script_repeat_reasons": {str(key): str(value) for key, value in reasons.items()},
                "case": case,
            }
        )
    return entries


def load_case_entries(suite: str, source: str | Path | None = None) -> list[dict[str, Any]]:
    suite = _validate_suite(suite)
    path = Path(source) if source is not None else DEFAULT_CASES_PATH
    if path.is_dir():
        raise ValueError(
            f"{suite} scenarios must use the canonical YAML corpus, not a directory: {path}"
        )
    entries = _yaml_entries(path, suite)
    canonical_default = (
        suite == "longitudinal_v2"
        and path.resolve() == DEFAULT_CASES_PATH.resolve()
    )
    if canonical_default:
        for entry in entries:
            case = entry["case"]
            if case.get("script"):
                raise ValueError(
                    f"{entry['scenario_id']}: canonical longitudinal_v2 cases cannot use scripted execution"
                )
            oracles = case.get("oracles", {})
            if isinstance(oracles, Mapping):
                forbidden = sorted(
                    key
                    for key in ("expected_catalog_ids", "forbidden_catalog_ids")
                    if key in oracles
                )
                if forbidden:
                    raise ValueError(
                        f"{entry['scenario_id']}: canonical cases cannot declare tool-result oracle fields: "
                        + ", ".join(forbidden)
                    )
    for index, entry in enumerate(entries):
        try:
            validate_script_repeats(
                str(entry["scenario_id"]),
                entry["case"].get("script", {}),
                entry.get("script_repeat_reasons", {}),
            )
            _validated_scenario(suite, entry)
        except Exception as exc:
            raise ValueError(
                f"{suite}[{index}] {entry['scenario_id']}: {exc}"
            ) from exc
    if suite == "longitudinal_v2" and path.resolve() == DEFAULT_CASES_PATH.resolve():
        ids = {str(entry["scenario_id"]) for entry in entries}
        if len(entries) != CANONICAL_LONGITUDINAL_CASE_COUNT:
            raise ValueError(
                "canonical longitudinal_v2 corpus requires exactly "
                f"{CANONICAL_LONGITUDINAL_CASE_COUNT} cases"
            )
        if not MATRIX_CASE_IDS.issubset(ids) or len(ids.intersection(MATRIX_CASE_IDS)) != 10:
            raise ValueError("canonical longitudinal_v2 corpus must contain exactly the ten frozen matrix cases")
    return entries


def _validated_scenario(suite: str, entry: Mapping[str, Any]) -> ScenarioV2:
    payload = entry["case"]
    schema_path = _schema_path(suite)
    try:
        return ScenarioV2.from_payload(payload, schema_path=schema_path)
    except Exception as exc:
        raise ValueError(
            f"{suite} case {entry['scenario_id']!r} failed schema/semantic validation: {exc}"
        ) from exc


def load_suite(suite: str, source: str | Path | None = None) -> list[ScenarioV2]:
    return [_validated_scenario(suite, entry) for entry in load_case_entries(suite, source)]


def validate_script_repeats(
    scenario_id: str,
    script: Mapping[str, Any],
    repeat_reasons: Mapping[str, str] | None,
) -> None:
    if not isinstance(script, Mapping):
        return
    reasons = dict(repeat_reasons or {})
    for key, entries in script.items():
        if not isinstance(entries, list):
            continue
        for index in range(1, len(entries)):
            if entries[index] != entries[index - 1]:
                continue
            reason = str(reasons.get(str(key)) or reasons.get("*") or "").strip()
            if not reason:
                raise ValueError(
                    f"{scenario_id}: duplicate adjacent script entry {key}[{index}] "
                    "requires a declared reason"
                )
            allowed = {"turn", "repair", "retry", "resume", "replay", "idempotent"}
            if not any(token in reason.lower() for token in allowed):
                raise ValueError(
                    f"{scenario_id}: duplicate adjacent script entry {key}[{index}] "
                    f"has no allowed repeat reason: {reason}"
                )


def canonical_cases_path() -> Path:
    return DEFAULT_CASES_PATH


def load_longitudinal_cases(source: str | Path | None = None) -> list[ScenarioV2]:
    values = load_suite("longitudinal_v2", source)
    return [value for value in values if isinstance(value, ScenarioV2)]

