
"""AI Daily Rehab workspace orchestration for saved-summary-activated rehab flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AIDailyRehabList, AIDailyRehabRecommendation, CareEpisode, CareEpisodeTriageSummary, EpisodeRehabSession, Patient
from app.services.care_relationship_access import EpisodeAccessDenied, EpisodeNotFound, require_patient_episode
from app.services.rehab_recommendations import InvalidRehabExerciseIds, get_or_create_rehab_list, latest_active_recommendation, update_rehab_list_items


class AiDailyRehabWorkspaceError(Exception):
    """Base error for AI Daily Rehab workspace operations."""

    def __init__(self, detail: str = "AI Daily Rehab workspace action failed") -> None:
        self.detail = detail
        super().__init__(detail)


class AiDailyRehabSafetyGateBlocked(AiDailyRehabWorkspaceError):
    pass


class AiDailyRehabRecommendationNotFound(AiDailyRehabWorkspaceError):
    pass


class AiDailyRehabListNotFound(AiDailyRehabWorkspaceError):
    pass


class AiDailyRehabSessionNotFound(AiDailyRehabWorkspaceError):
    pass


class AiDailyRehabAccessDenied(AiDailyRehabWorkspaceError):
    pass


@dataclass(frozen=True)
class AiDailyRehabWorkspace:
    recommendation: AIDailyRehabRecommendation
    rehab_list: AIDailyRehabList


_SAFETY_GATE_DETAIL = "AI Daily Rehab requires AI triage or clinician-reviewed context"


def _patient_episode(db: Session, patient_id: UUID, episode_id: UUID) -> CareEpisode:
    if db.get(Patient, patient_id) is None:
        raise AiDailyRehabAccessDenied("Patient not found")
    try:
        return require_patient_episode(db, patient_id, episode_id)
    except EpisodeNotFound as exc:
        raise AiDailyRehabAccessDenied("Care Episode not found") from exc
    except EpisodeAccessDenied as exc:
        raise AiDailyRehabAccessDenied("Care Episode not found") from exc


def _latest_saved_triage_summary(db: Session, episode: CareEpisode) -> CareEpisodeTriageSummary | None:
    return db.execute(
        select(CareEpisodeTriageSummary)
        .where(
            CareEpisodeTriageSummary.care_episode_id == episode.care_episode_id,
            CareEpisodeTriageSummary.patient_id == episode.patient_id,
        )
        .order_by(CareEpisodeTriageSummary.version.desc(), CareEpisodeTriageSummary.created_at.desc())
    ).scalars().first()


def _require_saved_summary_safety_gate(db: Session, episode: CareEpisode) -> CareEpisodeTriageSummary:
    if episode.safety_gate_status not in {"triage_complete", "clinician_reviewed"}:
        raise AiDailyRehabSafetyGateBlocked(_SAFETY_GATE_DETAIL)
    summary = _latest_saved_triage_summary(db, episode)
    if summary is None:
        raise AiDailyRehabSafetyGateBlocked(_SAFETY_GATE_DETAIL)
    return summary


def get_ai_daily_rehab_recommendation(db: Session, patient_id: UUID, episode_id: UUID) -> AIDailyRehabRecommendation | None:
    episode = _patient_episode(db, patient_id, episode_id)
    _require_saved_summary_safety_gate(db, episode)
    return latest_active_recommendation(db, episode=episode)


def load_ai_daily_rehab_workspace(db: Session, patient_id: UUID, episode_id: UUID) -> AiDailyRehabWorkspace:
    episode = _patient_episode(db, patient_id, episode_id)
    _require_saved_summary_safety_gate(db, episode)
    recommendation = latest_active_recommendation(db, episode=episode)
    if recommendation is None:
        raise AiDailyRehabRecommendationNotFound("AI Daily Rehab recommendation not found")
    rehab_list = get_or_create_rehab_list(db, episode=episode)
    db.commit()
    db.refresh(recommendation)
    db.refresh(rehab_list)
    return AiDailyRehabWorkspace(recommendation=recommendation, rehab_list=rehab_list)


def _exercise_ids_from_payload(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(item) for item in getattr(payload, "exercise_ids")]


def _recommendation_exercise_ids(recommendation: AIDailyRehabRecommendation) -> set[str]:
    exercise_ids: set[str] = set()
    for item in recommendation.items:
        if not isinstance(item, dict):
            continue
        exercise_id = str(item.get("exercise_id") or "").strip()
        if exercise_id:
            exercise_ids.add(exercise_id)
    return exercise_ids


def update_ai_daily_rehab_list(db: Session, patient_id: UUID, episode_id: UUID, payload: Any) -> AIDailyRehabList:
    episode = _patient_episode(db, patient_id, episode_id)
    _require_saved_summary_safety_gate(db, episode)
    recommendation = latest_active_recommendation(db, episode=episode)
    if recommendation is None:
        raise AiDailyRehabRecommendationNotFound("AI Daily Rehab recommendation not found")
    exercise_ids = _exercise_ids_from_payload(payload)
    recommended_ids = _recommendation_exercise_ids(recommendation)
    invalid_ids = [exercise_id for exercise_id in exercise_ids if str(exercise_id or "").strip() not in recommended_ids]
    if invalid_ids:
        raise InvalidRehabExerciseIds(invalid_ids)
    rehab_list = get_or_create_rehab_list(db, episode=episode)
    update_rehab_list_items(db, rehab_list=rehab_list, exercise_ids=exercise_ids)
    db.commit()
    db.refresh(rehab_list)
    return rehab_list


def _exercise_label(item: dict[str, Any]) -> str:
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    return str(item.get("label") or snapshot.get("title") or item.get("exercise_id") or "")


def _session_checklist_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "exercise_id": item.get("exercise_id"),
        "label": _exercise_label(item),
        "snapshot": item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {},
        "completed": False,
    }


def start_ai_daily_rehab_session(
    db: Session,
    patient_id: UUID,
    episode_id: UUID,
    list_id: UUID,
    *,
    patient_notes: str = "",
) -> EpisodeRehabSession:
    episode = _patient_episode(db, patient_id, episode_id)
    _require_saved_summary_safety_gate(db, episode)
    rehab_list = db.get(AIDailyRehabList, list_id)
    if rehab_list is None or rehab_list.care_episode_id != episode.care_episode_id or rehab_list.patient_id != patient_id:
        raise AiDailyRehabListNotFound("AI Daily Rehab List not found")
    list_items = [dict(item) for item in rehab_list.items if isinstance(item, dict)]
    if not list_items:
        raise AiDailyRehabListNotFound("AI Daily Rehab List is empty")
    recommendation = latest_active_recommendation(db, episode=episode)
    if recommendation is None:
        raise AiDailyRehabRecommendationNotFound("AI Daily Rehab recommendation not found")
    recommended_ids = _recommendation_exercise_ids(recommendation)
    stale_ids = [str(item.get("exercise_id") or "").strip() for item in list_items if str(item.get("exercise_id") or "").strip() not in recommended_ids]
    if stale_ids:
        raise AiDailyRehabListNotFound("AI Daily Rehab List contains exercises outside the current recommendation")
    session = EpisodeRehabSession(
        care_episode_id=episode.care_episode_id,
        patient_id=patient_id,
        recommended_exercises=list_items,
        checklist=[_session_checklist_item(item) for item in list_items],
        patient_notes=patient_notes,
        completion_status="in_progress",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def list_ai_daily_rehab_sessions(db: Session, patient_id: UUID, episode_id: UUID) -> list[EpisodeRehabSession]:
    episode = _patient_episode(db, patient_id, episode_id)
    return list(
        db.execute(
            select(EpisodeRehabSession)
            .where(EpisodeRehabSession.care_episode_id == episode.care_episode_id)
            .order_by(EpisodeRehabSession.created_at.desc())
        ).scalars().all()
    )


def _session_for_patient(db: Session, patient_id: UUID, session_id: UUID, episode_id: UUID | None = None) -> EpisodeRehabSession:
    session = db.get(EpisodeRehabSession, session_id)
    if session is None or session.patient_id != patient_id:
        raise AiDailyRehabSessionNotFound("Rehab Session not found")
    if episode_id is not None and session.care_episode_id != episode_id:
        raise AiDailyRehabSessionNotFound("Rehab Session not found")
    _patient_episode(db, patient_id, session.care_episode_id)
    return session


def _completed_items_from_payload(payload: Any = None, *, completed_items: list[str] | None = None) -> list[str]:
    if completed_items is not None:
        return [str(item) for item in completed_items]
    return [str(item) for item in getattr(payload, "completed_items")]


def _patient_notes_from_payload(payload: Any = None, *, patient_notes: str | None = None) -> str | None:
    if patient_notes is not None:
        return patient_notes
    return getattr(payload, "patient_notes", None)


def update_ai_daily_rehab_session_checklist(
    db: Session,
    patient_id: UUID,
    episode_id: UUID | None,
    session_id: UUID,
    payload: Any = None,
    *,
    completed_items: list[str] | None = None,
    patient_notes: str | None = None,
) -> EpisodeRehabSession:
    session = _session_for_patient(db, patient_id, session_id, episode_id)
    completed = set(_completed_items_from_payload(payload, completed_items=completed_items))
    updated_checklist: list[dict[str, Any]] = []
    for item in session.checklist:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        exercise_id = str(item.get("exercise_id") or "")
        updated_item = dict(item)
        updated_item["completed"] = bool((exercise_id and exercise_id in completed) or (label and label in completed))
        updated_checklist.append(updated_item)
    session.checklist = updated_checklist
    next_patient_notes = _patient_notes_from_payload(payload, patient_notes=patient_notes)
    if next_patient_notes is not None:
        session.patient_notes = next_patient_notes
    db.commit()
    db.refresh(session)
    return session


def summarize_ai_daily_rehab_session(db: Session, patient_id: UUID, episode_id: UUID | None, session_id: UUID) -> EpisodeRehabSession:
    session = _session_for_patient(db, patient_id, session_id, episode_id)
    completed = [_exercise_label(item) for item in session.checklist if isinstance(item, dict) and item.get("completed")]
    remaining = [_exercise_label(item) for item in session.checklist if isinstance(item, dict) and not item.get("completed")]
    session.session_summary = (
        f"Completed: {', '.join(completed) or 'none'}. "
        f"Remaining: {', '.join(remaining) or 'none'}. "
        f"Patient notes: {session.patient_notes or 'none'}."
    )
    session.completion_status = "summarized"
    db.commit()
    db.refresh(session)
    return session
