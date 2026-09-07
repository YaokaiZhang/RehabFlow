from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import get_current_principal
from app.db.models import Doctor, Patient
from app.db.session import get_db
from app.schemas.professional_care import (
	CareConnectionRequestCreate,
	CareConnectionRequestListResponse,
	CareConnectionRequestResponse,
	CareRelationshipResponse,
	CareWorklistResponse,
	DoctorDirectoryResponse,
	DoctorSearchResponse,
	ProfessionalCareSubscriptionResponse,
	SelectCareRelationshipRequest,
)
from app.services import professional_care_workflow
from app.services.semantic_doctor_search import search_doctors_for_episode
from app.services.care_relationship_access import PrincipalRequired, principal_id

router = APIRouter(tags=["professional-care"])


def _principal_uuid(principal: dict, expected_role: str) -> UUID:
	if principal.get("role") != expected_role:
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Only {expected_role}s can use this endpoint")
	try:
		return UUID(principal_id(principal))
	except PrincipalRequired as exc:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing user identity") from exc


def _current_patient(principal: dict, db: Session) -> Patient:
	patient_id = _principal_uuid(principal, "patient")
	patient = db.get(Patient, patient_id)
	if patient is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
	return patient


def _current_doctor(principal: dict, db: Session) -> Doctor:
	doctor_id = _principal_uuid(principal, "doctor")
	doctor = db.get(Doctor, doctor_id)
	if doctor is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")
	return doctor


def _map_workflow_error(exc: professional_care_workflow.ProfessionalCareWorkflowError) -> HTTPException:
	return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/professional-care/subscribe", response_model=ProfessionalCareSubscriptionResponse)
def subscribe_to_professional_care(
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> ProfessionalCareSubscriptionResponse:
	patient = _current_patient(principal, db)
	try:
		return professional_care_workflow.subscribe_doctor(db, patient.patient_id)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc


@router.get("/professional-care/doctors", response_model=DoctorDirectoryResponse)
def list_professional_care_doctors(
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> DoctorDirectoryResponse:
	patient = _current_patient(principal, db)
	try:
		return professional_care_workflow.list_available_doctors(db, patient.patient_id)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc


@router.get("/care-episodes/{care_episode_id}/doctor-search", response_model=DoctorSearchResponse)
def search_professional_care_doctors(
	care_episode_id: UUID,
	query: str = "",
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> DoctorSearchResponse:
	patient = _current_patient(principal, db)
	try:
		results = search_doctors_for_episode(db, patient.patient_id, care_episode_id, query)
		return DoctorSearchResponse(results=results)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc


@router.post(
	"/care-episodes/{care_episode_id}/connection-requests",
	response_model=CareConnectionRequestResponse,
	status_code=status.HTTP_201_CREATED,
)
def create_care_connection_request(
	care_episode_id: UUID,
	payload: CareConnectionRequestCreate,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareConnectionRequestResponse:
	patient = _current_patient(principal, db)
	try:
		return professional_care_workflow.create_care_request(db, patient.patient_id, care_episode_id, payload)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc


@router.get("/care-episodes/{care_episode_id}/connection-requests", response_model=CareConnectionRequestListResponse)
def list_care_connection_requests(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareConnectionRequestListResponse:
	patient = _current_patient(principal, db)
	try:
		return professional_care_workflow.list_patient_care_requests(db, patient.patient_id, care_episode_id)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc


@router.get("/doctor/care-worklist", response_model=CareWorklistResponse)
def get_doctor_care_worklist(
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareWorklistResponse:
	doctor = _current_doctor(principal, db)
	try:
		return professional_care_workflow.get_doctor_worklist(db, doctor.doctor_id)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc


@router.post("/doctor/care-requests/{request_id}/accept", response_model=CareConnectionRequestResponse)
def accept_care_connection_request(
	request_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareConnectionRequestResponse:
	doctor = _current_doctor(principal, db)
	try:
		return professional_care_workflow.accept_care_request(db, doctor.doctor_id, request_id)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc


@router.post("/doctor/care-requests/{request_id}/reject", response_model=CareConnectionRequestResponse)
def reject_care_connection_request(
	request_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareConnectionRequestResponse:
	doctor = _current_doctor(principal, db)
	try:
		return professional_care_workflow.reject_care_request(db, doctor.doctor_id, request_id)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc


@router.post("/care-episodes/{care_episode_id}/care-relationship/select", response_model=CareRelationshipResponse)
def select_care_relationship_doctor(
	care_episode_id: UUID,
	payload: SelectCareRelationshipRequest,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareRelationshipResponse:
	patient = _current_patient(principal, db)
	try:
		return professional_care_workflow.select_patient_relationship(db, patient.patient_id, care_episode_id, payload)
	except professional_care_workflow.ProfessionalCareWorkflowError as exc:
		raise _map_workflow_error(exc) from exc
