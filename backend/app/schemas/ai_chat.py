from __future__ import annotations

from uuid import UUID

from app.ai.clarification import WORKFLOW_VERSION
from pydantic import BaseModel, ConfigDict, Field


class AIChatTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8_000)
    care_episode_id: UUID | None = None
    idempotency_key: str = Field(min_length=16, max_length=128)
    workflow_version: str = Field(default=WORKFLOW_VERSION, min_length=1, max_length=128)


class AIChatSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    care_episode_id: UUID | None = None
