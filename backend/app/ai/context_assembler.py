"""Context source cards and uncapped source-order packet assembly."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.ai.context_types import (
    MAX_CONTEXT_MODEL_TOKENS,
    ContextAssemblyResult,
    ContextPlan,
    ContextSourceCard,
    NodeContextPacket,
    NodeSpec,
)
from app.ai.node_specs import NODE_SPECS
from app.services.rehab_session_context import format_rehab_session_context


TRANSCRIPT_ROLES = {"user", "assistant"}


def estimate_tokens(text: str) -> int:
    compact = " ".join(str(text or "").split())
    return max(1, (len(compact) + 3) // 4) if compact else 0


def context_packet_metadata(packets: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(node_id): {
            "available": bool(packet.included_source_ids),
            "included_source_ids": list(packet.included_source_ids),
            "omitted_source_ids": list(packet.omitted_source_ids),
            "estimated_tokens": packet.estimated_tokens,
        }
        for node_id, packet in packets.items()
    }


def _attr(value: object, name: str, default: Any = None) -> Any:
    return getattr(value, name, default)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(value)
    return str(value)


def cards_from_memory_documents(
    patient_document: object | None,
    episode_document: object | None,
) -> list[ContextSourceCard]:
    cards: list[ContextSourceCard] = []
    if patient_document is not None and _attr(patient_document, "status", "active") == "active":
        summary = _stringify(_attr(patient_document, "summary_text")).strip()
        fields = _attr(patient_document, "editable_fields", {}) or {}
        field_text = _stringify(fields).strip()
        text = "\n".join(
            part
            for part in (
                f"Summary: {summary}" if summary else "",
                f"Editable fields: {field_text}" if field_text else "",
            )
            if part
        )
        if text:
            cards.append(
                ContextSourceCard(
                    source_id="memory:patient",
                    source_type="patient_memory_summary",
                    scope="patient",
                    title="Patient Memory",
                    text=text,
                    metadata={"scope": "patient"},
                    priority_hint="normal",
                )
            )

    if episode_document is not None and _attr(episode_document, "status", "active") == "active":
        keywords = [
            str(value).strip()
            for value in (_attr(episode_document, "level1_keywords", []) or [])
            if str(value).strip()
        ]
        description = _stringify(_attr(episode_document, "level1_description")).strip()
        session_ids = [
            str(value).strip()
            for value in (_attr(episode_document, "level1_session_ids", []) or [])
            if str(value).strip()
        ]
        level1 = "\n".join(
            part
            for part in (
                f"Keywords: {', '.join(keywords)}" if keywords else "",
                f"Description: {description}" if description else "",
                f"Relevant session IDs: {', '.join(session_ids)}" if session_ids else "",
            )
            if part
        )
        if level1:
            cards.append(
                ContextSourceCard(
                    source_id="memory:episode:level1",
                    source_type="episode_memory_level_1",
                    scope="episode",
                    title="Episode Memory Level 1",
                    text=level1,
                    metadata={"scope": "episode", "level": 1},
                    priority_hint="high",
                )
            )
    return cards


def cards_from_triage_summary(summary: object | None) -> list[ContextSourceCard]:
    if summary is None:
        return []
    summary_id = str(_attr(summary, "triage_summary_id", "") or "").strip()
    if not summary_id:
        return []
    fields = [
        ("Concern", _attr(summary, "concern")),
        ("Relevant context", _attr(summary, "relevant_context")),
        ("Safety signals", _attr(summary, "safety_signals")),
        ("Limitations", _attr(summary, "limitations")),
        ("Recommendation", _attr(summary, "recommendation")),
        ("Missing information", _attr(summary, "missing_information")),
        ("Unresolved questions", _attr(summary, "unresolved_questions")),
        ("Clinician review needed", _attr(summary, "clinician_review_needed")),
        ("Additional context", _attr(summary, "additional_context")),
    ]
    text = "\n".join(
        f"{label}: {_stringify(value).strip()}"
        for label, value in fields
        if _stringify(value).strip()
    )
    return [
        ContextSourceCard(
            source_id=f"triage_summary:{summary_id}",
            source_type="triage_summary",
            scope="episode",
            title="Saved triage summary",
            text=text,
            metadata={"scope": "episode"},
            priority_hint="high",
        )
    ]


def cards_from_rehab_session(session: object | None) -> list[ContextSourceCard]:
    if session is None:
        return []
    session_id = str(_attr(session, "session_id", "") or "").strip()
    if not session_id:
        return []
    return [
        ContextSourceCard(
            source_id=f"rehab_activity:{session_id}",
            source_type="rehab_session",
            scope="episode",
            title="Saved rehab session",
            text=format_rehab_session_context(session),
            metadata={"scope": "episode"},
            priority_hint="high",
        )
    ]


def cards_from_rehab_sessions(sessions: Iterable[object]) -> list[ContextSourceCard]:
    cards: list[ContextSourceCard] = []
    for index, session in enumerate(sessions):
        for card in cards_from_rehab_session(session):
            cards.append(
                ContextSourceCard(
                    source_id=card.source_id,
                    source_type=card.source_type,
                    scope=card.scope,
                    title=(
                        "Most recent saved rehab session"
                        if index == 0
                        else f"Earlier saved rehab session {index + 1}"
                    ),
                    text=card.text,
                    metadata=card.metadata,
                    priority_hint="high" if index == 0 else "normal",
                )
            )
    return cards


def latest_triage_summary_for_episode(db: object, care_episode_id: object) -> object | None:
    from sqlalchemy import select

    from app.db.models import CareEpisodeTriageSummary

    return (
        db.execute(
            select(CareEpisodeTriageSummary)
            .where(CareEpisodeTriageSummary.care_episode_id == care_episode_id)
            .order_by(
                CareEpisodeTriageSummary.version.desc(),
                CareEpisodeTriageSummary.created_at.desc(),
            )
            .limit(1)
        )
        .scalar_one_or_none()
    )


def cards_from_session_messages(messages: Iterable[object]) -> list[ContextSourceCard]:
    cards: list[ContextSourceCard] = []
    for index, message in enumerate(messages):
        role = str(_attr(message, "sender_role", "") or "").strip()
        if role not in TRANSCRIPT_ROLES:
            continue
        message_id = str(_attr(message, "message_id", index) or index)
        metadata = _attr(message, "message_metadata", {}) or {}
        sequence = metadata.get("sequence") if isinstance(metadata, Mapping) else None
        cards.append(
            ContextSourceCard(
                source_id=f"ai_message:{message_id}",
                source_type="session_message",
                scope="session",
                title=role,
                text=str(_attr(message, "content", "") or ""),
                metadata={"sender_role": role, "sequence": sequence},
                priority_hint="normal",
            )
        )
    return cards


def _candidate_count_by_source_type(candidate_cards: Iterable[ContextSourceCard]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for card in candidate_cards:
        counts[card.source_type] = counts.get(card.source_type, 0) + 1
    return counts


def _section_for_card(card: ContextSourceCard) -> str | None:
    if card.source_type == "patient_memory_summary":
        return "patient_memory"
    if card.source_type == "episode_memory_level_1":
        return "episode_memory"
    if card.source_type == "triage_summary" or card.source_id.startswith("triage_summary:"):
        return "triage_summary"
    if card.source_type in {"rehab_session", "rehab_activity"}:
        return "episode_memory"
    if card.source_type in {"session_message", "session_compacted_block", "session_internal"}:
        return "recent_turns"
    if card.source_type in {"retrieval_context", "retrieved_doc", "knowledge_document"}:
        return "retrieval_context"
    if card.source_id.startswith(("retrieval_context:", "retrieved_doc:", "knowledge_document:")):
        return "retrieval_context"
    return None


def deterministic_context_plan(
    *,
    candidate_cards: Iterable[ContextSourceCard],
    node_specs: Mapping[str, NodeSpec],
    **_ignored: Any,
) -> ContextPlan:
    source_ids = tuple(card.source_id for card in candidate_cards)
    return ContextPlan(
        turn_summary="Source-order context assembly.",
        ranked_source_ids=source_ids,
        node_hints={node_id: source_ids for node_id in node_specs},
        unresolved_questions=(),
        stale_source_ids=(),
        summary_update_needed=False,
        summary_notes="",
    )


class ContextAssembler:
    def __init__(self, planner: object | None = None):
        self.planner = planner

    def _call_planner(self, **kwargs: Any) -> ContextPlan:
        if self.planner is None:
            return deterministic_context_plan(**kwargs)
        planner_method = getattr(self.planner, "plan", None)
        if callable(planner_method):
            return planner_method(**kwargs)
        if callable(self.planner):
            return self.planner(**kwargs)
        return deterministic_context_plan(**kwargs)

    def assemble_for_turn(
        self,
        *,
        candidate_cards: Iterable[ContextSourceCard],
        current_user_message: str,
        node_specs: Mapping[str, NodeSpec] = NODE_SPECS,
        node_required_fields: Mapping[str, str] | None = None,
        **_ignored: Any,
    ) -> ContextAssemblyResult:
        cards = list(candidate_cards)
        specs = dict(node_specs)
        required_fields = dict(node_required_fields or {})
        allowed_source_ids = {card.source_id for card in cards}
        planner_error = ""
        try:
            plan = self._call_planner(
                candidate_cards=cards,
                current_user_message=current_user_message,
                node_specs=specs,
                node_required_fields=required_fields,
            ).restrict_to_source_ids(allowed_source_ids)
        except Exception as exc:
            planner_error = type(exc).__name__
            plan = deterministic_context_plan(candidate_cards=cards, node_specs=specs)
        packets = {
            node_id: self._packet_for_spec(
                spec=spec,
                cards=cards,
                plan=plan,
                current_user_message=current_user_message,
                required_fields=required_fields,
            )
            for node_id, spec in specs.items()
        }
        progressive_levels = self._progressive_levels(cards)
        metadata: dict[str, Any] = {
            "candidate_count": len(cards),
            "candidate_count_by_source_type": _candidate_count_by_source_type(cards),
            "packet_tokens": {
                node_id: packet.estimated_tokens for node_id, packet in packets.items()
            },
            "context_ceiling_tokens": MAX_CONTEXT_MODEL_TOKENS,
            "progressive_disclosure": True,
            "progressive_level_tokens": {
                level: estimate_tokens(prompt)
                for level, prompt in progressive_levels.items()
            },
        }
        if planner_error:
            metadata["planner_error"] = planner_error
        disclosure_trace = tuple(
            {
                "level": level,
                "reason": reason,
                "estimated_tokens": estimate_tokens(progressive_levels[level]),
            }
            for level, reason in (
                ("level_1", "initial Patient and Episode Level 1 memory"),
                ("level_2", "consultant memory tool request"),
                ("level_3", "session conversation search tool request"),
            )
        )
        return ContextAssemblyResult(
            plan=plan,
            packets=packets,
            assembly_metadata=metadata,
            progressive_levels=progressive_levels,
            disclosure_trace=disclosure_trace,
        )

    @staticmethod
    def _progressive_levels(cards: list[ContextSourceCard]) -> dict[str, str]:
        level_1 = "\n\n".join(
            card.to_prompt_text()
            for card in cards
            if card.source_type in {"patient_memory_summary", "episode_memory_level_1"}
        ).strip()
        return {"level_1": level_1, "level_2": "", "level_3": ""}

    def _packet_for_spec(
        self,
        *,
        spec: NodeSpec,
        cards: list[ContextSourceCard],
        plan: ContextPlan,
        current_user_message: str,
        required_fields: Mapping[str, str],
    ) -> NodeContextPacket:
        cards_by_id = {card.source_id: card for card in cards}
        ordered_source_ids = self._ordered_source_ids(spec.node_id, plan, cards)
        cards_by_section: dict[str, list[ContextSourceCard]] = {
            section: [] for section in spec.context_sections
        }
        for source_id in ordered_source_ids:
            card = cards_by_id[source_id]
            section = _section_for_card(card)
            if section in cards_by_section:
                cards_by_section[section].append(card)

        sections: dict[str, str] = {}
        included_source_ids: list[str] = []
        for section in spec.context_sections:
            section_parts = [
                current_user_message.strip()
            ] if section == "current_user_message" and current_user_message.strip() else []
            section_parts.extend(
                value.strip()
                for key, value in required_fields.items()
                if key == section and str(value).strip()
            )
            for card in cards_by_section[section]:
                section_parts.append(card.to_prompt_text())
                included_source_ids.append(card.source_id)
            if section_parts:
                sections[section] = "\n\n".join(section_parts)

        return NodeContextPacket(
            node_id=spec.node_id,
            sections=sections,
            included_source_ids=tuple(included_source_ids),
            omitted_source_ids=(),
            estimated_tokens=sum(estimate_tokens(value) for value in sections.values()),
        )

    @staticmethod
    def _ordered_source_ids(
        node_id: str,
        plan: ContextPlan,
        cards: list[ContextSourceCard],
    ) -> tuple[str, ...]:
        original_source_ids = tuple(card.source_id for card in cards)
        candidates = (
            *plan.node_hints.get(node_id, ()),
            *plan.ranked_source_ids,
            *original_source_ids,
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for source_id in candidates:
            if source_id in seen or source_id not in original_source_ids:
                continue
            seen.add(source_id)
            ordered.append(source_id)
        return tuple(ordered)
