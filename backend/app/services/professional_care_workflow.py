from __future__ import annotations

"""Professional Care workflow for patient-requested clinician involvement."""

import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CareConnectionRequest, CareConversationMessage, CareEpisode, CareRelationship, Doctor, Patient
from app.schemas.professional_care import (
	CareConnectionRequestCreate,
	CareConnectionRequestListResponse,
	CareConnectionRequestResponse,
	CareRelationshipResponse,
	CareWorklistRelationshipPreview,
	CareWorklistResponse,
	DoctorDirectoryEntry,
	DoctorDirectoryResponse,
	DoctorProfessionalProfileResponse,
	DoctorProfessionalProfileUpdate,
	ProfessionalCareSubscriptionResponse,
	SelectCareRelationshipRequest,
)


class ProfessionalCareWorkflowError(Exception):
	"""Base class for Professional Care workflow failures."""

	status_code = 409
	detail = "Professional Care workflow error"

	def __init__(self, detail: str | None = None) -> None:
		super().__init__(detail or self.detail)
		self.detail = detail or self.detail


class DoctorNotFound(ProfessionalCareWorkflowError):
	"""Raised when a doctor cannot be found."""

	status_code = 404
	detail = "Doctor not found"


class PatientEpisodeNotFound(ProfessionalCareWorkflowError):
	"""Raised when a patient-owned Care Episode cannot be found."""

	status_code = 404
	detail = "Care Episode not found"


class CareRequestNotFound(ProfessionalCareWorkflowError):
	"""Raised when a Care Connection Request cannot be found."""

	status_code = 404
	detail = "Care Connection Request not found"


class CareRelationshipNotFound(ProfessionalCareWorkflowError):
	"""Raised when a Care Relationship cannot be found."""

	status_code = 404
	detail = "Care Relationship not found"


class ProfessionalCareAccessDenied(ProfessionalCareWorkflowError):
	"""Raised when a caller cannot perform a Professional Care action."""

	status_code = 403
	detail = "Professional Care Subscription required"


class CareRequestTransitionDenied(ProfessionalCareWorkflowError):
    """Raised when a Care Connection Request transition is not allowed."""

    status_code = 409
    detail = "Care Connection Request transition is not allowed"


class CareRelationshipSelectionConflict(ProfessionalCareWorkflowError):
    """Raised when a selected Care Relationship would be reassigned unsafely."""

    status_code = 409
    detail = "Care Episode already has an active Care Relationship"


DEMO_DOCTOR_PASSWORD_HASH = "demo-professional-care-unusable-password-hash"


def _doctor_real_info(doctor: Doctor) -> dict:
	real_info = doctor.real_info
	return real_info if isinstance(real_info, dict) else {}


def _doctor_specialty(doctor: Doctor) -> str | None:
	value = _doctor_real_info(doctor).get("specialty")
	if isinstance(value, str) and value.strip():
		specialty = value.strip()
		if _looks_like_raw_identifier(specialty):
			return None
		return specialty
	return None


def _looks_like_raw_identifier(value: str) -> bool:
	candidate = value.strip()
	if not candidate:
		return True
	try:
		UUID(candidate)
		return True
	except ValueError:
		pass
	if "_" in candidate and any(char.isdigit() for char in candidate):
		return True
	compact = candidate.replace("-", "")
	if len(compact) >= 24 and re.fullmatch(r"[A-Za-z0-9]+", compact) and any(char.isdigit() for char in compact):
		return True
	return False


def doctor_display_name(doctor: Doctor) -> str:
	real_info = _doctor_real_info(doctor)
	value = real_info.get("display_name")
	if isinstance(value, str) and value.strip():
		display_name = value.strip()
		if not _looks_like_raw_identifier(display_name):
			return display_name
	return "Professional Care clinician"


