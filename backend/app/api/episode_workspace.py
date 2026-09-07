from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_principal
from app.db.models import AICareSummary, CareConversationMessage, CareEpisode, EpisodeRehabSession, Patient
from app.db.session import get_db
from app.schemas.episode_workspace import (
	AICareSummaryResponse,
	AIDailyRehabListResponse,
	AIDailyRehabListUpdate,
	AIDailyRehabRecommendationResponse,
	CareConversationListResponse,
	CareConversationMessageCreate,
	CareConversationMessageResponse,
	RehabSessionCreate,
	RehabSessionListResponse,
	RehabSessionResponse,
	RehabSessionUpdate,
)
from app.services.care_relationship_access import (
	EpisodeAccessDenied as CareAccessEpisodeAccessDenied,
	EpisodeNotFound as CareAccessEpisodeNotFound,
	PrincipalRequired,
	RelationshipAccessDenied,
	RelationshipNotActive,
	principal_id,
	require_patient_episode,
	require_relationship_access,
)
from app.services.ai_daily_rehab_workspace import (
    AiDailyRehabAccessDenied,
    AiDailyRehabListNotFound,
    AiDailyRehabRecommendationNotFound,
    AiDailyRehabSafetyGateBlocked,
    AiDailyRehabSessionNotFound,
    get_ai_daily_rehab_recommendation,
    list_ai_daily_rehab_sessions,
    load_ai_daily_rehab_workspace,
    start_ai_daily_rehab_session,
    summarize_ai_daily_rehab_session,
    update_ai_daily_rehab_list,
    update_ai_daily_rehab_session_checklist,
)
from app.services.rehab_recommendations import InvalidRehabExerciseIds

router = APIRouter(tags=["episode-workspace"])


def _principal_uuid(principal: dict) -> UUID:
	try:
		return UUID(principal_id(principal))
	except PrincipalRequired as exc:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing user identity") from exc


def _relationship_access_http_error(exc: Exception) -> HTTPException:
	if isinstance(exc, RelationshipNotActive):
		return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Active Care Relationship required")
	if isinstance(exc, CareAccessEpisodeNotFound):
		return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care Episode not found")
	if isinstance(exc, RelationshipAccessDenied):
		return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care workspace not found")
	if isinstance(exc, PrincipalRequired):
		return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing user identity")
	raise exc


def _relationship_access(care_episode_id: UUID, principal: dict, db: Session) -> tuple[CareEpisode, object, UUID, str]:
	user_id = _principal_uuid(principal)
	role = str(principal.get("role"))
	if role not in {"patient", "doctor"}:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care workspace not found")
	try:
		relationship, episode = require_relationship_access(db, user_id, episode_id=care_episode_id)
	except (PrincipalRequired, CareAccessEpisodeNotFound, RelationshipAccessDenied, RelationshipNotActive) as exc:
		raise _relationship_access_http_error(exc) from exc
	return episode, relationship, user_id, role


def _patient_episode_access(care_episode_id: UUID, principal: dict, db: Session) -> tuple[CareEpisode, Patient]:
	if principal.get("role") != "patient":
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only patients can use AI Daily Rehab")
	patient_id = _principal_uuid(principal)
	patient = db.get(Patient, patient_id)
	if patient is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
	try:
		episode = require_patient_episode(db, patient_id, care_episode_id)
	except (CareAccessEpisodeNotFound, CareAccessEpisodeAccessDenied) as exc:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Care Episode not found") from exc
	return episode, patient


def _require_rehab_safety_gate(episode: CareEpisode) -> None:
	if episode.safety_gate_status not in {"triage_complete", "clinician_reviewed"}:
		raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="AI Daily Rehab requires AI triage or clinician-reviewed context")


