from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping
from uuid import UUID, uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.runtime_dependencies import OpenAIProviderFactory, structured_output_model
from app.ai.session_compaction import _after_watermark, _latest_protected_rows, _watermark_value
from app.ai.model_input_budget import estimated_text_tokens
from app.core.config import get_settings
from app.db.models import (
    AIMessage,
    AISession,
    AISessionCompactionState,
    CareEpisode,
    MemoryDocument,
    MemoryMaintenanceRun,
    ai_message_ordering,
)
from app.services.memory_documents import (
    ensure_episode_memory_document,
    ensure_patient_memory_document,
    replace_episode_memory,
    replace_patient_summary,
)


MAX_AGENT_ATTEMPTS = 3
EPISODE_MEMORY_OUTPUT_LIMIT_TOKENS = 64_000
MAINTENANCE_CLAIM_LEASE_SECONDS = 600
EPISODE_OUTPUT_BUDGET_ERROR = (
    "episode memory output exceeded the 64K-token budget; regenerate a shorter episode memory."
)


class MemoryMaintenanceError(RuntimeError):
    """Raised when a memory maintenance agent cannot produce a valid result."""


class MemoryMaintenanceConflict(MemoryMaintenanceError):
    """Raised when one idempotency identity is reused with another payload."""


class EpisodeMemoryOutOfBudget(MemoryMaintenanceError):
    """Raised when an Episode Memory result exceeds the explicit safety guard."""


class EpisodeMemoryLevel2Entry(BaseModel):
    model_config = {"extra": "forbid"}

    session_id: str
    detail_state: Literal["substantive"]
    summary: str


class EpisodeMemoryOutput(BaseModel):
    model_config = {"extra": "forbid"}

    no_op: bool = False
    level1_keywords: list[str] = Field(default_factory=list)
    level1_description: str = ""
    level1_session_ids: list[str] = Field(default_factory=list)
    level2_entries: list[EpisodeMemoryLevel2Entry] = Field(default_factory=list)


class PatientMemoryOutput(BaseModel):
    summary: str = ""


@dataclass(frozen=True)
class MemoryMaintenanceSnapshot:
    value: dict[str, Any]


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonical_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, UUID):
        return str(value)
    return value


def canonical_request_hash(value: Any) -> str:
    encoded = json.dumps(_canonical_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def patient_edit_identity(
    patient_id: UUID,
    *,
    operation: str,
    payload: Mapping[str, Any],
    idempotency_key: str | None = None,
    state_token: str | None = None,
) -> tuple[str, str]:
    normalized_key = str(idempotency_key or "").strip()[:128]
    request_identity_payload: dict[str, Any] = {
        "operation": operation,
        "payload": payload,
        "idempotency_key": normalized_key,
    }
    if not normalized_key:
        request_identity_payload["state_token"] = str(state_token or "")
    request_hash = canonical_request_hash(request_identity_payload)
    operation_identity = normalized_key or f"payload:{request_hash[:32]}"
    return f"patient-edit:{patient_id}:{operation_identity}", request_hash


def find_existing_memory_maintenance(
    db: Session,
    trigger_identity: str,
    request_hash: str | None = None,
    *,
    for_update: bool = False,
) -> MemoryMaintenanceRun | None:
    query = select(MemoryMaintenanceRun).where(
        MemoryMaintenanceRun.trigger_identity == trigger_identity
    )
    if for_update:
        query = query.with_for_update()
    existing = db.execute(query).scalar_one_or_none()
    if existing is None:
        return None
    stored_hash = str(getattr(existing, "request_hash", "") or "") if existing is not None else ""
    if stored_hash.startswith("legacy-md5:") and request_hash:
        snapshot_request = dict((getattr(existing, "snapshot", {}) or {}).get("_snapshot_request") or {})
        legacy_canonical_hash = str(snapshot_request.get("request_hash") or "")
        if legacy_canonical_hash and legacy_canonical_hash != request_hash:
            raise MemoryMaintenanceConflict("legacy maintenance identity was reused with a different request payload")
        if not legacy_canonical_hash:
            raise MemoryMaintenanceConflict("legacy maintenance request hash cannot be safely replayed")
    if request_hash and stored_hash and not stored_hash.startswith("legacy-md5:") and stored_hash != request_hash:
        raise MemoryMaintenanceConflict("idempotency key was reused with a different request payload")
    return existing


def _invoke_with_timeout(model: Any, messages: list[Any], timeout_seconds: float) -> Any:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rehab-memory-agent")
    future = executor.submit(model.invoke, messages)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"memory agent timed out after {timeout_seconds} seconds") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response.strip()
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    return str(content or "").strip()