def _doctor_expertise_tags(doctor: Doctor) -> list[str]:
	real_info = _doctor_real_info(doctor)
	raw_tags = real_info.get("expertise_tags") or real_info.get("tags") or []
	tags = []
	if isinstance(raw_tags, list):
		for tag in raw_tags:
			if isinstance(tag, bool) or not isinstance(tag, (str, int, float)):
				continue
			value = str(tag).strip()
			if value and not _looks_like_raw_identifier(value):
				tags.append(value)
	specialty = _doctor_specialty(doctor)
	if specialty and specialty not in tags:
		tags.append(specialty)
	if not tags:
		tags = ["Rehabilitation clinician"]
	return tags[:8]


def _profile_display_name(doctor: Doctor) -> str:
	real_info = _doctor_real_info(doctor)
	value = real_info.get("display_name")
	if isinstance(value, str) and value.strip():
		return value.strip()
	return doctor.doctor_name


def _profile_specialty(doctor: Doctor) -> str | None:
	value = _doctor_real_info(doctor).get("specialty")
	if isinstance(value, str):
		specialty = value.strip()
		return specialty or None
	return None


def _normalize_profile_expertise_tags(raw_tags: object | None) -> list[str]:
	if not isinstance(raw_tags, list):
		return []
	normalized: list[str] = []
	seen: set[str] = set()
	for tag in raw_tags:
		if not isinstance(tag, str):
			continue
		value = tag.strip()
		if not value:
			continue
		key = value.casefold()
		if key in seen:
			continue
		seen.add(key)
		normalized.append(value)
	return normalized[:8]


def doctor_professional_profile(db: Session, doctor_id: UUID) -> DoctorProfessionalProfileResponse:
	doctor = _doctor(db, doctor_id)
	return DoctorProfessionalProfileResponse(
		doctor_id=doctor.doctor_id,
		display_name=_profile_display_name(doctor),
		verification_status=doctor.verification_status,
		specialty=_profile_specialty(doctor),
		expertise_tags=_normalize_profile_expertise_tags(
			_doctor_real_info(doctor).get("expertise_tags") or _doctor_real_info(doctor).get("tags")
		),
	)


def update_doctor_professional_profile(
	db: Session,
	doctor_id: UUID,
	payload: DoctorProfessionalProfileUpdate,
) -> DoctorProfessionalProfileResponse:
	doctor = _doctor(db, doctor_id)
	real_info = dict(_doctor_real_info(doctor))
	if "specialty" in payload.model_fields_set:
		value = payload.specialty.strip() if isinstance(payload.specialty, str) else ""
		real_info["specialty"] = value or None
	if "expertise_tags" in payload.model_fields_set:
		real_info["expertise_tags"] = _normalize_profile_expertise_tags(payload.expertise_tags)
	doctor.real_info = real_info
	db.commit()
	db.refresh(doctor)
	return doctor_professional_profile(db, doctor.doctor_id)


def _patient(db: Session, patient_id: UUID) -> Patient:
	patient = db.get(Patient, patient_id)
	if patient is None:
		raise ProfessionalCareAccessDenied("Patient not found")
	return patient


def _doctor(db: Session, doctor_id: UUID) -> Doctor:
	doctor = db.get(Doctor, doctor_id)
	if doctor is None:
		raise DoctorNotFound()
	return doctor


def _patient_episode(db: Session, patient_id: UUID, episode_id: UUID) -> CareEpisode:
	episode = db.get(CareEpisode, episode_id)
	if episode is None or episode.patient_id != patient_id:
		raise PatientEpisodeNotFound()
	return episode


def _require_professional_care(patient: Patient) -> None:
	if patient.subscription_tier != "professional_care":
		raise ProfessionalCareAccessDenied()


