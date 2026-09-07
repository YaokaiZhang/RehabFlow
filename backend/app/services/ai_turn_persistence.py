from __future__ import annotations

from contextlib import contextmanager
import logging
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import Integer, func, select, text

from app.db.models import AIChatTurnReceipt, AIInternalMessage, AIMessage
from app.observability.agent_trace import is_safe_trace_source_id

logger = logging.getLogger(__name__)

_INTERNAL_METADATA_KEYS = frozenset(
    {
        "action",
        "attempt",
        "agent",
        "model_call",
        "model_name",
        "provider",
        "sequence",
        "turn_id",
        "review_turn_id",
        "review_attempt",
        "decision",
        "if_emergency",
        "needs_more_info",
        "triage_notes",
        "safety_passed",
        "grounding_passed",
        "mode",
        "retry",
        "error_type",
        "exception_message",
        "serialized_provider_input_estimate",
        "model_call_attempt_count",
        "failing_boundary",
        "timeout_status",
        "provider_status",
        "usage_unavailable_reason",
        "input_chars",
        "output_chars",
        "catalog_suggestion_ids",
        "catalog_suggestion_count",
        "candidate_count",
        "candidate_count_by_source_type",
        "clinician_review_needed",
        "elapsed_ms",
        "aggregate_input_tokens",
        "estimated_input_tokens",
        "failure_category",
        "included_source_ids",
        "input_tokens",
        "model_calls",
        "limitation_count",
        "missing_information_count",
        "node",
        "omitted_source_ids",
        "outcome",
        "query_count",
        "query_budget",
        "result_count",
        "result_budget",
        "call_budget",
        "timeout_ms",
        "output_tokens",
        "omitted_sources",
        "packet_count",
        "packet_tokens",
        "planner_error",
        "reason_codes",
        "repair_count",
        "request_count",
        "request_sources",
        "reviewer",
        "route",
        "severity",
        "source_ids",
        "selected_sources",
        "status",
        "tool_call_count",
        "tool_calls",
        "tool_call_names",
        "tool_events",
        "tool_arguments",
        "tool_name",
        "total_tokens",
        "unavailable_evidence",
        "unresolved_question_count",
        "safety_signal_count",
        "retrieval_outcomes",
        "retrieval_errors",
        "version",
    }
)
_INTERNAL_LIST_KEYS = frozenset(
    {
        "catalog_suggestion_ids",
        "included_source_ids",
        "omitted_source_ids",
        "reason_codes",
        "request_sources",
        "source_ids",
        "tool_call_names",
        "tool_calls",
        "unavailable_evidence",
        "retrieval_outcomes",
        "retrieval_errors",
    }
)
_INTERNAL_INT_MAPPING_KEYS = frozenset(
    {"candidate_count_by_source_type", "packet_tokens", "serialized_provider_input_estimate"}
)
_INTERNAL_SOURCE_MAPPING_KEYS = frozenset({"omitted_sources", "selected_sources"})
_INTERNAL_STRUCTURED_LIST_KEYS = frozenset({"tool_events"})
_SAFE_INTERNAL_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_SECRET_LIKE_TEXT = re.compile(
    r"(?:bearer\s+|authorization\s*[:=]|password\s*[:=]|secret\s*[:=]|"
    r"api[_-]?key\s*[:=]|token\s*[:=]|-----BEGIN|sk-[A-Za-z0-9])",
    re.IGNORECASE,
)
_REDACT_INTERNAL_TEXT = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+|bearer\s+[^\s]+|"
    r"(?:password|secret|api[_-]?key|token|stream[_-]?ticket)\s*[:=]\s*[^\s,;]+|"
    r"sk-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)

def redact_internal_text(value: object) -> str:
    text = str(value or "")
    return _REDACT_INTERNAL_TEXT.sub("[REDACTED]", text)


