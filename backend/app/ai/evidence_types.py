"""Typed contracts for authorized evidence execution."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ai.context_types import ContextPacket


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum with stable serialized values."""

    def __str__(self) -> str:
        return self.value


class EvidenceSource(StrEnum):
    CONVERSATION = "conversation"
    PATIENT_MEMORY = "patient_memory"
    CARE_EPISODE = "care_episode"
    REHAB_KNOWLEDGE = "rehab_knowledge"


MAX_EVIDENCE_QUERY_CHARS = 4096
MAX_EVIDENCE_SOURCE_IDS = 64
MAX_EVIDENCE_ITEMS = 50
MAX_EVIDENCE_TIMEOUT_MS = 30_000


class EvidenceRequest(BaseModel):
    """A bounded, deterministic request for one authorized evidence source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    source: EvidenceSource
    query: str = Field(min_length=1, max_length=MAX_EVIDENCE_QUERY_CHARS)
    source_ids: list[str] = Field(max_length=MAX_EVIDENCE_SOURCE_IDS)
    max_items: int = Field(ge=1, le=MAX_EVIDENCE_ITEMS)
    timeout_ms: int = Field(ge=1, le=MAX_EVIDENCE_TIMEOUT_MS)

    @field_validator("request_id", "query")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("source_ids")
    @classmethod
    def normalize_source_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for source_id in value:
            if not isinstance(source_id, str):
                raise TypeError("source_ids must contain strings")
            item = source_id.strip()
            if not item:
                raise ValueError("source_ids must not contain blank identifiers")
            if item in seen:
                raise ValueError("source_ids must not contain duplicates")
            seen.add(item)
            normalized.append(item)
        return normalized

    def trace_metadata(self) -> dict[str, object]:
        """Return safe control metadata without query or packet content."""
        return {
            "request_id": self.request_id,
            "source": self.source.value,
            "source_ids": list(self.source_ids),
            "max_items": self.max_items,
            "timeout_ms": self.timeout_ms,
        }


class EvidenceResult(BaseModel):
    """Execution result; packet text is never part of trace/checkpoint metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    source: EvidenceSource
    status: Literal["ok", "empty", "timeout", "error", "forbidden"]
    packets: list[ContextPacket] = Field(default_factory=list)
    elapsed_ms: int = Field(ge=0)
    reason_code: str | None = Field(default=None, max_length=64)

    def trace_metadata(self) -> dict[str, object]:
        metadata = {
            "request_id": self.request_id,
            "source": self.source.value,
            "status": self.status,
            "source_ids": [packet.source_id for packet in self.packets],
            "packet_count": len(self.packets),
            "elapsed_ms": self.elapsed_ms,
        }
        if self.reason_code:
            metadata["reason_code"] = self.reason_code
        return metadata

    checkpoint_metadata = trace_metadata


class EvidenceResultMetadata(BaseModel):
    """Checkpoint-safe result metadata; packet content stays execution-only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    source: EvidenceSource
    status: Literal["ok", "empty", "timeout", "error", "forbidden"]
    source_ids: list[str] = Field(default_factory=list, max_length=50)
    packet_count: int = Field(ge=0, le=50)
    elapsed_ms: int = Field(ge=0)
    reason_code: str | None = Field(default=None, max_length=64)

    @classmethod
    def from_result(cls, result: EvidenceResult) -> "EvidenceResultMetadata":
        return cls(
            request_id=result.request_id,
            source=result.source,
            status=result.status,
            source_ids=[packet.source_id for packet in result.packets],
            packet_count=len(result.packets),
            elapsed_ms=result.elapsed_ms,
            reason_code=result.reason_code,
        )

    def trace_metadata(self) -> dict[str, object]:
        return self.model_dump(mode="json")
    checkpoint_metadata = trace_metadata
