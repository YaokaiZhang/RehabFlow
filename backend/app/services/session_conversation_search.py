from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, func, literal, or_, select
from sqlalchemy.orm import Session

from app.db.models import AIMessage, AISession, CareEpisode, ai_message_ordering


MAX_SEARCH_HITS = 5


class SessionConversationSearchError(ValueError):
    """Raised when a requested session search cannot be safely executed."""


@dataclass(frozen=True)
class SessionConversationSearchResult:
    messages: tuple[dict[str, str], ...]
    matched: int


def _sequence(row: AIMessage) -> int | None:
    value = (row.message_metadata or {}).get("sequence")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _validate_scope(
    db: Session,
    *,
    patient_id: UUID,
    care_episode_id: UUID,
    session_id: UUID,
) -> None:
    episode = db.get(CareEpisode, care_episode_id)
    session = db.get(AISession, session_id)
    if (
        episode is None
        or episode.patient_id != patient_id
        or str(getattr(episode, "status", "active")) != "active"
        or session is None
        or session.patient_id != patient_id
        or session.care_episode_id != care_episode_id
    ):
        raise SessionConversationSearchError("session_scope_invalid")


def _parser_node_count(db: Session, query: str) -> int:
    try:
        value = db.execute(
            select(func.numnode(func.plainto_tsquery("english", query)))
        ).scalar_one()
        return int(value or 0)
    except Exception as exc:
        raise SessionConversationSearchError("query_not_searchable") from exc


def _relaxed_query_terms(query: str) -> tuple[str, ...]:
    """Return distinct non-empty terms for the ranked any-term fallback."""
    return tuple(dict.fromkeys(term for term in query.split() if term))


def search_session_conversation(
    db: Session,
    *,
    patient_id: UUID,
    care_episode_id: UUID,
    session_id: UUID,
    query: str,
) -> SessionConversationSearchResult:
    _validate_scope(
        db,
        patient_id=patient_id,
        care_episode_id=care_episode_id,
        session_id=session_id,
    )
    normalized_query = " ".join(str(query or "").split())
    if not normalized_query:
        return SessionConversationSearchResult(messages=(), matched=0)
    if _parser_node_count(db, normalized_query) == 0:
        return SessionConversationSearchResult(messages=(), matched=0)

    vector = func.to_tsvector("english", AIMessage.content)

    def fetch_matches(predicate: Any, rank: Any) -> list[tuple[AIMessage, Any]]:
        return list(
            db.execute(
                select(AIMessage, rank.label("rank"))
                .where(AIMessage.session_id == session_id)
                .where(AIMessage.sender_role.in_(("user", "assistant")))
                .where(predicate)
                .order_by(
                    rank.desc(),
                    AIMessage.message_metadata["sequence"].astext.cast(Integer).asc().nullslast(),
                    AIMessage.message_id.asc(),
                )
                .limit(MAX_SEARCH_HITS)
            ).all()
        )

    tsquery = func.plainto_tsquery("english", normalized_query)
    rank = func.ts_rank_cd(vector, tsquery)
    matches = fetch_matches(vector.op("@@")(tsquery), rank)
    if not matches:
        # A model query can contain useful qualifiers that are absent from the
        # stored message. Keep the strict match first, then rank messages that
        # contain any searchable term instead of returning a false empty hit.
        term_queries = [
            func.plainto_tsquery("english", term)
            for term in _relaxed_query_terms(normalized_query)
        ]
        predicates = [vector.op("@@")(term_query) for term_query in term_queries]
        if predicates:
            relaxed_rank = sum(
                (func.ts_rank_cd(vector, term_query) for term_query in term_queries),
                literal(0),
            )
            matches = fetch_matches(or_(*predicates), relaxed_rank)
    if not matches:
        return SessionConversationSearchResult(messages=(), matched=0)

    all_rows = list(
        db.execute(
            select(AIMessage)
            .where(AIMessage.session_id == session_id)
            .where(AIMessage.sender_role.in_(("user", "assistant")))
            .order_by(*ai_message_ordering())
        ).scalars().all()
    )
    positions = {row.message_id: index for index, row in enumerate(all_rows)}
    selected_ids = {row.message_id for row, _rank in matches}
    for row, _rank in matches:
        position = positions.get(row.message_id)
        if position is None:
            continue
        if position > 0:
            selected_ids.add(all_rows[position - 1].message_id)
        if position + 1 < len(all_rows):
            selected_ids.add(all_rows[position + 1].message_id)

    selected = [
        row for row in all_rows
        if row.message_id in selected_ids
    ]
    return SessionConversationSearchResult(
        messages=tuple(
            {
                "role": str(row.sender_role),
                "content": str(row.content or ""),
            }
            for row in selected
        ),
        matched=len(matches),
    )


def search_session_conversation_for_tool(
    db: Session,
    *,
    patient_id: UUID,
    care_episode_id: UUID,
    session_id: UUID,
    query: str,
) -> list[dict[str, str]]:
    return list(
        search_session_conversation(
            db,
            patient_id=patient_id,
            care_episode_id=care_episode_id,
            session_id=session_id,
            query=query,
        ).messages
    )

