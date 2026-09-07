from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from evals.harness.scripted_models import structured_script_value_is_valid
RUBRIC_DIMENSIONS = (
    "context_fidelity",
    "temporal_reasoning",
    "grounding",
    "memory_appropriateness",
    "risk_handling",
    "uncertainty_calibration",
    "action_quality",
    "tool_use_quality",
    "communication_quality",
)
RUBRIC_LEVELS = ("primary", "secondary", "hard_gate", "not_applicable")


def _has_items(value: object) -> bool:
    return isinstance(value, (list, tuple, set, frozenset, dict)) and bool(value)


def _nonempty_text(step: Mapping[str, Any], field: str, index: int) -> str:
    value = step.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"timeline[{index}] {step.get('op')} requires non-empty {field}")
    return value.strip()


def _unique_ids(items: object, label: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    if not isinstance(items, list):
        return result
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            raise ValueError(f"{label}[{index}] requires an id")
        item_id = str(item["id"])
        if item_id in result:
            raise ValueError(f"{label} contains duplicate id: {item_id}")
        result[item_id] = item
    return result


def validate_timeline_semantics(
    initial_state: Mapping[str, Any],
    timeline: list[Mapping[str, Any]],
    oracles: Mapping[str, Any],
    script: Mapping[str, Any] | None = None,
) -> None:
    """Validate operation-specific fields and references before the adapter runs."""
    episodes = _unique_ids(initial_state.get("care_episodes"), "care_episodes")
    sessions = _unique_ids(initial_state.get("sessions"), "sessions")
    evidence = _unique_ids(initial_state.get("evidence"), "evidence")
    catalog = _unique_ids(initial_state.get("catalog"), "catalog")
    patient_memory = _unique_ids(initial_state.get("patient_memory"), "patient_memory")
    episode_memory = _unique_ids(initial_state.get("episode_memory"), "episode_memory")
    summaries = _unique_ids(initial_state.get("triage_summaries"), "triage_summaries")
    memory_ids = {**patient_memory, **episode_memory}
    if len(memory_ids) != len(patient_memory) + len(episode_memory):
        raise ValueError("initial_state memory ids must be unique across patient and episode scopes")

    for memory_id, memory_record in memory_ids.items():
        for relation in ("supersedes", "superseded_by"):
            related_id = memory_record.get(relation)
            if related_id and str(related_id) not in memory_ids:
                raise ValueError(
                    f"memory {memory_id} {relation} references unknown memory id: {related_id}"
                )

    current_session: str | None = None
    current_episode: str | None = None
    scenario_script = script or {}
    router_index = 0
    clarification_index = 0
    pending_checkpoint_possible = False
    for index, step in enumerate(timeline):
        if not isinstance(step, Mapping):
            raise ValueError(f"timeline[{index}] must be an object")
        operation = str(step.get("op", ""))
        if operation == "start_session":
            session_id = _nonempty_text(step, "session_id", index)
            episode_id = _nonempty_text(step, "care_episode_id", index)
            if episode_id not in episodes:
                raise ValueError(
                    f"timeline[{index}] start_session references unknown care episode: {episode_id}"
                )
            if session_id in sessions and str(sessions[session_id].get("care_episode_id")) != episode_id:
                raise ValueError(
                    f"timeline[{index}] start_session cannot reuse a session for a different Care Episode"
                )
            if episodes[episode_id].get("status") != "active":
                raise ValueError(
                    f"timeline[{index}] start_session must target an active Care Episode"
                )
            sessions[session_id] = {"id": session_id, "care_episode_id": episode_id}
            current_session = session_id
            current_episode = episode_id
            pending_checkpoint_possible = False
        elif operation == "turn":
            if current_session is None:
                raise ValueError(f"timeline[{index}] turn requires a prior start_session")
            _nonempty_text(step, "message", index)
            selected_session_id = str(step.get("selected_session_id") or "").strip()
            if selected_session_id:
                if selected_session_id not in sessions:
                    raise ValueError(
                        f"timeline[{index}] turn references unknown selected session: {selected_session_id}"
                    )
                if str(sessions[selected_session_id].get("care_episode_id") or "") != str(current_episode or ""):
                    raise ValueError(
                        f"timeline[{index}] turn selected session must target the active Care Episode"
                    )
            router_steps = scenario_script.get("router", [])
            router_step = (
                router_steps[router_index]
                if isinstance(router_steps, list) and router_index < len(router_steps)
                else {}
            )
            route = router_step.get("route") if isinstance(router_step, Mapping) else None
            clarification_steps = scenario_script.get("clarification", [])
            clarification_step = (
                clarification_steps[clarification_index]
                if isinstance(clarification_steps, list)
                and clarification_index < len(clarification_steps)
                else {}
            )
            pending_checkpoint_possible = (
                (not scenario_script)
                or route == "clarify"
                and isinstance(clarification_step, Mapping)
                and clarification_step.get("action") == "interrupt"
            )
            router_index += 1
            if pending_checkpoint_possible:
                clarification_index += 1
        elif operation == "resume":
            if current_session is None:
                raise ValueError(f"timeline[{index}] resume requires a prior start_session")
            if not pending_checkpoint_possible:
                raise ValueError(
                    f"timeline[{index}] resume requires a prior turn with a pending checkpoint"
                )
            _nonempty_text(step, "message", index)
            selected_session_id = str(step.get("selected_session_id") or "").strip()
            if selected_session_id:
                if selected_session_id not in sessions:
                    raise ValueError(
                        f"timeline[{index}] resume references unknown selected session: {selected_session_id}"
                    )
                if str(sessions[selected_session_id].get("care_episode_id") or "") != str(current_episode or ""):
                    raise ValueError(
                        f"timeline[{index}] resume selected session must target the active Care Episode"
                    )
            pending_checkpoint_possible = False
        elif operation == "advance_time":
            if not isinstance(step.get("seconds"), int) or isinstance(step.get("seconds"), bool):
                raise ValueError(f"timeline[{index}] advance_time requires integer seconds")
            if int(step["seconds"]) < 1:
                raise ValueError(f"timeline[{index}] advance_time seconds must be positive")
        elif operation == "memory_create":
            memory_id = _nonempty_text(step, "memory_id", index)
            scope = _nonempty_text(step, "scope", index)
            for field in ("title", "content", "source_id", "provenance"):
                _nonempty_text(step, field, index)
            if memory_id in memory_ids:
                raise ValueError(f"timeline[{index}] memory_create duplicates memory id: {memory_id}")
            if scope == "episode":
                episode_id = _nonempty_text(step, "care_episode_id", index)
                if episode_id not in episodes:
                    raise ValueError(
                        f"timeline[{index}] memory_create references unknown care episode: {episode_id}"
                    )
                if episode_id != current_episode:
                    raise ValueError(
                        f"timeline[{index}] memory_create must target the active Care Episode"
                    )
            memory_ids[memory_id] = step
            if scope == "episode":
                episode_memory[memory_id] = step
            else:
                patient_memory[memory_id] = step
        elif operation in {"memory_edit", "memory_delete", "memory_archive", "memory_supersede", "correct_fact"}:
            memory_id = _nonempty_text(step, "memory_id", index)
            if memory_id not in memory_ids:
                raise ValueError(
                    f"timeline[{index}] {operation} references unknown memory id: {memory_id}"
                )
            memory_record = memory_ids[memory_id]
            memory_scope = "episode" if memory_id in episode_memory else "patient"
            if (
                memory_scope == "episode"
                and str(memory_record.get("care_episode_id") or "") != str(current_episode or "")
            ):
                raise ValueError(
                    f"timeline[{index}] {operation} must target the active Care Episode"
                )
            if (
                memory_record.get("status", "active") != "active"
                or memory_record.get("stale") is True
                or memory_record.get("superseded_by")
                or any(
                    str(candidate.get("supersedes") or "") == memory_id
                    and candidate.get("status", "active") == "active"
                    and candidate.get("stale") is not True
                    for candidate in memory_ids.values()
                )
            ):
                raise ValueError(
                    f"timeline[{index}] {operation} must target a current active memory item"
                )
            if operation == "memory_edit" and not any(
                isinstance(step.get(field), str) and step[field].strip()
                for field in ("title", "content")
            ):
                raise ValueError(f"timeline[{index}] memory_edit requires title or content")
            if operation == "correct_fact":
                _nonempty_text(step, "content", index)
            if operation == "memory_supersede":
                superseding_id = next(
                    (
                        str(step.get(field) or "").strip()
                        for field in ("superseding_memory_id", "superseded_by", "new_memory_id")
                        if str(step.get(field) or "").strip()
                    ),
                    "",
                )
                if not superseding_id:
                    raise ValueError(
                        f"timeline[{index}] memory_supersede requires superseding_memory_id"
                    )
                if superseding_id == memory_id or superseding_id not in memory_ids:
                    raise ValueError(
                        f"timeline[{index}] memory_supersede references an invalid memory id"
                    )
                superseding_record = memory_ids[superseding_id]
                if (
                    superseding_record.get("status", "active") != "active"
                    or superseding_record.get("stale") is True
                    or superseding_record.get("superseded_by")
                ):
                    raise ValueError(
                        f"timeline[{index}] memory_supersede must target a current active memory item"
                    )
                if superseding_record.get("care_episode_id") and str(
                    superseding_record.get("care_episode_id") or ""
                ) != str(current_episode or ""):
                    raise ValueError(
                        f"timeline[{index}] memory_supersede must target the active Care Episode"
                    )
        elif operation == "save_triage_summary":
            summary_id = _nonempty_text(step, "summary_id", index)
            episode_id = str(step.get("care_episode_id") or current_episode or "")
            if episode_id not in episodes:
                raise ValueError(
                    f"timeline[{index}] save_triage_summary references unknown care episode"
                )
            if episode_id != current_episode or episodes[episode_id].get("status") != "active":
                raise ValueError(
                    f"timeline[{index}] save_triage_summary must target the active Care Episode"
                )
            if not isinstance(step.get("version"), int) or isinstance(step.get("version"), bool):
                raise ValueError(f"timeline[{index}] save_triage_summary requires integer version")
            if int(step["version"]) < 1:
                raise ValueError(f"timeline[{index}] save_triage_summary version must be positive")
            _nonempty_text(step, "content", index)
            if summary_id in summaries:
                raise ValueError(
                    f"timeline[{index}] save_triage_summary duplicates summary id: {summary_id}"
                )
            summaries[summary_id] = step
        elif operation == "restart":
            continue
        else:
            raise ValueError(f"timeline[{index}] has unsupported operation: {operation}")

    evidence_manifest = set(str(item) for item in oracles.get("evidence_manifest", []) or [])
    unknown_manifest = evidence_manifest - set(evidence)
    if unknown_manifest:
        raise ValueError(
            "oracle evidence_manifest references unknown evidence id: "
            + ", ".join(sorted(unknown_manifest))
        )
    active_current_episode = (
        current_episode
        if current_episode and episodes.get(current_episode, {}).get("status") == "active"
        else None
    )
    for item_id in evidence_manifest:
        item = evidence[item_id]
        if item.get("observed") is not True:
            raise ValueError(
                f"oracle evidence_manifest contains unobserved evidence id: {item_id}"
            )
        if item.get("stale") is True:
            raise ValueError(
                f"oracle evidence_manifest contains stale evidence id: {item_id}"
            )
        bound_episode = str(item.get("care_episode_id") or "")
        if bound_episode and (
            bound_episode != str(active_current_episode or "")
            or episodes.get(bound_episode, {}).get("status") != "active"
        ):
            raise ValueError(
                "oracle evidence_manifest evidence must target the current active Care Episode: "
                + item_id
            )
    unauthorized_manifest = {
        item_id for item_id in evidence_manifest if evidence[item_id].get("authorized") is not True
    }
    if unauthorized_manifest:
        raise ValueError(
            "oracle evidence_manifest contains unauthorized evidence id: "
            + ", ".join(sorted(unauthorized_manifest))
        )
    for key in ("expected_catalog_ids", "forbidden_catalog_ids"):
        for catalog_id in oracles.get(key, []) or []:
            if catalog_id not in catalog:
                raise ValueError(f"oracle {key} references unknown catalog id: {catalog_id}")
            if key == "expected_catalog_ids" and catalog[catalog_id].get("authorized") is not True:
                raise ValueError(
                    f"oracle expected_catalog_ids contains unauthorized catalog id: {catalog_id}"
                )
    for catalog_id in oracles.get("expected_catalog_ids", []) or []:
        item = catalog[catalog_id]
        if item.get("stale") is True:
            raise ValueError(
                f"oracle expected_catalog_ids contains stale catalog id: {catalog_id}"
            )
        bound_episode = str(item.get("care_episode_id") or "")
        if bound_episode and (
            bound_episode != str(active_current_episode or "")
            or episodes.get(bound_episode, {}).get("status") != "active"
        ):
            raise ValueError(
                "oracle expected_catalog_ids catalog must target the current active Care Episode: "
                + catalog_id
            )

    for claim_id in (oracles.get("evidence_claims") or {}):
        if claim_id not in evidence:
            raise ValueError(f"oracle evidence_claims references unknown evidence id: {claim_id}")


def _has_competing_versions(
    initial_state: Mapping[str, Any],
    timeline: list[Mapping[str, Any]],
) -> bool:
    for collection_name in ("patient_memory", "episode_memory"):
        items = initial_state.get(collection_name, []) or []
        if any(
            isinstance(item, Mapping)
            and (
                item.get("supersedes")
                or item.get("superseded_by")
                or item.get("stale") is True
            )
            for item in items
        ):
            return True
    summaries = initial_state.get("triage_summaries", []) or []
    grouped: dict[str, set[int]] = {}
    for item in summaries:
        if isinstance(item, Mapping):
            grouped.setdefault(str(item.get("care_episode_id")), set()).add(
                int(item.get("version", 0))
            )
    for step in timeline:
        if step.get("op") == "save_triage_summary":
            grouped.setdefault(
                str(step.get("care_episode_id") or "__current__"),
                set(),
            ).add(int(step.get("version", 0)))
    if any(len(versions) > 1 for versions in grouped.values()):
        return True
    return any(step.get("op") == "correct_fact" for step in timeline)


def derive_rubric_applicability(
    initial_state: Mapping[str, Any],
    timeline: list[Mapping[str, Any]],
    oracles: Mapping[str, Any],
) -> dict[str, str]:
    """Derive score coverage only where the fixture makes it decidable."""
    has_sources = _has_items(oracles.get("required_source_ids")) or _has_items(
        oracles.get("forbidden_source_ids")
    )
    has_facts = _has_items(oracles.get("required_fact_ids")) or _has_items(
        oracles.get("forbidden_fact_ids")
    )
    has_memory_boundary = bool(
        initial_state.get("patient_memory")
        or initial_state.get("episode_memory")
        or any(
            str(step.get("op"))
            in {"memory_create", "memory_edit", "memory_delete", "memory_archive", "save_triage_summary"}
            for step in timeline
        )
        or oracles.get("forbidden_raw_prior_session") is True
    )
    privacy_hard_gate = oracles.get("privacy_boundary") is True
    urgent_hard_gate = oracles.get("urgent_routing_boundary") is True

    return {
        "context_fidelity": "primary" if (has_sources or has_facts) else "not_applicable",
        "temporal_reasoning": "primary" if _has_competing_versions(initial_state, timeline) else "not_applicable",
        "grounding": (
            "primary"
            if _has_items(oracles.get("evidence_manifest"))
            or _has_items(oracles.get("expected_catalog_ids"))
            or _has_items(oracles.get("claim_sources"))
            else "not_applicable"
        ),
        "memory_appropriateness": (
            "hard_gate" if privacy_hard_gate
            else "primary" if has_memory_boundary
            else "not_applicable"
        ),
        "risk_handling": (
            "hard_gate" if urgent_hard_gate
            else "primary" if _has_items(oracles.get("risk_signals"))
            else "not_applicable"
        ),
        "uncertainty_calibration": (
            "primary"
            if _has_items(oracles.get("missing_context"))
            or _has_items(oracles.get("ambiguous_fact_ids"))
            or oracles.get("evidence_unavailable") is True
            else "not_applicable"
        ),
        "action_quality": (
            "primary"
            if oracles.get("user_goal") or _has_items(oracles.get("expected_actions"))
            else "not_applicable"
        ),
        "tool_use_quality": "primary",
        "communication_quality": (
            "secondary" if oracles.get("user_facing", True) is True else "not_applicable"
        ),
    }


_GENERIC_SCRIPT_TOKENS = frozenset(
    {
        "synthetic-final-response",
        "synthetic-evidence",
        "synthetic-evidence-token",
        "evidence-token",
        "tool-token",
    }
)


def _substantive_script_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return bool(normalized) and normalized not in _GENERIC_SCRIPT_TOKENS and not normalized.startswith("script-")


def _substantive_script_step(step: Mapping[str, Any]) -> bool:
    if "token" in step and not _substantive_script_value(step.get("token")):
        return False
    for field in ("value", "fields"):
        if field not in step:
            continue
        if not structured_script_value_is_valid(step.get(field)):
            return False
        return True
    return _substantive_script_value(step.get("token"))


def _validate_script_entries(
    script: Mapping[str, Any],
    keys: set[str],
) -> None:
    failure_outcomes = {"error", "timeout", "empty", "blocked"}
    for key in sorted(keys):
        entries = script.get(key)
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"scenario-specific script requires non-empty {key} entries")
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ValueError(f"scenario-specific script {key}[{index}] must be an object")
            outcome = str(entry.get("outcome") or "").strip().lower()
            if key == "tools":
                if outcome in failure_outcomes:
                    continue
                if not _substantive_script_step(entry):
                    raise ValueError(
                        f"scenario-specific script {key}[{index}] requires a substantive evidence token"
                    )
                continue
            if key in {"consultant", "consultant_repair"} or outcome not in failure_outcomes:
                if not _substantive_script_step(entry):
                    raise ValueError(
                        f"scenario-specific script {key}[{index}] requires a substantive response token"
                    )


