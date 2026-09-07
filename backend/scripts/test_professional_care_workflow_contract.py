from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CareConnectionRequest, CareEpisode, CareRelationship, Doctor, Patient
from app.db.session import SessionLocal, init_db
from app.schemas.professional_care import CareConnectionRequestCreate, SelectCareRelationshipRequest
from app.services import professional_care_workflow


def test_importing_workflow_service_does_not_import_fastapi() -> None:
	code = (
		"import sys; "
		"sys.path.insert(0, 'backend'); "
		"import app.services.professional_care_workflow; "
		"raise SystemExit(1 if 'fastapi' in sys.modules else 0)"
	)
	result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2])
	assert result.returncode == 0


def _unique_name(prefix: str) -> str:
	return f"{prefix}_{time.time_ns()}"


def _create_patient(db: Session, prefix: str = "pc_workflow_patient", subscription_tier: str = "professional_care") -> Patient:
	patient = Patient(
		patient_name=_unique_name(prefix),
		password_hash="test-hash",
		real_info={},
		subscription_tier=subscription_tier,
	)
	db.add(patient)
	db.flush()
	return patient


def _create_doctor(db: Session, prefix: str = "pc_workflow_doctor") -> Doctor:
	doctor = Doctor(
		doctor_name=_unique_name(prefix),
		password_hash="test-hash",
		real_info={"specialty": "Rehabilitation clinician"},
		verification_status="verified",
	)
	db.add(doctor)
	db.flush()
	return doctor


def _create_episode(db: Session, patient: Patient, title: str = "Shoulder stiffness") -> CareEpisode:
	episode = CareEpisode(
		patient_id=patient.patient_id,
		issue_title=title,
		body_area="Shoulder",
		goal="Move comfortably",
		short_description="Needs Professional Care review for a home rehab plan.",
		origin="manual",
		status="active",
		safety_gate_status="triage_complete",
		latest_triage_summary={
			"concern": title,
			"recommendation": "Use gentle activity while symptoms settle.",
			"clinician_review_needed": False,
		},
	)
	db.add(episode)
	db.flush()
	return episode


def _create_request(
	db: Session,
	episode: CareEpisode,
	doctor: Doctor,
	status: str = "pending",
	reason: str = "Please review this Care Episode.",
) -> CareConnectionRequest:
	request = CareConnectionRequest(
		care_episode_id=episode.care_episode_id,
		patient_id=episode.patient_id,
		doctor_id=doctor.doctor_id,
		request_reason=reason,
		status=status,
	)
	db.add(request)
	db.flush()
	return request


def _assert_raises(expected_type: type[Exception], callback) -> Exception:
	try:
		callback()
	except expected_type as exc:
		return exc
	raise AssertionError(f"Expected {expected_type.__name__}")


def _relationship_count(db: Session, episode: CareEpisode) -> int:
	return int(
		db.execute(
			select(func.count(CareRelationship.relationship_id)).where(
				CareRelationship.care_episode_id == episode.care_episode_id
			)
		).scalar_one()
	)


def test_create_request_uses_existing_subscribed_doctor_and_patient_episode() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_create_patient")
		doctor = _create_doctor(db, "pc_workflow_create_doctor")
		episode = _create_episode(db, patient, "Hip mobility plan")
		db.commit()

		result = professional_care_workflow.create_care_request(
			db,
			patient.patient_id,
			episode.care_episode_id,
			CareConnectionRequestCreate(doctor_id=doctor.doctor_id, request_reason="  Please review my hip plan.  "),
		)

		assert result.patient_id == patient.patient_id
		assert result.doctor_id == doctor.doctor_id
		assert result.care_episode_id == episode.care_episode_id
		assert result.request_reason == "Please review my hip plan."
		assert result.status == "pending"
		assert result.doctor_name == "Professional Care clinician"
		assert result.patient_name == patient.patient_name
		assert result.issue_title == "Hip mobility plan"
		assert result.latest_triage_summary["concern"] == "Hip mobility plan"

		stored = db.get(CareConnectionRequest, result.request_id)
		assert stored is not None
		assert stored.patient_id == patient.patient_id
		assert stored.doctor_id == doctor.doctor_id
		assert stored.care_episode_id == episode.care_episode_id


def test_accept_request_creates_or_activates_relationship_and_marks_request_accepted() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_accept_patient")
		doctor = _create_doctor(db, "pc_workflow_accept_doctor")
		episode = _create_episode(db, patient, "Accepted request episode")
		request = _create_request(db, episode, doctor)
		db.commit()

		result = professional_care_workflow.accept_care_request(db, doctor.doctor_id, request.request_id)

		assert result.request_id == request.request_id
		assert result.status == "accepted"
		assert result.responded_at is not None
		db.refresh(request)
		assert request.status == "accepted"
		assert request.responded_at is not None
		assert _relationship_count(db, episode) == 0
		db.refresh(episode)
		assert episode.selected_doctor_id is None


