from __future__ import annotations

import sys
import time
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session

from app.db.models import CareConnectionRequest, CareEpisode, CareRelationship, Doctor, Patient
from app.db.session import SessionLocal, init_db
from app.services import care_relationship_access



def _unique_name(prefix: str) -> str:
	return f"{prefix}_{time.time_ns()}"


def _create_patient(db: Session, prefix: str = "access_patient") -> Patient:
	patient = Patient(patient_name=_unique_name(prefix), password_hash="test-hash", real_info={})
	db.add(patient)
	db.flush()
	return patient


def _create_doctor(db: Session, prefix: str = "access_doctor") -> Doctor:
	doctor = Doctor(
		doctor_name=_unique_name(prefix),
		password_hash="test-hash",
		real_info={"specialty": "PT"},
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
		short_description="Needs guidance for home rehab.",
		origin="manual",
		status="active",
		safety_gate_status="triage_complete",
	)
	db.add(episode)
	db.flush()
	return episode


def _create_relationship(db: Session, episode: CareEpisode, doctor: Doctor, status: str = "active") -> CareRelationship:
	request = CareConnectionRequest(
		care_episode_id=episode.care_episode_id,
		patient_id=episode.patient_id,
		doctor_id=doctor.doctor_id,
		request_reason="Please review this episode.",
		status="accepted" if status == "active" else status,
	)
	db.add(request)
	db.flush()
	relationship = CareRelationship(
		care_episode_id=episode.care_episode_id,
		patient_id=episode.patient_id,
		doctor_id=doctor.doctor_id,
		source_request_id=request.request_id,
		status=status,
	)
	db.add(relationship)
	db.flush()
	return relationship


def _assert_raises(expected_type: type[Exception], callback) -> Exception:
	try:
		callback()
	except expected_type as exc:
		return exc
	raise AssertionError(f"Expected {expected_type.__name__}")


def test_require_patient_episode_allows_owner_and_rejects_other_patient() -> None:
	with SessionLocal() as db:
		owner = _create_patient(db, "access_owner")
		other = _create_patient(db, "access_other")
		episode = _create_episode(db, owner)
		db.commit()

		allowed = care_relationship_access.require_patient_episode(db, str(owner.patient_id), str(episode.care_episode_id))
		assert allowed.care_episode_id == episode.care_episode_id

		_assert_raises(
			care_relationship_access.EpisodeAccessDenied,
			lambda: care_relationship_access.require_patient_episode(db, str(other.patient_id), str(episode.care_episode_id)),
		)


def test_require_doctor_active_relationship_allows_monitoring_selected_patient() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "access_selected_patient")
		doctor = _create_doctor(db, "access_selected_doctor")
		episode = _create_episode(db, patient, "Selected monitor episode")
		_create_relationship(db, episode, doctor, status="active")
		episode.selected_doctor_id = doctor.doctor_id
		db.commit()

		relationship, returned_episode = care_relationship_access.require_doctor_active_relationship(
			db,
			str(doctor.doctor_id),
			str(episode.care_episode_id),
		)
		assert relationship.doctor_id == doctor.doctor_id
		assert relationship.status == "active"
		assert returned_episode.care_episode_id == episode.care_episode_id


def test_require_doctor_active_relationship_rejects_pending_or_rejected_relationship() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "access_inactive_patient")
		doctor = _create_doctor(db, "access_inactive_doctor")
		pending_episode = _create_episode(db, patient, "Pending relationship episode")
		rejected_episode = _create_episode(db, patient, "Rejected relationship episode")
		_create_relationship(db, pending_episode, doctor, status="pending")
		_create_relationship(db, rejected_episode, doctor, status="rejected")
		db.commit()

		for episode in [pending_episode, rejected_episode]:
			_assert_raises(
				care_relationship_access.RelationshipNotActive,
				lambda episode=episode: care_relationship_access.require_doctor_active_relationship(
					db,
					str(doctor.doctor_id),
					str(episode.care_episode_id),
				),
			)


def test_require_relationship_access_allows_patient_or_doctor_on_active_relationship() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "access_relationship_patient")
		doctor = _create_doctor(db, "access_relationship_doctor")
		episode = _create_episode(db, patient, "Relationship workspace episode")
		relationship = _create_relationship(db, episode, doctor, status="active")
		db.commit()

		patient_relationship, patient_episode = care_relationship_access.require_relationship_access(
			db,
			str(patient.patient_id),
			str(relationship.relationship_id),
			str(episode.care_episode_id),
		)
		doctor_relationship, doctor_episode = care_relationship_access.require_relationship_access(
			db,
			str(doctor.doctor_id),
			str(relationship.relationship_id),
			str(episode.care_episode_id),
		)

		assert patient_relationship.relationship_id == relationship.relationship_id
		assert patient_episode.care_episode_id == episode.care_episode_id
		assert doctor_relationship.relationship_id == relationship.relationship_id
		assert doctor_episode.care_episode_id == episode.care_episode_id


def test_resolve_monitor_patient_uses_same_active_relationship_rule_as_http() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db, "access_monitor_patient")
		active_doctor = _create_doctor(db, "access_monitor_active")
		other_doctor = _create_doctor(db, "access_monitor_other")
		episode = _create_episode(db, patient, "Live monitor episode")
		_create_relationship(db, episode, active_doctor, status="active")
		db.commit()

		resolved = care_relationship_access.resolve_monitor_patient(
			db,
			str(active_doctor.doctor_id),
			episode_id=str(episode.care_episode_id),
		)
		assert resolved.patient_id == patient.patient_id
		assert resolved.care_episode_id == episode.care_episode_id

		resolved_by_patient = care_relationship_access.resolve_monitor_patient(
			db,
			str(active_doctor.doctor_id),
			patient_id=str(patient.patient_id),
		)
		assert resolved_by_patient.patient_id == patient.patient_id
		assert resolved_by_patient.care_episode_id == episode.care_episode_id

		_assert_raises(
			care_relationship_access.RelationshipNotActive,
			lambda: care_relationship_access.resolve_monitor_patient(
				db,
				str(other_doctor.doctor_id),
				episode_id=str(episode.care_episode_id),
			),
		)


def main() -> None:
	init_db()
	test_require_patient_episode_allows_owner_and_rejects_other_patient()
	test_require_doctor_active_relationship_allows_monitoring_selected_patient()
	test_require_doctor_active_relationship_rejects_pending_or_rejected_relationship()
	test_require_relationship_access_allows_patient_or_doctor_on_active_relationship()
	test_resolve_monitor_patient_uses_same_active_relationship_rule_as_http()
	assert UUID(str(care_relationship_access.principal_id({"user_id": UUID(int=1)}))) == UUID(int=1)
	print("care relationship access contract ok")


if __name__ == "__main__":
	main()
