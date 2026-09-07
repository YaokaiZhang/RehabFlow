"""Shared prompt rendering and authorized context projection for graph nodes."""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Mapping

from langchain_core.messages import AIMessage as LangChainAIMessage, HumanMessage

from app.ai.active_turn_context import active_context_packets
from app.ai.context_types import ContextPlan, ContextSourceCard
from app.ai.workflow_state import RehabGraphState
from app.db.models import AISession

logger = logging.getLogger(__name__)


def build_memory_context_pack(*args: Any, **kwargs: Any) -> Any:
    from app.services.memory_documents import build_memory_context_pack as build_pack

    return build_pack(*args, **kwargs)


def _safe_prompt_text(value: Any, max_len: int = 260) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_len else f"{text[:max_len]}..."


def _node_context_prompt(state: RehabGraphState, node_id: str) -> str:
    packet = active_context_packets(state.get("evidence_turn_id")).get(
        node_id, ""
    ).strip()
    if not packet:
        legacy_packets = state.get("context_packets") or {}
        legacy_packet = legacy_packets.get(node_id)
        if isinstance(legacy_packet, str):
            packet = legacy_packet.strip()
    if not packet:
        return "Assembled Context: None"
    return f"Assembled Context for {node_id}:\n{packet}"


def _router_model_fields(
    state: RehabGraphState,
    fields: object,
) -> tuple[str, ...]:
    del state
    if not isinstance(fields, (list, tuple, set, frozenset)):
        return ()
    normalized_fields = (
        re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
        for value in fields
        if str(value).strip()
    )
    return tuple(dict.fromkeys(field for field in normalized_fields if field))


def _router_answered_fields(
    state: RehabGraphState,
    fields: object,
) -> set[str]:
    raw = state.get("clarification_answered_fields")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    allowed = {
        str(value).strip()
        for value in (
            fields if isinstance(fields, (list, tuple, set, frozenset)) else ()
        )
        if str(value).strip()
    }
    return {str(value).strip() for value in raw if str(value).strip() in allowed}


def _router_retrieval_owned_request(
    state: RehabGraphState,
    fields: object,
) -> bool:
    del fields
    return state.get("retrieval_owned_request") is True




def _sanitize_clarification_question(question: object, fields: object) -> str:
    del fields
    sanitized = str(question or "").strip()
    if re.search(
        r"(?<![a-z0-9])[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+(?![a-z0-9])",
        sanitized,
        re.IGNORECASE,
    ):
        return ""
    return sanitized


def _history_messages(state: RehabGraphState) -> list[Any]:
    raw_history = state.get("conversation_history")
    if not isinstance(raw_history, (list, tuple)):
        return []
    messages: list[Any] = []
    for raw_message in raw_history:
        if not isinstance(raw_message, Mapping):
            continue
        role = str(raw_message.get("role") or "").strip().lower()
        content = str(raw_message.get("content") or "")
        if not content.strip():
            continue
        marker = {"rehab_session_history": True}
        sequence = raw_message.get("sequence")
        if sequence is not None:
            marker["sequence"] = sequence
        if role == "user":
            messages.append(HumanMessage(content=content, additional_kwargs=marker))
        elif role == "assistant":
            messages.append(
                LangChainAIMessage(content=content, additional_kwargs=marker)
            )
        elif role == "context":
            messages.append(
                HumanMessage(
                    content=content,
                    additional_kwargs={
                        **marker,
                        "rehab_session_compaction": True,
                    },
                )
            )
    return messages


def _without_recent_turns(prompt: str) -> str:
    lines = prompt.splitlines()
    filtered: list[str] = []
    skip_section = False
    for line in lines:
        if line.startswith("## "):
            skip_section = line[3:].strip() == "recent_turns"
            if not skip_section:
                filtered.append(line)
            continue
        if not skip_section:
            filtered.append(line)
    return "\n".join(filtered).strip()


def _memory_context_prompt(state: RehabGraphState) -> str:
    context = (state.get("memory_context") or "").strip()
    if not context:
        return "Memory Documents Context: None"
    return (
        "Memory Documents Context (compiled source-labeled Patient Memory and Episode Memory; "
        "do not treat it as raw chat history):\n"
        f"{context}"
    )


def _restore_legacy_parent_session(db, parent_session: AISession | None) -> None:
    if parent_session is None:
        return
    db.add(parent_session)
    flush = getattr(db, "flush", None)
    if callable(flush):
        flush()


def _load_memory_documents_for_episode(
    db,
    patient_uuid: uuid.UUID,
    care_episode_id: str | None,
    parent_session: AISession | None = None,
) -> tuple[str | None, str, object | None, object | None]:
    from app.db.models import CareEpisode

    if not care_episode_id:
        return None, "", None, None
    raw_episode_id = str(care_episode_id)
    try:
        episode_uuid = uuid.UUID(raw_episode_id)
    except (TypeError, ValueError):
        return raw_episode_id, "", None, None
    try:
        episode = db.get(CareEpisode, episode_uuid)
        if episode is None or str(episode.patient_id) != str(patient_uuid):
            return str(episode_uuid), "", None, None
        episode_status = getattr(episode, "status", None) or "active"
        if episode_status != "active":
            return str(episode_uuid), "", None, None
        patient_document, episode_document, compiled_context = (
            build_memory_context_pack(db, episode)
        )
        return (
            str(episode_uuid),
            compiled_context or "",
            patient_document,
            episode_document,
        )
    except Exception:
        logger.warning(
            "[memory_context:error] failure_category=memory_context_unavailable"
        )
        if hasattr(db, "rollback"):
            db.rollback()
        _restore_legacy_parent_session(db, parent_session)
        return str(episode_uuid), "", None, None


def _source_order_context_plan(
    *,
    candidate_cards: list[ContextSourceCard],
    node_specs: dict[str, Any],
    **_kwargs: Any,
) -> ContextPlan:
    source_ids = tuple(card.source_id for card in candidate_cards)
    return ContextPlan(
        turn_summary="Deterministic source-order context plan.",
        ranked_source_ids=source_ids,
        node_hints={node_id: source_ids for node_id in node_specs},
        unresolved_questions=(),
        stale_source_ids=(),
        summary_update_needed=False,
        summary_notes="Context assembled without semantic planning.",
    )


def _safety_revision_instruction(state: RehabGraphState, attempts: int) -> str:
    feedback = (state.get("safety_feedback") or "").strip()
    if attempts <= 1 or not feedback:
        return "Safety Feedback From Previous Attempt: None"
    return (
        "FAILED SAFETY REVIEW - mandatory revision required.\n"
        f"Safety Feedback From Previous Attempt:\n{feedback}\n"
        "Revise the plan to satisfy every safety concern above before giving the final answer. "
        "Do not repeat the prior plan unless it has been changed to directly address the reviewer feedback."
    )