def _seed_demo_doctors(db: Session) -> list[Doctor]:
	count = db.execute(select(func.count()).select_from(Doctor)).scalar_one()
	if count:
		return []
	doctors = [
		Doctor(
			doctor_name="Demo Rehab Doctor",
			password_hash=DEMO_DOCTOR_PASSWORD_HASH,
			real_info={"display_name": "Demo Rehab Doctor", "specialty": "Rehabilitation medicine", "demo": True},
			verification_status="demo",
		),
		Doctor(
			doctor_name="Demo Sports PT",
			password_hash=DEMO_DOCTOR_PASSWORD_HASH,
			real_info={"display_name": "Demo Sports PT", "specialty": "Sports physical therapy", "demo": True},
			verification_status="demo",
		),
	]
	db.add_all(doctors)
	db.commit()
	return doctors


def request_response(request: CareConnectionRequest, db: Session) -> CareConnectionRequestResponse:
	"""Return the API response shape for a Care Connection Request."""
	doctor = db.get(Doctor, request.doctor_id)
	patient = db.get(Patient, request.patient_id)
	episode = db.get(CareEpisode, request.care_episode_id)
	return CareConnectionRequestResponse(
		request_id=request.request_id,
		care_episode_id=request.care_episode_id,
		patient_id=request.patient_id,
		doctor_id=request.doctor_id,
		request_reason=request.request_reason,
		status=request.status,
		created_at=request.created_at,
		updated_at=request.updated_at,
		responded_at=request.responded_at,
		doctor_name=doctor_display_name(doctor) if doctor else None,
		patient_name=patient.patient_name if patient else None,
		issue_title=episode.issue_title if episode else None,
		latest_triage_summary=episode.latest_triage_summary if episode else None,
	)


def relationship_preview(relationship: CareRelationship, db: Session) -> CareWorklistRelationshipPreview:
	"""Return the doctor worklist preview shape for an active Care Relationship."""
	patient = db.get(Patient, relationship.patient_id)
	episode = db.get(CareEpisode, relationship.care_episode_id)
	latest_message = db.execute(
		select(CareConversationMessage)
		.where(CareConversationMessage.relationship_id == relationship.relationship_id)
		.order_by(CareConversationMessage.created_at.desc())
		.limit(1)
	).scalars().first()
	last_activity_at = latest_message.created_at if latest_message else relationship.updated_at
	last_activity_label = "Latest Care Conversation message" if latest_message else "Relationship started"
	return CareWorklistRelationshipPreview(
		relationship_id=relationship.relationship_id,
		care_episode_id=relationship.care_episode_id,
		patient_id=relationship.patient_id,
		patient_name=patient.patient_name if patient else None,
		doctor_id=relationship.doctor_id,
		status=relationship.status,
		issue_title=episode.issue_title if episode else None,
		latest_triage_summary=episode.latest_triage_summary if episode else None,
		started_at=relationship.created_at,
		last_activity_at=last_activity_at,
		last_activity_label=last_activity_label,
	)


def default_request_reason(episode: CareEpisode) -> str:
	"""Return the fallback request reason for a Care Episode."""
	latest = episode.latest_triage_summary or {}
	if latest:
		recommendation = latest.get("recommendation") or ""
		return f"{episode.issue_title}: {recommendation}"[:2000]
	return f"Please review my Care Episode: {episode.issue_title}. Goal: {episode.goal}."[:2000]


def subscribe_doctor(db: Session, patient_id: UUID, payload: object | None = None) -> ProfessionalCareSubscriptionResponse:
	"""Activate the MVP Professional Care subscription state for a patient."""
	del payload
	patient = _patient(db, patient_id)
	patient.subscription_tier = "professional_care"
	db.commit()
	db.refresh(patient)
	return ProfessionalCareSubscriptionResponse(patient_id=patient.patient_id, subscription_tier=patient.subscription_tier)


