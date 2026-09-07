from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from contextlib import nullcontext
from collections.abc import Mapping
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from fastapi import HTTPException
from langgraph.types import Command
from sqlalchemy.exc import IntegrityError

from app.ai.clarification import WORKFLOW_VERSION, clarification_pending
from app.ai.checkpointing import cleanup_completed_checkpoint
from app.ai.active_turn_context import (
    clear_active_turn,
    create_active_turn,
    store_context_packets,
)
from app.ai.context_integrity import PUBLIC_REHAB_KNOWLEDGE_SCOPE
from app.ai.policy_gate import GateSeverity, evaluate_policy_gate
from app.ai.workflow_policy import WorkflowPolicy
from app.ai.context_assembler import (
    ContextAssembler,
    cards_from_memory_documents,
    cards_from_rehab_sessions,
    deterministic_context_plan,
    cards_from_triage_summary,
    context_packet_metadata,
    latest_triage_summary_for_episode,
)
from app.ai.node_specs import NODE_SPECS
from app.ai.session_compaction import prepare_session_context
from app.ai.repair import context_version_snapshot
from app.ai.invocation_support import _append_debug_event, _make_internal_event
from app.ai.graph_log_persistence import _persist_internal_graph_logs
from app.ai.consultant_tools import _consultant_tool_names_for_level1, _episode_memory_level1_session_ids
from app.core.config import evaluator_fault_profile
from app.db.models import AIChatTurnReceipt, AISession, CareEpisode
from app.db.session import SessionLocal
from app.services.ai_chat_access import AuthorizedAIChat
from app.services.ai_turn_persistence import (
    AIChatTurnPersistence,
    AIChatTurnPersistenceError,
)
from app.services.memory_documents import build_memory_context_pack
from app.services.rehab_session_context import rehab_sessions_for_episode

logger = logging.getLogger(__name__)


def _evaluator_context_planner(**kwargs: Any) -> Any:
    if evaluator_fault_profile("planner_failure"):
        raise RuntimeError("evaluator planner failure")
    return deterministic_context_plan(**kwargs)


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
        "rehab_activity",
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


def _conversation_history_entry(card: object) -> dict[str, object] | None:
    source_type = str(getattr(card, "source_type", "") or "")
    if source_type not in {"session_message", "session_compacted_block"}:
        return None
    metadata = getattr(card, "metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    entry: dict[str, object] = {
        "role": (
            "context"
            if source_type == "session_compacted_block"
            else str(metadata.get("sender_role") or getattr(card, "title", ""))
        ),
        "content": getattr(card, "text", ""),
    }
    if source_type == "session_message" and metadata.get("sequence") is not None:
        entry["sequence"] = metadata["sequence"]
    return entry


