from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
	CareAppointment,
	CareEpisode,
	CareRelationship,
	DoctorIntelligenceArtifact,
	Patient,
)
from app.schemas.doctor_dashboard import DoctorIntelligenceArtifactCreate
from app.services.memory_documents import build_memory_context_pack

BRIEFING_TYPE = "patient_panel_briefing"
CARE_WORKLIST_HREF = "/doctor"


def _active_relationship_rows(db: Session, doctor_id: UUID) -> list[tuple[CareRelationship, Patient, CareEpisode]]:
	return list(
		db.execute(
			select(CareRelationship, Patient, CareEpisode)
			.join(Patient, Patient.patient_id == CareRelationship.patient_id)
			.join(
				CareEpisode,
				and_(
					CareEpisode.care_episode_id == CareRelationship.care_episode_id,
					CareEpisode.patient_id == CareRelationship.patient_id,
				),
			)
			.where(CareRelationship.doctor_id == doctor_id, CareRelationship.status == "active")
			.order_by(CareRelationship.created_at.asc())
		).all()
	)


def _upcoming_appointments(db: Session, doctor_id: UUID, relationship_ids: list[UUID]) -> list[CareAppointment]:
	if not relationship_ids:
		return []
	now = datetime.now(timezone.utc)
	return list(
		db.execute(
			select(CareAppointment)
			.join(CareRelationship, CareRelationship.relationship_id == CareAppointment.relationship_id)
			.where(
				CareRelationship.relationship_id.in_(relationship_ids),
				CareRelationship.doctor_id == doctor_id,
				CareRelationship.status == "active",
				CareAppointment.doctor_id == CareRelationship.doctor_id,
				CareAppointment.patient_id == CareRelationship.patient_id,
				or_(
					CareAppointment.care_episode_id.is_(None),
					CareAppointment.care_episode_id == CareRelationship.care_episode_id,
				),
				CareAppointment.status.in_(["scheduled", "confirmed"]),
				CareAppointment.scheduled_start >= now,
			)
			.order_by(CareAppointment.scheduled_start.asc())
		).scalars().all()
	)


def _latest_artifact_version(db: Session, doctor_id: UUID, artifact_type: str) -> int:
	latest = db.execute(
		select(func.max(DoctorIntelligenceArtifact.version)).where(
			DoctorIntelligenceArtifact.doctor_id == doctor_id,
			DoctorIntelligenceArtifact.artifact_type == artifact_type,
		)
	).scalar_one_or_none()
	return int(latest or 0)


