"""Deterministic Context Integrity and source-allowance contracts."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.ai.evidence_types import EvidenceSource
from app.ai.policy_gate import GateSeverity
from app.ai.workflow_state import WORKFLOW_VERSION, RehabGraphState


PUBLIC_REHAB_KNOWLEDGE_SCOPE = "rehab_knowledge:public"

class SourceAllowance(BaseModel):
    """A source-specific allow-list emitted before evidence planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: EvidenceSource
    source_ids: list[str] = Field(default_factory=list, max_length=64)
    scope: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=128)
    visible: bool = True


class ContextIdentity(BaseModel):
    """Lightweight identity/version metadata safe for traces and checkpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1, max_length=128)
    patient_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    care_episode_id: str | None = Field(default=None, max_length=128)
    workflow_version: str = Field(min_length=1, max_length=128)


class ContextIntegrity(BaseModel):
    """Authorized source manifest; it contains identifiers, never source text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: ContextIdentity
    allowances: dict[EvidenceSource, SourceAllowance]
    source_versions: dict[str, str] = Field(default_factory=dict)

    def allowance_for(self, source: EvidenceSource | str) -> SourceAllowance | None:
        try:
            source_value = source if isinstance(source, EvidenceSource) else EvidenceSource(str(source))
        except ValueError:
            return None
        return self.allowances.get(source_value)

    @property
    def allowed_source_ids(self) -> frozenset[str]:
        return frozenset(
            source_id
            for allowance in self.allowances.values()
            if allowance.visible
            for source_id in allowance.source_ids
        )

    def source_id_is_allowed(
        self, source: EvidenceSource | str, source_id: str
    ) -> bool:
        try:
            source_value = (
                source
                if isinstance(source, EvidenceSource)
                else EvidenceSource(str(source))
            )
        except ValueError:
            return False
        allowance = self.allowance_for(source_value)
        if allowance is None or not allowance.visible:
            return False
        normalized_id = str(source_id).strip()
        if normalized_id in set(allowance.source_ids):
            return True
        if source_value is not EvidenceSource.REHAB_KNOWLEDGE:
            return False
        if PUBLIC_REHAB_KNOWLEDGE_SCOPE not in allowance.source_ids:
            return False
        return normalized_id.startswith(
            ("rehab_knowledge:", "retrieved_doc:", "knowledge_document:")
        ) and bool(normalized_id.split(":", 1)[-1].strip())

    def allowance_metadata(self) -> dict[str, dict[str, object]]:
        """Return source IDs, scopes, and versions without source packet content."""
        return {
            source.value: {
                "source_ids": list(allowance.source_ids),
                "scope": allowance.scope,
                "version": allowance.version,
                "visible": allowance.visible,
            }
            for source, allowance in self.allowances.items()
        }

    def trace_metadata(self) -> dict[str, object]:
        """Safe metadata for ordinary traces; no packet/query content is included."""
        return {
            "principal_id": self.identity.principal_id,
            "patient_id": self.identity.patient_id,
            "session_id": self.identity.session_id,
            "care_episode_id": self.identity.care_episode_id,
            "workflow_version": self.identity.workflow_version,
            "allowed_sources": {
                source.value: {
                    "scope": allowance.scope,
                    "version": allowance.version,
                    "visible": allowance.visible,
                    "source_id_count": len(allowance.source_ids),
                }
                for source, allowance in self.allowances.items()
            },
        }


_SOURCE_PREFIXES: dict[EvidenceSource, tuple[str, ...]] = {
    EvidenceSource.CONVERSATION: (
        "conversation:",
        "ai_message:",
        "ai_internal:",
        "session_compaction:",
    ),
    EvidenceSource.PATIENT_MEMORY: (
        "patient_memory:",
        "memory_item:",
        "memory_document:",
        "memory:patient:",
    ),
    # Episode Memory cards retain the historical memory_item: ID shape; the
    # source-specific allowance keeps that shared prefix scoped to the episode.
    EvidenceSource.CARE_EPISODE: ("care_episode:", "memory:episode:", "memory_item:", "triage_summary:", "rehab_activity:"),
    EvidenceSource.REHAB_KNOWLEDGE: ("rehab_knowledge:", "retrieved_doc:", "knowledge_document:"),
}


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw).strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _coerce_source(value: EvidenceSource | str) -> EvidenceSource:
    try:
        return value if isinstance(value, EvidenceSource) else EvidenceSource(str(value))
    except ValueError as exc:
        raise ValueError(f"Unknown evidence source: {value}") from exc


