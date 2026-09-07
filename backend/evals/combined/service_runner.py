"""Primary evaluator path for conversations against the real RehabFlow service."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote

from evals.combined.service import FAULT_PROFILES, ServiceHarness, compare_budget_usage
from evals.combined.tool_metrics import (
    ToolCallRecord,
    aggregate_tool_metrics,
    group_tool_metrics,
    records_from_backend_telemetry,
    select_tool_telemetry_records,
)
from evals.longitudinal.result import LongitudinalResult
from evals.longitudinal.contract import RUBRIC_DIMENSIONS, ScenarioV2
from evals.longitudinal.report import build_case_report, observed_evidence_manifest
from evals.longitudinal.judge import evaluate_with_repair
from evals.real_model.run import _tokens


@dataclass(frozen=True)
class ServiceIdentity:
    patient_id: str
    token: str
    episode_ids: Mapping[str, str]
    memory_ids: Mapping[str, str] = field(default_factory=dict)
    memory_scopes: Mapping[str, str] = field(default_factory=dict)
    source_id_map: Mapping[str, str] = field(default_factory=dict)
    fixture_setup: Mapping[str, Any] = field(default_factory=dict)
    case_manifest: Any | None = None
    case_resources: Any | None = None
    evaluator_db_control: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    record_memory_item: Callable[[str, str], None] | None = None
    record_ai_session: Callable[[str], None] | None = None
    record_triage_summary: Callable[[str, str], None] | None = None
    load_persisted_messages: Callable[[], Mapping[str, Any]] | None = None


@dataclass(frozen=True)
class _RealModelFixtureScenario:
    scenario_id: str
    initial_state: dict[str, Any]


_REAL_MODEL_PUBLIC_EVIDENCE_BY_CASE: dict[str, str] = {
    "synthetic-general-rehab": (
        "General knee rehabilitation is usually progressed from comfortable motion, "
        "to strength and control, and then to task-specific loading. The person's goal "
        "such as daily function, work, fitness, or sport determines which tasks are added. "
        "Progress only when the current level does not cause increasing pain, swelling, "
        "limping, loss of motion, or reduced function. Worsening pain or swelling, "
        "giving way, catching, or locking should slow or pause progression and may need "
        "clinical review."
    ),
    "synthetic-grounded-repair": (
        "General restart guidance after inactivity is to use comfortable, low-intensity "
        "range of motion and exercises that were previously taught or cleared. A "
        "conservative example is 5 to 10 minutes of easy walking, one set of 8 to 10 "
        "comfortable repetitions, and 1 to 2 minutes of rest between exercises. This "
        "is an example rather than an individualized prescription. Increase only one "
        "variable after several sessions without worsening during the session or later "
        "that day. Stop or back off for sharp or increasing pain, new swelling, warmth "
        "or redness, limping, difficulty bearing weight, giving way, catching, or "
        "locking; contact a physical therapist or clinician if symptoms persist or worsen."
    ),
    "synthetic-cold-knee-stiffness": (
        "For stiffness without pain or other warning signs, a brief comfortable warm-up "
        "and gentle knee range-of-motion before activity may be reasonable. Move slowly "
        "and do not force the joint. Stop and seek clinical advice if new pain, swelling, "
        "warmth, redness, limping, locking, or giving way develops or if stiffness is "
        "new and worsening."
    ),
    "synthetic-postoperative-check": (
        "After knee surgery, follow the surgeon's incision and activity instructions. "
        "Keep the incision clean and dry as directed, wash hands before touching it, "
        "and avoid picking or friction. Small localized pinkness can be monitored only "
        "when it is not spreading and pain is improving without fever or drainage. "
        "Contact the surgeon for spreading redness, increasing warmth, swelling, or pain, "
        "new drainage, fever or chills, an opening incision, or worsening ability to walk. "
        "New calf pain or swelling or breathing symptoms need prompt medical attention."
    ),
    "synthetic-clinician-cleared-progression": (
        "After an ankle fracture, weight bearing should follow the treating clinician's "
        "instructions. Once full weight bearing is cleared, begin with short supported "
        "walks on a flat surface and use any prescribed boot, brace, cane, or crutch. "
        "Increase walking little by little when pain stays mild and swelling does not "
        "increase. Stop or reduce activity for worsening pain, new or increasing swelling, "
        "numbness or tingling, a worsening limp, trouble bearing weight, or a new injury, "
        "and contact the clinician if those changes occur."
    ),
}

_DEFAULT_REAL_MODEL_PUBLIC_EVIDENCE = (
    "This public rehabilitation reference supports comfortable movement, gradual "
    "progression, and stopping or seeking clinical advice when pain, swelling, "
    "function, or other symptoms worsen. It is general educational information, "
    "not individualized medical clearance or a universal exercise dose."
)


def _real_model_fixture_content(case_id: object) -> str:
    normalized = str(case_id or "").strip()
    return _REAL_MODEL_PUBLIC_EVIDENCE_BY_CASE.get(
        normalized,
        _DEFAULT_REAL_MODEL_PUBLIC_EVIDENCE,
    )


def _real_model_fixture_scenario(case: Mapping[str, Any]) -> _RealModelFixtureScenario:
    content = _real_model_fixture_content(case.get("case_id"))
    evidence = []
    for raw_id in case.get("authorized_evidence", []) or []:
        source_id = str(raw_id or "").strip()
        if source_id:
            evidence.append({
                "id": source_id,
                "content": content,
                "authorized": True,
                "observed": True,
            })
    return _RealModelFixtureScenario(
        scenario_id=str(case.get("case_id") or "unknown-case"),
        initial_state={"evidence": evidence, "catalog": []},
    )


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


def _safe_unique_source_values(values: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        _privacy_safe_source_id(value)
        for value in _unique_values(values)
    ))


def _source_id_map(identity: ServiceIdentity) -> dict[str, str]:
    mapping = {
        str(raw): str(logical)
        for raw, logical in (identity.source_id_map or {}).items()
        if str(raw).strip() and str(logical).strip()
    }
    manifest = identity.case_manifest
    for prefix, attribute in (
        ("care_episode", "care_episode_ids"),
        ("episode", "care_episode_ids"),
        ("memory_item", "memory_item_ids"),
        ("triage_summary", "triage_summary_ids"),
        ("conversation", "ai_session_ids"),
    ):
        values = getattr(manifest, attribute, {}) if manifest is not None else {}
        if not isinstance(values, Mapping):
            continue
        for logical, physical in values.items():
            raw = f"{prefix}:{physical}"
            mapping.setdefault(raw, f"{prefix}:{logical}")
    memory_scopes = getattr(identity, "memory_scopes", {}) or {}
    if isinstance(memory_scopes, Mapping):
        for raw_source, scope in (
            ("memory:patient", "patient"),
            ("memory:episode:level1", "episode"),
        ):
            candidates = [
                str(logical_id)
                for logical_id, logical_scope in memory_scopes.items()
                if str(logical_scope) == scope
            ]
            if len(candidates) == 1:
                logical_source = f"memory_item:{candidates[0]}"
                mapping.setdefault(raw_source, logical_source)
                mapping.setdefault(_privacy_safe_source_id(raw_source), logical_source)
    return mapping


def _logical_source_id(value: object, identity: ServiceIdentity) -> str | None:
    source_id = str(value or "").strip()
    if not source_id:
        return None
    mapping = _source_id_map(identity)
    if source_id in _PRIVACY_SAFE_SOURCE_IDS:
        return source_id
    for raw, logical in mapping.items():
        if source_id == raw or source_id == _privacy_safe_source_id(raw):
            return logical
    if source_id in set(mapping.values()):
        return source_id
    return None


def _logical_source_ids(values: object, identity: ServiceIdentity) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        logical
        for value in _unique_values(values)
        if (logical := _logical_source_id(value, identity)) is not None
    ))


_CONTEXT_BLOCK_RE = re.compile(
    r"(?ms)^Source:\s*(?P<source>[^\n]+)\n(?P<body>.*?)(?=^Source:\s|^Included sources:|^Current user request:|^Safety Feedback From Previous Attempt:|\Z)"
)


def _context_model_inputs(persisted_messages: Mapping[str, Any]) -> tuple[str, ...]:
    """Return consultant context packets persisted by the live service."""
    raw_internal = persisted_messages.get("ai_internal_messages", [])
    if not isinstance(raw_internal, (list, tuple)):
        return ()
    contexts: list[str] = []
    for item in raw_internal:
        if not isinstance(item, Mapping):
            continue
        agent = str(item.get("agent") or "").lower()
        if "consultant" not in agent:
            continue
        model_input = item.get("model_input")
        if not isinstance(model_input, str):
            continue
        try:
            messages = json.loads(model_input)
        except (TypeError, ValueError):
            continue
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            role = str(message.get("role") or "").lower()
            if (
                role in {"human", "user"}
                and isinstance(content, str)
                and "Assembled Context for consultant:" in content
            ):
                contexts.append(content)
    return tuple(contexts)


def _context_source_id(raw_source: str, identity: ServiceIdentity | None) -> str:
    if identity is not None:
        logical = _logical_source_id(raw_source, identity)
        if logical is not None:
            return logical
    return _privacy_safe_source_id(raw_source)


def _tool_source_id(
    tool_name: str,
    raw_arguments: object,
    identity: ServiceIdentity | None,
) -> str | None:
    if identity is None or tool_name != "search_session_conversation":
        return None
    if not isinstance(raw_arguments, Mapping):
        return None
    session_id = str(raw_arguments.get("session_id") or "").strip()
    if not session_id:
        return None
    return _logical_source_id(f"conversation:{session_id}", identity)


def _context_runtime_events(
    case_id: str,
    persisted_messages: Mapping[str, Any],
    identity: ServiceIdentity | None,
) -> list[dict[str, Any]]:
    """Project auto-loaded context as evidence without exposing physical IDs."""
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(source_id: str, content: object) -> None:
        safe_content = _runtime_safe_text(content, limit=2400)
        if not source_id or safe_content is None:
            return
        key = (source_id, safe_content)
        if key in seen:
            return
        seen.add(key)
        projected.append({
            "sequence": 0,
            "event_type": "context_observed",
            "source": "live_service_context",
            "case_id": case_id,
            "source_id": source_id,
            "outcome": "ok_nonempty",
            "result": {"count": 1, "content": safe_content},
            "unavailable_reasons": {},
        })

    for context in _context_model_inputs(persisted_messages):
        for match in _CONTEXT_BLOCK_RE.finditer(context):
            raw_source = match.group("source").strip()
            card_content = match.group("body").strip()
            source_id = _context_source_id(raw_source, identity)
            add(source_id, card_content)
            if raw_source != "memory:patient":
                continue
            fields_match = re.search(
                r"(?ms)^Editable fields:\s*(\{.*\})\s*$",
                card_content,
            )
            if fields_match is None:
                continue
            try:
                fields = json.loads(fields_match.group(1))
            except (TypeError, ValueError):
                continue
            if not isinstance(fields, Mapping):
                continue
            memory_scopes = getattr(identity, "memory_scopes", {}) if identity is not None else {}
            for logical_id, value in fields.items():
                if (
                    isinstance(memory_scopes, Mapping)
                    and str(logical_id) in memory_scopes
                    and str(memory_scopes[str(logical_id)]) == "patient"
                ):
                    add(f"memory_item:{logical_id}", value)
    compaction_evidence = persisted_messages.get("compaction_evidence", [])
    if isinstance(compaction_evidence, (list, tuple)):
        for evidence in compaction_evidence:
            if not isinstance(evidence, Mapping) or evidence.get("compacted") is not True:
                continue
            session = str(evidence.get("session") or "session")
            watermark = evidence.get("source_watermark")
            watermark_text = json.dumps(watermark, sort_keys=True) if isinstance(watermark, Mapping) else "{}"
            add(
                f"conversation:{session}",
                "Live session compaction completed; "
                f"raw_messages={int(evidence.get('raw_message_count') or 0)}, "
                f"source_watermark={watermark_text}, "
                f"rolling_block_chars={int(evidence.get('rolling_block_length') or 0)}.",
            )
    return projected
def _response_text(payload: object) -> str:
    if isinstance(payload, Mapping):
        return str(payload.get("response") or payload.get("final_response") or "")
    content = getattr(payload, "content", None)
    if content is not None:
        return str(content)
    return str(payload or "")


def _persisted_message_payload(identity: ServiceIdentity) -> tuple[dict[str, Any], str | None]:
    empty = {"ai_messages": [], "ai_internal_messages": []}
    loader = identity.load_persisted_messages
    if loader is None:
        return empty, None
    try:
        payload = loader()
    except Exception as exc:
        return empty, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, Mapping):
        return empty, "persisted message loader returned an invalid payload"
    normalized = {
        "ai_messages": list(payload.get("ai_messages", []) or []),
        "ai_internal_messages": list(payload.get("ai_internal_messages", []) or []),
    }
    for key in ("message_capture_status", "message_capture", "compaction_evidence"):
        if key in payload:
            normalized[key] = payload[key]
    return normalized, None


def _persisted_message_report_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("message_capture_status", "message_capture", "compaction_evidence")
        if key in payload
    }


def _safe_scalar(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_trace(payload: Mapping[str, Any]) -> dict[str, Any]:
    telemetry = payload.get("evaluation_telemetry")
    telemetry = telemetry if isinstance(telemetry, Mapping) else {}
    allowed = {
        key: _safe_scalar(telemetry.get(key))
        for key in (
            "model_call_attempts",
            "tool_call_attempts",
            "total_tokens",
            "repair_attempts",
            "elapsed_ms",
            "evidence_fanout",
            "execution_failures",
        )
        if key in telemetry
    }
    policy = telemetry.get("budget")
    if isinstance(policy, Mapping):
        allowed["budget"] = {
            str(key): value
            for key, value in policy.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    for key in ("selected_source_ids", "evidence_source_ids", "reviewer_outcomes"):
        if key in telemetry:
            values = telemetry.get(key)
            allowed[key] = list(
                _safe_unique_source_values(values)
                if key != "reviewer_outcomes"
                else _unique_values(values)
            )
    tool_calls = select_tool_telemetry_records(telemetry)
    if isinstance(tool_calls, (list, tuple)):
        allowed["tool_calls"] = [
            {
                key: _safe_scalar(item.get(key))
                for key in (
                    "call_id",
                    "tool_name",
                    "attempt",
                    "outcome",
                    "latency_ms",
                    "result_count",
                    "failure_category",
                    "retry_of",
                    "retry_count",
                )
                if key in item
            }
            for item in tool_calls
            if isinstance(item, Mapping)
        ]
    debug_events = payload.get("debug_trace")
    if isinstance(debug_events, (list, tuple)):
        safe_events: list[dict[str, object]] = []
        for event in debug_events:
            if not isinstance(event, Mapping):
                continue
            item = {
                key: _safe_scalar(event.get(key))
                for key in ("step", "node", "attempt", "route", "routing_decision", "failure_category")
                if key in event
            }
            packet_tokens = event.get("context_packet_tokens")
            if isinstance(packet_tokens, Mapping):
                item["context_packet_tokens"] = {
                    str(node): int(tokens)
                    for node, tokens in packet_tokens.items()
                    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0
                }
            safe_events.append(item)
        allowed["debug_events"] = safe_events
    return {
        "routing_decision": _safe_scalar(payload.get("routing_decision")),
        "tool_calling_used": _safe_scalar(payload.get("tool_calling_used")),
        "evaluation_telemetry": allowed,
    }


_RUNTIME_UUID_RE = re.compile(
    r"(?<![A-Za-z0-9])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_RUNTIME_IDENTITY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:patient|session|care[_-]?episode|episode)[-_:][A-Za-z0-9_.:/-]+"
)
_RUNTIME_SECRET_RE = re.compile(
    r"(?i)(authorization|bearer|api[_-]?key|password|secret|credential|token)\s*[:=]\s*[^\s,;]+"
)


def _runtime_safe_text(value: object, *, limit: int = 1200) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _RUNTIME_SECRET_RE.sub(r"\1=[REDACTED]", value.strip())
    normalized = _RUNTIME_UUID_RE.sub("[REDACTED_ID]", normalized)
    normalized = _RUNTIME_IDENTITY_RE.sub("[REDACTED_ID]", normalized)
    return normalized[:limit] if normalized else None


def _runtime_safe_tool_name(value: object) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:/-]", "_", str(value or "unknown_tool"))
    return normalized[:128] or "unknown_tool"


def _runtime_safe_arguments(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    arguments: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key in {"query", "limit"} and isinstance(raw_value, (str, int)) and not isinstance(raw_value, bool):
            if key == "query":
                safe_value = _runtime_safe_text(raw_value, limit=600)
                if safe_value is not None:
                    arguments[key] = safe_value
            else:
                arguments[key] = max(0, min(int(raw_value), 1000))
        elif key == "session_id" and isinstance(raw_value, str) and raw_value.strip():
            arguments[key] = "[REDACTED_ID]"
        elif key == "queries" and isinstance(raw_value, (list, tuple)):
            safe_queries = [
                safe
                for item in raw_value
                if (safe := _runtime_safe_text(item, limit=300)) is not None
            ]
            if safe_queries:
                arguments[key] = safe_queries[:16]
    return arguments


def _runtime_outcome(value: object, result_count: object = None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"ok", "success", "completed"}:
        return "ok_nonempty" if isinstance(result_count, int) and result_count > 0 else "ok_empty"
    if normalized in {"empty", "no_results"}:
        return "ok_empty"
    if normalized in {"blocked", "reject", "rejected", "policy_rejection"}:
        return "blocked"
    if normalized in {"invalid", "invalid_request"}:
        return "invalid"
    if normalized in {"timeout", "timed_out"}:
        return "timeout"
    if normalized in {"ok_nonempty", "ok_empty", "error"}:
        return normalized
    return "error"



def _budget_comparison(checks: list[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [
        dict(item)
        for item in checks
        if isinstance(item, Mapping)
    ]
    if len(normalized) != len(checks) or not normalized:
        status = "unavailable"
    elif any(item.get("unavailable") is True for item in normalized):
        status = "unavailable"
    elif all(item.get("compliant") is True for item in normalized):
        status = "compliant"
    else:
        status = "noncompliant"
    return {
        "status": status,
        "compliant": status == "compliant",
        "unavailable": status == "unavailable",
        "checks": normalized,
    }

def _runtime_events_from_live_trace(
    case_id: str,
    persisted_messages: Mapping[str, Any],
    records: list[ToolCallRecord],
    identity: ServiceIdentity | None = None,
) -> list[dict[str, Any]]:
    """Project persisted service tool-result events plus telemetry into safe ordered evidence."""
    raw_internal = persisted_messages.get("ai_internal_messages", [])
    candidates: list[Mapping[str, Any]] = []
    if isinstance(raw_internal, (list, tuple)):
        for item in raw_internal:
            if not isinstance(item, Mapping):
                continue
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            event = str(item.get("event") or metadata.get("event") or "").lower()
            if event in {"tool_call", "tool_result"} or metadata.get("tool_name"):
                candidates.append(item)

    normalized_records = [
        record if isinstance(record, ToolCallRecord) else ToolCallRecord(**dict(record))
        for record in records
    ]
    used_record_indexes: set[int] = set()
    projected: list[dict[str, Any]] = []

    def find_record(tool_name: str, attempt: int) -> ToolCallRecord | None:
        for index, record in enumerate(normalized_records):
            if index in used_record_indexes:
                continue
            if record.tool_name == tool_name and record.attempt == attempt:
                used_record_indexes.add(index)
                return record
        for index, record in enumerate(normalized_records):
            if index in used_record_indexes:
                continue
            if record.tool_name == tool_name:
                used_record_indexes.add(index)
                return record
        return None

    def add_pair(
        *,
        raw_item: Mapping[str, Any] | None,
        record: ToolCallRecord | None,
        ordinal: int,
    ) -> None:
        metadata = raw_item.get("metadata") if isinstance(raw_item, Mapping) else {}
        metadata = metadata if isinstance(metadata, Mapping) else {}
        raw_name = (
            metadata.get("tool_name")
            or (raw_item.get("tool_name") if isinstance(raw_item, Mapping) else None)
            or (record.tool_name if record is not None else "unknown_tool")
        )
        tool_name = _runtime_safe_tool_name(raw_name)
        raw_attempt = metadata.get("attempt") or (raw_item.get("attempt") if isinstance(raw_item, Mapping) else None)
        attempt = raw_attempt if isinstance(raw_attempt, int) and raw_attempt > 0 else (record.attempt if record else 1)
        raw_count = metadata.get("result_count", metadata.get("count"))
        result_count = raw_count if isinstance(raw_count, int) and raw_count >= 0 else (
            record.result_count if record is not None else None
        )
        outcome = record.outcome if record is not None else _runtime_outcome(
            metadata.get("outcome") or (raw_item.get("outcome") if isinstance(raw_item, Mapping) else None),
            result_count,
        )
        turn_correlation = (
            record.turn_correlation
            if record is not None
            else f"{case_id}:turn-observed-{ordinal}"
        )
        call_id = (
            record.call_id
            if record is not None
            else f"{case_id}:live-tool-{ordinal}"
        )
        arguments = _runtime_safe_arguments(
            metadata.get("tool_arguments")
            or (raw_item.get("tool_arguments") if isinstance(raw_item, Mapping) else None)
        )
        raw_arguments = (
            metadata.get("tool_arguments")
            or (raw_item.get("tool_arguments") if isinstance(raw_item, Mapping) else None)
        )
        source_id = _tool_source_id(tool_name, raw_arguments, identity)
        latency = metadata.get("latency_ms")
        latency_ms = latency if isinstance(latency, int) and latency >= 0 else (
            record.latency_ms if record is not None else None
        )
        failure_category = (
            record.failure_category
            if record is not None
            else _runtime_safe_text(metadata.get("failure_category"), limit=120)
        )
        retry_of = record.retry_of if record is not None else (
            str(metadata["retry_of"]) if metadata.get("retry_of") is not None else None
        )
        retry_count = record.retry_count if record is not None else (
            metadata.get("retry_count") if isinstance(metadata.get("retry_count"), int) else None
        )
        unavailable_call: dict[str, str] = {}
        if not arguments:
            unavailable_call["arguments"] = "service_persistence_omitted_tool_arguments"
        if latency_ms is None:
            unavailable_call["latency_ms"] = "service_runtime_latency_unavailable"
        if retry_of is None and attempt > 1:
            unavailable_call["retry_relationship"] = "service_retry_parent_unavailable"
        call_event: dict[str, Any] = {
            "sequence": 0,
            "event_type": "tool_call",
            "source": "live_service_runtime",
            "case_id": case_id,
            "turn_correlation": turn_correlation,
            "call_id": call_id,
            "tool_name": tool_name,
            "attempt": attempt,
            "outcome": outcome,
            "arguments": arguments,
            "unavailable_reasons": unavailable_call,
        }
        if source_id is not None:
            call_event["source_id"] = source_id
        if latency_ms is not None:
            call_event["latency_ms"] = latency_ms
        if retry_of is not None:
            call_event["retry_of"] = retry_of
        if retry_count is not None:
            call_event["retry_count"] = retry_count
        projected.append(call_event)

        raw_content = raw_item.get("content") if isinstance(raw_item, Mapping) else None
        safe_content = _runtime_safe_text(raw_content, limit=24_000)
        result_payload: dict[str, Any] = {}
        unavailable_result: dict[str, str] = {}
        if result_count is not None and outcome in {"ok_nonempty", "ok_empty"}:
            result_payload["count"] = result_count
        if safe_content is not None:
            result_payload["content"] = safe_content
        if not result_payload and outcome in {"ok_nonempty", "ok_empty"}:
            unavailable_result["result"] = "service_persistence_omitted_tool_result_content"
        error_payload: dict[str, str] = {}
        if outcome not in {"ok_nonempty", "ok_empty"}:
            if failure_category:
                error_payload["category"] = failure_category
            else:
                unavailable_result["error"] = "service_runtime_error_detail_unavailable"
        if not arguments:
            unavailable_result["arguments"] = "service_persistence_omitted_tool_arguments"
        if latency_ms is None:
            unavailable_result["latency_ms"] = "service_runtime_latency_unavailable"
        if retry_of is None and attempt > 1:
            unavailable_result["retry_relationship"] = "service_retry_parent_unavailable"
        result_event: dict[str, Any] = {
            "sequence": 0,
            "event_type": "tool_result",
            "source": "live_service_runtime",
            "case_id": case_id,
            "turn_correlation": turn_correlation,
            "call_id": call_id,
            "tool_name": tool_name,
            "attempt": attempt,
            "outcome": outcome,
            "unavailable_reasons": unavailable_result,
        }
        if source_id is not None:
            result_event["source_id"] = source_id
        if result_payload:
            result_event["result"] = result_payload
        if error_payload:
            result_event["error"] = error_payload
        if latency_ms is not None:
            result_event["latency_ms"] = latency_ms
        if retry_of is not None:
            result_event["retry_of"] = retry_of
        if retry_count is not None:
            result_event["retry_count"] = retry_count
        projected.append(result_event)

    for ordinal, raw_item in enumerate(candidates, start=1):
        metadata = raw_item.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        name = _runtime_safe_tool_name(metadata.get("tool_name") or raw_item.get("tool_name"))
        raw_attempt = metadata.get("attempt") or raw_item.get("attempt")
        attempt = raw_attempt if isinstance(raw_attempt, int) and raw_attempt > 0 else 1
        add_pair(raw_item=raw_item, record=find_record(name, attempt), ordinal=ordinal)
    for index, record in enumerate(normalized_records, start=1):
        if index - 1 in used_record_indexes:
            continue
        add_pair(raw_item=None, record=record, ordinal=len(candidates) + index)
    if identity is not None:
        projected.extend(_context_runtime_events(case_id, persisted_messages, identity))
    for sequence, event in enumerate(projected, start=1):
        event["sequence"] = sequence
    return projected


def _logical_session_alias(
    identity: ServiceIdentity,
    physical_session_id: str | None,
) -> str | None:
    if not physical_session_id or identity.case_manifest is None:
        return None
    session_ids = getattr(identity.case_manifest, "ai_session_ids", {})
    if not isinstance(session_ids, Mapping):
        return None
    for logical_id, physical_id in session_ids.items():
        if str(physical_id) == str(physical_session_id):
            return str(logical_id)
    return None


def _persisted_assistant_response(
    persisted_messages: Mapping[str, Any],
    *,
    session_alias: str | None = None,
) -> str | None:
    messages = persisted_messages.get("ai_messages", [])
    if not isinstance(messages, (list, tuple)):
        return None
    for item in reversed(messages):
        if not isinstance(item, Mapping) or str(item.get("role") or "").lower() != "assistant":
            continue
        if session_alias is not None and str(item.get("session") or "") != session_alias:
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _task_context_for_case(
    case_id: str,
    messages: list[Mapping[str, Any]],
    *,
    scenario: ScenarioV2 | None = None,
    selected_session_id: str | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "case_id": case_id,
        "messages": [
            {"turn": index, "content": str(item.get("content") or "")[:4000]}
            for index, item in enumerate(messages, start=1)
        ],
    }
    if scenario is not None:
        context.update({
            "scenario_id": scenario.scenario_id,
            "user_goal": scenario.oracles.get("user_goal"),
            "case_condition": scenario.oracles.get("case_condition"),
            "information_need": scenario.oracles.get("information_need"),
            "target_boundary": scenario.oracles.get("target_boundary"),
        })
    if selected_session_id:
        context["selected_session_id"] = selected_session_id
    return context


def _unique_values(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    try:
        return tuple(dict.fromkeys(
            str(value) for value in values  # type: ignore[union-attr]
            if str(value).strip()
        ))
    except TypeError:
        return ()


def _observed_nodes(payload: Mapping[str, Any]) -> tuple[str, ...]:
    events = payload.get("debug_trace")
    if not isinstance(events, (list, tuple)):
        return ()
    return _unique_values(
        event.get("node")
        for event in events
        if isinstance(event, Mapping) and event.get("node")
    )


def _effective_budget(
    backend_policy: object,
    declared: object,
) -> dict[str, object]:
    backend = dict(backend_policy) if isinstance(backend_policy, Mapping) else {}
    ceiling = dict(declared) if isinstance(declared, Mapping) else {}
    merged = dict(backend)
    for key in ("max_model_calls", "max_tool_calls", "max_total_tokens", "max_repairs"):
        limit = ceiling.get(key)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
            current = merged.get(key)
            if isinstance(current, int) and not isinstance(current, bool):
                merged[key] = min(current, limit)
            else:
                merged[key] = limit
    elapsed = ceiling.get("max_elapsed_ms")
    if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
        seconds = max(1, elapsed // 1000)
        current = merged.get("deadline_seconds")
        merged["deadline_seconds"] = (
            min(int(current), seconds)
            if isinstance(current, int) and not isinstance(current, bool)
            else seconds
        )
    return merged


def _declared_budget(source: object) -> Mapping[str, object]:
    if not isinstance(source, Mapping):
        return {}
    for key in ("budget", "budgets", "scenario_budget"):
        value = source.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _fault_profile(source: object) -> str | None:
    if not isinstance(source, Mapping):
        return None
    explicit = source.get("fault_profile")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    for profile in FAULT_PROFILES:
        flag = profile

        if source.get(flag) is True:
            return profile
    return None


def _idempotency_key(namespace: str, ordinal: int) -> str:
    digest = hashlib.sha256(f"{namespace}:turn:{ordinal}".encode("utf-8")).hexdigest()
    return f"eval-{digest[:32]}"


def _episode_payload(case_id: str, logical_id: str, body_area: str) -> dict[str, str]:
    return {
        "issue_title": f"Evaluation {case_id} {logical_id}",
        "body_area": body_area[:128] or "synthetic",
        "goal": "Evaluate the real backend conversation boundary",
        "short_description": "Synthetic non-clinical evaluator Care Episode.",
    }


def provision_service_identity(
    harness: ServiceHarness,
    case_id: str,
    scenario: ScenarioV2 | None,
    *,
    fixture_store: Any | None = None,
) -> ServiceIdentity:
    nonce = uuid.uuid4().hex
    if fixture_store is not None:
        fixture = fixture_store.provision_case(case_id, scenario)
        return ServiceIdentity(
            patient_id=fixture.patient_id,
            token=fixture.token,
            episode_ids=fixture.episode_ids,
            memory_ids=fixture.memory_ids,
            memory_scopes=fixture.memory_scopes,
            source_id_map=fixture.source_id_map,
            fixture_setup=fixture.fixture_setup,
            case_manifest=fixture.case_manifest,
            case_resources=fixture.case_resources,
            evaluator_db_control=fixture.evaluator_db_control,
            record_memory_item=fixture.record_memory_item,
            record_ai_session=fixture.record_ai_session,
            record_triage_summary=fixture.record_triage_summary,
            load_persisted_messages=fixture.load_persisted_messages,
        )
    patient_name = f"eval-{case_id}-{nonce[:12]}"
    password = f"Eval-{nonce}-password"
    status, payload = harness.request_json(
        "/auth/register/patient",
        method="POST",
        payload={
            "patient_name": patient_name,
            "password": password,
            "real_info": {"evaluation_namespace": f"eval-{case_id}"},
        },
    )
    if status not in {200, 201}:
        raise RuntimeError(f"evaluator patient provisioning failed with HTTP {status}")
    token = str(payload.get("access_token") or "")
    user = payload.get("user")
    patient_id = str(user.get("user_id") or "") if isinstance(user, Mapping) else ""
    if not token or not patient_id:
        raise RuntimeError("evaluator patient provisioning returned incomplete identity")
    episode_ids: dict[str, str] = {}
    memory_ids: dict[str, str] = {}
    fixture_setup: dict[str, Any] = {
        "transport": "authenticated_product_api",
        "care_episodes_created": 0,
        "patient_memory_created": 0,
        "unprovisioned_initial_state": [],
        "failures": [],
    }
    episode_specs = (
        list(scenario.initial_state.get("care_episodes", []))
        if scenario is not None
        else []
    )
    if not episode_specs:
        episode_specs = [{"id": "__default__", "body_area": "synthetic"}]
    for item in episode_specs:
        if not isinstance(item, Mapping):
            continue
        logical_id = str(item.get("id") or "")
        if str(item.get("status") or "active") != "active":
            fixture_setup["unprovisioned_initial_state"].append("inactive_care_episode")
            continue
        status, episode_payload = harness.request_json(
            "/care-episodes",
            method="POST",
            payload=_episode_payload(
                case_id,
                logical_id,
                str(item.get("body_area") or "synthetic"),
            ),
            token=token,
        )
        if status not in {200, 201}:
            raise RuntimeError(f"evaluator Care Episode provisioning failed with HTTP {status}")
        episode_id = str(episode_payload.get("care_episode_id") or "")
        if logical_id and episode_id:
            episode_ids[logical_id] = episode_id
            fixture_setup["care_episodes_created"] += 1
    if scenario is not None:
        for item in scenario.initial_state.get("patient_memory", []):
            if not isinstance(item, Mapping):
                continue
            logical_memory_id = str(item.get("id") or "evaluation_memory")
            status, memory_payload = harness.request_json(
                "/memory/patient-document/fields",
                method="PATCH",
                payload={"fields": {logical_memory_id: str(item.get("content") or "Synthetic evaluator memory.")}},
                token=token,
            )
            if status not in {200, 201}:
                raise RuntimeError(f"evaluator Patient Memory provisioning failed with HTTP {status}")
            actual_memory_id = str(memory_payload.get("document_id") or "")
            if logical_memory_id and actual_memory_id:
                memory_ids[logical_memory_id] = actual_memory_id
            fixture_setup["patient_memory_created"] += 1
        unsupported = {
            "episode_memory": len(scenario.initial_state.get("episode_memory", [])),
            "evidence": len(scenario.initial_state.get("evidence", [])),
            "catalog": len(scenario.initial_state.get("catalog", [])),
            "triage_summaries": len(scenario.initial_state.get("triage_summaries", [])),
        }
        for kind, count in unsupported.items():
            if count:
                fixture_setup["unprovisioned_initial_state"].append(kind)
                fixture_setup["failures"].append(f"initial_state_{kind}_not_provisioned")
    return ServiceIdentity(
        patient_id=patient_id,
        token=token,
        episode_ids=episode_ids,
        memory_ids=memory_ids,
        memory_scopes={},
        fixture_setup=fixture_setup,
    )


class ServiceCaseRunner:
    def __init__(
        self,
        harness: ServiceHarness,
        identity_factory: Callable[[ServiceHarness, str, ScenarioV2 | None], ServiceIdentity],
    ) -> None:
        self.harness = harness
        self.identity_factory = identity_factory

    def _start_session(
        self,
        identity: ServiceIdentity,
        *,
        care_episode_id: str | None,
    ) -> str:
        status, payload = self.harness.request_json(
            "/ai/chat/session",
            method="POST",
            payload={"care_episode_id": care_episode_id},
            token=identity.token,
        )
        session_id = str(payload.get("session_id") or "")
        if status >= 400 or not session_id:
            raise RuntimeError(f"AI session creation failed with HTTP {status}")
        if identity.record_ai_session is not None:
            identity.record_ai_session(session_id)
        return session_id

    def _turn(
        self,
        identity: ServiceIdentity,
        *,
        case_id: str,
        session_id: str | None,
        message: str,
        care_episode_id: str | None,
        ordinal: int,
        workflow_version: str,
        idempotency_key: str | None = None,
    ) -> tuple[str | None, dict[str, Any], list[ToolCallRecord]]:
        path = f"/ai/chat/{session_id or 'new'}"
        started = time.perf_counter()
        status, payload = self.harness.request_json(
            path,
            method="POST",
            payload={
                "message": message,
                "care_episode_id": care_episode_id,
                "idempotency_key": idempotency_key or _idempotency_key(case_id, ordinal),
                "workflow_version": workflow_version,
            },
            token=identity.token,
            timeout_seconds=180.0,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        payload = dict(payload)
        payload.setdefault("http_status", status)
        payload.setdefault("service_elapsed_ms", elapsed_ms)
        if status < 400:
            if payload.get("event") != "response":
                raise ValueError("AI chat response event is malformed")
            if not str(payload.get("session_id") or "").strip():
                raise ValueError("AI chat response session is missing")
            if not isinstance(payload.get("response"), str) or not payload["response"].strip():
                raise ValueError("AI chat assistant response is missing")
            if not isinstance(payload.get("evaluation_telemetry"), Mapping):
                payload["evaluation_telemetry"] = {
                    "unavailable": True,
                    "unavailable_reasons": [
                        "service_replay_omitted_evaluation_telemetry",
                    ],
                }
        telemetry = payload.get("evaluation_telemetry")
        telemetry = telemetry if isinstance(telemetry, Mapping) else {}
        records = records_from_backend_telemetry(
            telemetry,
            turn_correlation=f"{case_id}:turn-{ordinal}",
        )
        return (
            str(payload.get("session_id") or session_id) if status < 400 else session_id,
            payload,
            records,
        )

    def run_real_model_case(
        self,
        case: Mapping[str, Any],
        *,
        judge: object | None,
        judge_model: str,
        workflow_version: str,
        identity: ServiceIdentity | None = None,
    ) -> dict[str, Any]:
        case_id = str(case.get("case_id") or "unknown-case")
        identity = identity or self.identity_factory(
            self.harness,
            case_id,
            _real_model_fixture_scenario(case),
        )
        messages = [
            item for item in case.get("messages", [])
            if isinstance(item, Mapping) and item.get("role") == "user"
        ]
        if not messages:
            messages = [{"role": "user", "content": str(case.get("prompt") or "")}]
        transcript: list[dict[str, Any]] = []
        records: list[ToolCallRecord] = []
        care_episode_id = identity.episode_ids.get("__default__")
        session_id: str | None = self._start_session(
            identity,
            care_episode_id=care_episode_id,
        )
        responses: list[str] = []
        failures: list[str] = []
        turn_observations: list[dict[str, Any]] = []
        budget_checks: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        for ordinal, message in enumerate(messages, start=1):
            content = str(message.get("content") or "").strip()
            try:
                session_id, payload, turn_records = self._turn(
                    identity,
                    case_id=case_id,
                    session_id=session_id,
                    message=content,
                    care_episode_id=care_episode_id,
                    ordinal=ordinal,
                    idempotency_key=None,
                    workflow_version=workflow_version,
                )
            except Exception as exc:
                failures.append(f"turn_{ordinal}_{type(exc).__name__}")
                break
            records.extend(turn_records)
            transcript.extend([
                {"role": "user", "content": content, "turn": ordinal},
                {"role": "assistant", "content": _response_text(payload), "turn": ordinal},
            ])
            responses.append(_response_text(payload))
            telemetry = payload.get("evaluation_telemetry")
            telemetry = telemetry if isinstance(telemetry, Mapping) else {}
            turn_usage = {
                "model_call_attempts": telemetry.get("model_call_attempts"),
                "tool_call_attempts": telemetry.get("tool_call_attempts"),
                "total_tokens": telemetry.get("total_tokens"),
                "repair_attempts": telemetry.get("repair_attempts"),
                "elapsed_ms": telemetry.get("elapsed_ms"),
            }
            traces.append(_safe_trace(payload))
            turn_observations.append({
                "turn": ordinal,
                "session_created": ordinal == 1,
                "care_episode_bound": care_episode_id is not None,
                "route": payload.get("routing_decision"),
                "visited_nodes": list(_observed_nodes(payload)),
                "selected_source_ids": list(_safe_unique_source_values(telemetry.get("selected_source_ids", []))),
                "evidence_source_ids": list(_safe_unique_source_values(telemetry.get("evidence_source_ids", []))),
                "reviewer_outcomes": list(_unique_values(telemetry.get("reviewer_outcomes", []))),
                "tool_metrics": aggregate_tool_metrics(turn_records),
                "budget_policy": _safe_trace(payload)["evaluation_telemetry"].get("budget"),
                "budget_usage": turn_usage,
            })
            policy = _effective_budget(
                telemetry.get("budget"),
                _declared_budget(case),
            )
            budget_checks.append(compare_budget_usage(policy, turn_usage))
            if int(payload.get("http_status", 500)) >= 400:
                failures.append(f"http_{payload.get('http_status')}")
        failures.extend(
            f"budget:{reason}"
            for check in budget_checks
            for reason in check.get("reason_codes", [])
        )
        persisted_messages, persisted_message_error = _persisted_message_payload(identity)
        if persisted_message_error:
            failures.append(f"persisted_messages_{persisted_message_error.split(':', 1)[0]}")
        runtime_events = _runtime_events_from_live_trace(case_id, persisted_messages, records, identity)
        persisted_response = _persisted_assistant_response(
            persisted_messages,
            session_alias=_logical_session_alias(identity, session_id),
        )
        if persisted_response is None:
            failures.append("persisted_assistant_response_missing")
        tool_metrics = aggregate_tool_metrics(records)
        last_response = persisted_response or ""
        task_context = _task_context_for_case(case_id, messages)
        judge_payload: dict[str, Any] | None = None
        judge_status = "not_configured" if judge is None else "error"
        judge_error: str | None = None
        if judge is not None and last_response:
            try:
                manifest = observed_evidence_manifest(runtime_events)
                scenario = type(
                    "RealModelScenario",
                    (),
                    {"scenario_id": case_id, "rubric_applicability": dict(REAL_MODEL_APPLICABILITY), "oracles": {
                        "user_goal": str(case.get("prompt") or ""),
                    }},
                )()
                evaluation = evaluate_with_repair(
                    scenario=scenario,
                    response=last_response,
                    evidence_manifest=manifest,
                    invoke=lambda prompt: judge.invoke(prompt, model=judge_model),
                    runtime_events=runtime_events,
                    task_context=task_context,
                )
                judge_status = "error" if evaluation.error else ("passed" if evaluation.summary and evaluation.summary.passed else "failed")
                judge_payload = {
                    "judge": evaluation.result.to_dict() if evaluation.result else None,
                    "rubric_summary": evaluation.summary.to_dict() if evaluation.summary else None,
                    "judge_error": evaluation.error,
                    "judge_attempts": [
                        {
                            "prompt": attempt.payload,
                            "response": _response_text(attempt.raw_output) if attempt.raw_output is not None else None,
                            "error": attempt.error,
                            "usage_metadata": dict(attempt.usage_metadata or {}),
                        }
                        for attempt in evaluation.attempts
                    ],
                    "judge_prompt": evaluation.attempts[-1].payload if evaluation.attempts else None,
                    "judge_input": evaluation.attempts[0].payload if evaluation.attempts else None,
                    "judge_response": (
                        _response_text(evaluation.attempts[-1].raw_output)
                        if evaluation.attempts and evaluation.attempts[-1].raw_output is not None
                        else None
                    ),
                }
                judge_error = evaluation.error
            except Exception as exc:
                judge_error = f"{type(exc).__name__}: {exc}"
        status = (
            "completed_unjudged" if judge_status == "not_configured"
            else "completed_with_judge_error" if judge_status == "error"
            else "passed" if judge_status == "passed" else "failed"
        )
        return {
            "suite": "real_model",
            "case_id": case_id,
            "execution_mode": "real-backend-service",
            "provider_free": False,
            "status": status,
            "judge_status": judge_status,
            "judge_error": judge_error,
            "conversation": transcript,
            "ai_messages": persisted_messages["ai_messages"],
            "ai_internal_messages": persisted_messages["ai_internal_messages"],
            **_persisted_message_report_fields(persisted_messages),
            "response": last_response,
            "runtime_events": runtime_events,
            "task_context": task_context,
            "session_provenance": {
                "transport": "http",
                "session_reused": len(messages) > 1,
                "care_episode_bound": care_episode_id is not None,
            },
            "fixture_setup": dict(identity.fixture_setup),
            "resource_manifest": identity.case_manifest.privacy_safe() if identity.case_manifest is not None else None,
            "turn_observations": turn_observations,
            "budget_comparison": _budget_comparison(budget_checks),
            "tool_metrics": tool_metrics,
            "tool_metrics_by_turn": group_tool_metrics(records, key="turn_correlation"),
            "tool_calls": [record.to_dict() for record in records],
            "authorized_evidence_manifest": observed_evidence_manifest(runtime_events),
            "trace": {"turns": traces, "runtime_events": runtime_events, "task_context": task_context, "authorized_evidence_manifest": observed_evidence_manifest(runtime_events)},
            **(judge_payload or {}),
            "failures": failures,
        }

    def _memory_action(
        self,
        identity: ServiceIdentity,
        step: Mapping[str, Any],
        memory_ids: dict[str, str],
    ) -> str | None:
        operation = str(step.get("op") or "")
        logical_id = str(step.get("memory_id") or "")
        scope = str(step.get("scope") or identity.memory_scopes.get(logical_id) or "patient")
        if identity.evaluator_db_control is not None and (
            scope == "episode"
            or operation in {"memory_archive", "memory_supersede", "correct_fact"}
            or step.get("care_episode_id") is not None
        ):
            try:
                identity.evaluator_db_control(step)
            except Exception as exc:
                return f"{operation}_{type(exc).__name__}"
            return None
        if operation == "memory_create":
            if str(step.get("scope") or "patient") == "episode":
                return "episode_memory_api_unavailable"
            field_name = logical_id or "evaluation_memory"
            status, payload = self.harness.request_json(
                "/memory/patient-document/fields",
                method="PATCH",
                payload={"fields": {field_name: str(step.get("content") or "Synthetic evaluator memory.")}},
                token=identity.token,
            )
            if status >= 400:
                return f"memory_create_http_{status}"
            actual_id = str(payload.get("document_id") or "")
            if not actual_id:
                return "memory_create_response_missing_memory_item_id"
            memory_ids[logical_id] = actual_id
            if identity.record_memory_item is not None:
                identity.record_memory_item(logical_id, actual_id)
            return None
        if operation == "memory_archive":
            return "memory_archive_product_api_unavailable"
        actual_id = memory_ids.get(logical_id)
        if not actual_id:
            return f"{operation}_missing_mapping"
        if step.get("care_episode_id") is not None:
            return "episode_memory_api_unavailable"
        if operation == "memory_edit":
            field_name = logical_id or "evaluation_memory"
            payload = {"fields": {field_name: str(step.get("content") or "")}}
            status, _ = self.harness.request_json(
                "/memory/patient-document/fields",
                method="PATCH",
                payload=payload,
                token=identity.token,
            )
        elif operation == "memory_delete":
            status, _ = self.harness.request_json(
                f"/memory/patient-document/fields/{quote(logical_id or 'evaluation_memory', safe='')}",
                method="DELETE",
                token=identity.token,
            )
        else:
            return f"{operation}_not_supported_by_product_api"
        return None if status < 400 else f"{operation}_http_{status}"

    @staticmethod
    def _seeded_session_id(identity: ServiceIdentity, logical_session_id: str) -> str | None:
        if not logical_session_id:
            return None
        manifest = identity.case_manifest
        session_ids = getattr(manifest, "ai_session_ids", None)
        if not isinstance(session_ids, Mapping):
            return None
        session_id = str(session_ids.get(logical_session_id) or "").strip()
        return session_id or None

    def _selected_session_message(
        self,
        identity: ServiceIdentity,
        step: Mapping[str, Any],
    ) -> str:
        message = str(step.get("message") or "")
        logical_session_id = str(step.get("selected_session_id") or "").strip()
        physical_session_id = self._seeded_session_id(identity, logical_session_id)
        if not physical_session_id:
            return message
        return (
            f"{message}\n\n"
            f"The selected prior session identifier is {physical_session_id}. "
            "Use this exact identifier for the authorized selected-session search."
        )

    def run_longitudinal_case(
        self,
        scenario: ScenarioV2,
        *,
        judge: object | None,
        judge_model: str,
        workflow_version: str,
        identity: ServiceIdentity | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        identity = identity or self.identity_factory(self.harness, scenario.scenario_id, scenario)
        episode_ids = dict(identity.episode_ids)
        session_ids: dict[str, str] = {}
        current_session: str | None = None
        current_episode: str | None = None
        records: list[ToolCallRecord] = []
        transcript: list[dict[str, Any]] = []
        turn_observations: list[dict[str, Any]] = []
        budget_checks: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        persistence_observations: list[dict[str, Any]] = []
        persistence_outcomes: list[str] = []
        memory_writes: list[dict[str, Any]] = []
        summary_ids: dict[str, str] = {}
        failures: list[str] = list(identity.fixture_setup.get("failures", ()))
        responses: list[str] = []
        route: str | None = None
        logical_memory_ids: dict[str, str] = dict(identity.memory_ids)
        idempotency_outcome = "unique_keys"
        seen_idempotency_keys: set[str] = set()
        for ordinal, step in enumerate(scenario.timeline, start=1):
            operation = str(step.get("op") or "")
            if operation == "start_session":
                logical_session = str(step.get("session_id") or "")
                current_episode = episode_ids.get(str(step.get("care_episode_id") or ""))
                if current_episode is None:
                    failures.append("start_session_episode_not_provisioned")
                    current_session = None
                else:
                    try:
                        seeded_session = self._seeded_session_id(identity, logical_session)
                        if seeded_session:
                            current_session = seeded_session
                            persistence_observations.append({
                                "operation": "start_session",
                                "transport": "seeded_fixture",
                                "session_id_created": False,
                                "session_id_reused": True,
                                "care_episode_bound": True,
                            })
                        else:
                            current_session = self._start_session(
                                identity,
                                care_episode_id=current_episode,
                            )
                            persistence_observations.append({
                                "operation": "start_session",
                                "transport": "http",
                                "session_id_created": True,
                                "care_episode_bound": True,
                            })
                        session_ids[logical_session] = current_session
                    except Exception as exc:
                        current_session = None
                        failures.append(f"start_session_{type(exc).__name__}")
            elif operation in {"turn", "resume"}:
                logical_session = str(step.get("session_id") or "")
                if logical_session:
                    current_session = session_ids.get(logical_session) or current_session
                if not current_session:
                    failures.append(f"{operation}_{ordinal}_session_not_started")
                    continue
                try:
                    message = self._selected_session_message(identity, step)
                    current_session, payload, turn_records = self._turn(
                        identity,
                        case_id=scenario.scenario_id,
                        session_id=current_session,
                        message=message,
                        care_episode_id=current_episode,
                        ordinal=ordinal,
                        idempotency_key=(
                            str(step.get("idempotency_key") or "").strip() or None
                        ),
                        workflow_version=workflow_version,
                    )
                except Exception as exc:
                    failures.append(f"{operation}_{ordinal}_{type(exc).__name__}")
                    continue
                requested_idempotency_key = str(step.get("idempotency_key") or "").strip()
                if requested_idempotency_key:
                    if requested_idempotency_key in seen_idempotency_keys:
                        idempotency_outcome = "replayed"
                    seen_idempotency_keys.add(requested_idempotency_key)
                if logical_session:
                    session_ids[logical_session] = current_session or ""
                records.extend(turn_records)
                response = _response_text(payload)
                responses.append(response)
                transcript.extend([
                    {"role": "user", "content": message, "turn": ordinal},
                    {"role": "assistant", "content": response, "turn": ordinal},
                ])
                route_value = payload.get("routing_decision")
                route = str(route_value) if route_value else route
                telemetry = payload.get("evaluation_telemetry")
                telemetry = telemetry if isinstance(telemetry, Mapping) else {}
                traces.append(_safe_trace(payload))
                turn_usage = {
                    "model_call_attempts": telemetry.get("model_call_attempts"),
                    "tool_call_attempts": telemetry.get("tool_call_attempts"),
                    "total_tokens": telemetry.get("total_tokens"),
                    "repair_attempts": telemetry.get("repair_attempts"),
                    "elapsed_ms": telemetry.get("elapsed_ms"),
                }
                turn_observations.append({
                    "turn": ordinal,
                    "route": route,
                    "visited_nodes": list(_observed_nodes(payload)),
                    "model_call_attempts": telemetry.get("model_call_attempts"),
                    "tool_call_attempts": telemetry.get("tool_call_attempts"),
                    "total_tokens": telemetry.get("total_tokens"),
                    "repair_attempts": telemetry.get("repair_attempts"),
                    "selected_source_ids": list(_logical_source_ids(telemetry.get("selected_source_ids", []), identity)),
                    "evidence_source_ids": list(_logical_source_ids(telemetry.get("evidence_source_ids", []), identity)),
                    "reviewer_outcomes": list(_unique_values(telemetry.get("reviewer_outcomes", []))),
                    "tool_metrics": aggregate_tool_metrics(turn_records),
                    "budget_policy": _safe_trace(payload)["evaluation_telemetry"].get("budget"),
                    "budget_usage": turn_usage,
                })
                budget_checks.append(compare_budget_usage(
                    _effective_budget(
                        telemetry.get("budget"),
                        _declared_budget(scenario.oracles),
                    ),
                    turn_usage,
                ))
                if int(payload.get("http_status", 500)) >= 400:
                    failures.append(f"http_{payload.get('http_status')}")
            elif operation == "restart":
                try:
                    self.harness.restart()
                    persistence_observations.append({"operation": "restart", "transport": "process_restart"})
                except Exception as exc:
                    failures.append(f"restart_{type(exc).__name__}")
            elif operation in {"memory_create", "memory_edit", "memory_delete", "memory_archive", "memory_supersede", "correct_fact"}:
                failure = self._memory_action(identity, step, logical_memory_ids)
                memory_writes.append({"id": str(step.get("memory_id") or ""), "action": operation, "transport": "evaluator_db_control" if identity.evaluator_db_control is not None and (str(step.get("scope") or "patient") == "episode" or operation in {"memory_archive", "memory_supersede", "correct_fact"} or step.get("care_episode_id") is not None) else "authenticated_product_api", "status": "failed" if failure else "applied"})
                if failure:
                    failures.append(failure)
            elif operation == "save_triage_summary":
                episode_id = episode_ids.get(str(step.get("care_episode_id") or "")) or current_episode
                requested_summary_id = str(step.get("summary_id") or "") or None
                requested_version = step.get("version")
                summary_content = str(
                    step.get("content")
                    or step.get("additional_context")
                    or ""
                )
                summary_request = {"additional_context": summary_content}
                # Direct publication requires an originating message/event before attaching a session.
                status, summary_payload = self.harness.request_json(
                    f"/care-episodes/{episode_id}/triage-summary" if episode_id else "/care-episodes/missing/triage-summary",
                    method="POST",
                    payload=summary_request,
                    token=identity.token,
                )
                summary_id_observed = (
                    str(summary_payload.get("triage_summary_id") or "") or None
                    if isinstance(summary_payload, Mapping)
                    else None
                )
                version_observed = (
                    summary_payload.get("version")
                    if isinstance(summary_payload, Mapping)
                    else None
                )
                persistence_observations.append({
                    "operation": operation,
                    "http_status": status,
                    "summary_id_requested": requested_summary_id,
                    "summary_id_observed": bool(summary_id_observed),
                    "version_requested": requested_version,
                    "version_observed": version_observed,
                    "content_present": bool(summary_content),
                })
                summary_failure = False
                if status < 400 and isinstance(summary_payload, Mapping):
                    if requested_summary_id and summary_id_observed:
                        summary_ids[requested_summary_id] = summary_id_observed
                        if identity.record_triage_summary is not None:
                            identity.record_triage_summary(requested_summary_id, summary_id_observed)
                    elif requested_summary_id:
                        failures.append("save_triage_summary_response_unverified")
                        summary_failure = True
                    if requested_version is not None and version_observed != requested_version:
                        failures.append("save_triage_summary_version_mismatch")
                        summary_failure = True
                elif status < 400:
                    failures.append("save_triage_summary_response_unverified")
                    summary_failure = True
                persistence_outcomes.append(
                    "applied" if status < 400 and not summary_failure else "failed"
                )
                if status >= 400:
                    failures.append(f"save_triage_summary_http_{status}")
            elif operation == "advance_time":
                persistence_observations.append({"operation": operation, "control": "service_fixture_boundary", "seconds": step.get("seconds")})
            else:
                failures.append(f"unsupported_timeline_operation:{operation}")
        telemetry_usage = {
            "model_call_attempts": sum(int(item.get("model_call_attempts") or 0) for item in turn_observations if item.get("model_call_attempts") is not None),
            "tool_call_attempts": len(records),
            "total_tokens": (
                sum(int(item["total_tokens"]) for item in turn_observations if item.get("total_tokens") is not None)
                if all(item.get("total_tokens") is not None for item in turn_observations)
                else None
            ),
            "repair_attempts": sum(
                int(item.get("repair_attempts") or 0)
                for item in turn_observations
                if item.get("repair_attempts") is not None
            ),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
        failures.extend(
            f"budget:{reason}"
            for check in budget_checks
            for reason in check.get("reason_codes", [])
        )
        persisted_messages, persisted_message_error = _persisted_message_payload(identity)
        if persisted_message_error:
            failures.append(f"persisted_messages_{persisted_message_error.split(':', 1)[0]}")
        requires_compaction = any(
            isinstance(session.get("generated_context"), Mapping)
            and session["generated_context"].get("report_capture") == "bounded"
            for session in scenario.initial_state.get("sessions", [])
            if isinstance(session, Mapping)
        )
        compaction_evidence = persisted_messages.get("compaction_evidence", [])
        evidence_items = compaction_evidence if isinstance(compaction_evidence, (list, tuple)) else ()
        if requires_compaction and not any(
            isinstance(item, Mapping) and item.get("compacted") is True
            for item in evidence_items
        ):
            failures.append("session_compaction_not_observed")
        runtime_events = _runtime_events_from_live_trace(scenario.scenario_id, persisted_messages, records, identity)
        persisted_response = _persisted_assistant_response(
            persisted_messages,
            session_alias=_logical_session_alias(identity, current_session),
        )
        if persisted_response is None:
            failures.append("persisted_assistant_response_missing")
        task_messages = [
            {"content": self._selected_session_message(identity, step)}
            for step in scenario.timeline
            if step.get("op") in {"turn", "resume"}
        ]
        selected_session_id = next(
            (
                str(step.get("selected_session_id") or "").strip()
                for step in scenario.timeline
                if str(step.get("selected_session_id") or "").strip()
            ),
            None,
        )
        task_context = _task_context_for_case(
            scenario.scenario_id,
            task_messages,
            scenario=scenario,
            selected_session_id=selected_session_id,
        )
        observed_nodes = _unique_values(
            node
            for item in turn_observations
            for node in item.get("visited_nodes", [])
        )
        selected_source_ids = _unique_values(
            source_id
            for item in turn_observations
            for source_id in item.get("selected_source_ids", [])
        )
        evidence_source_ids = _unique_values(
            source_id
            for item in turn_observations
            for source_id in item.get("evidence_source_ids", [])
        )
        reviewer_outcomes = _unique_values(
            outcome
            for item in turn_observations
            for outcome in item.get("reviewer_outcomes", [])
        )
        packet_tokens: dict[str, int] = {}
        for trace in traces:
            telemetry_trace = trace.get("evaluation_telemetry")
            if not isinstance(telemetry_trace, Mapping):
                continue
            for event in telemetry_trace.get("debug_events", []):
                if not isinstance(event, Mapping):
                    continue
                values = event.get("context_packet_tokens")
                if isinstance(values, Mapping):
                    packet_tokens.update({
                        str(node): int(tokens)
                        for node, tokens in values.items()
                        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0
                    })
        tool_metrics = aggregate_tool_metrics(records)
        result = LongitudinalResult(
            scenario_id=scenario.scenario_id,
            runtime="real-backend-service",
            final_response=persisted_response,
            route=route,
            visited_nodes=observed_nodes,
            compatibility_visited_nodes=observed_nodes,
            selected_source_ids=selected_source_ids,
            omitted_source_ids=(),
            packet_tokens=packet_tokens,
            evidence_source_ids=evidence_source_ids,
            reviewer_outcomes=reviewer_outcomes,
            repair_count=telemetry_usage["repair_attempts"],
            checkpoint_outcome="restarted" if any(step.get("op") == "restart" for step in scenario.timeline) else None,
            persistence_observations=tuple(persistence_observations),
            persistence_outcomes=tuple(persistence_outcomes),
            memory_writes=tuple(memory_writes),
            idempotency_outcome=idempotency_outcome,
            catalog_ids=(),
            failures=tuple(failures),
            checkpoint_durability="postgresql_service_checkpoint",
            persistence_mode="postgresql_product_api_and_evaluator_db_control",
            turn_observations=tuple(turn_observations),
            elapsed_ms=telemetry_usage["elapsed_ms"],
            elapsed_ms_provenance="service_wall_clock",
            model_calls=telemetry_usage["model_call_attempts"],
            tool_calls=telemetry_usage["tool_call_attempts"],
            total_tokens=telemetry_usage["total_tokens"],
            runtime_events=tuple(runtime_events),
            trace={
                "execution_mode": "real-backend-service",
                "runtime_events": runtime_events,
                "task_context": task_context,
                "tool_metrics": tool_metrics,
                "tool_metrics_by_turn": group_tool_metrics(records, key="turn_correlation"),
                "budget_usage": telemetry_usage,
                "budget_comparison": _budget_comparison(budget_checks),
                "judge_input": json.dumps({
                    "response": persisted_response,
                    "runtime_events": runtime_events,
                    "task_context": task_context,
                    "tool_metrics": tool_metrics,
                    "budget_comparison": _budget_comparison(budget_checks),
                    "authorized_evidence_manifest": observed_evidence_manifest(runtime_events),
                }, ensure_ascii=False, sort_keys=True),
                "turns": traces,
                "authorized_evidence_manifest": observed_evidence_manifest(runtime_events)
            },
        )
        applicable = any(level != "not_applicable" for level in scenario.rubric_applicability.values())
        case = build_case_report(
            scenario,
            result,
            judge=(lambda prompt: judge.invoke(prompt, model=judge_model)) if judge is not None and applicable else None,
        )
        payload = case.to_dict()
        payload.pop("metrics", None)
        payload.pop("result", None)
        case_trace = case.result.trace if isinstance(case.result.trace, Mapping) else {}
        judge_input = case_trace.get("judge_input")
        judge_output = case_trace.get("judge_output")
        if not applicable:
            judge_status = "not_applicable"
            case_status = "passed"
        else:
            judge_status = "not_configured" if judge is None else "error"
            if case.rubric_summary is not None:
                judge_status = "passed" if case.rubric_summary.passed else "failed"
            case_status = (
                "completed_unjudged" if judge is None
                else "completed_with_judge_error" if judge_status == "error"
                else "passed" if judge_status == "passed" else "failed"
            )
        if "session_compaction_not_observed" in result.failures:
            case_status = "failed"
        payload.update({
            "suite": "longitudinal",
            "execution_mode": "real-backend-service",
            "provider_free": False,
            "status": case_status,
            "judge_status": judge_status,
            "failures": list(result.failures),
            "response": persisted_response,
            "runtime_events": runtime_events,
            "task_context": task_context,
            "judge_input": judge_input,
            "judge_output": judge_output,
            "tool_metrics": tool_metrics,
            "tool_metrics_by_turn": group_tool_metrics(records, key="turn_correlation"),
            "tool_calls": [record.to_dict() for record in records],
            "transcript": transcript,
            "ai_messages": persisted_messages["ai_messages"],
            "ai_internal_messages": persisted_messages["ai_internal_messages"],
            **_persisted_message_report_fields(persisted_messages),
            "budget_usage": telemetry_usage,
            "budget_comparison": _budget_comparison(budget_checks),
            "authorized_evidence_manifest": observed_evidence_manifest(runtime_events),
            "fixture_setup": dict(identity.fixture_setup),
            "resource_manifest": identity.case_manifest.privacy_safe() if identity.case_manifest is not None else None,
            "persistence": {
                "observations": persistence_observations,
                "outcomes": persistence_outcomes,
                "summary_count": len(summary_ids),
            },
        })
        if judge is None:
            payload["_service_result"] = result.to_dict()
        return payload


def _judge_real_model_service_result(
    service_result: Mapping[str, Any],
    *,
    case: Mapping[str, Any],
    judge: object,
    judge_model: str,
) -> dict[str, Any]:
    payload = dict(service_result)
    payload.pop("_service_result", None)
    if payload.get("judge_status") == "not_attempted":
        return payload

    last_response = str(payload.get("response") or "")
    runtime_events = payload.get("runtime_events")
    runtime_events = list(runtime_events) if isinstance(runtime_events, (list, tuple)) else []
    task_context = (
        dict(payload["task_context"])
        if isinstance(payload.get("task_context"), Mapping)
        else {}
    )
    judge_status = "error"
    judge_error: str | None = None
    judge_payload: dict[str, Any] = {}
    if last_response:
        try:
            manifest = payload.get("authorized_evidence_manifest")
            if not isinstance(manifest, list):
                manifest = observed_evidence_manifest(runtime_events)
            scenario = type(
                "RealModelScenario",
                (),
                {
                    "scenario_id": str(case.get("case_id") or "unknown-case"),
                    "rubric_applicability": {
                        dimension: "primary" for dimension in RUBRIC_DIMENSIONS
                    },
                    "oracles": {
                        "user_goal": str(case.get("prompt") or ""),
                    },
                },
            )()
            evaluation = evaluate_with_repair(
                scenario=scenario,
                response=last_response,
                evidence_manifest=manifest,
                invoke=lambda prompt: judge.invoke(prompt, model=judge_model),
                runtime_events=runtime_events,
                task_context=task_context,
            )
            judge_status = (
                "error"
                if evaluation.error
                else "passed"
                if evaluation.summary and evaluation.summary.passed
                else "failed"
            )
            judge_payload = {
                "judge": evaluation.result.to_dict() if evaluation.result else None,
                "rubric_summary": (
                    evaluation.summary.to_dict() if evaluation.summary else None
                ),
                "judge_error": evaluation.error,
                "judge_attempts": [
                    {
                        "prompt": attempt.payload,
                        "response": (
                            _response_text(attempt.raw_output)
                            if attempt.raw_output is not None
                            else None
                        ),
                        "error": attempt.error,
                        "usage_metadata": dict(attempt.usage_metadata or {}),
                    }
                    for attempt in evaluation.attempts
                ],
                "judge_prompt": (
                    evaluation.attempts[-1].payload
                    if evaluation.attempts
                    else None
                ),
                "judge_input": (
                    evaluation.attempts[0].payload
                    if evaluation.attempts
                    else None
                ),
                "judge_response": (
                    _response_text(evaluation.attempts[-1].raw_output)
                    if evaluation.attempts
                    and evaluation.attempts[-1].raw_output is not None
                    else None
                ),
            }
            judge_error = evaluation.error
        except Exception as exc:
            judge_error = f"{type(exc).__name__}: {exc}"
    payload.update(judge_payload)
    payload["judge_status"] = judge_status
    payload["judge_error"] = judge_error
    payload["status"] = (
        "completed_with_judge_error"
        if judge_status == "error"
        else "passed"
        if judge_status == "passed"
        else "failed"
    )
    return payload


_LONGITUDINAL_TUPLE_FIELDS = frozenset(
    {
        "visited_nodes",
        "compatibility_visited_nodes",
        "selected_source_ids",
        "omitted_source_ids",
        "evidence_source_ids",
        "reviewer_outcomes",
        "persistence_observations",
        "persistence_outcomes",
        "memory_writes",
        "catalog_ids",
        "failures",
        "unknown_planner_source_ids_rejected",
        "compacted_source_ids",
        "context_candidate_source_ids",
        "turn_observations",
        "raw_visited_nodes",
        "raw_reviewer_outcomes",
        "runtime_events",
    }
)


def _longitudinal_result_from_service_payload(
    service_result: Mapping[str, Any],
) -> LongitudinalResult | None:
    raw_result = service_result.get("_service_result")
    if not isinstance(raw_result, Mapping):
        return None
    values: dict[str, Any] = {}
    for field_name in LongitudinalResult.__dataclass_fields__:
        if field_name not in raw_result:
            continue
        value = raw_result[field_name]
        if field_name in _LONGITUDINAL_TUPLE_FIELDS:
            value = tuple(value) if isinstance(value, (list, tuple)) else ()
        elif field_name == "packet_tokens":
            value = dict(value) if isinstance(value, Mapping) else {}
        elif field_name == "trace":
            value = dict(value) if isinstance(value, Mapping) else None
        values[field_name] = value
    required = {
        "scenario_id",
        "runtime",
        "final_response",
        "route",
        "visited_nodes",
        "compatibility_visited_nodes",
        "selected_source_ids",
        "omitted_source_ids",
        "packet_tokens",
        "evidence_source_ids",
        "reviewer_outcomes",
        "repair_count",
        "checkpoint_outcome",
        "persistence_observations",
        "persistence_outcomes",
        "memory_writes",
        "idempotency_outcome",
        "catalog_ids",
        "failures",
        "checkpoint_durability",
        "persistence_mode",
    }
    if not required.issubset(values):
        return None
    return LongitudinalResult(**values)


def _judge_longitudinal_service_result(
    service_result: Mapping[str, Any],
    *,
    scenario: ScenarioV2,
    judge: object,
    judge_model: str,
) -> dict[str, Any]:
    payload = dict(service_result)
    result = _longitudinal_result_from_service_payload(payload)
    if result is None:
        payload.pop("_service_result", None)
        payload.setdefault("judge_status", "error")
        payload.setdefault("judge_error", "service_result_missing")
        payload.setdefault("status", "completed_with_judge_error")
        return payload

    applicable = any(
        level != "not_applicable"
        for level in scenario.rubric_applicability.values()
    )
    case = build_case_report(
        scenario,
        result,
        judge=(
            lambda prompt: judge.invoke(prompt, model=judge_model)
            if applicable
            else None
        ),
    )
    report_payload = case.to_dict()
    report_payload.pop("metrics", None)
    report_payload.pop("result", None)
    payload.update(report_payload)
    case_trace = case.result.trace if isinstance(case.result.trace, Mapping) else {}
    judge_input = case_trace.get("judge_input")
    judge_output = (
        _response_text(case_trace["judge_output"])
        if case_trace.get("judge_output") is not None
        else None
    )
    if not applicable:
        judge_status = "not_applicable"
        case_status = "passed"
    else:
        judge_status = "error"
        if case.rubric_summary is not None:
            judge_status = "passed" if case.rubric_summary.passed else "failed"
        case_status = (
            "completed_with_judge_error"
            if judge_status == "error"
            else "passed"
            if judge_status == "passed"
            else "failed"
        )
    payload.update(
        {
            "suite": "longitudinal",
            "execution_mode": "real-backend-service",
            "provider_free": False,
            "status": case_status,
            "judge_status": judge_status,
            "failures": list(result.failures),
            "response": result.final_response,
            "runtime_events": list(result.runtime_events),
            "task_context": payload.get("task_context"),
            "judge_input": judge_input,
            "judge_output": judge_output,
            "tool_metrics": payload.get("tool_metrics"),
            "tool_metrics_by_turn": payload.get("tool_metrics_by_turn"),
            "tool_calls": payload.get("tool_calls"),
            "budget_usage": payload.get("budget_usage"),
            "budget_comparison": payload.get("budget_comparison"),
            "authorized_evidence_manifest": payload.get(
                "authorized_evidence_manifest"
            ),
        }
    )
    payload.pop("_service_result", None)
    return payload


def judge_service_result(
    service_result: Mapping[str, Any],
    *,
    case: Mapping[str, Any] | None = None,
    scenario: ScenarioV2 | None = None,
    judge: object | None,
    judge_model: str,
) -> dict[str, Any]:
    """Apply judge/report work after a service case releases its worker."""
    payload = dict(service_result)
    if payload.get("judge_status") == "not_attempted" or judge is None:
        payload.pop("_service_result", None)
        return payload
    if case is not None:
        return _judge_real_model_service_result(
            payload,
            case=case,
            judge=judge,
            judge_model=judge_model,
        )
    if scenario is not None:
        return _judge_longitudinal_service_result(
            payload,
            scenario=scenario,
            judge=judge,
            judge_model=judge_model,
        )
    payload.pop("_service_result", None)
    return payload


REAL_MODEL_APPLICABILITY = {dimension: "primary" for dimension in RUBRIC_DIMENSIONS}