def test_reject_request_does_not_create_active_relationship() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_reject_patient")
		doctor = _create_doctor(db, "pc_workflow_reject_doctor")
		episode = _create_episode(db, patient, "Rejected request episode")
		request = _create_request(db, episode, doctor)
		db.commit()

		result = professional_care_workflow.reject_care_request(db, doctor.doctor_id, request.request_id)

		assert result.request_id == request.request_id
		assert result.status == "rejected"
		assert result.responded_at is not None
		assert _relationship_count(db, episode) == 0


def test_select_relationship_marks_initial_active_relationship_selected_for_patient() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_select_patient")
		doctor = _create_doctor(db, "pc_workflow_select_doctor")
		episode = _create_episode(db, patient, "Selection episode")
		request = _create_request(db, episode, doctor, status="accepted")
		db.commit()

		selected = professional_care_workflow.select_patient_relationship(
			db,
			patient.patient_id,
			episode.care_episode_id,
			SelectCareRelationshipRequest(doctor_id=doctor.doctor_id),
		)

		assert selected.status == "active"
		assert selected.doctor_id == doctor.doctor_id
		assert selected.source_request_id == request.request_id
		assert _relationship_count(db, episode) == 1
		db.refresh(episode)
		assert episode.selected_doctor_id == doctor.doctor_id


def test_selecting_different_doctor_after_active_relationship_is_blocked() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_switch_patient")
		doctor_one = _create_doctor(db, "pc_workflow_switch_one")
		doctor_two = _create_doctor(db, "pc_workflow_switch_two")
		episode = _create_episode(db, patient, "Blocked switch episode")
		request_one = _create_request(db, episode, doctor_one, status="accepted")
		request_two = _create_request(db, episode, doctor_two, status="accepted")
		db.commit()

		selected = professional_care_workflow.select_patient_relationship(
			db,
			patient.patient_id,
			episode.care_episode_id,
			SelectCareRelationshipRequest(doctor_id=doctor_one.doctor_id),
		)
		error = _assert_raises(
			professional_care_workflow.ProfessionalCareWorkflowError,
			lambda: professional_care_workflow.select_patient_relationship(
				db,
				patient.patient_id,
				episode.care_episode_id,
				SelectCareRelationshipRequest(doctor_id=doctor_two.doctor_id),
			),
		)

		assert "already has an active Care Relationship" in error.detail
		db.refresh(episode)
		stored = db.get(CareRelationship, selected.relationship_id)
		assert stored is not None
		assert stored.doctor_id == doctor_one.doctor_id
		assert stored.source_request_id == request_one.request_id
		assert episode.selected_doctor_id == doctor_one.doctor_id
		assert _relationship_count(db, episode) == 1
		assert request_two.request_id


def test_request_transitions_are_pending_only() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_pending_only_patient")
		doctor = _create_doctor(db, "pc_workflow_pending_only_doctor")
		episode = _create_episode(db, patient, "Pending transition episode")
		rejected_request = _create_request(db, episode, doctor, status="rejected")
		db.commit()

		error = _assert_raises(
			professional_care_workflow.ProfessionalCareWorkflowError,
			lambda: professional_care_workflow.accept_care_request(db, doctor.doctor_id, rejected_request.request_id),
		)
		assert "pending" in error.detail
		db.refresh(rejected_request)
		assert rejected_request.status == "rejected"


	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_reject_selected_patient")
		doctor = _create_doctor(db, "pc_workflow_reject_selected_doctor")
		episode = _create_episode(db, patient, "Selected reject episode")
		request = _create_request(db, episode, doctor)
		db.commit()
		professional_care_workflow.accept_care_request(db, doctor.doctor_id, request.request_id)
		professional_care_workflow.select_patient_relationship(
			db,
			patient.patient_id,
			episode.care_episode_id,
			SelectCareRelationshipRequest(doctor_id=doctor.doctor_id),
		)

		error = _assert_raises(
			professional_care_workflow.ProfessionalCareWorkflowError,
			lambda: professional_care_workflow.reject_care_request(db, doctor.doctor_id, request.request_id),
		)
		assert "active Care Relationship" in error.detail
		db.refresh(request)
		assert request.status == "accepted"


	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_accept_again_patient")
		doctor = _create_doctor(db, "pc_workflow_accept_again_doctor")
		episode = _create_episode(db, patient, "Accept again episode")
		request = _create_request(db, episode, doctor)
		db.commit()
		professional_care_workflow.accept_care_request(db, doctor.doctor_id, request.request_id)

		error = _assert_raises(
			professional_care_workflow.ProfessionalCareWorkflowError,
			lambda: professional_care_workflow.accept_care_request(db, doctor.doctor_id, request.request_id),
		)
		assert "pending" in error.detail


