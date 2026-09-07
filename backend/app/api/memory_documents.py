from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_principal
from app.db.models import CareEpisode, Doctor, Patient
from app.db.session import get_db
from app.schemas.memory_documents import (
    MemoryContextPackResponse,
    MemoryDocumentResponse,
    PatientMemoryFieldsReplace,
    PatientMemoryFieldsUpdate,
)
from app.services.care_relationship_access import PrincipalRequired, principal_id
from app.services.memory_agent import (
    enqueue_patient_summary_refresh,
    find_existing_memory_maintenance,
    MemoryMaintenanceConflict,
    patient_edit_identity,
)
from app.services.memory_documents import (
    active_response_document,
    build_memory_context_pack,
    delete_patient_editable_field,
    doctor_can_read_episode,
    ensure_patient_memory_document,
    update_patient_editable_fields,
)


router = APIRouter(prefix="/memory", tags=["memory"])


def _principal_uuid(principal: dict, role: str) -> UUID:
    if principal.get("role") != role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only {role}s can use this endpoint",
        )
    try:
        return UUID(principal_id(principal))
    except PrincipalRequired as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing user identity",
        ) from exc


def _current_patient_id(principal: dict, db: Session) -> UUID:
    patient_id = _principal_uuid(principal, "patient")
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    return patient_id


def _current_doctor_id(principal: dict, db: Session) -> UUID:
    doctor_id = _principal_uuid(principal, "doctor")
    if db.get(Doctor, doctor_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")
    return doctor_id


def _readable_episode(care_episode_id: UUID, principal: dict, db: Session) -> CareEpisode:
    role = principal.get("role")
    if role == "patient":
        patient_id = _current_patient_id(principal, db)
        episode = db.get(CareEpisode, care_episode_id)
        if episode is None or episode.patient_id != patient_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care Episode not found")
        return episode
    if role == "doctor":
        doctor_id = _current_doctor_id(principal, db)
        episode = doctor_can_read_episode(db, doctor_id, care_episode_id)
        if episode is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care Episode not found")
        return episode
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only patients or doctors can use this endpoint",
    )


def _queue_patient_refresh(
    db: Session,
    *,
    patient_id: UUID,
    change_set: dict,
    trigger_identity: str,
    request_hash: str,
) -> None:
    run = enqueue_patient_summary_refresh(
        db,
        patient_id=patient_id,
        trigger_identity=trigger_identity,
        change_set=change_set,
        request_hash=request_hash,
        commit=False,
    )


def _patient_edit_identity_or_409(
    db: Session,
    patient_id: UUID,
    *,
    operation: str,
    payload: dict,
    idempotency_key: str | None,
) -> tuple[str, str, object | None]:
    patient = db.execute(
        select(Patient)
        .where(Patient.patient_id == patient_id)
        .with_for_update()
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    document = ensure_patient_memory_document(db, patient_id, commit=False, lock=True)
    state_token = str(document.updated_at or "")
    trigger_identity, request_hash = patient_edit_identity(
        patient_id,
        operation=operation,
        payload=payload,
        idempotency_key=idempotency_key,
        state_token=state_token,
    )
    try:
        existing = find_existing_memory_maintenance(db, trigger_identity, request_hash, for_update=True)
    except MemoryMaintenanceConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return trigger_identity, request_hash, existing


@router.get("/patient-document", response_model=MemoryDocumentResponse)
def get_patient_memory_document(
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> MemoryDocumentResponse:
    patient_id = _current_patient_id(principal, db)
    document = ensure_patient_memory_document(db, patient_id)
    return MemoryDocumentResponse.model_validate(active_response_document(db, document))


@router.patch("/patient-document/fields", response_model=MemoryDocumentResponse)
def patch_patient_memory_fields(
    payload: PatientMemoryFieldsUpdate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> MemoryDocumentResponse:
    patient_id = _current_patient_id(principal, db)
    trigger_identity, request_hash, existing = _patient_edit_identity_or_409(
        db,
        patient_id,
        operation="patch",
        payload={"fields": payload.fields},
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        document = ensure_patient_memory_document(db, patient_id)
        return MemoryDocumentResponse.model_validate(active_response_document(db, document))
    document, change_set = update_patient_editable_fields(
        db,
        patient_id,
        payload.fields,
        replace=False,
        commit=False,
    )
    _queue_patient_refresh(
        db,
        patient_id=patient_id,
        change_set=change_set,
        trigger_identity=trigger_identity,
        request_hash=request_hash,
    )
    db.commit()
    return MemoryDocumentResponse.model_validate(active_response_document(db, document))


@router.put("/patient-document/fields", response_model=MemoryDocumentResponse)
def replace_patient_memory_fields(
    payload: PatientMemoryFieldsReplace,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> MemoryDocumentResponse:
    patient_id = _current_patient_id(principal, db)
    trigger_identity, request_hash, existing = _patient_edit_identity_or_409(
        db,
        patient_id,
        operation="replace",
        payload={"fields": payload.fields},
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        document = ensure_patient_memory_document(db, patient_id)
        return MemoryDocumentResponse.model_validate(active_response_document(db, document))
    document, change_set = update_patient_editable_fields(
        db,
        patient_id,
        payload.fields,
        replace=True,
        commit=False,
    )
    _queue_patient_refresh(
        db,
        patient_id=patient_id,
        change_set=change_set,
        trigger_identity=trigger_identity,
        request_hash=request_hash,
    )
    db.commit()
    return MemoryDocumentResponse.model_validate(active_response_document(db, document))


@router.delete("/patient-document/fields/{field_name}", response_model=MemoryDocumentResponse)
def delete_patient_memory_field(
    field_name: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> MemoryDocumentResponse:
    patient_id = _current_patient_id(principal, db)
    trigger_identity, request_hash, existing = _patient_edit_identity_or_409(
        db,
        patient_id,
        operation="delete",
        payload={"field_name": field_name},
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        document = ensure_patient_memory_document(db, patient_id)
        return MemoryDocumentResponse.model_validate(active_response_document(db, document))
    try:
        document, change_set = delete_patient_editable_field(db, patient_id, field_name, commit=False)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Patient Memory field not found",
        ) from exc
    _queue_patient_refresh(
        db,
        patient_id=patient_id,
        change_set=change_set,
        trigger_identity=trigger_identity,
        request_hash=request_hash,
    )
    db.commit()
    return MemoryDocumentResponse.model_validate(active_response_document(db, document))


@router.get("/care-episodes/{care_episode_id}/context-pack", response_model=MemoryContextPackResponse)
def get_memory_context_pack(
    care_episode_id: UUID,
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> MemoryContextPackResponse:
    episode = _readable_episode(care_episode_id, principal, db)
    patient_document, episode_document, compiled_context = build_memory_context_pack(db, episode)
    return MemoryContextPackResponse(
        patient_document=MemoryDocumentResponse.model_validate(patient_document),
        episode_document=MemoryDocumentResponse.model_validate(episode_document),
        compiled_context=compiled_context,
    )
