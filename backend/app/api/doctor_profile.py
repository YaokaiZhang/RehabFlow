from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import get_current_principal
from app.db.models import Doctor
from app.db.session import get_db
from app.schemas.professional_care import DoctorProfessionalProfileResponse, DoctorProfessionalProfileUpdate
from app.services.care_relationship_access import PrincipalRequired, principal_id
from app.services.professional_care_workflow import doctor_professional_profile, update_doctor_professional_profile

router = APIRouter(tags=["doctor-profile"])


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


@router.get("/doctor/profile", response_model=DoctorProfessionalProfileResponse)
def get_doctor_profile(
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> DoctorProfessionalProfileResponse:
	doctor_id = _current_doctor_id(principal, db)
	return doctor_professional_profile(db, doctor_id)


@router.patch("/doctor/profile", response_model=DoctorProfessionalProfileResponse)
def patch_doctor_profile(
	payload: DoctorProfessionalProfileUpdate,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> DoctorProfessionalProfileResponse:
	doctor_id = _current_doctor_id(principal, db)
	return update_doctor_professional_profile(db, doctor_id, payload)
