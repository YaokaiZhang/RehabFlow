from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import create_access_token, get_current_principal, hash_password, verify_password
from app.db.models import Doctor, Patient
from app.db.session import get_db
from app.schemas.auth import (
	AuthResponse,
	AuthUser,
	DoctorRegisterRequest,
	LoginRequest,
	PatientRegisterRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _doctor_registration_real_info(payload: DoctorRegisterRequest) -> dict:
    real_info = dict(payload.real_info)
    if real_info.get("source") != "mvp-register-page":
        return real_info
    display_name = real_info.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        real_info["display_name"] = payload.doctor_name.strip()
    return real_info


@router.post("/register/patient", response_model=AuthResponse)
def register_patient(payload: PatientRegisterRequest, db: Session = Depends(get_db)) -> AuthResponse:
	existing = db.execute(select(Patient).where(Patient.patient_name == payload.patient_name)).scalar_one_or_none()
	if existing:
		raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Patient name already exists")

	patient = Patient(
		patient_name=payload.patient_name,
		password_hash=hash_password(payload.password),
		real_info=payload.real_info,
		subscription_tier=payload.subscription_tier,
	)
	db.add(patient)
	db.commit()
	db.refresh(patient)

	token = create_access_token(subject=patient.patient_name, role="patient", user_id=str(patient.patient_id))
	return AuthResponse(
		access_token=token,
		user=AuthUser(user_id=patient.patient_id, role="patient", username=patient.patient_name),
	)


@router.post("/register/doctor", response_model=AuthResponse)
def register_doctor(payload: DoctorRegisterRequest, db: Session = Depends(get_db)) -> AuthResponse:
	existing = db.execute(select(Doctor).where(Doctor.doctor_name == payload.doctor_name)).scalar_one_or_none()
	if existing:
		raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Doctor name already exists")

	doctor = Doctor(
		doctor_name=payload.doctor_name,
		password_hash=hash_password(payload.password),
		real_info=_doctor_registration_real_info(payload),
		verification_status=payload.verification_status,
	)
	db.add(doctor)
	db.commit()
	db.refresh(doctor)

	token = create_access_token(subject=doctor.doctor_name, role="doctor", user_id=str(doctor.doctor_id))
	return AuthResponse(
		access_token=token,
		user=AuthUser(user_id=doctor.doctor_id, role="doctor", username=doctor.doctor_name),
	)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
	if payload.role == "patient":
		user = db.execute(select(Patient).where(Patient.patient_name == payload.username)).scalar_one_or_none()
		if not user or not verify_password(payload.password, user.password_hash):
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

		token = create_access_token(subject=user.patient_name, role="patient", user_id=str(user.patient_id))
		return AuthResponse(
			access_token=token,
			user=AuthUser(user_id=user.patient_id, role="patient", username=user.patient_name),
		)

	user = db.execute(select(Doctor).where(Doctor.doctor_name == payload.username)).scalar_one_or_none()
	if not user or not verify_password(payload.password, user.password_hash):
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

	token = create_access_token(subject=user.doctor_name, role="doctor", user_id=str(user.doctor_id))
	return AuthResponse(
		access_token=token,
		user=AuthUser(user_id=user.doctor_id, role="doctor", username=user.doctor_name),
	)


@router.get("/me")
def me(principal: dict = Depends(get_current_principal)) -> dict:
	return {
		"user_id": principal.get("user_id"),
		"role": principal.get("role"),
		"username": principal.get("sub"),
	}