def _as_output(response: Any, schema: type[BaseModel]) -> BaseModel:
    if isinstance(response, schema):
        return response
    if isinstance(response, Mapping):
        return schema.model_validate(response)
    text = _response_text(response)
    fence = chr(96) * 3
    if text.startswith(fence):
        text = text.strip(chr(96)).strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return schema.model_validate(json.loads(text))
    except Exception as exc:
        raise MemoryMaintenanceError("memory agent returned invalid structured output") from exc


def _model_call(
    model: Any,
    schema: type[BaseModel],
    messages: list[Any],
    timeout_seconds: float,
) -> BaseModel:
    structured_factory = getattr(model, "with_structured_output", None)
    if callable(structured_factory):
        structured_model = structured_output_model(model, schema)
        response = _invoke_with_timeout(structured_model, messages, timeout_seconds)
    else:
        response = _invoke_with_timeout(model, messages, timeout_seconds)
    return _as_output(response, schema)


def _conversation_snapshot(
    db: Session,
    *,
    patient_id: UUID,
    episode_id: UUID | None,
    session_id: UUID | None,
    session_compaction_model: Any | None = None,
    session_compaction_timeout_seconds: float | None = None,
    exclude_message_ids: set[UUID] | None = None,
    exclude_message_contents: set[str] | None = None,
) -> list[dict[str, Any]]:
    if session_id is None:
        return []
    session = db.get(AISession, session_id)
    if (
        session is None
        or session.patient_id != patient_id
        or (episode_id is not None and session.care_episode_id != episode_id)
    ):
        return []
    model = session_compaction_model
    # Maintenance snapshots are materialized from database rows only. Provider
    # compaction belongs to the durable worker after this row is committed.
    rows = list(
        db.execute(
            select(AIMessage)
            .where(
                AIMessage.session_id == session_id,
                AIMessage.sender_role.in_(("user", "assistant")),
            )
            .order_by(*ai_message_ordering())
        ).scalars().all()
    )
    excluded_ids = exclude_message_ids or set()
    rows = [row for row in rows if row.message_id not in excluded_ids]
    # Content-based exclusions are intentionally ignored. They can remove a
    # legitimate conversation message with the same text as a summary field.
    state = db.get(AISessionCompactionState, session_id)
    rolling_block = str(getattr(state, "rolling_block", "") or "")
    selected_rows = rows
    if rolling_block.strip():
        watermark = _watermark_value(getattr(state, "source_watermark", None))
        protected = _latest_protected_rows(rows, "message_metadata")
        candidates = [
            row for row in rows
            if _after_watermark(row, "message_metadata", watermark.get("ai"))
        ]
        selected_rows = []
        seen: set[UUID] = set()
        for row in [*candidates, *protected]:
            if row.message_id in seen:
                continue
            seen.add(row.message_id)
            selected_rows.append(row)
        selected_rows.sort(
            key=lambda row: (
                (row.message_metadata or {}).get("sequence") is None,
                int((row.message_metadata or {}).get("sequence") or 0),
                str(row.message_id),
            )
        )
    snapshot: list[dict[str, Any]] = []
    if rolling_block.strip():
        snapshot.append(
            {
                "role": "compacted_context",
                "content": rolling_block,
                "sequence": None,
            }
        )
    snapshot.extend(
        {
            "role": str(row.sender_role),
            "content": str(row.content or ""),
            "sequence": (row.message_metadata or {}).get("sequence"),
            "message_id": str(row.message_id),
        }
        for row in selected_rows
    )
    return snapshot