def _classify_source_ids(source_ids: Iterable[str]) -> dict[EvidenceSource, list[str]]:
    classified = {source: [] for source in EvidenceSource}
    unknown: list[str] = []
    for source_id in _dedupe(source_ids):
        # memory_item: is intentionally shared by Patient Memory and Episode
        # Memory cards; without an explicit source map, retain the historical
        # patient-memory classification rather than treating it as ambiguous.
        if source_id == "memory:patient" or source_id.startswith("memory_item:"):
            classified[EvidenceSource.PATIENT_MEMORY].append(source_id)
            continue
        matches = [source for source, prefixes in _SOURCE_PREFIXES.items() if source_id.startswith(prefixes)]
        if len(matches) == 1:
            classified[matches[0]].append(source_id)
        else:
            unknown.append(source_id)
    if unknown:
        raise ValueError(f"Unclassified source identifier(s): {', '.join(unknown)}")
    return classified


def _validate_source_ids(
    source: EvidenceSource,
    source_ids: Iterable[str],
    *,
    session_id: str,
    care_episode_id: str | None,
    care_episode_authorized: bool,
    authorized_ids: frozenset[str],
) -> list[str]:
    normalized = _dedupe(source_ids)
    invalid: list[str] = []
    unauthorized: list[str] = []
    prefixes = _SOURCE_PREFIXES[source]
    for source_id in normalized:
        if source is EvidenceSource.PATIENT_MEMORY and source_id == "memory:patient":
            if source_id not in authorized_ids:
                unauthorized.append(source_id)
            continue
        matching_prefix = next(
            (prefix for prefix in prefixes if source_id.startswith(prefix)),
            None,
        )
        if matching_prefix is None or not source_id[len(matching_prefix):].strip():
            invalid.append(source_id)
            continue
        if source is EvidenceSource.CONVERSATION and matching_prefix == "conversation:":
            if source_id[len(matching_prefix):] != str(session_id):
                invalid.append(source_id)
                continue
        if source is EvidenceSource.CONVERSATION and matching_prefix == "session_compaction:":
            if source_id[len(matching_prefix):] != str(session_id):
                invalid.append(source_id)
                continue
        if source is EvidenceSource.CARE_EPISODE and matching_prefix == "care_episode:":
            if care_episode_id is None or source_id[len(matching_prefix):] != str(care_episode_id):
                invalid.append(source_id)
                continue
        if source_id not in authorized_ids:
            is_authorized_episode_marker = (
                source is EvidenceSource.CARE_EPISODE
                and matching_prefix == "care_episode:"
                and care_episode_authorized
                and care_episode_id is not None
                and source_id == f"care_episode:{care_episode_id}"
            )
            is_authorized_public_knowledge = (
                source is EvidenceSource.REHAB_KNOWLEDGE
                and (
                    PUBLIC_REHAB_KNOWLEDGE_SCOPE in authorized_ids
                    or source_id == PUBLIC_REHAB_KNOWLEDGE_SCOPE
                )
                and source_id.startswith(
                    ("rehab_knowledge:", "retrieved_doc:", "knowledge_document:")
                )
            )
            if not is_authorized_episode_marker and not is_authorized_public_knowledge:
                unauthorized.append(source_id)
    if invalid:
        raise ValueError(
            f"Source identifier(s) invalid for '{source.value}': {', '.join(invalid)}"
        )
    if unauthorized:
        raise ValueError(
            f"Source identifier(s) not authorized for '{source.value}': {', '.join(unauthorized)}"
        )
    return normalized


