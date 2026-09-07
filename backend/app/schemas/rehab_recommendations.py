from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RehabRecommendationItem(BaseModel):
	exercise_id: str = Field(min_length=1, max_length=128)
	title: str = Field(min_length=1, max_length=255)
	reason: str = Field(default="", max_length=1000)
	dosage: str = Field(default="", max_length=500)


class RehabRecommendationResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	recommendation_id: UUID
	care_episode_id: UUID
	patient_id: UUID
	source_triage_summary_id: UUID | None
	source_triage_summary_version: int
	status: str
	empty_reason: str
	query_text: str
	items: list[RehabRecommendationItem]
	active: bool
	created_at: datetime
	updated_at: datetime


class RehabListItemInput(BaseModel):
	exercise_id: str = Field(min_length=1, max_length=128)
	title: str = Field(min_length=1, max_length=255)
	completed: bool = False
	notes: str = Field(default="", max_length=1000)


class RehabListUpdateRequest(BaseModel):
	items: list[RehabListItemInput] = Field(default_factory=list)


class RehabListResponse(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	list_id: UUID
	care_episode_id: UUID
	patient_id: UUID
	source_recommendation_id: UUID | None
	items: list[RehabListItemInput]
	created_at: datetime
	updated_at: datetime
