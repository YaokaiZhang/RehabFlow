from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import get_current_principal
from app.db.models import AICareSummary, AISession, CareConnectionRequest, CareEpisode, CareEpisodeTriageSummary, CareEpisodeTriageSummaryDraft, CareRelationship, Doctor, EpisodeRehabSession, Patient
from app.db.session import get_db
from app.services.care_relationship_access import (
	EpisodeAccessDenied as CareAccessEpisodeAccessDenied,
	EpisodeNotFound as CareAccessEpisodeNotFound,
	PrincipalRequired,
	RelationshipAccessDenied,
	RelationshipNotActive,
	principal_id,
	require_doctor_active_relationship,
	require_patient_episode,
)
from app.services.memory_documents import build_memory_context_pack
from app.services.memory_agent import find_existing_memory_maintenance, MemoryMaintenanceConflict
from app.services.professional_care_workflow import doctor_display_name
from app.services.triage_summary_agent import TriageSummarySessionNotFound, derive_episode_fields_from_triage_context, load_triage_conversation_context
from app.services.triage_summary_publication import (
	EpisodeNotFound,
	TriageSummaryGenerationUnavailable,
	TriageSummaryIdempotencyConflict,
	TriageSummaryNotFound,
	TriageSummaryPublicationError,
	_replay_existing_publication,
	_triage_identity,
	delete_triage_summary_draft as delete_triage_summary_draft_service,
	generate_triage_summary_draft,
	latest_saved_triage_summary,
	publish_triage_summary_directly,
	select_triage_summary_draft as select_triage_summary_draft_service,
)
from app.schemas.care_episodes import (
	CareEpisodeCreateRequest,
	CareEpisodeHistoryResponse,
	CareEpisodeListResponse,
	CareEpisodeResponse,
	TriageSummaryDraftListResponse,
	TriageSummaryDraftResponse,
	TriageSummaryEpisodeDraftResponse,
	TriageSummaryListResponse,
	TriageSummaryRequest,
	TriageSummaryResponse,
	CareEpisodeWorkspaceSummaryResponse,
	ProfessionalCareWorkspaceSummary,
)
from app.schemas.memory_documents import MemoryDocumentResponse

router = APIRouter(prefix="/care-episodes", tags=["care-episodes"])


def _current_patient_id(principal: dict, db: Session) -> UUID:
	if principal.get("role") != "patient":
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only patients can use Care Episodes")
	try:
		patient_id = UUID(principal_id(principal))
	except PrincipalRequired as exc:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing patient identity") from exc
	if db.get(Patient, patient_id) is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
	return patient_id


def _patient_episode(care_episode_id: UUID, patient_id: UUID, db: Session) -> CareEpisode:
	try:
		return require_patient_episode(db, patient_id, care_episode_id)
	except (CareAccessEpisodeNotFound, CareAccessEpisodeAccessDenied) as exc:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care Episode not found") from exc


def _current_doctor_id(principal: dict, db: Session) -> UUID:
	if principal.get("role") != "doctor":
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only doctors can use this endpoint")
	try:
		doctor_id = UUID(principal_id(principal))
	except PrincipalRequired as exc:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing doctor identity") from exc
	if db.get(Doctor, doctor_id) is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")
	return doctor_id


def _doctor_monitored_episode(care_episode_id: UUID, doctor_id: UUID, db: Session) -> CareEpisode:
	try:
		_, episode = require_doctor_active_relationship(db, doctor_id, care_episode_id)
		return episode
	except (CareAccessEpisodeNotFound, RelationshipAccessDenied, RelationshipNotActive) as exc:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care Episode not found") from exc




def _rehab_session_dict(session: EpisodeRehabSession) -> dict:
	return jsonable_encoder({
		"session_id": session.session_id,
		"care_episode_id": session.care_episode_id,
		"patient_id": session.patient_id,
		"recommended_exercises": session.recommended_exercises,
		"checklist": session.checklist,
		"patient_notes": session.patient_notes,
		"session_summary": session.session_summary,
		"completion_status": session.completion_status,
		"created_at": session.created_at,
		"updated_at": session.updated_at,
	})


def _ai_care_summary_dict(summary: AICareSummary) -> dict:
	return jsonable_encoder({
		"care_summary_id": summary.care_summary_id,
		"care_episode_id": summary.care_episode_id,
		"relationship_id": summary.relationship_id,
		"conversation_digest": summary.conversation_digest,
		"plan_digest": summary.plan_digest,
		"unresolved_questions": summary.unresolved_questions,
		"raw_score_trend": summary.raw_score_trend,
		"created_at": summary.created_at,
	})


