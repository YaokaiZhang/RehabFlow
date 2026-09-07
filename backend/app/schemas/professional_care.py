from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProfessionalCareSubscriptionResponse(BaseModel):
	patient_id: UUID
	subscription_tier: str


class DoctorDirectoryEntry(BaseModel):
	doctor_id: UUID
	display_name: str
	verification_status: str
	specialty: str | None = None
	expertise_tags: list[str] = Field(default_factory=list)


class DoctorDirectoryResponse(BaseModel):
	doctors: list[DoctorDirectoryEntry]


class DoctorProfessionalProfileResponse(BaseModel):
	doctor_id: UUID
	display_name: str
	verification_status: str
	specialty: str | None = None
	expertise_tags: list[str] = Field(default_factory=list)


class DoctorProfessionalProfileUpdate(BaseModel):
	specialty: str | None = None
	expertise_tags: list[str] | None = None

	@model_validator(mode="after")
	def _validate_expertise_tags(self):
		if "expertise_tags" in self.model_fields_set and self.expertise_tags is None:
			raise ValueError("expertise_tags must be a list of strings")
		return self


class DoctorSearchResult(BaseModel):
	doctor: DoctorDirectoryEntry
	score: float
	match_reason: str


class DoctorSearchResponse(BaseModel):
	results: list[DoctorSearchResult]


class CareConnectionRequestCreate(BaseModel):
	doctor_id: UUID
	request_reason: str = Field(default="", max_length=2000)


class CareConnectionRequestResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	request_id: UUID
	care_episode_id: UUID
	patient_id: UUID
	doctor_id: UUID
	request_reason: str
	status: str
	created_at: datetime
	updated_at: datetime
	responded_at: datetime | None
	doctor_name: str | None = None
	patient_name: str | None = None
	issue_title: str | None = None
	latest_triage_summary: dict | None = None


class CareConnectionRequestListResponse(BaseModel):
	requests: list[CareConnectionRequestResponse]


class CareWorklistRelationshipPreview(BaseModel):
	relationship_id: UUID
	care_episode_id: UUID
	patient_id: UUID
	patient_name: str | None = None
	doctor_id: UUID
	status: str
	issue_title: str | None = None
	latest_triage_summary: dict | None = None
	started_at: datetime
	last_activity_at: datetime
	last_activity_label: str


class CareWorklistResponse(BaseModel):
	incoming_requests: list[CareConnectionRequestResponse]
	accepted_requests: list[CareConnectionRequestResponse]
	active_relationships: list[CareWorklistRelationshipPreview]


class SelectCareRelationshipRequest(BaseModel):
	doctor_id: UUID


class CareRelationshipResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	relationship_id: UUID
	care_episode_id: UUID
	patient_id: UUID
	doctor_id: UUID
	source_request_id: UUID
	status: str
	created_at: datetime
	updated_at: datetime