def _safe_internal_label(value: object, fallback: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not _SAFE_INTERNAL_TEXT.fullmatch(normalized):
        return fallback
    if _SECRET_LIKE_TEXT.search(normalized):
        return fallback
    return normalized[:64]


def safe_internal_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only allowlisted, non-content operational metadata."""
    return _safe_internal_metadata(metadata)


def _safe_internal_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for raw_key, value in metadata.items():
        key = str(raw_key)
        if key not in _INTERNAL_METADATA_KEYS:
            continue
        if key == "exception_message":
            safe[key] = redact_internal_text(str(value or ""))[:500]
            continue
        if key == "tool_arguments":
            if not isinstance(value, Mapping):
                continue
            normalized_arguments: dict[str, Any] = {}
            for raw_subkey, raw_subvalue in value.items():
                subkey = str(raw_subkey).strip()
                if subkey == "session_id":
                    text_value = redact_internal_text(str(raw_subvalue or "").strip())
                    if text_value:
                        normalized_arguments[subkey] = text_value[:2000]
                elif subkey == "limit" and isinstance(raw_subvalue, int) and not isinstance(raw_subvalue, bool):
                    normalized_arguments[subkey] = max(0, min(raw_subvalue, 100))
            if normalized_arguments:
                safe[key] = normalized_arguments
            continue
        if key in _INTERNAL_INT_MAPPING_KEYS:
            if not isinstance(value, Mapping):
                continue
            normalized_map: dict[str, int] = {}
            for raw_subkey, raw_value in value.items():
                subkey = str(raw_subkey).strip()
                if (
                    _SAFE_INTERNAL_TEXT.fullmatch(subkey)
                    and not _SECRET_LIKE_TEXT.search(subkey)
                    and isinstance(raw_value, int)
                    and not isinstance(raw_value, bool)
                    and raw_value >= 0
                ):
                    normalized_map[subkey[:64]] = raw_value
            safe[key] = normalized_map
            continue
        if key in _INTERNAL_SOURCE_MAPPING_KEYS:
            if not isinstance(value, Mapping):
                continue
            normalized_map: dict[str, list[str]] = {}
            for raw_subkey, raw_value in value.items():
                subkey = str(raw_subkey).strip()
                if (
                    not _SAFE_INTERNAL_TEXT.fullmatch(subkey)
                    or _SECRET_LIKE_TEXT.search(subkey)
                    or isinstance(raw_value, (str, bytes))
                    or not isinstance(raw_value, (list, tuple, set))
                ):
                    continue
                source_ids = sorted(
                    {
                        str(source_id)
                        for source_id in raw_value
                        if is_safe_trace_source_id(source_id)
                    }
                )
                normalized_map[subkey[:64]] = source_ids
            safe[key] = normalized_map
            continue
        if key in _INTERNAL_STRUCTURED_LIST_KEYS:
            if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
                continue
            safe_events: list[dict[str, Any]] = []
            for raw_event in value:
                if not isinstance(raw_event, Mapping):
                    continue
                safe_event: dict[str, Any] = {}
                for raw_subkey, raw_subvalue in raw_event.items():
                    subkey = str(raw_subkey)
                    if subkey not in {
                        "tool",
                        "tool_name",
                        "request_id",
                        "source",
                        "status",
                        "reason_code",
                        "latency_ms",
                        "query_count",
                        "query_budget",
                        "result_count",
                        "result_budget",
                        "call_budget",
                        "timeout_ms",
                        "doc_count",
                        "packet_count",
                        "legacy_compatibility",
                    }:
                        continue
                    if isinstance(raw_subvalue, bool):
                        safe_event[subkey] = raw_subvalue
                    elif isinstance(raw_subvalue, int) and not isinstance(raw_subvalue, bool) and raw_subvalue >= 0:
                        safe_event[subkey] = raw_subvalue
                    elif isinstance(raw_subvalue, float) and raw_subvalue >= 0:
                        safe_event[subkey] = raw_subvalue
                    elif isinstance(raw_subvalue, str) and _SAFE_INTERNAL_TEXT.fullmatch(raw_subvalue.strip()):
                        safe_event[subkey] = raw_subvalue.strip()[:256]
                if safe_event:
                    safe_events.append(safe_event)
            safe[key] = safe_events
            continue
        if key == "retrieval_outcomes":
            if not isinstance(value, Mapping):
                continue
            safe[key] = {
                str(name)[:64]: str(status)[:32]
                for name, status in value.items()
                if _SAFE_INTERNAL_TEXT.fullmatch(str(name).strip())
                and str(status) in {"failed", "empty", "substantive"}
            }
            continue
        if key == "retrieval_errors":
            if not isinstance(value, Mapping):
                continue
            safe[key] = {
                str(name)[:64]: redact_internal_text(str(error or ""))[:500]
                for name, error in value.items()
                if _SAFE_INTERNAL_TEXT.fullmatch(str(name).strip())
                and str(error or "").strip()
            }
            continue
        if key in _INTERNAL_LIST_KEYS:
            if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
                continue
            items: list[str] = []
            for item in value:
                if key in {"source_ids", "included_source_ids", "omitted_source_ids"}:
                    if is_safe_trace_source_id(item):
                        items.append(str(item))
                    continue
                normalized = str(item).strip()
                if _SAFE_INTERNAL_TEXT.fullmatch(normalized) and not _SECRET_LIKE_TEXT.search(normalized):
                    items.append(normalized[:256])
            safe[key] = sorted(set(items))
            continue
        if isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            safe[key] = value
        elif isinstance(value, float) and value >= 0:
            safe[key] = value
        elif isinstance(value, str):
            normalized = value.strip()
            if _SAFE_INTERNAL_TEXT.fullmatch(normalized) and not _SECRET_LIKE_TEXT.search(normalized):
                safe[key] = normalized[:256]
    return safe


def _project_internal_event(
    event: Mapping[str, Any],
    *,
    fallback_agent: str,
    fallback_type: str,
    include_content: bool = False,
    fallback_turn_id: str | None = None,
) -> dict[str, Any]:
    raw_metadata = event.get("metadata")
    metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    for key in _INTERNAL_METADATA_KEYS:
        if key in event:
            metadata.setdefault(key, event[key])
    if "turn_id" not in metadata and fallback_turn_id:
        metadata["turn_id"] = fallback_turn_id
    if "sequence" not in metadata and event.get("step") is not None:
        metadata["sequence"] = event.get("step")
    return {
        "agent_name": _safe_internal_label(
            event.get("agent_name") or event.get("node"), fallback_agent
        ),
        "event_type": _safe_internal_label(
            event.get("event_type") or event.get("event") or fallback_type,
            fallback_type,
        ),
        "content": redact_internal_text(event.get("content") or event.get("response_preview") or "") if include_content else "",
        "model_input": redact_internal_text(event.get("model_input") or "") if include_content else "",
        "metadata": _safe_internal_metadata(metadata),
    }


def _order_projected_internal_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Model trace metadata uses local node/debug sequence values. Durable
    # internal ordering follows the emitted event stream instead.
    for sequence, event in enumerate(events, start=1):
        metadata = dict(event.get("metadata") or {})
        metadata["sequence"] = sequence
        event["metadata"] = metadata
    return events


def project_internal_trace_events(
    output: Mapping[str, Any], *, include_content: bool = True
) -> list[dict[str, Any]]:
    """Project one ordered graph event stream into durable internal trace rows.

    Model input/output is retained for authorized internal debugging; operational
    metadata remains allowlisted and credential-like text is redacted.
    """
    projected: list[dict[str, Any]] = []
    fallback_turn_id = next(
        (
            str(output.get(key))
            for key in ("trace_turn_id", "evidence_turn_id", "review_turn_id")
            if output.get(key)
        ),
        None,
    )
    structured = output.get("structured_trace_events")
    if isinstance(structured, (list, tuple)):
        for event in structured:
            if isinstance(event, Mapping):
                projected.append(
                    _project_internal_event(
                        event,
                        fallback_agent="graph",
                        fallback_type="trace_event",
                        include_content=include_content,
                        fallback_turn_id=fallback_turn_id,
                    )
                )
        return _order_projected_internal_events(projected)

    internal_events = output.get("internal_log_events")
    if isinstance(internal_events, (list, tuple)):
        internal_agents: set[str] = set()
        for event in internal_events:
            if isinstance(event, Mapping):
                projected.append(
                    _project_internal_event(
                        event,
                        fallback_agent="graph",
                        fallback_type="internal_event",
                        include_content=include_content,
                        fallback_turn_id=fallback_turn_id,
                    )
                )
                agent_name = str(event.get("agent_name") or event.get("node") or "").strip()
                if agent_name:
                    internal_agents.add(agent_name)
        # Debug trace entries are deterministic control events unless the
        # corresponding model/deterministic event is already in the ordered
        # internal stream. This preserves control visibility without making a
        # second row for a model output.
        debug_events = output.get("debug_trace")
        if isinstance(debug_events, (list, tuple)):
            for event in debug_events:
                if not isinstance(event, Mapping):
                    continue
                node_name = str(event.get("node") or "").strip()
                if not node_name or node_name in internal_agents:
                    continue
                projected.append(
                    _project_internal_event(
                        event,
                        fallback_agent="graph",
                        fallback_type="control_trace",
                        include_content=False,
                        fallback_turn_id=fallback_turn_id,
                    )
                )
    elif not internal_events:
        debug_events = output.get("debug_trace")
        if isinstance(debug_events, (list, tuple)):
            for event in debug_events:
                if isinstance(event, Mapping):
                    projected.append(
                        _project_internal_event(
                            event,
                            fallback_agent="graph",
                            fallback_type="control_trace",
                            include_content=False,
                            fallback_turn_id=fallback_turn_id,
                        )
                    )
    observed_agents = {str(event.get("agent_name")) for event in projected}
    if output.get("consult_response") and "consultant" not in observed_agents:
        projected.append(
            _project_internal_event(
                {"agent_name": "consultant", "event_type": "agent_output", "metadata": {"outcome": "available"}},
                fallback_agent="consultant",
                fallback_type="agent_output",
                include_content=False,
                fallback_turn_id=fallback_turn_id,
            )
        )
    if output.get("safety_feedback") and "safety_reviewer" not in observed_agents:
        projected.append(
            _project_internal_event(
                {"agent_name": "safety_reviewer", "event_type": "agent_output", "metadata": {"outcome": "available"}},
                fallback_agent="safety_reviewer",
                fallback_type="agent_output",
                include_content=False,
                fallback_turn_id=fallback_turn_id,
            )
        )
    return _order_projected_internal_events(projected)

_REPLAYABLE_KEYS = (
    "status",
    "final_response",
    "clarification_question",
    "workflow_version",
    "session_id",
    "routing_decision",
    "needs_more_info",
    "if_emergency",
    "emergency_reason",
    "safety_passed",
    "safety_feedback",
    "web_sources",
    "evidence_turn_id",
    "trace_turn_id",
)


class AIChatTurnPersistenceError(RuntimeError):
    """The product-record transaction could not be committed."""


class AIChatTurnPersistence:
    """Own the atomic product-record write for one AI chat turn.

    Required internal model rows are written in this transaction. Optional
    operational trace export remains separate and may fail after product records
    are committed without making a durable turn look unsuccessful.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    @contextmanager
    def _write_transaction(self):
        begin = getattr(self.db, "begin", None)
        if callable(begin):
            with begin():
                yield
            return

        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _next_message_sequence(self, session_id: Any) -> int:
        execute = getattr(self.db, "execute", None)
        if not callable(execute):
            # Small persistence doubles may not expose reads; a fresh turn starts
            # at one and production uses the database-backed maximum below.
            return 1
        result = execute(
            select(
                func.max(
                    AIMessage.message_metadata["sequence"].astext.cast(Integer)
                )
            ).where(AIMessage.session_id == session_id)
        )
        current = result.scalar()
        return int(current or 0) + 1

    def _next_internal_sequence(self, session_id: Any) -> int:
        execute = getattr(self.db, "execute", None)
        if not callable(execute):
            return 1
        bind = getattr(self.db, "bind", None)
        dialect = getattr(getattr(bind, "dialect", None), "name", None)
        if dialect == "postgresql":
            execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(CAST(:session_id AS text), 0))"
                ),
                {"session_id": str(session_id)},
            )
        result = execute(
            select(
                func.max(
                    AIInternalMessage.event_metadata["sequence"].astext.cast(Integer)
                )
            ).where(AIInternalMessage.session_id == session_id)
        )
        current = result.scalar()
        return int(current or 0) + 1

    def _reset_read_transaction(self) -> None:
        in_transaction = getattr(self.db, "in_transaction", None)
        if callable(in_transaction) and in_transaction():
            self.db.rollback()

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {
                str(key): AIChatTurnPersistence._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [AIChatTurnPersistence._json_safe(item) for item in value]
        return str(value)

    @classmethod
    def replay_response_payload(
        cls,
        output: Mapping[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        payload = {
            key: cls._json_safe(output[key])
            for key in _REPLAYABLE_KEYS
            if key in output
        }
        payload.setdefault("session_id", session_id)
        return payload

    def persist_turn(
        self,
        *,
        session_id: Any,
        patient_id: Any | None = None,
        user_input: str,
        output: Mapping[str, Any],
        internal_trace: Mapping[str, Any] | None = None,
        receipt: AIChatTurnReceipt | None,
        status: str,
    ) -> dict[str, Any]:
        """Persist conversation messages and the outcome in one commit.

        The caller receives a durable-success result only after commit has
        returned. Any insert or commit failure rolls the transaction back and
        raises an unavailable error.
        """
        self._reset_read_transaction()
        try:
            with self._write_transaction():
                message_sequence = self._next_message_sequence(session_id)
                originating_event_id = str(output.get("evidence_turn_id") or output.get("trace_turn_id") or "").strip()

                def add_message(sender_role: str, content: str) -> None:
                    nonlocal message_sequence
                    message_metadata = {"sequence": message_sequence}
                    if sender_role == "user" and originating_event_id:
                        message_metadata["originating_event_id"] = originating_event_id
                    self.db.add(
                        AIMessage(
                            session_id=session_id,
                            sender_role=sender_role,
                            content=content,
                            message_metadata=message_metadata,
                        )
                    )
                    message_sequence += 1

                add_message("user", str(user_input))
                final_response = str(output.get("final_response") or "")
                if final_response:
                    add_message("assistant", final_response)

                trace_source = internal_trace if internal_trace is not None else output
                internal_sequence = self._next_internal_sequence(session_id)
                for event in project_internal_trace_events(trace_source, include_content=True):
                    event_metadata = dict(event.get("metadata") or {})
                    event_metadata["sequence"] = internal_sequence
                    internal_sequence += 1
                    self.db.add(
                        AIInternalMessage(
                            session_id=session_id,
                            patient_id=patient_id,
                            agent_name=str(event["agent_name"])[:64],
                            event_type=str(event["event_type"])[:64],
                            content=redact_internal_text(event.get("content") or ""),
                            model_input=redact_internal_text(event.get("model_input") or ""),
                            event_metadata=safe_internal_metadata(event_metadata),
                        )
                    )

                if receipt is not None:
                    receipt.status = status
                    receipt.response_payload = (
                        self.replay_response_payload(output, str(session_id))
                        if status in {"completed", "interrupted"}
                        else None
                    )
                    self.db.add(receipt)
        except Exception as exc:
            if receipt is not None:
                receipt.status = "failed"
                receipt.response_payload = None
            raise AIChatTurnPersistenceError(
                "The AI turn is unavailable because its product records could not be saved."
            ) from exc

        return dict(output)

    def export_trace_after_commit(self, exporter: Callable[[], None]) -> None:
        """Best-effort operational trace export after product commit."""
        try:
            exported = exporter()
            if exported is False:
                return
            pending = getattr(self.db, "new", None)
            if pending is not None:
                if pending:
                    self.db.commit()
                return
            added = getattr(self.db, "added", None)
            if added is not None:
                return
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.warning("[trace:export] failure_category=trace_export_failed")