def _document_snapshot(document: MemoryDocument | None) -> dict[str, Any]:
    if document is None:
        return {
            "summary": "",
            "editable_fields": {},
            "level1_keywords": [],
            "level1_description": "",
            "level1_session_ids": [],
            "level2_summary": "",
        }
    return {
        "summary": str(document.summary_text or ""),
        "editable_fields": dict(document.editable_fields or {}),
        "level1_keywords": list(document.level1_keywords or []),
        "level1_description": str(document.level1_description or ""),
        "level1_session_ids": list(document.level1_session_ids or []),
        "level2_summary": str(document.level2_summary or ""),
    }


def build_memory_snapshot(
    db: Session,
    *,
    patient_id: UUID,
    care_episode_id: UUID | None,
    session_id: UUID | None,
    trigger: str,
    change_set: dict[str, Any] | None = None,
    previous_patient_editable_fields: dict[str, Any] | None = None,
    session_compaction_model: Any | None = None,
    session_compaction_timeout_seconds: float | None = None,
    exclude_message_ids: set[UUID] | None = None,
    exclude_message_contents: set[str] | None = None,
    commit: bool = True,
) -> MemoryMaintenanceSnapshot:
    patient_document = ensure_patient_memory_document(db, patient_id, commit=commit)
    episode_document = None
    if care_episode_id is not None:
        episode = db.get(CareEpisode, care_episode_id)
        if episode is None or episode.patient_id != patient_id:
            raise MemoryMaintenanceError("care episode not found for memory snapshot")
        episode_document = ensure_episode_memory_document(db, episode, commit=commit)
    previous_patient_memory = _document_snapshot(patient_document)
    if previous_patient_editable_fields is not None:
        previous_patient_memory["editable_fields"] = dict(previous_patient_editable_fields)
    value: dict[str, Any] = {
        "trigger": trigger,
        "patient_id": str(patient_id),
        "care_episode_id": str(care_episode_id) if care_episode_id else None,
        "session_id": str(session_id) if session_id else None,
        "previous_patient_memory": previous_patient_memory,
        "previous_episode_memory": _document_snapshot(episode_document),
        "current_session_ai_messages": _conversation_snapshot(
            db,
            patient_id=patient_id,
            episode_id=care_episode_id,
            session_id=session_id,
            session_compaction_model=session_compaction_model,
            session_compaction_timeout_seconds=session_compaction_timeout_seconds,
            exclude_message_ids=exclude_message_ids,
            exclude_message_contents=exclude_message_contents,
        ) if trigger != "patient_edit" else [],
        "patient_change_set": change_set or {},
    }
    return MemoryMaintenanceSnapshot(value=value)