def _briefing_context(db: Session, doctor_id: UUID) -> dict:
	relationship_rows = _active_relationship_rows(db, doctor_id)
	relationship_ids = [row[0].relationship_id for row in relationship_rows]
	appointments = _upcoming_appointments(db, doctor_id, relationship_ids)
	appointments_by_relationship: dict[UUID, list[CareAppointment]] = {}
	for appointment in appointments:
		appointments_by_relationship.setdefault(appointment.relationship_id, []).append(appointment)

	relationships: list[dict] = []
	memory_highlights: list[dict] = []
	clinician_review_items: list[dict] = []
	upcoming_items: list[dict] = []
	quiet_items: list[dict] = []

	for relationship, patient, episode in relationship_rows:
		_, _, memory_context = build_memory_context_pack(db, episode)
		latest_triage_summary = episode.latest_triage_summary or None
		next_appointments = appointments_by_relationship.get(relationship.relationship_id, [])
		next_appointment = next_appointments[0] if next_appointments else None
		relationship_payload = {
			"relationship_id": str(relationship.relationship_id),
			"care_episode_id": str(relationship.care_episode_id),
			"patient_id": str(relationship.patient_id),
			"patient_name": patient.patient_name,
			"doctor_id": str(relationship.doctor_id),
			"status": relationship.status,
			"issue_title": episode.issue_title,
			"body_area": episode.body_area,
			"goal": episode.goal,
			"episode_status": episode.status,
			"safety_gate_status": episode.safety_gate_status,
			"latest_triage_summary": latest_triage_summary,
			"memory_context": memory_context,
			"appointment_signal": {
				"appointment_id": str(next_appointment.appointment_id),
				"scheduled_start": next_appointment.scheduled_start.isoformat(),
				"purpose": next_appointment.purpose,
				"status": next_appointment.status,
			}
			if next_appointment is not None
			else None,
			"started_at": relationship.created_at.isoformat() if relationship.created_at else None,
		}
		relationships.append(relationship_payload)
		memory_highlights.append(
			{
				"relationship_id": str(relationship.relationship_id),
				"patient_id": str(patient.patient_id),
				"care_episode_id": str(episode.care_episode_id),
				"issue_title": episode.issue_title,
				"memory_context": memory_context,
			}
		)
		if latest_triage_summary and latest_triage_summary.get("clinician_review_needed", True):
			clinician_review_items.append(
				{
					"relationship_id": str(relationship.relationship_id),
					"patient_id": str(patient.patient_id),
					"care_episode_id": str(episode.care_episode_id),
					"label": f"Review {episode.issue_title}",
				}
			)
		elif latest_triage_summary is None:
			clinician_review_items.append(
				{
					"relationship_id": str(relationship.relationship_id),
					"patient_id": str(patient.patient_id),
					"care_episode_id": str(episode.care_episode_id),
					"label": f"No saved triage summary for {episode.issue_title}",
				}
			)
		else:
			quiet_items.append(
				{
					"relationship_id": str(relationship.relationship_id),
					"patient_id": str(patient.patient_id),
					"care_episode_id": str(episode.care_episode_id),
					"label": f"{episode.issue_title} is informational only",
				}
			)

	for appointment in appointments:
		upcoming_items.append(
			{
				"appointment_id": str(appointment.appointment_id),
				"relationship_id": str(appointment.relationship_id),
				"patient_id": str(appointment.patient_id),
				"care_episode_id": str(appointment.care_episode_id) if appointment.care_episode_id else None,
				"label": appointment.purpose or "Scheduled care appointment",
				"scheduled_start": appointment.scheduled_start.isoformat(),
			}
		)

	buckets = []
	if clinician_review_items:
		buckets.append({"bucket": "clinician_review", "label": "Clinician review", "items": clinician_review_items})
	if upcoming_items:
		buckets.append({"bucket": "upcoming_appointments", "label": "Upcoming appointments", "items": upcoming_items})
	if not buckets:
		buckets.append({"bucket": "quiet_panel", "label": "Quiet panel", "items": quiet_items})
	attention_map = {
		"summary": f"{len(relationship_rows)} active Care Relationship(s), {len(appointments)} upcoming appointment(s).",
		"buckets": buckets,
	}

	sections = [
		{
			"key": "patient_panel",
			"title": "Patient panel",
			"items": [
				{
					"relationship_id": item["relationship_id"],
					"patient_id": item["patient_id"],
					"patient_name": item["patient_name"],
					"issue_title": item["issue_title"],
				}
				for item in relationships
			],
		},
		{
			"key": "episodes",
			"title": "Care Episodes",
			"items": [
				{
					"care_episode_id": item["care_episode_id"],
					"issue_title": item["issue_title"],
					"goal": item["goal"],
					"latest_triage_summary": item["latest_triage_summary"],
				}
				for item in relationships
			],
		},
		{"key": "memory_highlights", "title": "Memory highlights", "items": memory_highlights},
		{"key": "upcoming_appointments", "title": "Upcoming appointments", "items": upcoming_items},
	]
	if relationships:
		content_lines = ["Patient Panel Briefing"]
		for item in relationships:
			content_lines.append(f"- {item['patient_name']}: {item['issue_title']} ({item['goal']})")
			if item["memory_context"]:
				content_lines.append(f"  Memory: {item['memory_context']}")
		if upcoming_items:
			content_lines.append("Upcoming appointments:")
			for item in upcoming_items:
				content_lines.append(f"- {item['label']}")
	else:
		content_lines = ["Patient Panel Briefing", "No active Care Relationships for this doctor."]

	input_refs = {
		"relationship_ids": [item["relationship_id"] for item in relationships],
		"patient_ids": [item["patient_id"] for item in relationships],
		"care_episode_ids": [item["care_episode_id"] for item in relationships],
		"appointment_ids": [str(appointment.appointment_id) for appointment in appointments],
	}
	return {
		"relationships": relationships,
		"appointments": appointments,
		"attention_map": attention_map,
		"sections": sections,
		"content": "\n".join(content_lines),
		"input_refs": input_refs,
	}


