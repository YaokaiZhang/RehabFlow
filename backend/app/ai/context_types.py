"""Data contracts for assembling AI chat context packets."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, Mapping
from pydantic import BaseModel, Field

ContextScope = Literal["patient", "episode", "session"]
MAX_CONTEXT_MODEL_TOKENS = 256_000


def _freeze_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class ContextSourceCard:
    source_id: str
    source_type: str
    scope: ContextScope
    title: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    priority_hint: str = "normal"

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_prompt_text(self) -> str:
        if self.metadata:
            metadata_text = ", ".join(f"{key}={value}" for key, value in sorted(self.metadata.items()))
        else:
            metadata_text = "{}"
        parts = [
            f"Source: {self.source_id}",
            f"Type: {self.source_type}",
            f"Scope: {self.scope}",
            f"Title: {self.title}",
            f"Priority: {self.priority_hint}",
            f"Metadata: {metadata_text}",
            "Content:",
            self.text.strip(),
        ]
        return "\n".join(part for part in parts if part)


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    context_sections: tuple[str, ...]

    def __post_init__(self) -> None:
        sections = tuple(self.context_sections)
        object.__setattr__(self, "context_sections", sections)

        seen: set[str] = set()
        duplicates: list[str] = []
        for section in sections:
            if section in seen and section not in duplicates:
                duplicates.append(section)
            seen.add(section)
        if duplicates:
            raise ValueError(f"Duplicate context section(s) for {self.node_id}: {', '.join(duplicates)}")


@dataclass(frozen=True)
class ContextPlan:
    turn_summary: str
    ranked_source_ids: tuple[str, ...]
    node_hints: Mapping[str, tuple[str, ...]]
    unresolved_questions: tuple[str, ...]
    stale_source_ids: tuple[str, ...]
    summary_update_needed: bool
    summary_notes: str

    def __post_init__(self) -> None:
        if not isinstance(self.summary_notes, str):
            raise TypeError("summary_notes must be a string")

        object.__setattr__(self, "ranked_source_ids", tuple(self.ranked_source_ids))
        object.__setattr__(
            self,
            "node_hints",
            MappingProxyType({node_id: tuple(source_ids) for node_id, source_ids in self.node_hints.items()}),
        )
        object.__setattr__(self, "unresolved_questions", tuple(self.unresolved_questions))
        object.__setattr__(self, "stale_source_ids", tuple(self.stale_source_ids))

    def restrict_to_source_ids(self, source_ids: set[str] | frozenset[str]) -> "ContextPlan":
        allowed = set(source_ids)
        return replace(
            self,
            ranked_source_ids=tuple(source_id for source_id in self.ranked_source_ids if source_id in allowed),
            node_hints={
                node_id: tuple(source_id for source_id in hinted_ids if source_id in allowed)
                for node_id, hinted_ids in self.node_hints.items()
            },
            stale_source_ids=tuple(source_id for source_id in self.stale_source_ids if source_id in allowed),
        )


@dataclass(frozen=True)
class NodeContextPacket:
    node_id: str
    sections: Mapping[str, str]
    included_source_ids: tuple[str, ...]
    omitted_source_ids: tuple[str, ...]
    estimated_tokens: int

    def __post_init__(self) -> None:
        normalized_sections: dict[str, str] = {}
        for name, content in self.sections.items():
            if not isinstance(name, str):
                raise TypeError("section names must be strings")
            if not isinstance(content, str):
                raise TypeError("section content must be strings")
            normalized_sections[name] = content

        object.__setattr__(self, "sections", MappingProxyType(normalized_sections))
        object.__setattr__(self, "included_source_ids", tuple(self.included_source_ids))
        object.__setattr__(self, "omitted_source_ids", tuple(self.omitted_source_ids))

    def to_prompt_text(self) -> str:
        chunks: list[str] = []
        for section_name, content in self.sections.items():
            if not content.strip():
                continue
            chunks.append(f"## {section_name}")
            chunks.append(content.strip())
        if self.included_source_ids:
            chunks.append(f"Included sources: {', '.join(self.included_source_ids)}")
        return "\n\n".join(chunks)


@dataclass(frozen=True)
class ContextAssemblyResult:
    plan: ContextPlan
    packets: Mapping[str, NodeContextPacket]
    assembly_metadata: Mapping[str, Any]
    progressive_levels: Mapping[str, str] = field(default_factory=dict)
    disclosure_trace: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "packets", MappingProxyType(dict(self.packets)))
        object.__setattr__(self, "assembly_metadata", _freeze_mapping(self.assembly_metadata))
        object.__setattr__(self, "progressive_levels", _freeze_mapping(self.progressive_levels))
        object.__setattr__(self, "disclosure_trace", tuple(dict(item) for item in self.disclosure_trace))


class ContextPacket(BaseModel):
    """Evidence packet whose content is available only during execution."""

    source_id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


def __getattr__(name: str):
    """Preserve the historical import path without maintaining a duplicate contract."""
    if name == "EvidenceResult":
        from app.ai.evidence_types import EvidenceResult
        return EvidenceResult
    raise AttributeError(name)