def _validate_behavior_contract(
    timeline: tuple[dict[str, Any], ...],
    oracles: Mapping[str, Any],
    applicability: Mapping[str, str],
    script: Mapping[str, Any],
) -> None:
    if not any(step.get("op") in {"turn", "resume"} for step in timeline):
        return
    if script:
        required_keys = {"router"}
        route = oracles.get("expected_route")
        if route == "consultant":
            required_keys.update({"consultant", "safety_reviewer", "grounding_reviewer", "tools"})
        elif route == "clarify":
            required_keys.add("clarification")
        if any(step.get("op") == "resume" for step in timeline):
            required_keys.update({"safety_reviewer", "grounding_reviewer", "tools"})
        missing_keys = sorted(key for key in required_keys if not script.get(key))
        if missing_keys:
            raise ValueError(
                "scenario-specific script is missing behavior boundary: "
                + ", ".join(missing_keys)
            )
        _validate_script_entries(script, set(script))
    if oracles.get("user_facing", True) is True and not _has_items(
        oracles.get("expected_response_contains")
    ):
        raise ValueError("user-facing behavior requires expected_response_contains")
    if applicability.get("action_quality") != "not_applicable" and not _has_items(
        oracles.get("expected_actions")
    ):
        raise ValueError("action-quality behavior requires expected_actions")
    forbidden_defaults = {"synthetic-final-response", "synthetic-evidence"}
    emitted_tokens = {
        str(item.get("token"))
        for calls in script.values()
        for item in calls
        if isinstance(item, Mapping) and item.get("token") is not None
    }
    if forbidden_defaults.intersection(emitted_tokens):
        raise ValueError("scenario-specific script contains a generic default output")