def build_context_integrity(
    *,
    principal_id: str,
    patient_id: str,
    session_id: str,
    care_episode_id: str | None = None,
    care_episode_authorized: bool = False,
    patient_memory_visible: bool = True,
    rehab_knowledge_visible: bool = True,
    source_ids_by_source: Mapping[EvidenceSource | str, Iterable[str]] | None = None,
    authorized_source_ids: Iterable[str] | None = None,
    source_ids: Iterable[str] = (),
    source_versions: Mapping[EvidenceSource | str, str] | None = None,
    workflow_version: str = WORKFLOW_VERSION,
) -> ContextIntegrity:
    """Build the source manifest after deterministic identity and scope checks."""
    if not principal_id or not patient_id or not session_id:
        raise ValueError("principal_id, patient_id, and session_id are required")
    if workflow_version != WORKFLOW_VERSION:
        raise ValueError(f"Unsupported workflow version: {workflow_version}")

    authorized_ids = frozenset(_dedupe(authorized_source_ids or ()))
    ids_by_source = {source: [] for source in EvidenceSource}
    if source_ids_by_source is not None:
        for raw_source, values in source_ids_by_source.items():
            source = _coerce_source(raw_source)
            ids_by_source[source].extend(str(value) for value in values)
        # A source-specific map is itself an explicit service allow-list.  The
        # compiled runtime never uses this builder shortcut: it rehydrates
        # from state and requires a nonempty flattened authorization manifest.
        if authorized_source_ids is None:
            authorized_ids = frozenset(
                _dedupe(
                    source_id
                    for source_ids in ids_by_source.values()
                    for source_id in source_ids
                )
            )
    else:
        classified = _classify_source_ids([*authorized_ids, *source_ids])
        for source, values in classified.items():
            ids_by_source[source].extend(values)

    for source in EvidenceSource:
        ids_by_source[source] = _validate_source_ids(
            source,
            ids_by_source[source],
            session_id=session_id,
            care_episode_id=care_episode_id,
            care_episode_authorized=care_episode_authorized,
            authorized_ids=authorized_ids,
        )

    versions = source_versions or {}

    def version_for(source: EvidenceSource) -> str:
        raw = versions.get(source, versions.get(source.value, f"{source.value}:v1"))
        return str(raw)

    allowances: dict[EvidenceSource, SourceAllowance] = {
        EvidenceSource.CONVERSATION: SourceAllowance(
            source=EvidenceSource.CONVERSATION,
            source_ids=ids_by_source[EvidenceSource.CONVERSATION],
            scope="session",
            version=version_for(EvidenceSource.CONVERSATION),
        )
    }
    if patient_memory_visible:
        allowances[EvidenceSource.PATIENT_MEMORY] = SourceAllowance(
            source=EvidenceSource.PATIENT_MEMORY,
            source_ids=ids_by_source[EvidenceSource.PATIENT_MEMORY],
            scope="patient",
            version=version_for(EvidenceSource.PATIENT_MEMORY),
        )
    if care_episode_authorized and care_episode_id:
        episode_ids = ids_by_source[EvidenceSource.CARE_EPISODE]
        episode_marker = f"care_episode:{care_episode_id}"
        if episode_marker not in episode_ids:
            episode_ids.insert(0, episode_marker)
        allowances[EvidenceSource.CARE_EPISODE] = SourceAllowance(
            source=EvidenceSource.CARE_EPISODE,
            source_ids=episode_ids,
            scope="episode",
            version=version_for(EvidenceSource.CARE_EPISODE),
        )
    if rehab_knowledge_visible:
        allowances[EvidenceSource.REHAB_KNOWLEDGE] = SourceAllowance(
            source=EvidenceSource.REHAB_KNOWLEDGE,
            source_ids=ids_by_source[EvidenceSource.REHAB_KNOWLEDGE],
            scope="public_knowledge",
            version=version_for(EvidenceSource.REHAB_KNOWLEDGE),
        )

    return ContextIntegrity(
        identity=ContextIdentity(
            principal_id=principal_id,
            patient_id=patient_id,
            session_id=session_id,
            care_episode_id=care_episode_id,
            workflow_version=workflow_version,
        ),
        allowances=allowances,
        source_versions={
            source.value: allowance.version
            for source, allowance in allowances.items()
        },
    )


def context_integrity_from_state(state: Mapping[str, Any]) -> ContextIntegrity:
    if not state.get("session_id") or not (
        state.get("patient_id") or state.get("principal_id")
    ):
        raise ValueError("authenticated session and patient identity are required")
    if state.get("principal_authorized") is not True:
        raise ValueError("principal authorization is required")
    if state.get("session_authorized") is not True:
        raise ValueError("session authorization is required")
    if (
        state.get("care_episode_id") is not None
        and state.get("care_episode_authorized") is not True
    ):
        raise ValueError("Care Episode authorization is required")
    authorized_source_ids = state.get("authorized_source_ids")
    if not isinstance(authorized_source_ids, (list, tuple, set, frozenset)) or not authorized_source_ids:
        raise ValueError("an explicit authorized source allow-list is required")

    return build_context_integrity(
        principal_id=str(state.get("principal_id") or state.get("patient_id") or state.get("session_id") or ""),
        patient_id=str(state.get("patient_id") or state.get("principal_id") or ""),
        session_id=str(state.get("session_id") or ""),
        care_episode_id=state.get("care_episode_id"),
        care_episode_authorized=bool(state.get("care_episode_authorized")),
        source_ids_by_source=state.get("source_ids_by_source"),
        authorized_source_ids=state.get("authorized_source_ids") or (),
        source_ids=state.get("source_ids") or (),
        workflow_version=state.get("workflow_version") or WORKFLOW_VERSION,
    )


