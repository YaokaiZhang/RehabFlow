from __future__ import annotations

from datetime import datetime
from uuid import UUID

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CareConversationMessageCreate(BaseModel):
	content: str = Field(min_length=1, max_length=4000)


class CareConversationMessageResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	message_id: UUID
	care_episode_id: UUID
	relationship_id: UUID
	sender_id: UUID
	sender_role: str
	content: str
	created_at: datetime


class CareConversationListResponse(BaseModel):
	messages: list[CareConversationMessageResponse]


class AICareSummaryResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	care_summary_id: UUID
	care_episode_id: UUID
	relationship_id: UUID
	conversation_digest: str
	plan_digest: str
	unresolved_questions: list[str]
	raw_score_trend: dict | None
	created_at: datetime


class AIDailyRehabRecommendationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    recommendation_id: UUID
    care_episode_id: UUID
    patient_id: UUID
    source_triage_summary_id: UUID | None
    source_triage_summary_version: int
    status: str
    empty_reason: str
    query_text: str
    items: list[dict[str, Any]]
    active: bool
    created_at: datetime
    updated_at: datetime


class AIDailyRehabListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    list_id: UUID
    care_episode_id: UUID
    patient_id: UUID
    source_recommendation_id: UUID | None
    items: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class AIDailyRehabListUpdate(BaseModel):
    exercise_ids: list[str] = Field(default_factory=list, max_length=24)


class RehabSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_notes: str = Field(default="", max_length=4000)
    recommended_exercises: list[str] | None = None


class RehabSessionUpdate(BaseModel):
	completed_items: list[str] = Field(default_factory=list)
	patient_notes: str | None = Field(default=None, max_length=4000)


class RehabSessionResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	session_id: UUID
	care_episode_id: UUID
	patient_id: UUID
	recommended_exercises: list
	checklist: list[dict]
	patient_notes: str
	session_summary: str
	completion_status: str
	created_at: datetime
	updated_at: datetime


class RehabSessionListResponse(BaseModel):
	sessions: list[RehabSessionResponse]