@router.post("/care-episodes/{care_episode_id}/care-conversation/messages", response_model=CareConversationMessageResponse, status_code=status.HTTP_201_CREATED)
def create_care_conversation_message(
	care_episode_id: UUID,
	payload: CareConversationMessageCreate,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareConversationMessage:
	_, relationship, sender_id, sender_role = _relationship_access(care_episode_id, principal, db)
	message = CareConversationMessage(
		care_episode_id=care_episode_id,
		relationship_id=relationship.relationship_id,
		sender_id=sender_id,
		sender_role=sender_role,
		content=payload.content,
	)
	db.add(message)
	db.commit()
	db.refresh(message)
	return message


@router.get("/care-episodes/{care_episode_id}/care-conversation/messages", response_model=CareConversationListResponse)
def list_care_conversation_messages(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> CareConversationListResponse:
	_, relationship, _, _ = _relationship_access(care_episode_id, principal, db)
	messages = db.execute(
		select(CareConversationMessage)
		.where(CareConversationMessage.relationship_id == relationship.relationship_id)
		.order_by(CareConversationMessage.created_at.asc())
	).scalars().all()
	return CareConversationListResponse(messages=list(messages))


@router.post("/care-episodes/{care_episode_id}/ai-care-summary", response_model=AICareSummaryResponse)
def generate_ai_care_summary(
	care_episode_id: UUID,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> AICareSummary:
	_, relationship, _, _ = _relationship_access(care_episode_id, principal, db)
	messages = db.execute(
		select(CareConversationMessage)
		.where(CareConversationMessage.relationship_id == relationship.relationship_id)
		.order_by(CareConversationMessage.created_at.asc())
	).scalars().all()
	patient_lines = [message.content for message in messages if message.sender_role == "patient"]
	doctor_lines = [message.content for message in messages if message.sender_role == "doctor"]
	conversation_digest = " ".join(patient_lines + doctor_lines) or "No Care Conversation messages yet."
	plan_digest = " ".join(doctor_lines) or "No doctor care plan guidance has been recorded yet."
	summary = AICareSummary(
		care_episode_id=care_episode_id,
		relationship_id=relationship.relationship_id,
		conversation_digest=conversation_digest,
		plan_digest=plan_digest,
		unresolved_questions=[] if doctor_lines else ["Doctor guidance has not been recorded yet."],
		raw_score_trend=None,
	)
	db.add(summary)
	db.commit()
	db.refresh(summary)
	return summary



def _ai_daily_rehab_patient_id(principal: dict, *, detail: str = "Only patients can use AI Daily Rehab") -> UUID:
    if principal.get("role") != "patient":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return _principal_uuid(principal)


def _ai_daily_rehab_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AiDailyRehabSafetyGateBlocked):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail)
    if isinstance(exc, AiDailyRehabRecommendationNotFound):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail)
    if isinstance(exc, AiDailyRehabListNotFound):
        status_code = status.HTTP_409_CONFLICT if exc.detail == "AI Daily Rehab List is empty" else status.HTTP_404_NOT_FOUND
        return HTTPException(status_code=status_code, detail=exc.detail)
    if isinstance(exc, AiDailyRehabSessionNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.detail)
    if isinstance(exc, AiDailyRehabAccessDenied):
        detail = "Patient not found" if exc.detail == "Patient not found" else "Care Episode not found"
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    raise exc


