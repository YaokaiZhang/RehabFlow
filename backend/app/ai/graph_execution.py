"""Authenticated graph execution and persistence boundary."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import replace
from typing import Any

from app.ai.active_turn_context import (
    begin_active_turn_tracking,
    clear_active_turn,
    create_active_turn,
    end_active_turn_tracking,
    store_context_packets,
)
from app.ai.checkpoint_privacy import ensure_metadata_only_checkpointer
from app.ai.context_assembler import ContextAssembler
from app.ai.context_assembler import (
    cards_from_memory_documents,
    cards_from_rehab_sessions,
    cards_from_triage_summary,
    context_packet_metadata,
    latest_triage_summary_for_episode,
)
from app.ai.context_integrity import PUBLIC_REHAB_KNOWLEDGE_SCOPE
from app.ai.context_types import ContextSourceCard
from app.ai.consultant_tools import (
    _consultant_tool_names_for_level1,
    _episode_memory_level1_session_ids,
)
from app.ai.evidence_registry import build_production_evidence_registry
from app.ai.graph_builder import build_rehab_graph
from app.ai.graph_dependencies import (
    QdrantKnowledgeStore,
    RehabGraphDeps,
    adapt_runtime_dependencies,
)
from app.ai.graph_log_persistence import _add_internal_log, _persist_internal_graph_logs
from app.ai.graph_prompt_context import (
    _load_memory_documents_for_episode,
    _restore_legacy_parent_session,
    _source_order_context_plan,
)
from app.ai.invocation_support import (
    _append_debug_event,
    _make_internal_event,
    DEBUG_GRAPH,
)
from app.ai.node_specs import NODE_SPECS
from app.ai.graph_routes import route_after_context_integrity
from app.ai.graph_state_projection import _initial_workflow_control_state, _legacy_request_hash
from app.ai.runtime_dependencies import AgentRuntimeDependencies, build_runtime_dependencies
from app.ai.session_compaction import prepare_session_context
from app.ai.workflow_policy import WorkflowPolicy
from app.ai.workflow_state import WORKFLOW_VERSION, Route
from app.core.config import evaluator_fault_profile, evaluator_mode_enabled, get_settings
from app.db.models import AIMessage, AISession, CareEpisode, ai_message_ordering
from app.services.rehab_session_context import rehab_sessions_for_episode

SessionLocal = None
logger = logging.getLogger(__name__)

def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return str(value)


from app.ai.graph_log_persistence import (
    _add_internal_log,
    _persist_internal_graph_logs,
)

async def _ainvoke_graph(graph: Any, state: dict[str, Any]) -> dict[str, Any]:
    ainvoke = getattr(graph, "ainvoke", None)
    if callable(ainvoke):
        return await ainvoke(state)
    # Compatibility for narrow test doubles; production compiled graphs use
    # ainvoke and therefore exercise LangGraph's async Send scheduler.
    return await asyncio.to_thread(graph.invoke, state)


def run_rehab_graph(
    user_input: str,
    session_id: str | None = None,
    patient_id: str | None = None,
    care_episode_id: str | None = None,
    *,
    principal_id: str | None = None,
) -> dict:
    from app.db.models import AIMessage, AISession, CareEpisode, ai_message_ordering

    if not principal_id:
        raise PermissionError("authenticated principal ownership is required")
    try:
        principal_uuid = uuid.UUID(str(principal_id))
    except (TypeError, ValueError) as exc:
        raise PermissionError("authenticated principal ownership is invalid") from exc
    if patient_id is not None:
        try:
            supplied_patient_uuid = uuid.UUID(str(patient_id))
        except (TypeError, ValueError) as exc:
            raise PermissionError("caller patient ownership is invalid") from exc
        if supplied_patient_uuid != principal_uuid:
            raise PermissionError("caller patient ownership does not match the principal")

    runtime_deps = None
    active_turn_id: str | None = None
    active_turn_ids, active_turn_tracking_token = begin_active_turn_tracking()
    created_parent_session: AISession | None = None
    global SessionLocal
    if SessionLocal is None:
        from app.db.session import SessionLocal as configured_session_factory
        SessionLocal = configured_session_factory
    db = SessionLocal()
    try:
        # --- DB Session Resolution (Kept Original Pattern) ---
        if session_id:
            session_uuid = uuid.UUID(session_id)
            session_obj = db.query(AISession).filter(AISession.session_id == session_uuid).first()
            if not session_obj:
                raise ValueError("Session not found")
            try:
                session_patient_uuid = uuid.UUID(str(session_obj.patient_id))
            except (TypeError, ValueError) as exc:
                raise PermissionError("session ownership is invalid") from exc
            if session_patient_uuid != principal_uuid:
                raise PermissionError("session is not owned by the authenticated principal")
            patient_uuid = session_patient_uuid
            session_authorized = True
        else:
            session_uuid = uuid.uuid4()
            patient_uuid = principal_uuid
            created_parent_session = AISession(session_id=session_uuid, patient_id=patient_uuid)
            db.add(created_parent_session)
            flush = getattr(db, "flush", None)
            if callable(flush):
                flush()
            session_authorized = True

        principal_authorized = patient_uuid == principal_uuid
        if not principal_authorized or not session_authorized:
            raise PermissionError("direct graph invocation failed authorization")

        runtime_deps = default_runtime_dependencies(None)

        # --- DB History Loading ---
        messages = db.query(AIMessage).filter(AIMessage.session_id == session_uuid).order_by(*ai_message_ordering()).all()

        session_view = prepare_session_context(
            db,
            session_uuid,
            model=getattr(runtime_deps, "session_compaction_model", None),
            timeout_seconds=float(getattr(runtime_deps, "session_compaction_timeout_seconds", 120.0)),
            current_user_message=user_input,
        )
        session_cards = list(session_view.cards)
        conversation_history: list[dict[str, Any]] = []
        for card in session_cards:
            if card.source_type not in {"session_compacted_block", "session_message"}:
                continue
            history_entry: dict[str, Any] = {
                "role": (
                    "context"
                    if card.source_type == "session_compacted_block"
                    else str(card.metadata.get("sender_role") or card.title)
                ),
                "content": card.text,
            }
            if card.source_type == "session_message" and card.metadata.get("sequence") is not None:
                history_entry["sequence"] = card.metadata["sequence"]
            conversation_history.append(history_entry)

        # --- Memory Documents Context Loading ---
        resolved_care_episode_id, memory_context, patient_memory_document, episode_memory_document = (
            _load_memory_documents_for_episode(
                db,
                patient_uuid,
                care_episode_id,
                parent_session=created_parent_session,
            )
        )
        memory_cards = cards_from_memory_documents(patient_memory_document, episode_memory_document)
        rehab_session_cards: list[ContextSourceCard] = []
        if resolved_care_episode_id:
            try:
                rehab_sessions = rehab_sessions_for_episode(
                    db,
                    uuid.UUID(str(resolved_care_episode_id)),
                    patient_uuid,
                )
                rehab_session_cards = cards_from_rehab_sessions(rehab_sessions)
            except Exception:
                logger.warning("[context_assembly:error] failure_category=rehab_session_source_unavailable")
                if hasattr(db, "rollback"):
                    db.rollback()
                _restore_legacy_parent_session(db, created_parent_session)
        triage_summary_cards: list[ContextSourceCard] = []
        if episode_memory_document is not None and resolved_care_episode_id:
            try:
                triage_summary_cards = cards_from_triage_summary(
                    latest_triage_summary_for_episode(db, uuid.UUID(str(resolved_care_episode_id)))
                )
            except Exception:
                logger.warning("[context_assembly:error] failure_category=triage_source_unavailable")
                if hasattr(db, "rollback"):
                    db.rollback()
                _restore_legacy_parent_session(db, created_parent_session)
        candidate_cards = [*memory_cards, *triage_summary_cards, *rehab_session_cards, *session_cards]
        context_result = ContextAssembler(_source_order_context_plan).assemble_for_turn(
            candidate_cards=candidate_cards,
            current_user_message=user_input,
            node_specs=NODE_SPECS,
        )
        context_prompts = {
            node_id: packet.to_prompt_text()
            for node_id, packet in context_result.packets.items()
        }
        context_packets = context_packet_metadata(context_result.packets)
        authorized_source_ids = sorted(
            {
                *{card.source_id for card in candidate_cards},
                PUBLIC_REHAB_KNOWLEDGE_SCOPE,
            }
        )
        source_ids_by_source = {
            "conversation": sorted(
                card.source_id for card in session_cards
            ),
            "patient_memory": sorted(
                card.source_id for card in memory_cards if card.scope == "patient"
            ),
            "care_episode": sorted(
                [
                    *(
                        card.source_id
                        for card in memory_cards
                        if card.scope == "episode"
                    ),
                    *(card.source_id for card in triage_summary_cards),
                    *(card.source_id for card in rehab_session_cards),
                ]
            ),
            "rehab_knowledge": [PUBLIC_REHAB_KNOWLEDGE_SCOPE],
        }
        source_ids = sorted(
            {
                source_id
                for packet in context_result.packets.values()
                for source_id in packet.included_source_ids
            }
        )
        care_episode_authorized = care_episode_id is None
        if care_episode_id is not None:
            try:
                requested_episode = db.get(CareEpisode, uuid.UUID(str(care_episode_id)))
                care_episode_authorized = bool(
                    requested_episode is not None
                    and str(getattr(requested_episode, "patient_id", "")) == str(patient_uuid)
                    and (getattr(requested_episode, "status", None) or "active") == "active"
                )
            except (TypeError, ValueError):
                care_episode_authorized = False

        context_assembly_event = _make_internal_event(
            "context_assembler",
            "context_assembly",
            context_result.plan.turn_summary,
            **context_result.assembly_metadata,
            selected_sources={
                node_id: list(packet.included_source_ids)
                for node_id, packet in context_result.packets.items()
            },
            omitted_sources={
                node_id: list(packet.omitted_source_ids)
                for node_id, packet in context_result.packets.items()
            },
        )

        # --- Graph Execution ---
        evidence_registry = build_production_evidence_registry(
            session_factory=SessionLocal,
            retrieval=runtime_deps.retrieval,
        )
        try:
            runtime_deps = replace(runtime_deps, evidence_registry=evidence_registry)
        except TypeError:
            # Narrow legacy test doubles may be SimpleNamespace-like rather
            # than dataclass instances; keep their existing seam intact.
            setattr(runtime_deps, "evidence_registry", evidence_registry)
        deps = adapt_runtime_dependencies(runtime_deps)
        active_turn_id = create_active_turn(str(session_uuid))
        store_context_packets(active_turn_id, context_prompts)
        graph_builder = build_rehab_graph(deps)
        graph = graph_builder.compile() if hasattr(graph_builder, "compile") else graph_builder

        initial_state = {
            **_initial_workflow_control_state(deps.policy),
            "user_input": user_input,
            "session_id": str(session_uuid),
            "patient_id": str(patient_uuid),
            "care_episode_id": resolved_care_episode_id,
            "workflow_version": WORKFLOW_VERSION,
            "context_rehydrated": True,
            "principal_id": str(principal_uuid),
            "principal_authorized": principal_authorized,
            "session_authorized": session_authorized,
            "care_episode_authorized": care_episode_authorized,
            "requested_route": Route.CONSULTANT,
            "authorized_source_ids": authorized_source_ids,
            "source_ids": source_ids,
            "source_ids_by_source": source_ids_by_source,
            "evidence_turn_id": active_turn_id,
            "trace_turn_id": active_turn_id,
            "rehab_knowledge_requested": True,
            "episode_memory_level1_session_ids": _episode_memory_level1_session_ids(
                getattr(episode_memory_document, "level1_session_ids", [])
            ),
            "authorized_tool_names": _consultant_tool_names_for_level1(
                getattr(episode_memory_document, "level1_session_ids", [])
            ),
            "allowed_catalog_ids": [],
            "model_calls": 0,
            "tool_calls": 0,
            "total_tokens": 0,
            "idempotency_key": None,
            "request_hash": _legacy_request_hash(
                user_input=user_input,
                session_id=session_uuid,
                care_episode_id=resolved_care_episode_id,
            ),
            "prior_request_hash": None,
            "memory_context": memory_context,
            "conversation_history": conversation_history,
            "context_packets": context_packets,
            "consult_attempts": 0,
            "debug_trace": [],
            "internal_log_events": [context_assembly_event],
        }
        initial_state["debug_trace"] = _append_debug_event(
            initial_state,
            "graph_start",
            {
                "session_id": str(session_uuid),
                "patient_id": str(patient_uuid),
                "history_count": len(conversation_history),
                "injected_history_count": 0,
                "context_candidate_count": context_result.assembly_metadata.get("candidate_count"),
                "context_packet_tokens": context_result.assembly_metadata.get("packet_tokens"),
                "care_episode_id": resolved_care_episode_id,
                "has_memory_context": bool(memory_context),
            },
        )

        try:
            output = asyncio.run(_ainvoke_graph(graph, initial_state))
        except Exception as graph_exc:
            logger.warning("[graph:error] failure_category=graph_execution_failed")
            output = {
                "final_response": (
                    "I encountered an internal issue while processing your request. "
                    "Please try again shortly."
                ),
                "debug_trace": _append_debug_event(
                    initial_state,
                    "graph_error",
                    {"failure_category": "graph_execution_failed"},
                ),
            }

        output_internal_events = list(output.get("internal_log_events", []) or [])
        if context_assembly_event not in output_internal_events:
            output_internal_events.insert(0, context_assembly_event)
        output["internal_log_events"] = output_internal_events
        output["debug_trace"] = _append_debug_event(
            output,
            "graph_complete",
            {
                "routing_decision": output.get("routing_decision"),
                "needs_more_info": output.get("needs_more_info"),
                "safety_passed": output.get("safety_passed"),
                "final_response_len": len(output.get("final_response", "") or ""),
            },
        )

        if DEBUG_GRAPH:
            logger.info("[graph-output] debug_trace_event_count=%d", len(output.get("debug_trace", []) or []))

        # Keep short-term memory current for next turn persistence in DB transcript.
        updated_history = list(output.get("conversation_history", []))
        updated_history.append({"role": "user", "content": user_input})
        if output.get("final_response"):
            updated_history.append({"role": "assistant", "content": output["final_response"]})
        output["conversation_history"] = updated_history

        # --- DB Save ---
        message_sequence = max(
            (
                int((getattr(row, "message_metadata", {}) or {}).get("sequence", 0))
                for row in messages
                if isinstance(getattr(row, "message_metadata", {}), dict)
            ),
            default=len(messages),
        ) + 1

        def add_message(sender_role: str, content: str) -> None:
            nonlocal message_sequence
            db.add(
                AIMessage(
                    session_id=session_uuid,
                    sender_role=sender_role,
                    content=content,
                    message_metadata={"sequence": message_sequence},
                )
            )
            message_sequence += 1

        add_message("user", user_input)

        if output.get("final_response"):
            add_message("assistant", output["final_response"])

        _persist_internal_graph_logs(db, session_uuid, patient_uuid, output)

        try:
            db.commit()
        except Exception:
            logger.warning("[db:error] failure_category=conversation_persistence_failed")
            db.rollback()
            output["final_response"] = "I encountered an issue saving our conversation, but let's continue."

        return output
    finally:
        for turn_id in active_turn_ids:
            clear_active_turn(turn_id)
        end_active_turn_tracking(active_turn_tracking_token)
        if runtime_deps is not None:
            retrieval = getattr(runtime_deps, "retrieval", None)
            close = getattr(retrieval, "close", None)
            if callable(close):
                close()
        db.close()


def default_runtime_dependencies(checkpointer: Any) -> AgentRuntimeDependencies:
    return build_runtime_dependencies(
        checkpointer,
        vector_store_factory=QdrantKnowledgeStore,
        model_overrides={},
        policy=WorkflowPolicy(debug=DEBUG_GRAPH),
    )
