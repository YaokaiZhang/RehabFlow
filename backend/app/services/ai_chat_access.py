from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import Principal
from app.db.models import AISession, CareEpisode, Patient


_NOT_FOUND_DETAIL = "AI chat resource not found"


@dataclass(frozen=True)
class AuthorizedAIChat:
    patient_id: UUID
    session_id: str
    care_episode_id: UUID | None


def _raise_not_found() -> NoReturn:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)


def authorize_ai_chat(
    db: Session,
    principal: Principal,
    *,
    session_id: str,
    care_episode_id: UUID | None,
) -> AuthorizedAIChat:
    """Authorize an AI-chat session before any clinical context is read."""
    if principal.role != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only patients can use AI chat",
        )

    patient = db.query(Patient).filter(Patient.patient_id == principal.user_id).first()
    if patient is None:
        _raise_not_found()

    try:
        requested_session_id = UUID(session_id)
    except (TypeError, ValueError):
        _raise_not_found()

    session_query = db.query(AISession).filter(
        AISession.session_id == requested_session_id,
        AISession.patient_id == patient.patient_id,
    )
    lock_session = getattr(session_query, "with_for_update", None)
    if callable(lock_session):
        session_query = lock_session()
    session = session_query.first()
    if session is None or session.patient_id != patient.patient_id or session.session_id != requested_session_id:
        _raise_not_found()

    episode: CareEpisode | None = None
    if care_episode_id is not None:
        episode = (
            db.query(CareEpisode)
            .filter(
                CareEpisode.care_episode_id == care_episode_id,
                CareEpisode.patient_id == patient.patient_id,
            )
            .first()
        )
        if (
            episode is None
            or episode.care_episode_id != care_episode_id
            or episode.patient_id != patient.patient_id
        ):
            _raise_not_found()
        if session.care_episode_id is not None and session.care_episode_id != care_episode_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="AI session is bound to another Care Episode",
            )
        if session.care_episode_id is None:
            try:
                session.bind_care_episode(care_episode_id)
                flush = getattr(db, "flush", None)
                if callable(flush):
                    flush()
                commit = getattr(db, "commit", None)
                if callable(commit):
                    commit()
            except Exception as exc:
                rollback = getattr(db, "rollback", None)
                if callable(rollback):
                    rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="AI session could not be bound to the requested Care Episode",
                ) from exc

    effective_episode_id = session.care_episode_id
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        rollback()
    return AuthorizedAIChat(
        patient_id=patient.patient_id,
        session_id=str(session.session_id),
        care_episode_id=effective_episode_id,
    )