@router.get(
    "/care-episodes/{care_episode_id}/rehab-recommendation",
    response_model=AIDailyRehabRecommendationResponse | None,
)
def get_rehab_recommendation(
    care_episode_id: UUID,
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    patient_id = _ai_daily_rehab_patient_id(principal)
    try:
        return get_ai_daily_rehab_recommendation(db, patient_id, care_episode_id)
    except (AiDailyRehabAccessDenied, AiDailyRehabSafetyGateBlocked) as exc:
        raise _ai_daily_rehab_http_error(exc) from exc


@router.get("/care-episodes/{care_episode_id}/rehab-list", response_model=AIDailyRehabListResponse)
def get_rehab_list(
    care_episode_id: UUID,
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    patient_id = _ai_daily_rehab_patient_id(principal)
    try:
        return load_ai_daily_rehab_workspace(db, patient_id, care_episode_id).rehab_list
    except (
        AiDailyRehabAccessDenied,
        AiDailyRehabSafetyGateBlocked,
        AiDailyRehabRecommendationNotFound,
        AiDailyRehabListNotFound,
    ) as exc:
        raise _ai_daily_rehab_http_error(exc) from exc


@router.put("/care-episodes/{care_episode_id}/rehab-list", response_model=AIDailyRehabListResponse)
def update_rehab_list(
    care_episode_id: UUID,
    payload: AIDailyRehabListUpdate,
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    patient_id = _ai_daily_rehab_patient_id(principal)
    try:
        return update_ai_daily_rehab_list(db, patient_id, care_episode_id, payload)
    except InvalidRehabExerciseIds as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"invalid_exercise_ids": exc.exercise_ids},
        ) from exc
    except (
        AiDailyRehabAccessDenied,
        AiDailyRehabSafetyGateBlocked,
        AiDailyRehabRecommendationNotFound,
        AiDailyRehabListNotFound,
    ) as exc:
        raise _ai_daily_rehab_http_error(exc) from exc


@router.post("/care-episodes/{care_episode_id}/rehab-sessions", response_model=RehabSessionResponse, status_code=status.HTTP_201_CREATED)
def create_rehab_session(
    care_episode_id: UUID,
    payload: RehabSessionCreate,
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> EpisodeRehabSession:
    patient_id = _ai_daily_rehab_patient_id(principal)
    if payload.recommended_exercises is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rehab sessions start from the saved AI Daily Rehab List",
        )
    try:
        workspace = load_ai_daily_rehab_workspace(db, patient_id, care_episode_id)
        return start_ai_daily_rehab_session(
            db,
            patient_id,
            care_episode_id,
            workspace.rehab_list.list_id,
            patient_notes=payload.patient_notes,
        )
    except (
        AiDailyRehabAccessDenied,
        AiDailyRehabSafetyGateBlocked,
        AiDailyRehabRecommendationNotFound,
        AiDailyRehabListNotFound,
    ) as exc:
        raise _ai_daily_rehab_http_error(exc) from exc


@router.get("/care-episodes/{care_episode_id}/rehab-sessions", response_model=RehabSessionListResponse)
def list_rehab_sessions(
    care_episode_id: UUID,
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> RehabSessionListResponse:
    patient_id = _ai_daily_rehab_patient_id(principal)
    try:
        sessions = list_ai_daily_rehab_sessions(db, patient_id, care_episode_id)
    except AiDailyRehabAccessDenied as exc:
        raise _ai_daily_rehab_http_error(exc) from exc
    return RehabSessionListResponse(sessions=sessions)


@router.patch("/rehab-sessions/{session_id}", response_model=RehabSessionResponse)
def update_rehab_session(
    session_id: UUID,
    payload: RehabSessionUpdate,
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> EpisodeRehabSession:
    patient_id = _ai_daily_rehab_patient_id(principal, detail="Only patients can update rehab sessions")
    try:
        return update_ai_daily_rehab_session_checklist(db, patient_id, None, session_id, payload)
    except (AiDailyRehabAccessDenied, AiDailyRehabSessionNotFound) as exc:
        raise _ai_daily_rehab_http_error(exc) from exc


@router.post("/rehab-sessions/{session_id}/summary", response_model=RehabSessionResponse)
def summarize_rehab_session(
    session_id: UUID,
    principal: dict = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> EpisodeRehabSession:
    patient_id = _ai_daily_rehab_patient_id(principal, detail="Only patients can update rehab sessions")
    try:
        return summarize_ai_daily_rehab_session(db, patient_id, None, session_id)
    except (AiDailyRehabAccessDenied, AiDailyRehabSessionNotFound) as exc:
        raise _ai_daily_rehab_http_error(exc) from exc