@dataclass(frozen=True)
class ScenarioV2:
    schema_version: int
    scenario_id: str
    initial_state: dict[str, Any]
    timeline: tuple[dict[str, Any], ...]
    oracles: dict[str, Any]
    rubric_applicability: dict[str, str]
    script: dict[str, Any]

    @classmethod
    def from_path(cls, path: str | Path) -> "ScenarioV2":
        scenario_path = Path(path)
        schema_path = scenario_path.parents[2] / "schema" / "longitudinal.schema.json"
        return cls.from_payload(
            json.loads(scenario_path.read_text(encoding="utf-8")),
            schema_path=schema_path,
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        schema_path: str | Path | None = None,
    ) -> "ScenarioV2":
        if not isinstance(payload, Mapping):
            raise ValueError("longitudinal scenario payload must be an object")
        schema_file = Path(schema_path) if schema_path is not None else (
            Path(__file__).parents[1] / "schema" / "longitudinal.schema.json"
        )
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:
            raise RuntimeError("jsonschema is required to load longitudinal scenarios") from exc
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(payload),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            path_text = ".".join(str(part) for part in errors[0].absolute_path) or "scenario"
            raise ValueError(
                f"longitudinal scenario schema validation failed at {path_text}: "
                f"{errors[0].validator}"
            )
        initial_state = dict(payload["initial_state"])
        timeline = tuple(dict(step) for step in payload["timeline"])
        oracles = dict(payload["oracles"])
        script = dict(payload.get("script", {}))
        validate_timeline_semantics(initial_state, list(timeline), oracles, script)
        applicability = derive_rubric_applicability(initial_state, list(timeline), oracles)
        declared_applicability = payload.get("rubric_applicability")
        if declared_applicability is not None and dict(declared_applicability) != applicability:
            raise ValueError(
                "longitudinal rubric applicability must equal the mapping derived from oracle fields"
            )
        _validate_behavior_contract(timeline, oracles, applicability, script)
        if set(applicability) != set(RUBRIC_DIMENSIONS):
            raise ValueError("longitudinal rubric applicability must cover every dimension")
        if any(level not in RUBRIC_LEVELS for level in applicability.values()):
            raise ValueError("longitudinal rubric applicability contains an unknown level")
        return cls(
            schema_version=int(payload["schema_version"]),
            scenario_id=str(payload["scenario_id"]),
            initial_state=initial_state,
            timeline=timeline,
            oracles=oracles,
            rubric_applicability=applicability,
            script=script,
        )

    @property
    def primary_dimensions(self) -> tuple[str, ...]:
        return tuple(
            dimension
            for dimension in RUBRIC_DIMENSIONS
            if self.rubric_applicability[dimension] == "primary"
        )

    @property
    def hard_gate_dimensions(self) -> tuple[str, ...]:
        return tuple(
            dimension
            for dimension in RUBRIC_DIMENSIONS
            if self.rubric_applicability[dimension] == "hard_gate"
        )
