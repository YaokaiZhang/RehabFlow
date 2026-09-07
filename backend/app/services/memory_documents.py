from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AISession, CareEpisode, CareRelationship, MemoryDocument, Patient


PATIENT_EMPTY_TEXT = "No Patient Memory summary has been generated yet."
EPISODE_EMPTY_TEXT = "No Episode Memory has been generated yet."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_patient_memory_document(
    db: Session,
    patient_id: UUID,
    *,
    lock: bool = False,
) -> MemoryDocument | None:
    query = select(MemoryDocument).where(
            MemoryDocument.scope == "patient",
            MemoryDocument.patient_id == patient_id,
            MemoryDocument.care_episode_id.is_(None),
        )
    if lock:
        query = query.with_for_update()
    return db.execute(query).scalars().first()


def ensure_patient_memory_document(
    db: Session,
    patient_id: UUID,
    *,
    commit: bool = True,
    lock: bool = False,
) -> MemoryDocument:
    document = _load_patient_memory_document(db, patient_id, lock=lock)
    if document is not None:
        return document
    document = MemoryDocument(
        scope="patient",
        patient_id=patient_id,
        care_episode_id=None,
        compiled_text=PATIENT_EMPTY_TEXT,
        summary_text="",
        editable_fields={},
        status="active",
        last_compiled_at=_now(),
    )
    db.add(document)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        if not commit:
            raise
        db.rollback()
        existing = _load_patient_memory_document(db, patient_id, lock=lock)
        if existing is None:
            raise
        return existing
    db.refresh(document)
    return document


def _load_episode_memory_document(db: Session, episode: CareEpisode) -> MemoryDocument | None:
    return db.execute(
        select(MemoryDocument).where(
            MemoryDocument.scope == "episode",
            MemoryDocument.patient_id == episode.patient_id,
            MemoryDocument.care_episode_id == episode.care_episode_id,
        )
    ).scalars().first()


def ensure_episode_memory_document(
    db: Session,
    episode: CareEpisode,
    *,
    commit: bool = True,
) -> MemoryDocument:
    document = _load_episode_memory_document(db, episode)
    if document is not None:
        return document
    document = MemoryDocument(
        scope="episode",
        patient_id=episode.patient_id,
        care_episode_id=episode.care_episode_id,
        compiled_text=EPISODE_EMPTY_TEXT,
        summary_text="",
        level1_keywords=[],
        level1_description="",
        level1_session_ids=[],
        level2_summary="",
        status="active",
        last_compiled_at=_now(),
    )
    db.add(document)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        if not commit:
            raise
        db.rollback()
        existing = _load_episode_memory_document(db, episode)
        if existing is None:
            raise
        return existing
    db.refresh(document)
    return document


def _field_text(fields: dict[str, Any]) -> str:
    if not fields:
        return ""
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def compile_memory_document(document: MemoryDocument) -> str:
    if document.scope == "patient":
        parts: list[str] = []
        if str(document.summary_text or "").strip():
            parts.append(f"Summary:\n{document.summary_text.strip()}")
        if document.editable_fields:
            parts.append(f"Editable fields:\n{_field_text(dict(document.editable_fields))}")
        return "\n\n".join(parts) or PATIENT_EMPTY_TEXT

    parts = []
    keywords = [str(value).strip() for value in (document.level1_keywords or []) if str(value).strip()]
    if keywords or str(document.level1_description or "").strip():
        level1 = []
        if keywords:
            level1.append(f"Keywords: {', '.join(keywords)}")
        if str(document.level1_description or "").strip():
            level1.append(f"Description: {document.level1_description.strip()}")
        if document.level1_session_ids:
            level1.append(f"Relevant sessions: {', '.join(str(value) for value in document.level1_session_ids)}")
        parts.append("Level 1\n" + "\n".join(level1))
    if str(document.level2_summary or "").strip():
        parts.append(f"Level 2\n{document.level2_summary.strip()}")
    return "\n\n".join(parts) or EPISODE_EMPTY_TEXT


def refresh_compiled_text(document: MemoryDocument) -> MemoryDocument:
    document.compiled_text = compile_memory_document(document)
    document.last_compiled_at = _now()
    return document


def active_response_document(db: Session, document: MemoryDocument) -> MemoryDocument:
    refresh_compiled_text(document)
    db.flush()
    return document


