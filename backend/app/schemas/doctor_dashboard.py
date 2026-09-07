from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CareAppointmentResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	appointment_id: UUID
	relationship_id: UUID
	care_episode_id: UUID | None
	patient_id: UUID
	doctor_id: UUID
	scheduled_start: datetime
	scheduled_end: datetime | None
	status: str
	purpose: str
	created_at: datetime
	updated_at: datetime


class DoctorIntelligenceArtifactCreate(BaseModel):
	model_config = ConfigDict(extra="ignore")

	artifact_type: str = Field(min_length=1, max_length=64)
	title: str = Field(min_length=1, max_length=255)
	content: str = Field(min_length=1, max_length=20000)
	sections: list[dict] = Field(default_factory=list)
	attention_map: dict = Field(default_factory=dict)
	input_refs: dict = Field(default_factory=dict)


class DoctorIntelligenceArtifactResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	artifact_id: UUID
	doctor_id: UUID
	artifact_type: str
	title: str
	content: str
	sections: list
	attention_map: dict
	input_refs: dict
	visibility: str
	version: int
	status: str
	created_at: datetime
	updated_at: datetime


class AttentionMapBucket(BaseModel):
	bucket: str
	label: str
	items: list[dict] = Field(default_factory=list)


class AttentionMapResponse(BaseModel):
	summary: str
	buckets: list[AttentionMapBucket]


class DoctorDashboardResponse(BaseModel):
	doctor_id: UUID
	care_worklist_href: str
	patient_panel_briefing: DoctorIntelligenceArtifactResponse
	attention_map: AttentionMapResponse
	upcoming_appointments: list[CareAppointmentResponse]
	care_relationships: list[dict]
