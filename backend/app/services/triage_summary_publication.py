from __future__ import annotations

"""Write-side workflow for Triage Summary publication.

Only saved Triage Summaries activate AI Daily Rehab Recommendation creation.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AIMessage, AISession, CareEpisode, CareEpisodeTriageSummary, CareEpisodeTriageSummaryDraft, Patient
from app.schemas.care_episodes import TriageSummaryRequest
from app.services.memory_agent import MemoryMaintenanceConflict, canonical_request_hash, enqueue_memory_maintenance, find_existing_memory_maintenance
from app.services.rehab_recommendations import create_rehab_recommendation_for_summary
from app.services.triage_summary_agent import TriageSummaryGenerationFailed, TriageSummarySessionNotFound, build_triage_summary



class TriageSummaryPublicationError(Exception):
	"""Base error for Triage Summary publication workflow failures."""


class TriageSummaryNotFound(TriageSummaryPublicationError):
	"""Raised when a Triage Summary draft or related generation source is not found."""


class TriageSummaryGenerationUnavailable(TriageSummaryPublicationError):
	"""Raised when required LLM Triage Summary generation fails."""


class TriageSummaryIdempotencyConflict(TriageSummaryPublicationError):
	"""Raised when a triage identity is reused with another request payload."""


class EpisodeNotFound(TriageSummaryPublicationError):
	"""Raised when the patient does not own the requested Care Episode."""


@dataclass(frozen=True)
class TriageSummaryPublicationResult:
	artifact: dict[str, Any]
	summary: CareEpisodeTriageSummary | None = None
	draft: CareEpisodeTriageSummaryDraft | None = None
	maintenance_run_id: UUID | None = None


def _triage_originating_event(
	db: Session,
	patient_id: UUID,
	episode_id: UUID | None,
	request: TriageSummaryRequest,
	idempotency_key: str | None = None,
) -> tuple[str, UUID | None]:
	session = db.get(AISession, request.ai_session_id) if request.ai_session_id is not None else None
	if request.ai_session_id is not None and (
		session is None or session.patient_id != patient_id
	):
		raise TriageSummaryNotFound("AI triage session not found")
	if session is not None and episode_id is not None and session.care_episode_id not in {None, episode_id}:
		raise TriageSummaryNotFound("AI triage session is not bound to this Care Episode")
	if request.originating_message_id is not None:
		if session is None:
			raise TriageSummaryNotFound("an originating message requires an AI triage session")
		message = db.get(AIMessage, request.originating_message_id)
		if (
			message is None
			or message.session_id != session.session_id
			or message.sender_role != "user"
		):
			raise TriageSummaryNotFound("originating triage message not found")
		return f"ai_message:{message.message_id}", message.message_id
	if request.originating_event_id is not None:
		if session is None:
			raise TriageSummaryNotFound("an originating event requires an AI triage session")
		normalized_event_id = str(request.originating_event_id).strip()
		event_rows = db.execute(
			select(AIMessage).where(
				AIMessage.session_id == session.session_id,
				AIMessage.sender_role == "user",
			)
		).scalars().all()
		if not any(
			str(
				(row.message_metadata or {}).get("originating_event_id")
				if isinstance(row.message_metadata, dict)
				else ""
			).strip()
			in {
				normalized_event_id,
				normalized_event_id.removeprefix("triage_event:"),
			}
			for row in event_rows
		):
			raise TriageSummaryNotFound("originating triage event not found")
		return f"triage_event:{request.originating_event_id}", None
	if request.ai_session_id is not None:
		user_messages = db.execute(
			select(AIMessage).where(
				AIMessage.session_id == session.session_id,
				AIMessage.sender_role == "user",
			)
		).scalars().all()
		user_messages.sort(
			key=lambda row: (
				int((row.message_metadata or {}).get("sequence", 0))
				if isinstance(row.message_metadata, dict)
				and str((row.message_metadata or {}).get("sequence", "")).isdigit()
				else 0,
				str(row.message_id),
			)
		)
		latest_message = user_messages[-1] if user_messages else None
		if latest_message is not None:
			metadata = latest_message.message_metadata if isinstance(latest_message.message_metadata, dict) else {}
			latest_event_id = str(metadata.get("originating_event_id") or "").strip()
			if latest_event_id:
				return f"triage_event:{latest_event_id}", latest_message.message_id
			return f"ai_session:{session.session_id}", latest_message.message_id
		return f"ai_session:{session.session_id}", None
	normalized_key = str(idempotency_key or "").strip()[:128]
	if normalized_key:
		event_hash = canonical_request_hash(
			{
				"patient_id": patient_id,
				"ai_session_id": request.ai_session_id,
				"additional_context": request.additional_context.strip(),
				"idempotency_key": normalized_key,
			}
		)[:32]
		return f"triage_event:{event_hash}", None
	return f"triage_event:{uuid4().hex}", None


def _bind_triage_session(
	db: Session,
	patient_id: UUID,
	episode_id: UUID,
	request: TriageSummaryRequest,
) -> None:
	if request.ai_session_id is None:
		return
	session = db.get(AISession, request.ai_session_id)
	if session is None or session.patient_id != patient_id or (
		session.care_episode_id is not None and session.care_episode_id != episode_id
	):
		raise TriageSummaryNotFound("AI triage session is not owned by this Care Episode")
	if session.care_episode_id is None:
		session.care_episode_id = episode_id
	db.flush()


def _triage_identity(
	db: Session,
	patient_id: UUID,
	episode_id: UUID | None,
	request: TriageSummaryRequest,
	idempotency_key: str | None,
) -> tuple[str, str, str, UUID | None]:
	patient = db.execute(
		select(Patient)
		.where(Patient.patient_id == patient_id)
		.with_for_update()
	).scalar_one_or_none()
	if patient is None:
		raise TriageSummaryNotFound("patient not found")
	origin_event, origin_message_id = _triage_originating_event(db, patient_id, episode_id, request, idempotency_key)
	normalized_key = str(idempotency_key or "").strip()[:128]
	operation_identity = f"key:{normalized_key}" if normalized_key else f"event:{origin_event}"
	payload = {
		"patient_id": patient_id,
		"origin_event": origin_event,
		"origin_message_id": origin_message_id,
		"ai_session_id": request.ai_session_id,
		"additional_context": request.additional_context.strip(),
		"idempotency_key": normalized_key,
	}
	return (
		f"triage-summary-generation:{patient_id}:{operation_identity}",
		canonical_request_hash(payload),
		origin_event,
		origin_message_id,
	)


def _stable_excluded_message_ids(
	db: Session,
	*,
	session_id: UUID | None,
	origin_message_id: UUID | None,
	originating_event_id: str | None = None,
	artifact_id: UUID,
) -> set[UUID]:
	ids = {origin_message_id} if origin_message_id is not None else set()
	if session_id is None:
		return ids
	rows = db.execute(select(AIMessage).where(AIMessage.session_id == session_id)).scalars().all()
	for row in rows:
		metadata = row.message_metadata if isinstance(row.message_metadata, dict) else {}
		if str(metadata.get("triage_summary_id") or metadata.get("originating_artifact_id") or "") == str(artifact_id):
			ids.add(row.message_id)
		event_id = str(metadata.get("originating_event_id") or "").strip()
		if originating_event_id and event_id in {
			str(originating_event_id).strip(),
			str(originating_event_id).removeprefix("triage_event:").strip(),
		}:
			ids.add(row.message_id)
	return ids


def _replay_existing_publication(
	db: Session,
	existing: Any,
	*,
	is_draft: bool,
	expected_patient_id: UUID | None = None,
	expected_episode_id: UUID | None = None,
) -> TriageSummaryPublicationResult | None:
	if expected_patient_id is not None and existing.patient_id != expected_patient_id:
		raise TriageSummaryIdempotencyConflict("triage maintenance is not owned by this patient")
	existing_episode = db.get(CareEpisode, existing.care_episode_id)
	if (
		existing_episode is None
		or existing_episode.patient_id != existing.patient_id
		or str(existing_episode.status or "active") != "active"
	):
		raise TriageSummaryIdempotencyConflict("triage maintenance is not bound to an active patient episode")
	if expected_episode_id is not None and existing_episode.care_episode_id != expected_episode_id:
		raise TriageSummaryIdempotencyConflict("triage idempotency identity is bound to another Care Episode")
	snapshot = dict(existing.snapshot or {})
	request = dict(snapshot.get("_snapshot_request") or {})
	artifact_id = request.get("originating_artifact_id") or snapshot.get("originating_artifact_id")
	if not artifact_id:
		return None
	try:
		artifact_uuid = UUID(str(artifact_id))
	except ValueError:
		return None
	if is_draft:
		draft = db.get(CareEpisodeTriageSummaryDraft, artifact_uuid)
		if draft is None:
			return None
		return TriageSummaryPublicationResult(
			artifact=triage_summary_artifact(draft, is_draft=True),
			draft=draft,
			maintenance_run_id=existing.maintenance_id,
		)
	summary = db.get(CareEpisodeTriageSummary, artifact_uuid)
	if summary is None:
		return None
	return TriageSummaryPublicationResult(
		artifact=triage_summary_artifact(summary),
		summary=summary,
		maintenance_run_id=existing.maintenance_id,
	)
def triage_summary_artifact(
	candidate: CareEpisodeTriageSummary | CareEpisodeTriageSummaryDraft,
	summary: CareEpisodeTriageSummary | CareEpisodeTriageSummaryDraft | None = None,
	*,
	is_draft: bool = False,
) -> dict[str, Any]:
	item = summary or candidate
	created_at = item.created_at.isoformat() if getattr(item, "created_at", None) else None
	if is_draft:
		return {
			"triage_summary_draft_id": str(getattr(item, "triage_summary_draft_id")),
			"care_episode_id": str(item.care_episode_id),
			"patient_id": str(item.patient_id),
			"saved": False,
			"concern": item.concern,
			"relevant_context": item.relevant_context,
			"safety_signals": item.safety_signals,
			"limitations": item.limitations,
			"recommendation": item.recommendation,
			"missing_information": item.missing_information,
			"unresolved_questions": item.unresolved_questions,
			"clinician_review_needed": item.clinician_review_needed,
			"additional_context": item.additional_context,
			"source_ai_session_id": str(item.source_ai_session_id) if item.source_ai_session_id else None,
			"source_conversation_transcript": item.source_conversation_transcript,
			"created_at": created_at,
		}
	return {
		"triage_summary_id": str(getattr(item, "triage_summary_id")),
		"care_episode_id": str(item.care_episode_id),
		"version": item.version,
		"concern": item.concern,
		"relevant_context": item.relevant_context,
		"safety_signals": item.safety_signals,
		"limitations": item.limitations,
		"recommendation": item.recommendation,
		"missing_information": item.missing_information,
		"unresolved_questions": item.unresolved_questions,
		"clinician_review_needed": item.clinician_review_needed,
		"source_ai_session_id": str(item.source_ai_session_id) if item.source_ai_session_id else None,
		"source_conversation_transcript": item.source_conversation_transcript,
		"created_at": created_at,
	}


def latest_saved_triage_summary(db: Session, episode_id: UUID) -> CareEpisodeTriageSummary | None:
	return (
		db.execute(
			select(CareEpisodeTriageSummary)
			.where(CareEpisodeTriageSummary.care_episode_id == episode_id)
			.order_by(CareEpisodeTriageSummary.version.desc(), CareEpisodeTriageSummary.created_at.desc())
		)
		.scalars()
		.first()
	)


def _enqueue_triage_maintenance(
	db: Session,
	*,
	patient_id: UUID,
	episode_id: UUID,
	request: TriageSummaryRequest,
	identity: str,
	request_hash: str,
	origin_event: str,
	origin_message_id: UUID | None,
	candidate: CareEpisodeTriageSummary,
	artifact_id: UUID,
	artifact_type: str,
) -> Any:
	session_id = candidate.source_ai_session_id or request.ai_session_id
	return enqueue_memory_maintenance(
		db,
		patient_id=patient_id,
		care_episode_id=episode_id,
		session_id=session_id,
		trigger="triage_summary_generation",
		trigger_identity=identity,
		request_hash=request_hash,
		exclude_message_ids=_stable_excluded_message_ids(
			db,
			session_id=session_id,
			origin_message_id=origin_message_id,
			originating_event_id=origin_event,
			artifact_id=artifact_id,
		),
		originating_artifact_id=artifact_id,
		originating_artifact_type=artifact_type,
		originating_event_id=origin_event,
		originating_message_id=origin_message_id,
		commit=False,
	)


def generate_triage_summary_draft(
	db: Session,
	patient_id: UUID,
	episode_id: UUID,
	request: TriageSummaryRequest,
	*,
	idempotency_key: str | None = None,
) -> TriageSummaryPublicationResult:
	episode = _get_patient_episode(db, patient_id, episode_id)
	_bind_triage_session(db, patient_id, episode_id, request)
	identity, request_hash, origin_event, origin_message_id = _triage_identity(
		db, patient_id, episode_id, request, idempotency_key
	)
	try:
		existing = find_existing_memory_maintenance(db, identity, request_hash, for_update=True)
	except MemoryMaintenanceConflict as exc:
		raise TriageSummaryIdempotencyConflict(str(exc)) from exc
	if existing is not None:
		replayed = _replay_existing_publication(
			db,
			existing,
			is_draft=True,
			expected_patient_id=patient_id,
			expected_episode_id=episode_id,
		)
		if replayed is not None:
			return replayed
	candidate = _build_candidate(db, episode, request, version=0)
	draft = _draft_from_candidate(candidate)
	try:
		db.add(draft)
		db.flush()
		run = _enqueue_triage_maintenance(
			db,
			patient_id=patient_id,
			episode_id=episode_id,
			request=request,
			identity=identity,
			request_hash=request_hash,
			origin_event=origin_event,
			origin_message_id=origin_message_id,
			candidate=candidate,
			artifact_id=draft.triage_summary_draft_id,
			artifact_type="draft",
		)
		db.commit()
	except Exception:
		db.rollback()
		raise
	db.refresh(draft)
	return TriageSummaryPublicationResult(
		artifact=triage_summary_artifact(draft, is_draft=True),
		draft=draft,
		maintenance_run_id=run.maintenance_id,
	)


def publish_triage_summary_directly(
	db: Session,
	patient_id: UUID,
	episode_id: UUID,
	request: TriageSummaryRequest,
	*,
	idempotency_key: str | None = None,
) -> TriageSummaryPublicationResult:
	episode = _get_patient_episode(db, patient_id, episode_id)
	_bind_triage_session(db, patient_id, episode_id, request)
	identity, request_hash, origin_event, origin_message_id = _triage_identity(
		db, patient_id, episode_id, request, idempotency_key
	)
	try:
		existing = find_existing_memory_maintenance(db, identity, request_hash, for_update=True)
	except MemoryMaintenanceConflict as exc:
		raise TriageSummaryIdempotencyConflict(str(exc)) from exc
	if existing is not None:
		replayed = _replay_existing_publication(
			db,
			existing,
			is_draft=False,
			expected_patient_id=patient_id,
			expected_episode_id=episode_id,
		)
		if replayed is not None:
			return replayed
	summary = _build_candidate(db, episode, request, version=0)
	try:
		summary = _save_summary_under_publication_lock(db, patient_id, episode_id, summary, commit=False)
		run = _enqueue_triage_maintenance(
			db,
			patient_id=patient_id,
			episode_id=episode_id,
			request=request,
			identity=identity,
			request_hash=request_hash,
			origin_event=origin_event,
			origin_message_id=origin_message_id,
			candidate=summary,
			artifact_id=summary.triage_summary_id,
			artifact_type="summary",
		)
		db.commit()
	except Exception:
		db.rollback()
		raise
	create_recommendation_after_publication(db, episode_id=episode_id, summary=summary)
	return TriageSummaryPublicationResult(
		artifact=triage_summary_artifact(summary),
		summary=summary,
		maintenance_run_id=run.maintenance_id,
	)


def select_triage_summary_draft(
	db: Session,
	patient_id: UUID,
	episode_id: UUID,
	summary_id: UUID,
) -> TriageSummaryPublicationResult:
	_get_patient_episode(db, patient_id, episode_id)
	_get_patient_triage_summary_draft(db, patient_id, episode_id, summary_id)
	summary = _save_draft_selection_under_publication_lock(db, patient_id, episode_id, summary_id)
	create_recommendation_after_publication(db, episode_id=episode_id, summary=summary)
	return TriageSummaryPublicationResult(artifact=triage_summary_artifact(summary), summary=summary)


def delete_triage_summary_draft(db: Session, patient_id: UUID, episode_id: UUID, summary_id: UUID) -> None:
	episode = _get_patient_episode(db, patient_id, episode_id)
	draft = _get_patient_triage_summary_draft(db, patient_id, episode.care_episode_id, summary_id)
	try:
		db.delete(draft)
		db.commit()
	except Exception:
		db.rollback()
		raise


def _save_summary_under_publication_lock(
	db: Session,
	patient_id: UUID,
	episode_id: UUID,
	summary: CareEpisodeTriageSummary,
	*,
	commit: bool = True,
) -> CareEpisodeTriageSummary:
	try:
		episode = _get_locked_patient_episode_for_publication(db, patient_id, episode_id)
		summary.version = _next_summary_version(db, episode.care_episode_id)
		db.add(summary)
		db.flush()
		db.refresh(summary)
		mark_latest_summary_snapshot(episode, summary)
		if commit:
			db.commit()
	except Exception:
		db.rollback()
		raise
	db.refresh(summary)
	return summary


def _save_draft_selection_under_publication_lock(
	db: Session,
	patient_id: UUID,
	episode_id: UUID,
	summary_id: UUID,
) -> CareEpisodeTriageSummary:
	try:
		episode = _get_locked_patient_episode_for_publication(db, patient_id, episode_id)
		draft = _get_patient_triage_summary_draft(db, patient_id, episode.care_episode_id, summary_id)
		summary = _summary_from_draft(draft, _next_summary_version(db, episode.care_episode_id))
		db.add(summary)
		db.flush()
		db.refresh(summary)
		mark_latest_summary_snapshot(episode, summary)
		db.delete(draft)
		db.commit()
	except Exception:
		db.rollback()
		raise
	db.refresh(summary)
	return summary

def create_recommendation_after_publication(
	db: Session,
	*,
	episode_id: UUID,
	summary: CareEpisodeTriageSummary,
	commit: bool = True,
) -> None:
	episode = db.get(CareEpisode, episode_id)
	if episode is None:
		raise EpisodeNotFound("Care Episode not found")
	try:
		create_rehab_recommendation_for_summary(db, episode=episode, summary=summary)
		if commit:
			db.commit()
	except Exception:
		db.rollback()
		raise


def _get_patient_episode(db: Session, patient_id: UUID, episode_id: UUID) -> CareEpisode:
	episode = (
		db.execute(
			select(CareEpisode)
			.where(CareEpisode.care_episode_id == episode_id)
			.with_for_update()
		)
		.scalars()
		.one_or_none()
	)
	if episode is None or episode.patient_id != patient_id or str(episode.status or "active") != "active":
		raise EpisodeNotFound("Care Episode not found")
	return episode


def _get_locked_patient_episode_for_publication(db: Session, patient_id: UUID, episode_id: UUID) -> CareEpisode:
	episode = (
		db.execute(
			select(CareEpisode)
			.where(CareEpisode.care_episode_id == episode_id)
			.with_for_update()
		)
		.scalars()
		.one_or_none()
	)
	if episode is None or episode.patient_id != patient_id:
		raise EpisodeNotFound("Care Episode not found")
	return episode


def _get_patient_triage_summary_draft(
	db: Session,
	patient_id: UUID,
	episode_id: UUID,
	summary_id: UUID,
) -> CareEpisodeTriageSummaryDraft:
	draft = db.get(CareEpisodeTriageSummaryDraft, summary_id)
	if draft is None or draft.care_episode_id != episode_id or draft.patient_id != patient_id:
		raise TriageSummaryNotFound("Triage Summary draft not found")
	return draft


def _build_candidate(
	db: Session,
	episode: CareEpisode,
	request: TriageSummaryRequest,
	*,
	version: int,
) -> CareEpisodeTriageSummary:
	try:
		return build_triage_summary(
			episode,
			request.additional_context.strip(),
			version,
			db=db,
			ai_session_id=request.ai_session_id,
		)
	except TriageSummarySessionNotFound as exc:
		raise TriageSummaryNotFound("AI triage session not found") from exc
	except TriageSummaryGenerationFailed as exc:
		raise TriageSummaryGenerationUnavailable(
			"Triage Summary generation unavailable"
		) from exc
	except TriageSummaryPublicationError:
		raise


def _next_summary_version(db: Session, episode_id: UUID) -> int:
	latest_version = db.execute(
		select(func.max(CareEpisodeTriageSummary.version)).where(CareEpisodeTriageSummary.care_episode_id == episode_id)
	).scalar_one()
	return int(latest_version or 0) + 1


def _draft_from_candidate(candidate: CareEpisodeTriageSummary) -> CareEpisodeTriageSummaryDraft:
	return CareEpisodeTriageSummaryDraft(
		care_episode_id=candidate.care_episode_id,
		patient_id=candidate.patient_id,
		concern=candidate.concern,
		relevant_context=candidate.relevant_context,
		safety_signals=candidate.safety_signals,
		limitations=candidate.limitations,
		recommendation=candidate.recommendation,
		missing_information=candidate.missing_information,
		unresolved_questions=candidate.unresolved_questions,
		clinician_review_needed=candidate.clinician_review_needed,
		additional_context=candidate.additional_context,
		source_ai_session_id=candidate.source_ai_session_id,
		source_conversation_transcript=candidate.source_conversation_transcript,
	)


def _summary_from_draft(draft: CareEpisodeTriageSummaryDraft, version: int) -> CareEpisodeTriageSummary:
	return CareEpisodeTriageSummary(
		care_episode_id=draft.care_episode_id,
		patient_id=draft.patient_id,
		version=version,
		concern=draft.concern,
		relevant_context=draft.relevant_context,
		safety_signals=draft.safety_signals,
		limitations=draft.limitations,
		recommendation=draft.recommendation,
		missing_information=draft.missing_information,
		unresolved_questions=draft.unresolved_questions,
		clinician_review_needed=draft.clinician_review_needed,
		additional_context=draft.additional_context,
		source_ai_session_id=draft.source_ai_session_id,
		source_conversation_transcript=draft.source_conversation_transcript,
	)


def mark_latest_summary_snapshot(episode: CareEpisode, summary: CareEpisodeTriageSummary) -> None:
	episode.latest_triage_summary = triage_summary_artifact(summary)
	episode.safety_gate_status = "triage_complete"
