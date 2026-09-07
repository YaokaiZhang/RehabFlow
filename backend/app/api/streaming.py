from __future__ import annotations

from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import Principal, get_current_principal
from app.db.models import Doctor, Patient
from app.db.session import get_db
from app.schemas.streaming import StreamScope, StreamTicketClaims, StreamTicketResponse
from app.services.care_relationship_access import (
    CareRelationshipAccessError,
    require_doctor_active_relationship,
)
from app.services.stream_tickets import StreamTicketService

router = APIRouter(prefix="/stream-tickets", tags=["streaming"])
settings = get_settings()


@lru_cache(maxsize=1)
def get_stream_ticket_service() -> StreamTicketService:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return StreamTicketService(redis, max_ttl_seconds=settings.stream_ticket_ttl_seconds)


def _principal_user_id(principal: Principal) -> UUID:
    try:
        return UUID(str(principal.user_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims") from exc


@router.post("/patient-rehab", response_model=StreamTicketResponse)
async def issue_patient_rehab_ticket(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    ticket_service: StreamTicketService = Depends(get_stream_ticket_service),
) -> StreamTicketResponse:
    if principal.role != "patient":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only patients can use rehab streaming")

    patient_id = _principal_user_id(principal)
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    claims = StreamTicketClaims(
        scope=StreamScope.PATIENT_REHAB,
        principal_user_id=patient_id,
        patient_id=patient_id,
        care_episode_id=None,
    )
    ticket = await ticket_service.issue(claims)
    return StreamTicketResponse(ticket=ticket, expires_in_seconds=settings.stream_ticket_ttl_seconds)


@router.post("/doctor-monitor/{care_episode_id}", response_model=StreamTicketResponse)
async def issue_doctor_monitor_ticket(
    care_episode_id: UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    ticket_service: StreamTicketService = Depends(get_stream_ticket_service),
) -> StreamTicketResponse:
    if principal.role != "doctor":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only doctors can use episode monitoring")

    doctor_id = _principal_user_id(principal)
    if db.get(Doctor, doctor_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

    try:
        _, episode = require_doctor_active_relationship(db, doctor_id, care_episode_id)
    except (CareRelationshipAccessError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care Episode not found") from exc

    claims = StreamTicketClaims(
        scope=StreamScope.DOCTOR_EPISODE_MONITOR,
        principal_user_id=doctor_id,
        patient_id=episode.patient_id,
        care_episode_id=episode.care_episode_id,
    )
    ticket = await ticket_service.issue(claims)
    return StreamTicketResponse(ticket=ticket, expires_in_seconds=settings.stream_ticket_ttl_seconds)