def _latest_patient_panel_briefing(db: Session, doctor_id: UUID) -> DoctorIntelligenceArtifact | None:
	return (
		db.execute(
			select(DoctorIntelligenceArtifact)
			.where(
				DoctorIntelligenceArtifact.doctor_id == doctor_id,
				DoctorIntelligenceArtifact.artifact_type == BRIEFING_TYPE,
				DoctorIntelligenceArtifact.status == "active",
			)
			.order_by(DoctorIntelligenceArtifact.version.desc(), DoctorIntelligenceArtifact.created_at.desc())
		)
		.scalars()
		.first()
	)


def _briefing_matches_context(artifact: DoctorIntelligenceArtifact, context: dict) -> bool:
	return (
		artifact.content == context["content"]
		and artifact.sections == context["sections"]
		and artifact.attention_map == context["attention_map"]
		and artifact.input_refs == context["input_refs"]
	)


def _create_patient_panel_briefing_from_context(
	db: Session, doctor_id: UUID, context: dict
) -> DoctorIntelligenceArtifact:
	version = _latest_artifact_version(db, doctor_id, BRIEFING_TYPE) + 1
	artifact = DoctorIntelligenceArtifact(
		doctor_id=doctor_id,
		artifact_type=BRIEFING_TYPE,
		title="Patient Panel Briefing",
		content=context["content"],
		sections=context["sections"],
		attention_map=context["attention_map"],
		input_refs=context["input_refs"],
		visibility="doctor_private",
		version=version,
		status="active",
	)
	db.add(artifact)
	db.commit()
	db.refresh(artifact)
	return artifact


def create_patient_panel_briefing(db: Session, doctor_id: UUID) -> DoctorIntelligenceArtifact:
	context = _briefing_context(db, doctor_id)
	return _create_patient_panel_briefing_from_context(db, doctor_id, context)


def latest_or_create_patient_panel_briefing(
	db: Session, doctor_id: UUID, context: dict | None = None
) -> DoctorIntelligenceArtifact:
	current_context = context if context is not None else _briefing_context(db, doctor_id)
	artifact = _latest_patient_panel_briefing(db, doctor_id)
	if artifact is not None and _briefing_matches_context(artifact, current_context):
		return artifact
	return _create_patient_panel_briefing_from_context(db, doctor_id, current_context)


def save_doctor_artifact(db: Session, doctor_id: UUID, payload: DoctorIntelligenceArtifactCreate) -> DoctorIntelligenceArtifact:
	version = _latest_artifact_version(db, doctor_id, payload.artifact_type) + 1
	artifact = DoctorIntelligenceArtifact(
		doctor_id=doctor_id,
		artifact_type=payload.artifact_type,
		title=payload.title,
		content=payload.content,
		sections=payload.sections,
		attention_map=payload.attention_map,
		input_refs=payload.input_refs,
		visibility="doctor_private",
		version=version,
		status="active",
	)
	db.add(artifact)
	db.commit()
	db.refresh(artifact)
	return artifact


def doctor_dashboard_payload(db: Session, doctor_id: UUID) -> dict:
	context = _briefing_context(db, doctor_id)
	return {
		"doctor_id": doctor_id,
		"care_worklist_href": CARE_WORKLIST_HREF,
		"patient_panel_briefing": latest_or_create_patient_panel_briefing(db, doctor_id, context),
		"attention_map": context["attention_map"],
		"upcoming_appointments": context["appointments"],
		"care_relationships": context["relationships"],
	}
