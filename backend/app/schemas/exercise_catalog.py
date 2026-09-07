from __future__ import annotations

from pydantic import BaseModel


class ExerciseCatalogItem(BaseModel):
	exercise_id: str
	slug: str
	title: str
	introduction: str
	structures_involved: list[str]
	related_conditions: list[str]
	source_url: str
	video_url: str
	video_provider: str
	video_id: str
	video_available: bool


class ExerciseCatalogSearchResponse(BaseModel):
	total: int
	limit: int
	offset: int
	exercises: list[ExerciseCatalogItem]


class ExerciseCatalogFacetsResponse(BaseModel):
	structures: list[str]
	conditions: list[str]
