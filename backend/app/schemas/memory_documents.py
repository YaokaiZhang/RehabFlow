from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PatientMemoryFieldsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any] = Field(default_factory=dict)


class PatientMemoryFieldsReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any] = Field(default_factory=dict)


class MemoryDocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: UUID
    scope: str
    patient_id: UUID
    care_episode_id: UUID | None
    compiled_text: str
    status: str
    last_compiled_at: datetime | None
    editable_fields: dict[str, Any] = Field(default_factory=dict)
    summary_text: str
    level1_keywords: list[str] = Field(default_factory=list)
    level1_description: str
    level1_session_ids: list[str] = Field(default_factory=list)
    level2_summary: str
    last_memory_error: str
    created_at: datetime
    updated_at: datetime


class MemoryContextPackResponse(BaseModel):
    patient_document: MemoryDocumentResponse
    episode_document: MemoryDocumentResponse
    compiled_context: str


class MemoryMaintenanceRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    maintenance_id: UUID
    trigger_identity: str
    trigger: str
    patient_id: UUID
    care_episode_id: UUID | None
    status: str
    episode_status: str
    patient_status: str
    episode_attempts: int
    patient_attempts: int
    errors: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