def test_create_request_reopens_rejected_request_without_integrity_error() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_reopen_patient")
		doctor = _create_doctor(db, "pc_workflow_reopen_doctor")
		episode = _create_episode(db, patient, "Reopen request episode")
		rejected_request = _create_request(db, episode, doctor, status="rejected", reason="Old declined reason.")
		db.commit()

		result = professional_care_workflow.create_care_request(
			db,
			patient.patient_id,
			episode.care_episode_id,
			CareConnectionRequestCreate(doctor_id=doctor.doctor_id, request_reason="Please reconsider."),
		)

		assert result.request_id == rejected_request.request_id
		assert result.status == "pending"
		assert result.request_reason == "Please reconsider."
		db.refresh(rejected_request)
		assert rejected_request.status == "pending"
		assert rejected_request.request_reason == "Please reconsider."
		assert rejected_request.responded_at is None


def test_reopening_rejected_request_respects_pending_request_cap() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "pc_workflow_reopen_cap_patient")
		rejected_doctor = _create_doctor(db, "pc_workflow_reopen_cap_rejected")
		pending_doctors = [_create_doctor(db, f"pc_workflow_reopen_cap_pending_{index}") for index in range(3)]
		episode = _create_episode(db, patient, "Reopen cap episode")
		rejected_request = _create_request(db, episode, rejected_doctor, status="rejected", reason="Old rejected request.")
		for doctor in pending_doctors:
			_create_request(db, episode, doctor, status="pending", reason="Pending request.")
		db.commit()

		error = _assert_raises(
			professional_care_workflow.ProfessionalCareWorkflowError,
			lambda: professional_care_workflow.create_care_request(
				db,
				patient.patient_id,
				episode.care_episode_id,
				CareConnectionRequestCreate(doctor_id=rejected_doctor.doctor_id, request_reason="Please reconsider."),
			),
		)

		assert error.detail == "At most three pending Care Connection Requests are allowed"
		db.refresh(rejected_request)
		assert rejected_request.status == "rejected"
		assert rejected_request.request_reason == "Old rejected request."
		pending_count = db.execute(
			select(func.count()).select_from(CareConnectionRequest).where(
				CareConnectionRequest.care_episode_id == episode.care_episode_id,
				CareConnectionRequest.status == "pending",
			)
		).scalar_one()
		assert pending_count == 3


def test_worklist_contains_request_and_active_relationship_rows_for_doctor() -> None:
	with SessionLocal() as db:
		pending_patient = _create_patient(db, "pc_workflow_worklist_pending_patient")
		active_patient = _create_patient(db, "pc_workflow_worklist_active_patient")
		doctor = _create_doctor(db, "pc_workflow_worklist_doctor")
		pending_episode = _create_episode(db, pending_patient, "Pending worklist episode")
		active_episode = _create_episode(db, active_patient, "Active worklist episode")
		pending_request = _create_request(db, pending_episode, doctor, reason="Please review pending episode.")
		accepted_request = _create_request(db, active_episode, doctor, status="accepted", reason="Please review active episode.")
		db.commit()

		professional_care_workflow.select_patient_relationship(
			db,
			active_patient.patient_id,
			active_episode.care_episode_id,
			SelectCareRelationshipRequest(doctor_id=doctor.doctor_id),
		)

		worklist = professional_care_workflow.get_doctor_worklist(db, doctor.doctor_id)

		assert [item.request_id for item in worklist.incoming_requests] == [pending_request.request_id]
		assert worklist.accepted_requests == []
		assert len(worklist.active_relationships) == 1
		preview = worklist.active_relationships[0]
		assert preview.relationship_id
		assert preview.care_episode_id == active_episode.care_episode_id
		assert preview.patient_id == active_patient.patient_id
		assert preview.patient_name == active_patient.patient_name
		assert preview.doctor_id == doctor.doctor_id
		assert preview.status == "active"
		assert preview.issue_title == "Active worklist episode"
		assert preview.latest_triage_summary["concern"] == "Active worklist episode"
		assert accepted_request.request_id


def main() -> None:
	init_db()
	test_importing_workflow_service_does_not_import_fastapi()
	test_create_request_uses_existing_subscribed_doctor_and_patient_episode()
	test_accept_request_creates_or_activates_relationship_and_marks_request_accepted()
	test_reject_request_does_not_create_active_relationship()
	test_select_relationship_marks_initial_active_relationship_selected_for_patient()
	test_selecting_different_doctor_after_active_relationship_is_blocked()
	test_request_transitions_are_pending_only()
	test_create_request_reopens_rejected_request_without_integrity_error()
	test_reopening_rejected_request_respects_pending_request_cap()
	test_worklist_contains_request_and_active_relationship_rows_for_doctor()
	print("professional care workflow contract ok")


if __name__ == "__main__":
	main()
