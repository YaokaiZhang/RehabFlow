from __future__ import annotations

"""Care Relationship authorization rules shared by HTTP and websocket transports."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CareEpisode, CareRelationship

ACTIVE_RELATIONSHIP_STATUS = "active"


class CareRelationshipAccessError(Exception):
	"""Base class for Care Relationship authorization failures."""


class PrincipalRequired(CareRelationshipAccessError):
	"""Raised when an authenticated principal cannot be resolved."""


class EpisodeAccessDenied(CareRelationshipAccessError):
	"""Raised when a principal cannot access an existing Care Episode."""


class RelationshipAccessDenied(CareRelationshipAccessError):
	"""Raised when a principal cannot access an existing Care Relationship."""


class RelationshipNotActive(CareRelationshipAccessError):
	"""Raised when an active Care Relationship is required but absent."""


class EpisodeNotFound(CareRelationshipAccessError):
	"""Raised when a Care Episode cannot be found."""


def _uuid(value: object, error_type: type[CareRelationshipAccessError] = PrincipalRequired) -> UUID:
	if value is None or value == "":
		raise error_type("Missing identity")
	try:
		return UUID(str(value))
	except (TypeError, ValueError) as exc:
		raise error_type("Invalid identity") from exc


def principal_id(user: object) -> str:
	"""Return the authenticated user id string from a principal-like object."""
	if not isinstance(user, dict):
		raise PrincipalRequired("Missing user identity")
	user_id = user.get("user_id")
	_uuid(user_id, PrincipalRequired)
	return str(user_id)


def require_patient_episode(db: Session, patient_id: object, episode_id: object) -> CareEpisode:
	"""Return an episode only when it belongs to the patient."""
	patient_uuid = _uuid(patient_id, PrincipalRequired)
	episode_uuid = _uuid(episode_id, EpisodeNotFound)
	episode = db.get(CareEpisode, episode_uuid)
	if episode is None:
		raise EpisodeNotFound("Care Episode not found")
	if episode.patient_id != patient_uuid:
		raise EpisodeAccessDenied("Care Episode not found")
	return episode


def _active_relationship_for_episode(db: Session, *, episode_id: UUID, doctor_id: UUID | None = None) -> CareRelationship:
	conditions = [
		CareRelationship.care_episode_id == episode_id,
		CareRelationship.status == ACTIVE_RELATIONSHIP_STATUS,
	]
	if doctor_id is not None:
		conditions.append(CareRelationship.doctor_id == doctor_id)
	relationship = db.execute(select(CareRelationship).where(*conditions)).scalars().first()
	if relationship is None:
		raise RelationshipNotActive("Active Care Relationship required")
	return relationship


def require_doctor_active_relationship(db: Session, doctor_id: object, episode_id: object) -> tuple[CareRelationship, CareEpisode]:
	"""Return the active relationship and episode for the doctor assigned to the episode patient."""
	doctor_uuid = _uuid(doctor_id, PrincipalRequired)
	episode_uuid = _uuid(episode_id, EpisodeNotFound)
	episode = db.get(CareEpisode, episode_uuid)
	if episode is None:
		raise EpisodeNotFound("Care Episode not found")
	relationship = _active_relationship_for_episode(db, episode_id=episode.care_episode_id, doctor_id=doctor_uuid)
	return relationship, episode


def require_relationship_access(
	db: Session,
	principal_id: object,
	relationship_id: object | None = None,
	episode_id: object | None = None,
) -> tuple[CareRelationship, CareEpisode]:
	"""Return an active relationship and episode when the caller is its patient or doctor."""
	principal_uuid = _uuid(principal_id, PrincipalRequired)
	if relationship_id is None and episode_id is None:
		raise RelationshipAccessDenied("Care Relationship not found")

	if relationship_id is not None:
		relationship_uuid = _uuid(relationship_id, RelationshipAccessDenied)
		relationship = db.get(CareRelationship, relationship_uuid)
		if relationship is None:
			raise RelationshipAccessDenied("Care Relationship not found")
		if relationship.status != ACTIVE_RELATIONSHIP_STATUS:
			raise RelationshipNotActive("Active Care Relationship required")
		if episode_id is not None and relationship.care_episode_id != _uuid(episode_id, EpisodeNotFound):
			raise RelationshipAccessDenied("Care workspace not found")
	else:
		episode_uuid = _uuid(episode_id, EpisodeNotFound)
		episode = db.get(CareEpisode, episode_uuid)
		if episode is None:
			raise EpisodeNotFound("Care Episode not found")
		relationship = _active_relationship_for_episode(db, episode_id=episode.care_episode_id)

	episode = db.get(CareEpisode, relationship.care_episode_id)
	if episode is None:
		raise EpisodeNotFound("Care Episode not found")
	if relationship.patient_id != principal_uuid and relationship.doctor_id != principal_uuid:
		raise RelationshipAccessDenied("Care workspace not found")
	return relationship, episode


def resolve_monitor_patient(
	db: Session,
	doctor_id: object,
	patient_id: object | None = None,
	episode_id: object | None = None,
) -> CareEpisode:
	"""Resolve the patient episode a doctor may monitor through an active Care Relationship."""
	doctor_uuid = _uuid(doctor_id, PrincipalRequired)
	if episode_id is None and patient_id is None:
		raise EpisodeNotFound("Care Episode not found")

	if episode_id is not None:
		relationship, episode = require_doctor_active_relationship(db, doctor_uuid, episode_id)
		if patient_id is not None and episode.patient_id != _uuid(patient_id, EpisodeAccessDenied):
			raise EpisodeAccessDenied("Care Episode not found")
		return episode

	patient_uuid = _uuid(patient_id, EpisodeAccessDenied)
	relationship = db.execute(
		select(CareRelationship).where(
			CareRelationship.patient_id == patient_uuid,
			CareRelationship.doctor_id == doctor_uuid,
			CareRelationship.status == ACTIVE_RELATIONSHIP_STATUS,
		)
	).scalars().first()
	if relationship is None:
		raise RelationshipNotActive("Active Care Relationship required")
	episode = db.get(CareEpisode, relationship.care_episode_id)
	if episode is None:
		raise EpisodeNotFound("Care Episode not found")
	return episode