def enqueue_memory_maintenance(
    db: Session,
    *,
    patient_id: UUID,
    care_episode_id: UUID | None,
    session_id: UUID | None,
    trigger: str,
    trigger_identity: str,
    change_set: dict[str, Any] | None = None,
    previous_patient_editable_fields: dict[str, Any] | None = None,
    request_hash: str | None = None,
    exclude_message_ids: set[UUID] | None = None,
    exclude_message_contents: set[str] | None = None,
    originating_artifact_id: UUID | None = None,
    originating_artifact_type: str | None = None,
    originating_event_id: str | None = None,
    originating_message_id: UUID | None = None,
    session_compaction_model: Any | None = None,
    session_compaction_timeout_seconds: float | None = None,
    commit: bool = True,
) -> MemoryMaintenanceRun:
    request_hash = request_hash or canonical_request_hash(
        {
            "trigger": trigger,
            "trigger_identity": trigger_identity,
            "patient_id": patient_id,
            "care_episode_id": care_episode_id,
            "session_id": session_id,
            "change_set": change_set or {},
        }
    )
    existing = find_existing_memory_maintenance(db, trigger_identity, request_hash, for_update=True)
    if existing is not None:
        return existing
    if care_episode_id is not None:
        episode = db.get(CareEpisode, care_episode_id)
        if episode is None or episode.patient_id != patient_id:
            raise MemoryMaintenanceError("care episode not found for memory maintenance")
    if session_id is not None:
        session = db.get(AISession, session_id)
        if session is None or session.patient_id != patient_id:
            raise MemoryMaintenanceError("AI session is not owned by the maintenance patient")
        if care_episode_id is not None and session.care_episode_id != care_episode_id:
            raise MemoryMaintenanceError("AI session is not bound to the maintenance episode")
    snapshot_request = {
        "patient_id": str(patient_id),
        "care_episode_id": str(care_episode_id) if care_episode_id else None,
        "session_id": str(session_id) if session_id else None,
        "trigger": trigger,
        "change_set": change_set or {},
        "previous_patient_editable_fields": previous_patient_editable_fields,
        "exclude_message_ids": [str(value) for value in sorted(exclude_message_ids or set(), key=str)],
        "request_hash": request_hash,
        "originating_artifact_id": str(originating_artifact_id) if originating_artifact_id else None,
        "originating_artifact_type": originating_artifact_type,
        "originating_event_id": originating_event_id,
        "originating_message_id": str(originating_message_id) if originating_message_id else None,
    }
    snapshot = build_memory_snapshot(
        db,
        patient_id=patient_id,
        care_episode_id=care_episode_id,
        session_id=session_id,
        trigger=trigger,
        change_set=change_set,
        previous_patient_editable_fields=previous_patient_editable_fields,
        session_compaction_model=session_compaction_model,
        session_compaction_timeout_seconds=session_compaction_timeout_seconds,
        exclude_message_ids=exclude_message_ids,
        exclude_message_contents=exclude_message_contents,
        commit=commit,
    ).value
    snapshot["_snapshot_request"] = snapshot_request
    snapshot["originating_artifact_id"] = snapshot_request["originating_artifact_id"]
    snapshot["originating_artifact_type"] = snapshot_request["originating_artifact_type"]
    snapshot["originating_event_id"] = snapshot_request["originating_event_id"]
    snapshot["originating_message_id"] = snapshot_request["originating_message_id"]
    run = MemoryMaintenanceRun(
        trigger_identity=trigger_identity,
        request_hash=request_hash,
        trigger=trigger,
        patient_id=patient_id,
        care_episode_id=care_episode_id,
        snapshot=snapshot,
        status="queued",
        episode_status="queued" if care_episode_id is not None else "skipped",
        patient_status="queued",
        errors={},
    )
    db.add(run)
    if not commit:
        db.flush()
        return run
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = find_existing_memory_maintenance(db, trigger_identity, request_hash)
        if existing is None:
            raise
        return existing
    db.refresh(run)
    return run


def enqueue_patient_summary_refresh(
    db: Session,
    *,
    patient_id: UUID,
    trigger_identity: str,
    change_set: dict[str, Any],
    request_hash: str | None = None,
    commit: bool = True,
) -> MemoryMaintenanceRun:
    return enqueue_memory_maintenance(
        db,
        patient_id=patient_id,
        care_episode_id=None,
        session_id=None,
        trigger="patient_edit",
        trigger_identity=trigger_identity,
        change_set=change_set,
        previous_patient_editable_fields=dict(change_set.get("previous") or {}),
        request_hash=request_hash,
        commit=commit,
    )