def _memory_document_dict(document) -> dict | None:
	if document is None:
		return None
	return MemoryDocumentResponse.model_validate(document).model_dump(mode="json")

def _triage_publication_http_error(exc: TriageSummaryPublicationError) -> HTTPException:
	if isinstance(exc, EpisodeNotFound):
		return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care Episode not found")
	if isinstance(exc, TriageSummaryNotFound):
		return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Triage Summary draft not found")
	if isinstance(exc, TriageSummaryGenerationUnavailable):
		return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Triage Summary generation failed")
	if isinstance(exc, TriageSummaryIdempotencyConflict):
		return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
	raise exc

@router.post("", response_model=CareEpisodeResponse, status_code=status.HTTP_201_CREATED)
def create_manual_care_episode(
	payload: CareEpisodeCreateRequest,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareEpisode:
	patient_id = _current_patient_id(principal, db)
	episode = CareEpisode(
		patient_id=patient_id,
		issue_title=payload.issue_title,
		body_area=payload.body_area,
		goal=payload.goal,
		short_description=payload.short_description,
		symptom_started_on=payload.symptom_started_on,
		origin="manual",
		safety_gate_status="needs_triage",
		status="active",
	)
	db.add(episode)
	db.commit()
	db.refresh(episode)
	return episode


@router.post("/triage-summary-request", response_model=TriageSummaryEpisodeDraftResponse, status_code=status.HTTP_201_CREATED)
def create_episode_from_triage_summary_request(
	payload: TriageSummaryRequest,
	idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> dict[str, CareEpisode | CareEpisodeTriageSummaryDraft]:
	patient_id = _current_patient_id(principal, db)
	source_session = db.get(AISession, payload.ai_session_id) if payload.ai_session_id is not None else None
	if payload.ai_session_id is not None and (
		source_session is None or source_session.patient_id != patient_id
	):
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI triage session not found")
	try:
		identity, request_hash, _origin_event, _origin_message_id = _triage_identity(
			db, patient_id, None, payload, idempotency_key
		)
		existing = find_existing_memory_maintenance(db, identity, request_hash, for_update=True)
	except TriageSummaryPublicationError as exc:
		raise _triage_publication_http_error(exc) from exc
	except MemoryMaintenanceConflict as exc:
		raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
	if existing is not None:
		replayed = _replay_existing_publication(
			db,
			existing,
			is_draft=True,
			expected_patient_id=patient_id,
		)
		if replayed is not None and replayed.draft is not None:
			existing_episode = db.get(CareEpisode, existing.care_episode_id)
			if existing_episode is not None and existing_episode.patient_id == patient_id and str(existing_episode.status or "active") == "active":
				return {"episode": existing_episode, "draft": replayed.draft}
	if source_session is not None and source_session.care_episode_id is not None:
		raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="AI triage session is already bound to a Care Episode")
	try:
		conversation_context = load_triage_conversation_context(db, patient_id, payload.ai_session_id)
	except TriageSummarySessionNotFound as exc:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI triage session not found") from exc
	episode_fields = derive_episode_fields_from_triage_context(payload.additional_context.strip(), conversation_context)
	episode = CareEpisode(
		patient_id=patient_id,
		issue_title=episode_fields["issue_title"],
		body_area=episode_fields["body_area"],
		goal=episode_fields["goal"],
		short_description=episode_fields["short_description"],
		origin="triage",
		safety_gate_status="needs_triage",
		status="active",
	)
	db.add(episode)
	db.flush()
	if source_session is not None:
		source_session.care_episode_id = episode.care_episode_id
	try:
		draft_result = generate_triage_summary_draft(db, patient_id, episode.care_episode_id, payload, idempotency_key=idempotency_key)
	except TriageSummaryPublicationError as exc:
		raise _triage_publication_http_error(exc) from exc
	db.refresh(episode)
	return {"episode": episode, "draft": draft_result.draft}


@router.get("", response_model=CareEpisodeListResponse)
def list_care_episodes(
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareEpisodeListResponse:
	patient_id = _current_patient_id(principal, db)
	episodes = db.execute(
		select(CareEpisode)
		.where(CareEpisode.patient_id == patient_id)
		.order_by(CareEpisode.updated_at.desc(), CareEpisode.created_at.desc())
	).scalars().all()
	return CareEpisodeListResponse(episodes=list(episodes))


@router.get("/{care_episode_id}/workspace-summary", response_model=CareEpisodeWorkspaceSummaryResponse)
def get_care_episode_workspace_summary(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareEpisodeWorkspaceSummaryResponse:
	role = principal.get("role")
	if role == "patient":
		patient_id = _current_patient_id(principal, db)
		patient = db.get(Patient, patient_id)
		episode = _patient_episode(care_episode_id, patient_id, db)
	elif role == "doctor":
		doctor_id = _current_doctor_id(principal, db)
		episode = _doctor_monitored_episode(care_episode_id, doctor_id, db)
		patient = db.get(Patient, episode.patient_id)
	else:
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only patients or doctors can use this endpoint")
	latest_triage = latest_saved_triage_summary(db, episode.care_episode_id)
	latest_rehab = db.execute(
		select(EpisodeRehabSession)
		.where(EpisodeRehabSession.care_episode_id == episode.care_episode_id)
		.order_by(EpisodeRehabSession.updated_at.desc(), EpisodeRehabSession.created_at.desc())
	).scalars().first()
	latest_care_summary = db.execute(
		select(AICareSummary)
		.where(AICareSummary.care_episode_id == episode.care_episode_id)
		.order_by(AICareSummary.created_at.desc())
	).scalars().first()
	unsaved_triage_summaries = []
	if role == "patient":
		unsaved_triage_summaries = db.execute(
			select(CareEpisodeTriageSummaryDraft)
			.where(CareEpisodeTriageSummaryDraft.care_episode_id == episode.care_episode_id)
			.order_by(CareEpisodeTriageSummaryDraft.created_at.desc())
		).scalars().all()
	relationship = db.execute(
		select(CareRelationship).where(
			CareRelationship.care_episode_id == episode.care_episode_id,
			CareRelationship.status == "active",
		)
	).scalars().first()
	request_rows = db.execute(
		select(CareConnectionRequest.status, func.count())
		.where(CareConnectionRequest.care_episode_id == episode.care_episode_id)
		.group_by(CareConnectionRequest.status)
	).all()
	selected_doctor = db.get(Doctor, episode.selected_doctor_id) if episode.selected_doctor_id else None

	latest_rehab_data = _rehab_session_dict(latest_rehab) if latest_rehab else None

	latest_care_summary_data = _ai_care_summary_dict(latest_care_summary) if latest_care_summary else None

	return CareEpisodeWorkspaceSummaryResponse(
		episode=episode,
		latest_triage_summary=latest_triage,
		unsaved_triage_summaries=list(unsaved_triage_summaries),
		latest_rehab_session=latest_rehab_data,
		latest_ai_care_summary=latest_care_summary_data,
		professional_care=ProfessionalCareWorkspaceSummary(
			subscription_tier=patient.subscription_tier if patient else "self_serve",
			selected_doctor_id=episode.selected_doctor_id,
			selected_doctor_name=doctor_display_name(selected_doctor) if selected_doctor else None,
			has_active_relationship=relationship is not None,
			relationship_id=relationship.relationship_id if relationship else None,
			request_counts={str(status): int(count) for status, count in request_rows},
			show_live_monitor=relationship is not None,
		),
	)


@router.get("/{care_episode_id}/history", response_model=CareEpisodeHistoryResponse)
def get_care_episode_history(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareEpisodeHistoryResponse:
	patient_id = _current_patient_id(principal, db)
	episode = _patient_episode(care_episode_id, patient_id, db)
	triage_summaries = db.execute(
		select(CareEpisodeTriageSummary)
		.where(CareEpisodeTriageSummary.care_episode_id == episode.care_episode_id)
		.order_by(CareEpisodeTriageSummary.version.desc(), CareEpisodeTriageSummary.created_at.desc())
	).scalars().all()
	triage_summary_drafts = db.execute(
		select(CareEpisodeTriageSummaryDraft)
		.where(CareEpisodeTriageSummaryDraft.care_episode_id == episode.care_episode_id)
		.order_by(CareEpisodeTriageSummaryDraft.created_at.desc())
	).scalars().all()
	patient_memory, episode_memory, compiled_memory_context = build_memory_context_pack(db, episode)
	rehab_sessions = db.execute(
		select(EpisodeRehabSession)
		.where(EpisodeRehabSession.care_episode_id == episode.care_episode_id)
		.order_by(EpisodeRehabSession.updated_at.desc(), EpisodeRehabSession.created_at.desc())
	).scalars().all()
	ai_care_summaries = db.execute(
		select(AICareSummary)
		.where(AICareSummary.care_episode_id == episode.care_episode_id)
		.order_by(AICareSummary.created_at.desc())
	).scalars().all()
	return CareEpisodeHistoryResponse(
		episode=episode,
		triage_summaries=list(triage_summaries),
		triage_summary_drafts=list(triage_summary_drafts),
		patient_memory=_memory_document_dict(patient_memory),
		episode_memory=_memory_document_dict(episode_memory),
		compiled_memory_context=compiled_memory_context,
		rehab_sessions=[_rehab_session_dict(session) for session in rehab_sessions],
		ai_care_summaries=[_ai_care_summary_dict(summary) for summary in ai_care_summaries],
	)


@router.get("/{care_episode_id}", response_model=CareEpisodeResponse)
def get_care_episode(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareEpisode:
	if principal.get("role") == "patient":
		patient_id = _current_patient_id(principal, db)
		return _patient_episode(care_episode_id, patient_id, db)
	if principal.get("role") == "doctor":
		doctor_id = _current_doctor_id(principal, db)
		return _doctor_monitored_episode(care_episode_id, doctor_id, db)
	raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only patients or doctors can use this endpoint")


@router.delete("/{care_episode_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_care_episode(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> Response:
	patient_id = _current_patient_id(principal, db)
	episode = _patient_episode(care_episode_id, patient_id, db)
	db.delete(episode)
	db.commit()
	return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{care_episode_id}/triage-summary", response_model=TriageSummaryResponse, status_code=status.HTTP_201_CREATED)
def request_triage_summary(
	care_episode_id: UUID,
	payload: TriageSummaryRequest,
	idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareEpisodeTriageSummary:
	patient_id = _current_patient_id(principal, db)
	try:
		result = publish_triage_summary_directly(db, patient_id, care_episode_id, payload, idempotency_key=idempotency_key)
	except TriageSummaryPublicationError as exc:
		raise _triage_publication_http_error(exc) from exc
	return result.summary


@router.post("/{care_episode_id}/triage-summary-drafts", response_model=TriageSummaryDraftResponse, status_code=status.HTTP_201_CREATED)
def create_triage_summary_draft(
	care_episode_id: UUID,
	payload: TriageSummaryRequest,
	idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareEpisodeTriageSummaryDraft:
	patient_id = _current_patient_id(principal, db)
	try:
		result = generate_triage_summary_draft(db, patient_id, care_episode_id, payload, idempotency_key=idempotency_key)
	except TriageSummaryPublicationError as exc:
		raise _triage_publication_http_error(exc) from exc
	return result.draft


@router.get("/{care_episode_id}/triage-summary-drafts", response_model=TriageSummaryDraftListResponse)
def list_triage_summary_drafts(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> TriageSummaryDraftListResponse:
	patient_id = _current_patient_id(principal, db)
	episode = _patient_episode(care_episode_id, patient_id, db)
	drafts = db.execute(
		select(CareEpisodeTriageSummaryDraft)
		.where(CareEpisodeTriageSummaryDraft.care_episode_id == episode.care_episode_id)
		.order_by(CareEpisodeTriageSummaryDraft.created_at.desc())
	).scalars().all()
	return TriageSummaryDraftListResponse(drafts=list(drafts))


@router.post(
	"/{care_episode_id}/triage-summary-drafts/{triage_summary_draft_id}/select",
	response_model=TriageSummaryResponse,
	status_code=status.HTTP_201_CREATED,
)
def select_triage_summary_draft(
	care_episode_id: UUID,
	triage_summary_draft_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareEpisodeTriageSummary:
	patient_id = _current_patient_id(principal, db)
	try:
		result = select_triage_summary_draft_service(db, patient_id, care_episode_id, triage_summary_draft_id)
	except TriageSummaryPublicationError as exc:
		raise _triage_publication_http_error(exc) from exc
	return result.summary


@router.delete("/{care_episode_id}/triage-summary-drafts/{triage_summary_draft_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_triage_summary_draft(
	care_episode_id: UUID,
	triage_summary_draft_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> Response:
	patient_id = _current_patient_id(principal, db)
	try:
		delete_triage_summary_draft_service(db, patient_id, care_episode_id, triage_summary_draft_id)
	except TriageSummaryPublicationError as exc:
		raise _triage_publication_http_error(exc) from exc
	return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{care_episode_id}/triage-summaries", response_model=TriageSummaryListResponse)
def list_triage_summaries(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> TriageSummaryListResponse:
	patient_id = _current_patient_id(principal, db)
	episode = _patient_episode(care_episode_id, patient_id, db)
	summaries = db.execute(
		select(CareEpisodeTriageSummary)
		.where(CareEpisodeTriageSummary.care_episode_id == episode.care_episode_id)
		.order_by(CareEpisodeTriageSummary.version.desc())
	).scalars().all()
	return TriageSummaryListResponse(summaries=list(summaries))
