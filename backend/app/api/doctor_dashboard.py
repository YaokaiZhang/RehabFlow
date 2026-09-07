from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import get_current_principal
from app.db.models import Doctor
from app.db.session import get_db
from app.schemas.doctor_dashboard import (
	DoctorDashboardResponse,
	DoctorIntelligenceArtifactCreate,
	DoctorIntelligenceArtifactResponse,
)
from app.services.care_relationship_access import PrincipalRequired, principal_id
from app.services.doctor_dashboard import (
	create_patient_panel_briefing,
	doctor_dashboard_payload,
	save_doctor_artifact,
)

router = APIRouter(tags=["doctor-dashboard"])


def _current_doctor_id(principal: dict, db: Session) -> UUID:
	if principal.get("role") != "doctor":
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only doctors can use this endpoint")
	try:
		doctor_id = UUID(principal_id(principal))
	except (PrincipalRequired, ValueError) as exc:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found") from exc
	if db.get(Doctor, doctor_id) is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")
	return doctor_id


@router.get("/doctor/dashboard", response_model=DoctorDashboardResponse)
def get_doctor_dashboard(
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> DoctorDashboardResponse:
	doctor_id = _current_doctor_id(principal, db)
	return DoctorDashboardResponse.model_validate(doctor_dashboard_payload(db, doctor_id))


@router.post(
	"/doctor/dashboard/briefing/refresh",
	response_model=DoctorIntelligenceArtifactResponse,
	status_code=status.HTTP_201_CREATED,
)
def refresh_patient_panel_briefing(
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> DoctorIntelligenceArtifactResponse:
	doctor_id = _current_doctor_id(principal, db)
	artifact = create_patient_panel_briefing(db, doctor_id)
	return DoctorIntelligenceArtifactResponse.model_validate(artifact)


@router.post(
	"/doctor/dashboard/artifacts",
	response_model=DoctorIntelligenceArtifactResponse,
	status_code=status.HTTP_201_CREATED,
)
def create_doctor_dashboard_artifact(
	payload: DoctorIntelligenceArtifactCreate,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> DoctorIntelligenceArtifactResponse:
	doctor_id = _current_doctor_id(principal, db)
	artifact = save_doctor_artifact(db, doctor_id, payload)
	return DoctorIntelligenceArtifactResponse.model_validate(artifact)