def _deleted_patient_fields(snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot.get("trigger") != "patient_edit":
        return {}
    change_set = snapshot.get("patient_change_set") or {}
    previous = dict(change_set.get("previous") or {})
    current = dict(change_set.get("current") or {})
    changed_keys = change_set.get("changed_keys") or sorted(set(previous) | set(current))
    return {
        str(key): previous[key]
        for key in changed_keys
        if key in previous and key not in current
    }


def _deleted_patient_field_violation(snapshot: dict[str, Any], summary: str) -> str | None:
    normalized_summary = str(summary or "").casefold()
    for field_name, value in _deleted_patient_fields(snapshot).items():
        normalized_value = str(value or "").strip().casefold()
        if not normalized_value:
            continue
        normalized_name = field_name.casefold()
        normalized_label = field_name.replace("_", " ").casefold()
        if normalized_value in normalized_summary and (
            normalized_name in normalized_summary or normalized_label in normalized_summary
        ):
            return f"patient memory agent retained deleted field {field_name!r}"
    return None


def _agent_snapshot(kind: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    shared = {
        "trigger": snapshot.get("trigger"),
        "patient_id": snapshot.get("patient_id"),
        "care_episode_id": snapshot.get("care_episode_id"),
        "session_id": snapshot.get("session_id"),
    }
    if kind == "episode":
        return {
            **shared,
            "previous_episode_memory": snapshot.get("previous_episode_memory", {}),
            "current_session_ai_messages": snapshot.get("current_session_ai_messages", []),
        }
    patient_change_set = dict(snapshot.get("patient_change_set") or {})
    deleted_fields = _deleted_patient_fields(snapshot)
    if deleted_fields:
        patient_change_set["deleted_fields"] = deleted_fields
    return {
        **shared,
        "previous_patient_memory": snapshot.get("previous_patient_memory", {}),
        "current_session_ai_messages": snapshot.get("current_session_ai_messages", []),
        "patient_change_set": patient_change_set,
    }


def _agent_messages(kind: str, snapshot: dict[str, Any], previous_error: str) -> list[Any]:
    if kind == "episode":
        instructions = (
            "You are the RehabFlow Episode Memory Agent. Decide what is worth remembering from "
            "the previous Episode Memory and this session's AI-message conversation. Generate "
            "Episode Level 1 keywords, one description sentence, relevant session IDs, and a "
            "session-addressable Level 2 entry for every Level 1 session ID. Include only relevant sessions for which you can write a short substantive summary containing material facts and qualifiers. Set detail_state=substantive and never make the summary only a pointer to another session. "
            "You decide what to remember; do not use a deterministic extractor. Preserve uncertainty and do not invent facts. Return no_op=true when no "
            "change is warranted. Do not read or request tools."
        )
    else:
        instructions = (
            "You are the RehabFlow Patient Memory Agent. Decide what is worth remembering from "
            "the previous Patient Memory, its authoritative editable fields, and this session's "
            "AI-message conversation. This summary is patient-wide and is injected into future "
            "Care Episodes. Keep only stable cross-episode identity, long-running constraints, "
            "preferences, clinician relationships, or recurring patterns. Facts explicitly "
            "limited to the current Care Episode, current symptoms, current triage session, "
            "current rehab event, or current issue belong in Episode Memory and must be omitted "
            "from Patient Memory, even if the conversation says to remember them. If the previous "
            "Patient Memory contains an entry qualified as episode-specific or care-episode-only, "
            "remove it from the replacement summary rather than repeating it or its qualification. "
            "Return one very concise Patient summary, normally below "
            "1,000 tokens. Editable fields are authoritative and must not be overwritten. Be "
            "cautious, but decide based on the conversation rather than a deterministic extractor. "
            "For a patient_edit trigger, treat patient_change_set.current as the complete "
            "authoritative editable-field state. If a key appears in changed_keys but is absent "
            "from current, the patient explicitly deleted it: remove that field's old value from "
            "the replacement summary and never infer it from previous_patient_memory. If "
            "patient_change_set.deleted_fields is present, each entry is an explicit user "
            "deletion command; do not mention its key or prior value in the replacement summary. "
            "Preserve unrelated facts from the previous summary. Do not read or request tools."
        )
    retry = f"\nThe previous attempt failed with this error:\n{previous_error}\n" if previous_error else ""
    return [
        SystemMessage(content=instructions),
        HumanMessage(
            content=(
                "Use only this immutable snapshot. It intentionally excludes triage summaries, "
                "internal events, tool calls, hidden reasoning, rehab records, and Professional "
                "Care notes.\n\n"
                f"{json.dumps(_agent_snapshot(kind, snapshot), ensure_ascii=False, sort_keys=True)}\n"
                f"{retry}"
            )
        ),
    ]


def _run_memory_agent(
    model: Any,
    *,
    kind: str,
    snapshot: dict[str, Any],
    timeout_seconds: float,
    persisted_attempts: int = 0,
    reserve_attempt: Callable[[int], None] | None = None,
) -> tuple[BaseModel, int]:
    schema = EpisodeMemoryOutput if kind == "episode" else PatientMemoryOutput
    previous_error = ""
    first_attempt = max(1, int(persisted_attempts or 0) + 1)
    for attempt in range(first_attempt, MAX_AGENT_ATTEMPTS + 1):
        try:
            if reserve_attempt is not None:
                reserve_attempt(attempt)
            output = _model_call(
                model,
                schema,
                _agent_messages(kind, snapshot, previous_error),
                timeout_seconds,
            )
            if kind == "episode":
                level1_ids = [str(value) for value in output.level1_session_ids]
                entry_ids = [str(entry.session_id) for entry in output.level2_entries]
                if not output.no_op and (
                    len(level1_ids) != len(set(level1_ids))
                    or len(entry_ids) != len(set(entry_ids))
                    or set(level1_ids) != set(entry_ids)
                ):
                    raise MemoryMaintenanceError(
                        "episode Level 2 entries must match the unique Level 1 session IDs"
                    )
                if any(not entry.summary.strip() for entry in output.level2_entries):
                    raise MemoryMaintenanceError("episode Level 2 summary must be substantive")
                encoded = json.dumps(output.model_dump(), ensure_ascii=False)
                if estimated_text_tokens(encoded) > EPISODE_MEMORY_OUTPUT_LIMIT_TOKENS:
                    raise EpisodeMemoryOutOfBudget(EPISODE_OUTPUT_BUDGET_ERROR)
            elif kind == "patient":
                summary = str(getattr(output, "summary", "") or "")
                violation = _deleted_patient_field_violation(snapshot, summary)
                if violation:
                    raise MemoryMaintenanceError(violation)
                if not summary.strip():
                    raise MemoryMaintenanceError("patient memory agent returned an empty summary")
            return output, attempt
        except Exception as exc:
            previous_error = str(exc) or type(exc).__name__
    raise MemoryMaintenanceError(
        f"{kind} memory agent failed after {MAX_AGENT_ATTEMPTS} lifetime attempts: "
        f"{previous_error or 'attempt budget exhausted'}"
    )


def _set_document_error(db: Session, *, patient_id: UUID, episode_id: UUID | None, error: str) -> None:
    patient_document = ensure_patient_memory_document(db, patient_id)
    patient_document.last_memory_error = error
    if episode_id is not None:
        episode = db.get(CareEpisode, episode_id)
        if episode is not None:
            episode_document = ensure_episode_memory_document(db, episode)
            episode_document.last_memory_error = error
    db.flush()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def recover_stale_memory_maintenance(db: Session) -> int:
    """Return expired claims to the queue without invoking any provider."""
    now = _utcnow()
    runs = list(
        db.execute(
            select(MemoryMaintenanceRun)
            .where(MemoryMaintenanceRun.status == "processing")
            .with_for_update(skip_locked=True)
        ).scalars().all()
    )
    stale_runs = [
        run for run in runs
        if run.claim_expires_at is None or run.claim_expires_at <= now
    ]
    for run in stale_runs:
        run.status = "queued"
        run.claim_token = None
        run.claim_expires_at = None
    if stale_runs:
        db.commit()
    return len(stale_runs)


def _claim_memory_maintenance(
    db: Session,
    maintenance_id: UUID,
    *,
    lease_seconds: float,
) -> tuple[MemoryMaintenanceRun | None, str | None]:
    run = db.execute(
        select(MemoryMaintenanceRun)
        .where(MemoryMaintenanceRun.maintenance_id == maintenance_id)
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise MemoryMaintenanceError("memory maintenance run not found")
    if run.status in {"completed", "partial", "failed"}:
        return run, None
    now = _utcnow()
    if (
        run.status == "processing"
        and run.claim_expires_at is not None
        and run.claim_expires_at > now
    ):
        return None, None
    token = uuid4().hex
    run.status = "processing"
    run.claim_token = token
    run.claim_expires_at = now + timedelta(seconds=max(60.0, lease_seconds))
    db.commit()
    return run, token


def _reserve_agent_attempt(
    db: Session,
    maintenance_id: UUID,
    claim_token: str,
    *,
    kind: str,
) -> int:
    field_name = "episode_attempts" if kind == "episode" else "patient_attempts"
    run = db.execute(
        select(MemoryMaintenanceRun)
        .where(MemoryMaintenanceRun.maintenance_id == maintenance_id)
        .with_for_update()
    ).scalar_one_or_none()
    if run is None or run.claim_token != claim_token:
        raise MemoryMaintenanceError("memory maintenance claim was lost")
    current = int(getattr(run, field_name) or 0)
    if current >= MAX_AGENT_ATTEMPTS:
        raise MemoryMaintenanceError(f"{kind} memory agent lifetime attempt budget exhausted")
    current += 1
    setattr(run, field_name, current)
    db.commit()
    return current


def _materialize_memory_snapshot(db: Session, run: MemoryMaintenanceRun) -> dict[str, Any]:
    snapshot = dict(run.snapshot or {})
    if snapshot.get("_snapshot_status") != "pending":
        return snapshot
    raise MemoryMaintenanceError(
        "legacy recipe maintenance snapshot cannot be materialized after enqueue"
    )


def process_memory_maintenance(
    db: Session,
    maintenance_id: UUID,
    *,
    episode_model: Any | None = None,
    patient_model: Any | None = None,
    timeout_seconds: float | None = None,
) -> MemoryMaintenanceRun:
    if timeout_seconds is None:
        timeout_seconds = get_settings().memory_agent_timeout_seconds
    recover_stale_memory_maintenance(db)
    lease_seconds = max(
        float(MAINTENANCE_CLAIM_LEASE_SECONDS),
        float(timeout_seconds) * MAX_AGENT_ATTEMPTS * 3.0,
    )
    run, claim_token = _claim_memory_maintenance(
        db,
        maintenance_id,
        lease_seconds=lease_seconds,
    )
    if run is None:
        current = db.get(MemoryMaintenanceRun, maintenance_id)
        if current is None:
            raise MemoryMaintenanceError("memory maintenance run not found")
        return current
    if claim_token is None:
        return run
    try:
        snapshot = _materialize_memory_snapshot(db, run)
    except Exception as exc:
        run.errors = {**dict(run.errors or {}), "snapshot": str(exc)}
        run.status = "failed"
        run.claim_token = None
        run.claim_expires_at = None
        db.commit()
        db.refresh(run)
        return run

    errors = dict(run.errors or {})
    episode_ok = run.episode_status in {"skipped", "completed"}
    patient_ok = run.patient_status == "completed"

    def reserve(kind: str, _attempt: int) -> None:
        _reserve_agent_attempt(db, maintenance_id, claim_token, kind=kind)

    if run.care_episode_id is not None:
        if run.episode_status != "completed":
            try:
                model = episode_model or OpenAIProviderFactory().episode_memory_model()
                output, _attempt = _run_memory_agent(
                    model,
                    kind="episode",
                    snapshot=snapshot,
                    timeout_seconds=timeout_seconds,
                    persisted_attempts=int(run.episode_attempts or 0),
                    reserve_attempt=lambda attempt: reserve("episode", attempt),
                )
                episode = db.get(CareEpisode, run.care_episode_id)
                if episode is None:
                    raise MemoryMaintenanceError("care episode not found during memory maintenance")
                if not output.no_op:
                    replace_episode_memory(
                        db,
                        episode=episode,
                        level1_keywords=output.level1_keywords,
                        level1_description=output.level1_description,
                        level1_session_ids=output.level1_session_ids,
                        level2_summary=json.dumps(
                            {
                                "version": 2,
                                "entries": [entry.model_dump() for entry in output.level2_entries],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                run.episode_status = "completed"
                episode_ok = True
                db.commit()
            except Exception as exc:
                run.episode_status = "failed"
                errors["episode"] = str(exc)
                _set_document_error(
                    db,
                    patient_id=run.patient_id,
                    episode_id=run.care_episode_id,
                    error=str(exc),
                )
                db.commit()

    if run.patient_status != "completed":
        try:
            model = patient_model or OpenAIProviderFactory().patient_memory_model()
            output, _attempt = _run_memory_agent(
                model,
                kind="patient",
                snapshot=snapshot,
                timeout_seconds=timeout_seconds,
                persisted_attempts=int(run.patient_attempts or 0),
                reserve_attempt=lambda attempt: reserve("patient", attempt),
            )
            replace_patient_summary(
                db,
                patient_id=run.patient_id,
                summary_text=output.summary,
            )
            run.patient_status = "completed"
            patient_ok = True
            db.commit()
        except Exception as exc:
            run.patient_status = "failed"
            errors["patient"] = str(exc)
            _set_document_error(
                db,
                patient_id=run.patient_id,
                episode_id=None,
                error=str(exc),
            )
            db.commit()

    run.errors = errors
    if episode_ok and patient_ok:
        run.status = "completed"
    elif episode_ok or patient_ok:
        run.status = "partial"
    else:
        run.status = "failed"
    run.claim_token = None
    run.claim_expires_at = None
    db.commit()
    db.refresh(run)
    return run


def process_queued_memory_maintenance(
    db: Session,
    *,
    limit: int = 10,
    episode_model: Any | None = None,
    patient_model: Any | None = None,
    timeout_seconds: float | None = None,
) -> int:
    """Process a bounded queue batch; claims make concurrent workers safe."""
    recover_stale_memory_maintenance(db)
    maintenance_ids = list(
        db.execute(
            select(MemoryMaintenanceRun.maintenance_id)
            .where(MemoryMaintenanceRun.status == "queued")
            .order_by(MemoryMaintenanceRun.created_at.asc(), MemoryMaintenanceRun.maintenance_id.asc())
            .limit(max(1, int(limit)))
        ).scalars().all()
    )
    for maintenance_id in maintenance_ids:
        process_memory_maintenance(
            db,
            maintenance_id,
            episode_model=episode_model,
            patient_model=patient_model,
            timeout_seconds=timeout_seconds,
        )
    return len(maintenance_ids)


def dispatch_memory_maintenance(
    maintenance_id: UUID,
    *,
    session_factory: Any | None = None,
    episode_model: Any | None = None,
    patient_model: Any | None = None,
    timeout_seconds: float | None = None,
) -> None:
    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal
    if timeout_seconds is None:
        timeout_seconds = get_settings().memory_agent_timeout_seconds
    db = session_factory()
    try:
        process_memory_maintenance(
            db,
            maintenance_id,
            episode_model=episode_model,
            patient_model=patient_model,
            timeout_seconds=timeout_seconds,
        )
    finally:
        db.close()