def update_patient_editable_fields(
    db: Session,
    patient_id: UUID,
    fields: dict[str, Any],
    *,
    replace: bool = False,
    commit: bool = True,
) -> tuple[MemoryDocument, dict[str, Any]]:
    patient = db.execute(
        select(Patient).where(Patient.patient_id == patient_id).with_for_update()
    ).scalar_one_or_none()
    if patient is None:
        raise ValueError("patient not found")
    document = ensure_patient_memory_document(db, patient_id, commit=commit, lock=True)
    previous = dict(document.editable_fields or {})
    next_fields = dict(fields) if replace else {**previous, **fields}
    document.editable_fields = next_fields
    refresh_compiled_text(document)
    if commit:
        db.commit()
        db.refresh(document)
    else:
        db.flush()
    return document, {
        "previous": previous,
        "current": next_fields,
        "changed_keys": sorted(set(previous) | set(next_fields)),
    }


def delete_patient_editable_field(
    db: Session,
    patient_id: UUID,
    field_name: str,
    *,
    commit: bool = True,
) -> tuple[MemoryDocument, dict[str, Any]]:
    patient = db.execute(
        select(Patient).where(Patient.patient_id == patient_id).with_for_update()
    ).scalar_one_or_none()
    if patient is None:
        raise ValueError("patient not found")
    document = ensure_patient_memory_document(db, patient_id, commit=commit, lock=True)
    previous = dict(document.editable_fields or {})
    if field_name not in previous:
        raise ValueError("Patient Memory field not found")
    next_fields = dict(previous)
    del next_fields[field_name]
    document.editable_fields = next_fields
    refresh_compiled_text(document)
    if commit:
        db.commit()
        db.refresh(document)
    else:
        db.flush()
    return document, {
        "previous": previous,
        "current": next_fields,
        "changed_keys": [field_name],
    }


def replace_episode_memory(
    db: Session,
    *,
    episode: CareEpisode,
    level1_keywords: list[str],
    level1_description: str,
    level1_session_ids: list[str],
    level2_summary: str,
) -> MemoryDocument:
    document = ensure_episode_memory_document(db, episode)
    requested_session_ids: list[UUID] = []
    for raw_session_id in level1_session_ids:
        try:
            requested_session_ids.append(UUID(str(raw_session_id)))
        except (TypeError, ValueError):
            continue
    allowed_session_ids: set[UUID] = set()
    if requested_session_ids:
        allowed_session_ids = set(
            db.execute(
                select(AISession.session_id).where(
                    AISession.session_id.in_(requested_session_ids),
                    AISession.patient_id == episode.patient_id,
                    AISession.care_episode_id == episode.care_episode_id,
                )
            ).scalars().all()
        )
    safe_session_ids = [
        str(session_id)
        for session_id in requested_session_ids
        if session_id in allowed_session_ids
    ]
    document.level1_keywords = list(level1_keywords)
    document.level1_description = level1_description
    document.level1_session_ids = safe_session_ids
    document.level2_summary = level2_summary
    document.last_memory_error = ""
    refresh_compiled_text(document)
    db.flush()
    return document


def replace_patient_summary(
    db: Session,
    *,
    patient_id: UUID,
    summary_text: str,
) -> MemoryDocument:
    patient = db.execute(
        select(Patient).where(Patient.patient_id == patient_id).with_for_update()
    ).scalar_one_or_none()
    if patient is None:
        raise ValueError("patient not found")
    document = ensure_patient_memory_document(db, patient_id, lock=True)
    document.summary_text = summary_text
    document.last_memory_error = ""
    refresh_compiled_text(document)
    db.flush()
    return document


def doctor_can_read_episode(db: Session, doctor_id: UUID, episode_id: UUID) -> CareEpisode | None:
    return db.execute(
        select(CareEpisode)
        .join(CareRelationship, CareRelationship.care_episode_id == CareEpisode.care_episode_id)
        .where(
            CareEpisode.care_episode_id == episode_id,
            CareRelationship.patient_id == CareEpisode.patient_id,
            CareRelationship.doctor_id == doctor_id,
            CareRelationship.status == "active",
        )
    ).scalars().first()


def build_memory_context_pack(
    db: Session,
    episode: CareEpisode,
) -> tuple[MemoryDocument, MemoryDocument, str]:
    patient_document = ensure_patient_memory_document(db, episode.patient_id)
    episode_document = ensure_episode_memory_document(db, episode)
    refresh_compiled_text(patient_document)
    refresh_compiled_text(episode_document)
    db.commit()
    db.refresh(patient_document)
    db.refresh(episode_document)
    compiled_context = (
        f"PATIENT MEMORY\n{patient_document.compiled_text}\n\n"
        f"EPISODE MEMORY\n{episode_document.compiled_text}"
    )
    return patient_document, episode_document, compiled_context
