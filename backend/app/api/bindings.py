from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_principal
from app.db.models import Doctor, Patient, PatientDoctorMapping
from app.db.session import get_db
from app.schemas.auth import BindPatientRequest, BindingResponse

router = APIRouter(prefix="/bindings", tags=["bindings"])


@router.post("/link", response_model=BindingResponse)
def link_patient_to_doctor(
	payload: BindPatientRequest,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> BindingResponse:
	if principal.get("role") != "doctor":
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only doctors can create bindings")

	doctor_id = principal.get("user_id")
	if not doctor_id:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing doctor identity")

	doctor = db.get(Doctor, UUID(doctor_id))
	if doctor is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")

	patient = db.get(Patient, payload.patient_id)
	if patient is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

	existing = db.execute(
		select(PatientDoctorMapping).where(
			PatientDoctorMapping.doctor_id == doctor.doctor_id,
			PatientDoctorMapping.patient_id == payload.patient_id,
		)
	).scalar_one_or_none()

	if existing:
		existing.status = payload.status
		db.commit()
		db.refresh(existing)
		return BindingResponse(
			mapping_id=existing.mapping_id,
			patient_id=existing.patient_id,
			doctor_id=existing.doctor_id,
			status=existing.status,
		)

	mapping = PatientDoctorMapping(
		patient_id=payload.patient_id,
		doctor_id=doctor.doctor_id,
		status=payload.status,
	)
	db.add(mapping)
	db.commit()
	db.refresh(mapping)

	return BindingResponse(
		mapping_id=mapping.mapping_id,
		patient_id=mapping.patient_id,
		doctor_id=mapping.doctor_id,
		status=mapping.status,
	)
