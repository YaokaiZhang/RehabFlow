from __future__ import annotations

import re
from uuid import UUID

from qdrant_client.models import FieldCondition, Filter, MatchAny
from sqlalchemy.orm import Session

from app.db.models import CareEpisode
from app.schemas.professional_care import DoctorDirectoryEntry, DoctorSearchResult
from app.services import professional_care_workflow
from app.vector import QdrantKnowledgeStore
from app.vector.doctor_profiles import care_episode_search_text, doctor_profile_text

DOCTOR_PROFILE_COLLECTION = "rehab_doctor_profiles"


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", text.casefold()) if len(token) > 2]


def _lexical_score(entry: DoctorDirectoryEntry, query_text: str) -> float:
    profile_text = doctor_profile_text(entry).casefold()
    query_tokens = _tokens(query_text)
    if not query_tokens:
        return 0.0

    score = 0.0
    body_goal_lines = []
    for line in query_text.splitlines():
        if line.startswith(("Issue:", "Body area:", "Goal:", "Description:")):
            body_goal_lines.append(line.casefold())
    body_goal_text = " ".join(body_goal_lines)

    for token in query_tokens:
        if token in profile_text:
            score += 1.0
        if token in body_goal_text and token in profile_text:
            score += 1.5

    for tag in entry.expertise_tags:
        tag_text = tag.casefold()
        tag_tokens = _tokens(tag_text)
        if tag_text and tag_text in query_text.casefold():
            score += 8.0
        for token in tag_tokens:
            if token in query_tokens:
                score += 3.0
            if token in body_goal_text:
                score += 2.0

    specialty = (entry.specialty or "").casefold()
    if specialty:
        for token in _tokens(specialty):
            if token in query_tokens:
                score += 2.0
            if token in body_goal_text:
                score += 1.0
    return float(score)


def _match_reason(entry: DoctorDirectoryEntry, query_text: str) -> str:
    query_lower = query_text.casefold()
    matched_tags = [tag for tag in entry.expertise_tags if any(token in query_lower for token in _tokens(tag))]
    if matched_tags:
        return "Matches " + ", ".join(matched_tags[:3]) + " for this Care Episode."
    if entry.specialty:
        return f"Matches {entry.specialty} profile context for this Care Episode."
    return "Matches Professional Care profile context for this Care Episode."


def _call_with_optional_query_filter(method: object, kwargs: dict[str, object], query_filter: Filter) -> object:
    try:
        return method(**kwargs, query_filter=query_filter)  # type: ignore[misc]
    except TypeError as exc:
        if "query_filter" not in str(exc):
            raise
        return method(**kwargs)  # type: ignore[misc]


def _vector_scores(query_text: str, allowed_doctor_ids: set[str], limit: int) -> dict[str, float]:
    if not allowed_doctor_ids:
        return {}
    store = QdrantKnowledgeStore(collection_name=DOCTOR_PROFILE_COLLECTION)
    try:
        store.client.get_collection(collection_name=DOCTOR_PROFILE_COLLECTION)
        query_vector = store.embed_text(query_text)
        result_limit = max(limit, len(allowed_doctor_ids))
        query_filter = Filter(
            must=[FieldCondition(key="doctor_id", match=MatchAny(any=sorted(allowed_doctor_ids)))]
        )
        try:
            response = _call_with_optional_query_filter(
                store.client.query_points,
                {
                    "collection_name": DOCTOR_PROFILE_COLLECTION,
                    "query": query_vector,
                    "limit": result_limit,
                    "with_payload": True,
                },
                query_filter,
            ).points
        except AttributeError:
            response = _call_with_optional_query_filter(
                store.client.search,
                {
                    "collection_name": DOCTOR_PROFILE_COLLECTION,
                    "query_vector": query_vector,
                    "limit": result_limit,
                    "with_payload": True,
                },
                query_filter,
            )

        scores: dict[str, float] = {}
        for item in response:
            payload = item.get("payload") if isinstance(item, dict) else getattr(item, "payload", None)
            if not isinstance(payload, dict):
                continue
            doctor_id = payload.get("doctor_id")
            if isinstance(doctor_id, str) and doctor_id in allowed_doctor_ids:
                raw_score = item.get("score") if isinstance(item, dict) else getattr(item, "score", 0.0)
                scores[doctor_id] = float(raw_score or 0.0)
        return scores
    except Exception:
        return {}
    finally:
        store.close()


def search_doctors_for_episode(db: Session, patient_id: UUID, care_episode_id: UUID, query: str = "", limit: int | None = None) -> list[DoctorSearchResult]:
    """Return ranked, sanitized doctor matches for a subscribed patient Care Episode."""
    directory = professional_care_workflow.list_available_doctors(db, patient_id, care_episode_id, seed_demo=False)
    episode = db.get(CareEpisode, care_episode_id)
    if episode is None:
        raise professional_care_workflow.PatientEpisodeNotFound()

    entries = directory.doctors
    allowed_doctor_ids = {str(entry.doctor_id) for entry in entries}
    entry_order_by_doctor_id = {str(entry.doctor_id): index for index, entry in enumerate(entries)}
    query_text = care_episode_search_text(episode, query, episode.latest_triage_summary)
    score_limit = limit if limit is not None else len(entries)
    vector_scores = _vector_scores(query_text, allowed_doctor_ids, score_limit)

    def combined_score(entry: DoctorDirectoryEntry) -> float:
        return float(_lexical_score(entry, query_text) * 10.0 + vector_scores.get(str(entry.doctor_id), 0.0))

    def rank_key(entry: DoctorDirectoryEntry) -> tuple[float, int, str, str]:
        doctor_id = str(entry.doctor_id)
        return (
            -combined_score(entry),
            entry_order_by_doctor_id.get(doctor_id, len(entries)),
            entry.display_name.casefold(),
            doctor_id,
        )

    ranked = sorted(entries, key=rank_key)
    if limit is not None:
        ranked = ranked[:limit]
    return [
        DoctorSearchResult(
            doctor=entry,
            score=combined_score(entry),
            match_reason=_match_reason(entry, query_text),
        )
        for entry in ranked
    ]
