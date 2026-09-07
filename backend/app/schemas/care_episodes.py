from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CareEpisodeCreateRequest(BaseModel):
	issue_title: str = Field(min_length=2, max_length=255)
	body_area: str = Field(min_length=2, max_length=128)
	goal: str = Field(min_length=2, max_length=255)
	short_description: str = Field(min_length=5, max_length=4000)
	symptom_started_on: date | None = None


class CareEpisodeResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	care_episode_id: UUID
	patient_id: UUID
	issue_title: str
	body_area: str
	goal: str
	short_description: str
	symptom_started_on: date | None
	origin: Literal["manual", "triage"]
	safety_gate_status: Literal["needs_triage", "triage_complete", "clinician_reviewed"]
	status: str
	selected_doctor_id: UUID | None
	latest_triage_summary: dict | None
	created_at: datetime
	updated_at: datetime


class CareEpisodeListResponse(BaseModel):
	episodes: list[CareEpisodeResponse]


class TriageSummaryRequest(BaseModel):
	additional_context: str = Field(default="", max_length=4000)
	ai_session_id: UUID | None = None
	originating_event_id: UUID | None = None
	originating_message_id: UUID | None = None


class TriageSummaryResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	triage_summary_id: UUID
	care_episode_id: UUID
	patient_id: UUID
	version: int
	concern: str
	relevant_context: str
	safety_signals: list[str]
	limitations: list[str]
	recommendation: str
	missing_information: list[str]
	unresolved_questions: list[str]
	clinician_review_needed: bool
	additional_context: str
	source_ai_session_id: UUID | None = None
	source_conversation_transcript: str = ""
	created_at: datetime


class TriageSummaryDraftResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	triage_summary_draft_id: UUID
	care_episode_id: UUID
	patient_id: UUID
	saved: bool
	concern: str
	relevant_context: str
	safety_signals: list[str]
	limitations: list[str]
	recommendation: str
	missing_information: list[str]
	unresolved_questions: list[str]
	clinician_review_needed: bool
	additional_context: str
	source_ai_session_id: UUID | None = None
	source_conversation_transcript: str = ""
	created_at: datetime


class TriageSummaryListResponse(BaseModel):
	summaries: list[TriageSummaryResponse]


class TriageSummaryDraftListResponse(BaseModel):
	drafts: list[TriageSummaryDraftResponse]


class CareEpisodeHistoryResponse(BaseModel):
	episode: CareEpisodeResponse
	triage_summaries: list[TriageSummaryResponse]
	triage_summary_drafts: list[TriageSummaryDraftResponse]
	patient_memory: dict | None
	episode_memory: dict | None
	compiled_memory_context: str
	rehab_sessions: list[dict]
	ai_care_summaries: list[dict]


class TriageSummaryEpisodeDraftResponse(BaseModel):
	episode: CareEpisodeResponse
	draft: TriageSummaryDraftResponse


class ProfessionalCareWorkspaceSummary(BaseModel):
	subscription_tier: str
	selected_doctor_id: UUID | None
	selected_doctor_name: str | None
	has_active_relationship: bool
	relationship_id: UUID | None
	request_counts: dict[str, int]
	show_live_monitor: bool


class CareEpisodeWorkspaceSummaryResponse(BaseModel):
	episode: CareEpisodeResponse
	latest_triage_summary: TriageSummaryResponse | None
	unsaved_triage_summaries: list[TriageSummaryDraftResponse]
	latest_rehab_session: dict | None
	latest_ai_care_summary: dict | None
	professional_care: ProfessionalCareWorkspaceSummary