class AIChatTurnService:
    """Route-facing owner of per-turn SQLAlchemy work around the compiled runtime."""

    def __init__(self, runtime: Any, *, session_factory=SessionLocal, policy: WorkflowPolicy | None = None) -> None:
        self.runtime = runtime
        self.session_factory = session_factory
        runtime_policy = getattr(getattr(runtime, "deps", None), "policy", None)
        self.policy = policy or (
            runtime_policy
            if isinstance(runtime_policy, WorkflowPolicy)
            else WorkflowPolicy(
                use_tool_calling=bool(getattr(runtime_policy, "use_tool_calling", True)),
                debug=bool(getattr(runtime_policy, "debug", False)),
            )
        )

    def _load_context_sources(
        self,
        db: Any,
        *,
        patient_id: uuid.UUID,
        care_episode_id: uuid.UUID | None,
    ) -> tuple[str | None, str, list[Any], list[str]]:
        if care_episode_id is None:
            return None, "", [], []

        try:
            episode = db.get(CareEpisode, care_episode_id)
            if (
                episode is None
                or str(getattr(episode, "patient_id", "")) != str(patient_id)
                or (getattr(episode, "status", None) or "active") != "active"
            ):
                return str(care_episode_id), "", [], []

            patient_document, episode_document, memory_context = build_memory_context_pack(db, episode)
            memory_cards = cards_from_memory_documents(patient_document, episode_document)
            level1_session_ids = _episode_memory_level1_session_ids(
                getattr(episode_document, "level1_session_ids", [])
            )
        except Exception:
            logger.warning("[memory_context:error] failure_category=memory_context_unavailable")
            if hasattr(db, "rollback"):
                db.rollback()
            return str(care_episode_id), "", [], []

        rehab_cards: list[Any] = []
        try:
            rehab_sessions = rehab_sessions_for_episode(db, episode.care_episode_id, episode.patient_id)
            rehab_cards = cards_from_rehab_sessions(rehab_sessions)
        except Exception:
            logger.warning("[context_assembly:error] failure_category=rehab_session_source_unavailable")

        triage_cards: list[Any] = []
        try:
            triage_cards = cards_from_triage_summary(
                latest_triage_summary_for_episode(db, care_episode_id)
            )
        except Exception:
            logger.warning("[context_assembly:error] failure_category=triage_source_unavailable")
            if hasattr(db, "rollback"):
                db.rollback()
        return (
            str(care_episode_id),
            memory_context or "",
            [*memory_cards, *triage_cards, *rehab_cards],
            level1_session_ids,
        )

    @staticmethod
    def _care_episode_is_authorized(
        db: Any,
        *,
        patient_id: uuid.UUID,
        care_episode_id: str | None,
    ) -> bool:
        if care_episode_id is None:
            return True
        try:
            episode = db.get(CareEpisode, uuid.UUID(str(care_episode_id)))
        except Exception:
            return False
        return bool(
            episode is not None
            and str(getattr(episode, "patient_id", "")) == str(patient_id)
            and (getattr(episode, "status", None) or "active") == "active"
        )

    @staticmethod
    def _request_hash(user_input: str, access: AuthorizedAIChat, workflow_version: str) -> str:
        payload = {
            "care_episode_id": str(access.care_episode_id) if access.care_episode_id is not None else None,
            "message": user_input,
            "workflow_version": workflow_version,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _graph_config(session_id: str, owner_id: str | None = None) -> dict[str, Any]:
        configurable: dict[str, Any] = {"thread_id": session_id}
        if owner_id:
            configurable["authenticated_owner"] = str(owner_id)
        return {"configurable": configurable}

    @staticmethod
    def _next_review_identity(
        snapshot: Any | None,
        active_turn_id: str,
    ) -> tuple[int, str]:
        values = getattr(snapshot, "values", {}) or {}
        if not isinstance(values, Mapping):
            values = {}
        previous_attempt = values.get("review_attempt")
        if not isinstance(previous_attempt, int) or isinstance(previous_attempt, bool) or previous_attempt < 0:
            attempts = [
                decision.get("attempt")
                if isinstance(decision, Mapping)
                else getattr(decision, "attempt", None)
                for decision in values.get("reviewer_decisions", ()) or ()
            ]
            previous_attempt = max(
                (
                    attempt
                    for attempt in attempts
                    if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0
                ),
                default=0,
            )
        next_attempt = previous_attempt + 1
        return next_attempt, f"{active_turn_id}:review:{next_attempt}"

    def _graph_state(self, session_id: str, owner_id: str | None = None) -> Any | None:
        config = self._graph_config(session_id, owner_id)
        graph = getattr(self.runtime, "graph", None)
        checkpointer = getattr(self.runtime, "checkpointer", None)
        if checkpointer is None:
            checkpointer = getattr(graph, "checkpointer", None)
        rehydrate = getattr(checkpointer, "rehydrate", None)
        with rehydrate() if callable(rehydrate) else nullcontext():
            getter = getattr(self.runtime, "get_state", None)
            if callable(getter):
                try:
                    return getter(session_id=session_id)
                except TypeError:
                    return getter(config)
            graph_getter = getattr(graph, "get_state", None)
            return graph_getter(config) if callable(graph_getter) else None

    def _update_graph_context(self, session_id: str, values: dict[str, Any], owner_id: str | None = None) -> None:
        config = self._graph_config(session_id, owner_id)
        graph = getattr(self.runtime, "graph", None)
        checkpointer = getattr(self.runtime, "checkpointer", None)
        if checkpointer is None:
            checkpointer = getattr(graph, "checkpointer", None)
        rehydrate = getattr(checkpointer, "rehydrate", None)
        scope = rehydrate() if callable(rehydrate) else nullcontext()
        updater = getattr(self.runtime, "update_state", None)
        with scope:
            if callable(updater):
                try:
                    updater(values, session_id=session_id)
                except TypeError:
                    updater(config, values)
                return
            graph_updater = getattr(graph, "update_state", None)
            if callable(graph_updater):
                graph_updater(config, values)

    def _invoke_runtime(self, command: object, *, session_id: str, owner_id: str) -> dict[str, Any]:
        invoke = self.runtime.invoke_turn
        try:
            return invoke(command, session_id=session_id, owner_id=owner_id)
        except TypeError as exc:
            # Narrow legacy test doubles predate the owner binding seam.
            if "owner_id" not in str(exc):
                raise
            return invoke(command, session_id=session_id)

    @staticmethod
    def _interrupt_payload(output: Mapping[str, Any]) -> dict[str, Any] | None:
        for pending in output.get("__interrupt__") or ():
            value = getattr(pending, "value", pending)
            if isinstance(value, Mapping) and value.get("kind") == "clarification":
                return dict(value)
        return None

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {str(key): AIChatTurnService._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [AIChatTurnService._json_safe(item) for item in value]
        return str(value)

    @staticmethod
    def _replay_response_payload(output: Mapping[str, Any], session_id: str) -> dict[str, Any]:
        replayable_keys = (
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
        payload = {
            key: AIChatTurnService._json_safe(output[key])
            for key in replayable_keys
            if key in output
        }
        payload.setdefault("session_id", session_id)
        return payload

    @staticmethod
    def _trace_payload(output: Mapping[str, Any]) -> dict[str, Any]:
        """Build operational metadata without copying clinical text or prompts."""
        safe_scalar_keys = (
            "status",
            "workflow_version",
            "routing_decision",
            "needs_more_info",
            "if_emergency",
            "safety_passed",
            "tool_calling_used",
            "consult_attempts",
        )
        payload = {
            key: output[key]
            for key in safe_scalar_keys
            if key in output
        }

        def count_values(key: str) -> int:
            value = output.get(key)
            return len(value) if isinstance(value, (list, tuple)) else 0

        payload.update(
            {
                "internal_log_event_count": count_values("internal_log_events"),
                "debug_trace_event_count": count_values("debug_trace"),
                "tool_event_count": count_values("tool_events"),
                "catalog_exercise_suggestion_count": count_values(
                    "catalog_exercise_suggestions"
                ),
                "retrieved_doc_count": count_values("retrieved_docs"),
                "web_summary_len": (
                    len(output["web_search_summary"])
                    if isinstance(output.get("web_search_summary"), str)
                    else 0
                ),
                "tool_calling_error_present": bool(
                    output.get("tool_calling_error")
                ),
            }
        )
        return payload

    @staticmethod
    def _evaluation_telemetry(
        output: Mapping[str, Any],
        elapsed_ms: int,
        policy: WorkflowPolicy,
        *,
        turn_correlation: str | None = None,
    ) -> dict[str, Any]:
        correlation = hashlib.sha256(
            str(turn_correlation or "unknown-turn").encode("utf-8")
        ).hexdigest()[:24]
        tool_events: list[dict[str, Any]] = []
        for index, raw in enumerate(output.get("tool_events", ()) or (), start=1):
            if not isinstance(raw, Mapping):
                continue
            tool_name = str(raw.get("tool_name") or raw.get("tool") or "unknown_tool")
            result_count = next(
                (
                    int(raw[key])
                    for key in ("result_count", "doc_count", "summary_count")
                    if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool)
                ),
                None,
            )
            raw_outcome = str(raw.get("outcome") or "").lower()
            if raw_outcome in {"ok", "success", "completed"}:
                outcome = "ok_nonempty" if (result_count or 0) > 0 else "ok_empty"
            elif raw_outcome in {"ok_nonempty", "ok_empty"}:
                outcome = raw_outcome
            elif raw_outcome in {"empty", "no_results"}:
                outcome = "ok_empty"
            elif raw_outcome in {"blocked", "invalid", "timeout", "error"}:
                outcome = raw_outcome
            elif raw.get("failure_category") or raw.get("error"):
                outcome = "error"
            else:
                outcome = "error"
            raw_call_id = str(raw.get("call_id") or f"{tool_name}:{index}")
            call_id = hashlib.sha256(
                f"{correlation}:{raw_call_id}".encode("utf-8")
            ).hexdigest()[:24]
            event: dict[str, Any] = {
                "call_id": call_id,
                "turn_correlation": correlation,
                "tool_name": tool_name,
                "attempt": int(raw.get("attempt") or index),
                "outcome": outcome,
            }
            for key in (
                "latency_ms",
                "failure_category",
                "retry_of",
                "retry_count",
                "query_count",
                "query_budget",
                "result_budget",
                "call_budget",
                "timeout_ms",
            ):
                if raw.get(key) is not None:
                    event[key] = (
                        hashlib.sha256(f"{correlation}:{raw[key]}".encode("utf-8")).hexdigest()[:24]
                        if key == "retry_of"
                        else raw[key]
                    )
            if outcome in {"ok_nonempty", "ok_empty"} and result_count is not None:
                event["result_count"] = max(0, result_count)
            tool_events.append(event)
        active_decisions = output.get("reviewer_decisions", ()) or ()
        decision_history = output.get("reviewer_decision_history", ()) or ()
        decisions = decision_history if decision_history else active_decisions
        current_attempt = output.get("review_attempt")
        current_turn = str(output.get("review_turn_id") or "").strip()
        reviewer_outcomes_by_name: dict[str, str] = {}
        for decision in decisions:
            if isinstance(decision, Mapping):
                reviewer = decision.get("reviewer") or decision.get("agent") or decision.get("name")
                status = decision.get("status") or decision.get("outcome")
                attempt = decision.get("attempt")
                review_turn_id = decision.get("review_turn_id")
            else:
                reviewer = getattr(decision, "reviewer", None) or getattr(decision, "agent", None)
                status = getattr(decision, "status", None) or getattr(decision, "outcome", None)
                attempt = getattr(decision, "attempt", None)
                review_turn_id = getattr(decision, "review_turn_id", None)
            if (
                isinstance(current_attempt, int)
                and not isinstance(current_attempt, bool)
                and attempt != current_attempt
            ):
                continue
            if current_turn and str(review_turn_id or "").strip() != current_turn:
                continue
            if reviewer in {"safety", "grounding"} and status:
                reviewer_outcomes_by_name[str(reviewer)] = str(
                    getattr(status, "value", status)
                )
        reviewer_outcomes = [
            f"{reviewer}:{reviewer_outcomes_by_name[reviewer]}"
            for reviewer in ("safety", "grounding")
            if reviewer in reviewer_outcomes_by_name
        ]
        policy_payload = policy.model_dump(mode="json") if hasattr(policy, "model_dump") else {}
        return {
            "model_call_attempts": output.get("model_calls") if isinstance(output.get("model_calls"), int) and not isinstance(output.get("model_calls"), bool) else None,
            "tool_call_attempts": output.get("tool_calls") if isinstance(output.get("tool_calls"), int) and not isinstance(output.get("tool_calls"), bool) else None,
            "total_tokens": output.get("total_tokens") if isinstance(output.get("total_tokens"), int) and not isinstance(output.get("total_tokens"), bool) else None,
            "repair_attempts": output.get("repair_count") if isinstance(output.get("repair_count"), int) and not isinstance(output.get("repair_count"), bool) else None,
            "elapsed_ms": elapsed_ms,
            "budget": policy_payload,
            "tool_calls": tool_events,
            "evidence_fanout": {"attempted": len(output.get("evidence_results", ()) or ())},
            "selected_source_ids": [
                _privacy_safe_source_id(item) for item in output.get("source_ids", ()) or ()
            ],
            "evidence_source_ids": [
                _privacy_safe_source_id(item) for item in output.get("evidence_source_ids", ()) or ()
            ],
            "reviewer_outcomes": reviewer_outcomes,
            "execution_failures": ["runtime_error"] if output.get("status") == "failed" else [],
        }

    def _load_receipt(self, db: Any, *, session_uuid: uuid.UUID, idempotency_key: str) -> AIChatTurnReceipt | None:
        return (
            db.query(AIChatTurnReceipt)
            .filter(
                AIChatTurnReceipt.session_id == session_uuid,
                AIChatTurnReceipt.idempotency_key == idempotency_key,
            )
            .first()
        )

    def _preflight_receipt(
        self,
        db: Any,
        *,
        session_uuid: uuid.UUID,
        access: AuthorizedAIChat,
        idempotency_key: str,
        request_hash: str,
        workflow_version: str,
    ) -> tuple[AIChatTurnReceipt, bool]:
        existing = self._load_receipt(db, session_uuid=session_uuid, idempotency_key=idempotency_key)
        if existing is not None:
            if not existing.matches_request_hash(request_hash):
                raise HTTPException(status_code=409, detail="Idempotency key was already used with a different request.")
            if existing.workflow_version != workflow_version:
                raise HTTPException(status_code=409, detail="The request uses a stale workflow version.")
            if existing.replayable_response_payload is not None:
                return existing, False
            raise HTTPException(status_code=409, detail="The AI turn is already running or failed; retry with a new idempotency key.")
        receipt = AIChatTurnReceipt(
            session_id=session_uuid,
            patient_id=access.patient_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="running",
            workflow_version=workflow_version,
        )
        db.add(receipt)
        return receipt, True

    def _claim_receipt(
        self,
        db: Any,
        *,
        session_uuid: uuid.UUID,
        access: AuthorizedAIChat,
        idempotency_key: str,
        request_hash: str,
        workflow_version: str,
    ) -> dict[str, Any] | None:
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            winner = self._load_receipt(
                db,
                session_uuid=session_uuid,
                idempotency_key=idempotency_key,
            )
            if winner is None:
                raise HTTPException(
                    status_code=409,
                    detail="The AI turn could not be claimed; retry with a new idempotency key.",
                ) from exc
            if not winner.matches_request_hash(request_hash):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was already used with a different request.",
                ) from exc
            if winner.workflow_version != workflow_version:
                raise HTTPException(
                    status_code=409,
                    detail="The request uses a stale workflow version.",
                ) from exc
            if winner.replayable_response_payload is not None:
                return dict(winner.replayable_response_payload)
            raise HTTPException(
                status_code=409,
                detail="The AI turn is already running or failed; retry with a new idempotency key.",
            ) from exc
        return None

    def _pending_clarification(self, snapshot: Any | None, *, workflow_version: str) -> bool:
        if snapshot is None:
            return False
        values = getattr(snapshot, "values", {}) or {}
        if not isinstance(values, Mapping):
            return False
        snapshot_workflow_version = values.get("workflow_version")
        if snapshot_workflow_version and snapshot_workflow_version != workflow_version:
            raise HTTPException(
                status_code=409,
                detail="The pending clarification uses a stale workflow version.",
            )
        pending = clarification_pending(snapshot, workflow_version=workflow_version)
        if pending:
            return True
        if values.get("clarification_question") and not values.get("clarification_answered"):
            raise HTTPException(
                status_code=409,
                detail="No pending clarification interrupt exists for this thread.",
            )
        return False

    def _cleanup_completed_checkpoint(self, session_id: str) -> None:
        checkpointer = getattr(self.runtime, "checkpointer", None)
        if checkpointer is None:
            graph = getattr(self.runtime, "graph", None)
            checkpointer = getattr(graph, "checkpointer", None)
        if checkpointer is None and callable(getattr(self.runtime, "delete_thread", None)):
            checkpointer = self.runtime
        if checkpointer is None:
            return
        try:
            cleanup_completed_checkpoint(checkpointer, session_id)
        except Exception:
            logger.warning("[checkpoint:cleanup] failure_category=checkpoint_cleanup_failed")

    def _run_preflight(
        self,
        *,
        access: AuthorizedAIChat,
        session_id: str,
        care_episode_id: object | None,
        workflow_version: str,
        request_hash: str,
        idempotency_key: str | None,
    ) -> None:
        policy = self.policy
        source_ids = {f"conversation:{session_id}"}
        if care_episode_id is not None:
            source_ids.add(f"episode:{care_episode_id}")
        result = evaluate_policy_gate(
            {
                "principal_authorized": True,
                "session_authorized": True,
                "care_episode_authorized": (
                    care_episode_id is None
                    or access.care_episode_id is not None
                    and str(access.care_episode_id) == str(care_episode_id)
                ),
                "workflow_version": workflow_version,
                "current_route": None,
                "requested_route": "consultant",
                "authorized_source_ids": source_ids,
                "source_ids": (),
                "authorized_tool_names": (),
                "tool_names": (),
                "allowed_catalog_ids": (),
                "catalog_ids": (),
                "model_calls": 0,
                "tool_calls": 0,
                "total_tokens": 0,
                "budget": policy.build_budget(),
                "required_nodes": (),
                "required_outcomes": {},
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "prior_request_hash": request_hash,
                "now": datetime.now(timezone.utc),
            },
            policy=policy,
        )
        if result.severity is GateSeverity.HARD_STOP:
            raise HTTPException(
                status_code=409,
                detail="The AI turn failed deterministic workflow preflight.",
            )

    def invoke_turn(
        self,
        user_input: str,
        access: AuthorizedAIChat,
        *,
        idempotency_key: str | None = None,
        workflow_version: str = WORKFLOW_VERSION,
    ) -> dict[str, Any]:
        user_input = str(user_input or "").strip()
        if not user_input:
            raise HTTPException(status_code=422, detail="Message is required.")
        if evaluator_fault_profile("service_unavailable"):
            raise HTTPException(status_code=503, detail="Evaluator service fault profile is active.")
        if len(user_input) > 8_000:
            raise HTTPException(status_code=422, detail="Message is too long.")
        if workflow_version != WORKFLOW_VERSION:
            raise HTTPException(status_code=409, detail="The request uses an unsupported workflow version.")
        if idempotency_key is not None:
            idempotency_key = str(idempotency_key).strip()
            if not idempotency_key:
                raise HTTPException(status_code=422, detail="Idempotency key is required.")

        db = self.session_factory()
        turn_started = perf_counter()
        active_turn_id: str | None = None
        try:
            session_uuid = uuid.UUID(access.session_id)
            session_query = db.query(AISession).filter(AISession.session_id == session_uuid)
            lock_session = getattr(session_query, "with_for_update", None)
            if callable(lock_session):
                session_query = lock_session()
            session = session_query.first()
            if session is None or str(getattr(session, "patient_id", "")) != str(access.patient_id):
                raise ValueError("Session not found")
            bound_episode_id = getattr(session, "care_episode_id", None)
            requested_episode_id = access.care_episode_id
            if (
                bound_episode_id is not None
                and requested_episode_id is not None
                and str(bound_episode_id) != str(requested_episode_id)
            ):
                raise HTTPException(status_code=409, detail="AI session is bound to another Care Episode")
            if bound_episode_id is None and requested_episode_id is not None:
                try:
                    binder = getattr(session, "bind_care_episode", None)
                    if callable(binder):
                        binder(requested_episode_id)
                    else:
                        setattr(session, "care_episode_id", requested_episode_id)
                    flush = getattr(db, "flush", None)
                    if callable(flush):
                        flush()
                    if callable(binder):
                        commit = getattr(db, "commit", None)
                        if callable(commit):
                            commit()
                    bound_episode_id = getattr(session, "care_episode_id", requested_episode_id)
                except Exception as exc:
                    rollback = getattr(db, "rollback", None)
                    if callable(rollback):
                        rollback()
                    raise HTTPException(
                        status_code=409,
                        detail="AI session could not be bound to the requested Care Episode",
                    ) from exc
            effective_episode_id = bound_episode_id or requested_episode_id
            effective_access = AuthorizedAIChat(
                patient_id=access.patient_id,
                session_id=access.session_id,
                care_episode_id=effective_episode_id,
            )

            request_hash = self._request_hash(user_input, effective_access, workflow_version)
            self._run_preflight(
                access=effective_access,
                session_id=str(session_uuid),
                care_episode_id=effective_episode_id,
                workflow_version=workflow_version,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
            )

            receipt: AIChatTurnReceipt | None = None
            newly_admitted = False
            if idempotency_key is not None:
                receipt, newly_admitted = self._preflight_receipt(
                    db,
                    session_uuid=session_uuid,
                    access=effective_access,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    workflow_version=workflow_version,
                )
                if receipt.replayable_response_payload is not None:
                    return dict(receipt.replayable_response_payload)
                if newly_admitted:
                    replay = self._claim_receipt(
                        db,
                        session_uuid=session_uuid,
                        access=access,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        workflow_version=workflow_version,
                    )
                    if replay is not None:
                        return replay

            session_model = getattr(getattr(self.runtime, "deps", None), "session_compaction_model", None)
            session_timeout = float(
                getattr(getattr(self.runtime, "deps", None), "session_compaction_timeout_seconds", 120.0)
            )
            (
                resolved_episode_id,
                memory_context,
                durable_cards,
                level1_session_ids,
            ) = self._load_context_sources(
                db,
                patient_id=access.patient_id,
                care_episode_id=uuid.UUID(str(effective_episode_id)) if effective_episode_id is not None else None,
            )
            session_view = prepare_session_context(
                db,
                session_uuid,
                model=session_model,
                timeout_seconds=session_timeout,
                current_user_message=user_input,
                durable_cards=durable_cards,
            )
            db.commit()
            session_cards = list(session_view.cards)
            candidate_cards = [*durable_cards, *session_cards]
            context = ContextAssembler(_evaluator_context_planner).assemble_for_turn(
                candidate_cards=candidate_cards,
                current_user_message=user_input,
                node_specs=NODE_SPECS,
            )
            context_prompts = {key: value.to_prompt_text() for key, value in context.packets.items()}
            context_packets = context_packet_metadata(context.packets)
            active_turn_id = create_active_turn(str(session_uuid))
            store_context_packets(active_turn_id, context_prompts)
            context_metadata = dict(getattr(context, "assembly_metadata", {}) or {})
            context_planner_error = str(context_metadata.get("planner_error") or "")
            context_event = _make_internal_event(
                "context_assembler",
                "context_assembly",
                getattr(getattr(context, "plan", None), "turn_summary", ""),
                **context_metadata,
                disclosure_trace=list(getattr(context, "disclosure_trace", ()) or ()),
                selected_sources={node_id: list(packet.included_source_ids) for node_id, packet in context.packets.items()},
                omitted_sources={node_id: list(packet.omitted_source_ids) for node_id, packet in context.packets.items()},
            )
            authorized_source_ids = {str(card.source_id) for card in candidate_cards}
            authorized_source_ids.add(PUBLIC_REHAB_KNOWLEDGE_SCOPE)
            selected_source_ids = {
                str(source_id)
                for packet in context.packets.values()
                for source_id in packet.included_source_ids
            }
            source_ids_by_source = {
                "conversation": sorted(
                    str(card.source_id)
                    for card in session_cards[-64:]
                ),
                "patient_memory": sorted(
                    str(card.source_id)
                    for card in durable_cards
                    if getattr(card, "scope", None) == "patient"
                ),
                "care_episode": sorted(
                    str(card.source_id)
                    for card in durable_cards
                    if getattr(card, "scope", None) == "episode"
                ),
                "rehab_knowledge": [PUBLIC_REHAB_KNOWLEDGE_SCOPE],
            }
            selected_source_ids.intersection_update(
                source_id
                for source_values in source_ids_by_source.values()
                for source_id in source_values
            )
            fresh_context = {
                "session_id": access.session_id,
                "patient_id": str(access.patient_id),
                "authenticated_owner": str(access.patient_id),
                "care_episode_id": resolved_episode_id,
                "memory_context": memory_context,
                "context_packets": context_packets,
                "context_disclosure_trace": list(getattr(context, "disclosure_trace", ()) or ()),
                "context_planner_error": context_planner_error,
                "workflow_version": workflow_version,
                "context_rehydrated": True,
                "principal_authorized": True,
                "session_authorized": True,
                "care_episode_authorized": self._care_episode_is_authorized(
                    db,
                    patient_id=access.patient_id,
                    care_episode_id=resolved_episode_id,
                ),
                "requested_route": "consultant",
                "authorized_source_ids": sorted(authorized_source_ids),
                "source_ids": sorted(selected_source_ids),
                "source_ids_by_source": source_ids_by_source,
                "evidence_turn_id": active_turn_id,
                "rehab_knowledge_requested": True,
                "rehab_knowledge_scope": PUBLIC_REHAB_KNOWLEDGE_SCOPE,
                "episode_memory_level1_session_ids": level1_session_ids,
                "authorized_tool_names": _consultant_tool_names_for_level1(level1_session_ids),
                "allowed_catalog_ids": [],
                "budget": self.policy.build_budget(),
                "model_calls": 0,
                "tool_calls": 0,
                "total_tokens": 0,
                "repair_count": 0,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "prior_request_hash": request_hash,
            }
            in_transaction = getattr(db, "in_transaction", None)
            if callable(in_transaction) and in_transaction():
                db.rollback()
            try:
                snapshot = self._graph_state(access.session_id, str(access.patient_id))
            except Exception as exc:
                raise HTTPException(status_code=503, detail="The AI conversation checkpoint is unavailable.") from exc
            review_attempt, review_turn_id = self._next_review_identity(
                snapshot,
                active_turn_id,
            )
            fresh_context.update(
                {
                    "review_attempt": review_attempt,
                    "review_turn_id": review_turn_id,
                }
            )
            pending = self._pending_clarification(snapshot, workflow_version=workflow_version)
            if pending:
                snapshot_values = getattr(snapshot, "values", {}) or {}
                if not isinstance(snapshot_values, Mapping):
                    snapshot_values = {}
                prior_repair_snapshot = snapshot_values.get("repair_context_snapshot")
                if isinstance(prior_repair_snapshot, Mapping):
                    fresh_context["repair_context_snapshot"] = dict(prior_repair_snapshot)
                else:
                    snapshot_state = dict(snapshot_values)
                    snapshot_state.update(fresh_context)
                    fresh_context["repair_context_snapshot"] = context_version_snapshot(snapshot_state)
                graph_input = Command(
                    update=fresh_context,
                    resume={"message": user_input},
                )
            else:
                conversation_history: list[dict[str, object]] = []
                for card in session_cards:
                    entry = _conversation_history_entry(card)
                    if entry is not None:
                        conversation_history.append(entry)
                graph_input = {
                    "user_input": user_input,
                    **fresh_context,
                    "conversation_history": conversation_history,
                    "consult_attempts": 0,
                    "debug_trace": [],
                    "internal_log_events": [context_event],
                }
                graph_input["debug_trace"] = _append_debug_event(
                    graph_input,
                    "graph_start",
                    {
                        "session_id": access.session_id,
                        "patient_id": str(access.patient_id),
                        "history_count": len(session_cards),
                        "context_candidate_count": context_metadata.get("candidate_count"),
                        "context_packet_tokens": context_metadata.get("packet_tokens"),
                        "care_episode_id": resolved_episode_id,
                        "has_memory_context": bool(memory_context),
                        "resuming_clarification": False,
                    },
                )
            try:
                output = self._invoke_runtime(
                    graph_input,
                    session_id=access.session_id,
                    owner_id=str(access.patient_id),
                )
            except Exception as exc:
                error_type = type(exc).__name__
                validation_errors = getattr(exc, "errors", None)
                if callable(validation_errors):
                    details = validation_errors()
                    if details and isinstance(details[0], Mapping):
                        first = details[0]
                        location = ".".join(str(value) for value in first.get("loc", ()))
                        validation_type = str(first.get("type") or "unknown")
                        error_type = ":".join(
                            part for part in (error_type, validation_type, location) if part
                        )[:255]
                logger.warning("[graph:error] failure_category=graph_execution_failed error_type=%s", error_type)
                output = {
                    "final_response": "I encountered an internal issue while processing your request. Please try again shortly.",
                    "debug_trace": _append_debug_event(fresh_context, "graph_error", {"failure_category": "graph_execution_failed", "error_type": error_type}),
                    "internal_log_events": [],
                    "status": "failed",
                }
            if not isinstance(output, dict):
                output = {
                    "final_response": "I encountered an internal issue while processing your request. Please try again shortly.",
                    "debug_trace": _append_debug_event(fresh_context, "graph_error", {"error": "Runtime returned a non-dict result."}),
                    "internal_log_events": [],
                    "status": "failed",
                }
            interrupt_value = self._interrupt_payload(output)
            output = {key: value for key, value in output.items() if key != "__interrupt__"}
            if interrupt_value is not None:
                output["status"] = "clarification_required"
                output["clarification_question"] = str(interrupt_value.get("question") or "")
                output["final_response"] = output["clarification_question"]
                output["workflow_version"] = workflow_version
            elif output.get("status") != "failed":
                output["status"] = "completed"
            output_internal_events = list(output.get("internal_log_events", []) or [])
            if context_event not in output_internal_events:
                output_internal_events.insert(0, context_event)
            output["internal_log_events"] = output_internal_events
            output["debug_trace"] = _append_debug_event(
                output,
                "graph_complete",
                {
                    "routing_decision": output.get("routing_decision"),
                    "needs_more_info": output.get("needs_more_info"),
                    "safety_passed": output.get("safety_passed"),
                    "final_response_len": len(output.get("final_response", "") or ""),
                    "status": output.get("status"),
                },
            )
            updated_history = list(output.get("conversation_history", []) or [])
            updated_history.append({"role": "user", "content": user_input})
            if output.get("final_response"):
                updated_history.append({"role": "assistant", "content": output["final_response"]})
            output["conversation_history"] = updated_history
            output["evaluation_telemetry"] = self._evaluation_telemetry(
                output,
                int((perf_counter() - turn_started) * 1000),
                self.policy,
                turn_correlation=active_turn_id,
            )
            if receipt is not None:
                receipt_status = (
                    "interrupted"
                    if output.get("status") == "clarification_required"
                    else "failed"
                    if output.get("status") == "failed"
                    else "completed"
                )
            else:
                receipt_status = (
                    "interrupted"
                    if output.get("status") == "clarification_required"
                    else "failed"
                    if output.get("status") == "failed"
                    else "completed"
                )
            persistence = AIChatTurnPersistence(db)
            try:
                persistence.persist_turn(
                    session_id=session_uuid,
                    patient_id=access.patient_id,
                    user_input=user_input,
                    output=output,
                    internal_trace=output,
                    receipt=receipt,
                    status=receipt_status,
                )
            except AIChatTurnPersistenceError:
                logger.warning("[db:error] failure_category=conversation_persistence_failed")
                output["status"] = "failed"
                output["final_response"] = "I encountered an issue saving our conversation, so this turn is currently unavailable. Please try again."
                return output
            if output.get("status") == "completed":
                self._cleanup_completed_checkpoint(access.session_id)
            return output
        finally:
            if active_turn_id:
                clear_active_turn(active_turn_id)
            db.close()
