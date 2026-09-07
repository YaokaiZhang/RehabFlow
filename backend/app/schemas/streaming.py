from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return self.value


class StreamScope(StrEnum):
    PATIENT_REHAB = "patient_rehab"
    DOCTOR_EPISODE_MONITOR = "doctor_episode_monitor"


@dataclass(frozen=True)
class StreamTicketClaims:
    scope: StreamScope
    principal_user_id: UUID
    patient_id: UUID
    care_episode_id: UUID | None


class StreamTicketResponse(BaseModel):
    ticket: str
    expires_in_seconds: int