def list_available_doctors(db: Session, patient_id: UUID, episode_id: UUID | None = None, *, seed_demo: bool = True) -> DoctorDirectoryResponse:
	"""Return doctors visible to a subscribed patient."""
	patient = _patient(db, patient_id)
	_require_professional_care(patient)
	if episode_id is not None:
		_patient_episode(db, patient.patient_id, episode_id)
	if seed_demo:
		_seed_demo_doctors(db)
	doctors = db.execute(select(Doctor).order_by(Doctor.created_at.desc(), Doctor.doctor_name.asc())).scalars().all()
	return DoctorDirectoryResponse(
		doctors=[
			DoctorDirectoryEntry(
				doctor_id=doctor.doctor_id,
				display_name=doctor_display_name(doctor),
				verification_status=doctor.verification_status,
				specialty=_doctor_specialty(doctor),
				expertise_tags=_doctor_expertise_tags(doctor),
			)
			for doctor in doctors
		]
	)


def create_care_request(
    db: Session,
    patient_id: UUID,
    episode_id: UUID,
    payload: CareConnectionRequestCreate,
) -> CareConnectionRequestResponse:
    """Create a Care Connection Request for a subscribed patient episode."""
    patient = _patient(db, patient_id)
    _require_professional_care(patient)
    episode = _patient_episode(db, patient.patient_id, episode_id)
    doctor = _doctor(db, payload.doctor_id)
    existing = db.execute(
        select(CareConnectionRequest).where(
            CareConnectionRequest.care_episode_id == episode.care_episode_id,
            CareConnectionRequest.doctor_id == doctor.doctor_id,
        )
    ).scalar_one_or_none()
    if existing and existing.status in {"pending", "accepted"}:
        raise ProfessionalCareWorkflowError("Care Connection Request already exists for this doctor")
    pending_count = db.execute(
        select(func.count()).select_from(CareConnectionRequest).where(
            CareConnectionRequest.care_episode_id == episode.care_episode_id,
            CareConnectionRequest.status == "pending",
        )
    ).scalar_one()
    if pending_count >= 3:
        raise ProfessionalCareWorkflowError("At most three pending Care Connection Requests are allowed")
    if existing and existing.status == "rejected":
        existing.request_reason = payload.request_reason.strip() or default_request_reason(episode)
        existing.status = "pending"
        existing.responded_at = None
        db.commit()
        db.refresh(existing)
        return request_response(existing, db)
    request = CareConnectionRequest(
        care_episode_id=episode.care_episode_id,
        patient_id=patient.patient_id,
        doctor_id=doctor.doctor_id,
        request_reason=payload.request_reason.strip() or default_request_reason(episode),
        status="pending",
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request_response(request, db)

def list_patient_care_requests(db: Session, patient_id: UUID, episode_id: UUID) -> CareConnectionRequestListResponse:
	"""Return Care Connection Requests for a patient-owned Care Episode."""
	episode = _patient_episode(db, patient_id, episode_id)
	requests = db.execute(
		select(CareConnectionRequest)
		.where(CareConnectionRequest.care_episode_id == episode.care_episode_id)
		.order_by(CareConnectionRequest.created_at.desc())
	).scalars().all()
	return CareConnectionRequestListResponse(requests=[request_response(item, db) for item in requests])


def get_doctor_worklist(db: Session, doctor_id: UUID) -> CareWorklistResponse:
	"""Return pending, accepted-waiting, and active selected Professional Care work for a doctor."""
	_doctor(db, doctor_id)
	incoming = db.execute(
		select(CareConnectionRequest)
		.where(CareConnectionRequest.doctor_id == doctor_id, CareConnectionRequest.status == "pending")
		.order_by(CareConnectionRequest.created_at.desc())
	).scalars().all()
	relationships = db.execute(
		select(CareRelationship)
		.join(CareEpisode, CareEpisode.care_episode_id == CareRelationship.care_episode_id)
		.where(
			CareRelationship.doctor_id == doctor_id,
			CareRelationship.status == "active",
			CareEpisode.selected_doctor_id == doctor_id,
		)
		.order_by(CareRelationship.updated_at.desc())
	).scalars().all()
	active_episode_ids = {item.care_episode_id for item in relationships}
	accepted = db.execute(
		select(CareConnectionRequest)
		.where(CareConnectionRequest.doctor_id == doctor_id, CareConnectionRequest.status == "accepted")
		.order_by(CareConnectionRequest.responded_at.desc(), CareConnectionRequest.updated_at.desc())
	).scalars().all()
	accepted_waiting = [item for item in accepted if item.care_episode_id not in active_episode_ids]
	return CareWorklistResponse(
		incoming_requests=[request_response(item, db) for item in incoming],
		accepted_requests=[request_response(item, db) for item in accepted_waiting],
		active_relationships=[relationship_preview(item, db) for item in relationships],
	)


def _doctor_request(db: Session, doctor_id: UUID, request_id: UUID) -> CareConnectionRequest:
	request = db.get(CareConnectionRequest, request_id)
	if request is None or request.doctor_id != doctor_id:
		raise CareRequestNotFound()
	return request


def _active_selected_relationship_for_request(db: Session, request: CareConnectionRequest) -> CareRelationship | None:
    return db.execute(
        select(CareRelationship)
        .join(CareEpisode, CareEpisode.care_episode_id == CareRelationship.care_episode_id)
        .where(
            CareRelationship.source_request_id == request.request_id,
            CareRelationship.status == "active",
            CareEpisode.selected_doctor_id == request.doctor_id,
        )
    ).scalar_one_or_none()


def _require_pending_request(request: CareConnectionRequest, action: str) -> None:
    if request.status != "pending":
        raise CareRequestTransitionDenied(f"Care Connection Request must be pending before it can be {action}")


def accept_care_request(db: Session, doctor_id: UUID, request_id: UUID) -> CareConnectionRequestResponse:
    """Mark a doctor-owned pending Care Connection Request accepted."""
    _doctor(db, doctor_id)
    request = _doctor_request(db, doctor_id, request_id)
    _require_pending_request(request, "accepted")
    request.status = "accepted"
    request.responded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(request)
    return request_response(request, db)


def reject_care_request(db: Session, doctor_id: UUID, request_id: UUID) -> CareConnectionRequestResponse:
    """Mark a doctor-owned pending Care Connection Request rejected without creating a relationship."""
    _doctor(db, doctor_id)
    request = _doctor_request(db, doctor_id, request_id)
    if _active_selected_relationship_for_request(db, request) is not None:
        raise CareRequestTransitionDenied("Cannot reject a request that backs an active Care Relationship")
    _require_pending_request(request, "rejected")
    request.status = "rejected"
    request.responded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(request)
    return request_response(request, db)

def select_patient_relationship(
    db: Session,
    patient_id: UUID,
    episode_id: UUID,
    payload: SelectCareRelationshipRequest,
) -> CareRelationshipResponse:
    """Select one accepted doctor as the active Care Relationship for a patient episode."""
    episode = _patient_episode(db, patient_id, episode_id)
    accepted_request = db.execute(
        select(CareConnectionRequest).where(
            CareConnectionRequest.care_episode_id == episode.care_episode_id,
            CareConnectionRequest.doctor_id == payload.doctor_id,
            CareConnectionRequest.status == "accepted",
        )
    ).scalar_one_or_none()
    if accepted_request is None:
        raise ProfessionalCareWorkflowError("Doctor must accept the Care Connection Request before selection")
    relationship = db.execute(
        select(CareRelationship).where(CareRelationship.care_episode_id == episode.care_episode_id)
    ).scalar_one_or_none()
    if relationship is not None:
        if relationship.doctor_id != payload.doctor_id or episode.selected_doctor_id not in {None, payload.doctor_id}:
            raise CareRelationshipSelectionConflict()
        relationship.source_request_id = accepted_request.request_id
        relationship.status = "active"
    else:
        relationship = CareRelationship(
            care_episode_id=episode.care_episode_id,
            patient_id=patient_id,
            doctor_id=payload.doctor_id,
            source_request_id=accepted_request.request_id,
            status="active",
        )
        db.add(relationship)
    episode.selected_doctor_id = payload.doctor_id
    db.commit()
    db.refresh(relationship)
    return CareRelationshipResponse.model_validate(relationship)
