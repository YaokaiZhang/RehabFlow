from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EpisodeRehabSession


_MAX_CONTEXT_CHARS = 4000


def _compact(value: object, *, limit: int = 800) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _exercise_label(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("label") or item.get("exercise_id") or "").strip()


def format_rehab_session_context(session: EpisodeRehabSession) -> str:
    completed: list[str] = []
    remaining: list[str] = []
    for item in session.checklist or []:
        label = _exercise_label(item)
        if not label:
            continue
        (completed if isinstance(item, dict) and item.get("completed") else remaining).append(label)

    lines = [
        "Saved rehab session:",
        f"Completion status: {_compact(session.completion_status or 'in_progress', limit=64)}.",
        f"Completed exercises: {_compact(', '.join(completed) or 'none', limit=800)}.",
        f"Remaining exercises: {_compact(', '.join(remaining) or 'none', limit=800)}.",
    ]
    if session.patient_notes.strip():
        lines.append(f"Patient notes: {_compact(session.patient_notes)}")
    if session.session_summary.strip():
        lines.append(f"Session summary: {_compact(session.session_summary)}")
    return _compact("\n".join(lines), limit=_MAX_CONTEXT_CHARS)


def rehab_sessions_for_episode(
    db: Session,
    episode_id: UUID,
    patient_id: UUID,
) -> list[EpisodeRehabSession]:
    """Load the authorized episode history, newest first."""
    return list(
        db.execute(
            select(EpisodeRehabSession)
            .where(
                EpisodeRehabSession.care_episode_id == episode_id,
                EpisodeRehabSession.patient_id == patient_id,
            )
            .order_by(EpisodeRehabSession.updated_at.desc(), EpisodeRehabSession.created_at.desc())
        )
        .scalars()
        .all()
    )


def format_longitudinal_rehab_context(sessions: list[EpisodeRehabSession]) -> str:
    if not sessions:
        return ""
    return "\n\n".join(
        f"Session {index}:\n{format_rehab_session_context(session)}"
        for index, session in enumerate(sessions, start=1)
    )


def latest_rehab_session_for_episode(
    db: Session,
    episode_id: UUID,
    patient_id: UUID,
) -> EpisodeRehabSession | None:
    return (
        db.execute(
            select(EpisodeRehabSession)
            .where(
                EpisodeRehabSession.care_episode_id == episode_id,
                EpisodeRehabSession.patient_id == patient_id,
            )
            .order_by(EpisodeRehabSession.updated_at.desc(), EpisodeRehabSession.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def load_latest_rehab_session_context(
    db: Session,
    episode_id: UUID,
    patient_id: UUID,
) -> str:
    session = latest_rehab_session_for_episode(db, episode_id, patient_id)
    return format_rehab_session_context(session) if session is not None else ""