build_context_integrity_manifest = build_context_integrity


def context_integrity(state: RehabGraphState) -> RehabGraphState:
    """Validate identity and source allowances before any model invocation."""
    from app.ai.invocation_support import _debug_event
    from app.ai.repair import context_version_snapshot

    workflow_version = state.get("workflow_version") or WORKFLOW_VERSION
    if workflow_version != WORKFLOW_VERSION:
        return {
            "workflow_version": workflow_version,
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": ["workflow_version_unsupported"],
            "context_rehydrated": bool(state.get("context_rehydrated", False)),
            "debug_trace": _debug_event(
                state,
                "context_integrity",
                {
                    "workflow_version": workflow_version,
                    "severity": GateSeverity.HARD_STOP.value,
                    "reason_codes": ["workflow_version_unsupported"],
                },
            ),
        }

    context_rehydrated = bool(state.get("context_rehydrated", False))
    authorization_reasons: list[str] = []
    if not state.get("session_id") or not (
        state.get("patient_id") or state.get("principal_id")
    ):
        authorization_reasons.append("identity_missing")
    if state.get("principal_authorized") is not True:
        authorization_reasons.append("principal_unauthorized")
    if state.get("session_authorized") is not True:
        authorization_reasons.append("session_unauthorized")
    if (
        state.get("care_episode_id") is not None
        and state.get("care_episode_authorized") is not True
    ):
        authorization_reasons.append("care_episode_unauthorized")
    authorized_source_ids = state.get("authorized_source_ids")
    if (
        not isinstance(
            authorized_source_ids, (list, tuple, set, frozenset)
        )
        or not authorized_source_ids
    ):
        authorization_reasons.append("authorized_source_allowlist_missing")
    if authorization_reasons:
        return {
            "workflow_version": workflow_version,
            "context_rehydrated": context_rehydrated,
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": authorization_reasons,
            "debug_trace": _debug_event(
                state,
                "context_integrity",
                {
                    "workflow_version": workflow_version,
                    "context_rehydrated": context_rehydrated,
                    "severity": GateSeverity.HARD_STOP.value,
                    "reason_codes": authorization_reasons,
                },
            ),
        }

    try:
        integrity = context_integrity_from_state(state)
    except ValueError as exc:
        reason_codes = ["context_authorization_invalid"]
        return {
            "workflow_version": workflow_version,
            "context_rehydrated": context_rehydrated,
            "policy_gate_severity": GateSeverity.HARD_STOP.value,
            "policy_gate_reason_codes": reason_codes,
            "debug_trace": _debug_event(
                state,
                "context_integrity",
                {
                    "workflow_version": workflow_version,
                    "context_rehydrated": context_rehydrated,
                    "severity": GateSeverity.HARD_STOP.value,
                    "reason_codes": reason_codes,
                    "error_type": type(exc).__name__,
                },
            ),
        }
    integrity_metadata = integrity.allowance_metadata()
    version_metadata = context_version_snapshot(
        {
            **dict(state),
            "context_identity": integrity.identity.model_dump(),
            "source_allowances": integrity_metadata,
        }
    )
    return {
        "workflow_version": workflow_version,
        "context_rehydrated": context_rehydrated,
        "context_integrity": integrity,
        "context_identity": integrity.identity.model_dump(),
        "source_allowances": integrity_metadata,
        "context_version": version_metadata["context_version"],
        "evidence_source_versions": version_metadata["source_versions"],
        "authorized_source_ids": sorted(integrity.allowed_source_ids),
        "repair_context_snapshot": dict(version_metadata),
        "debug_trace": _debug_event(
            state,
            "context_integrity",
            {
                "workflow_version": workflow_version,
                "context_rehydrated": context_rehydrated,
                "clarification_answered": bool(
                    state.get("clarification_answered", False)
                ),
                "allowed_sources": sorted(
                    source.value for source in integrity.allowances
                ),
                "source_id_counts": {
                    source.value: len(allowance.source_ids)
                    for source, allowance in integrity.allowances.items()
                },
            },
        ),
    }
