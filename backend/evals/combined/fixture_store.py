"""Evaluator-only fixture provisioning over product boundaries and production models."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from evals.combined.case_resources import CaseResources, build_case_resources
from evals.combined.resource_manifest import CaseManifest, RunManifest
from evals.longitudinal.contract import ScenarioV2


def _text(value: object, default: str = "") -> str:
    return " ".join(str(value or default).split())

def _rows_in_session_order(rows: list[Any], session_ids: list[uuid.UUID]) -> list[Any]:
    """Order case-wide query results by session, then by the query's local order."""
    rows_by_session: dict[str, list[Any]] = {}
    for row in rows:
        session_id = getattr(row, "session_id", None)
        if session_id is not None:
            rows_by_session.setdefault(str(session_id), []).append(row)
    return [
        row
        for session_id in session_ids
        for row in rows_by_session.get(str(session_id), [])
    ]


def _time(value: object, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value)) if _text(value) else fallback
    except ValueError:
        parsed = fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamp(item: object, when: datetime) -> None:
    if hasattr(item, "created_at"):
        item.created_at = when
    if hasattr(item, "updated_at"):
        item.updated_at = when


_GENERATED_CONTEXT_CHUNK_CHARS = 10_000
_GENERATED_CONTEXT_MAX_MESSAGES = 512
_GENERATED_CONTEXT_TEMPLATES = (
    "This synthetic checkpoint records ordinary pacing and comfort observations for the active ankle episode.",
    "The discussion remains within the current rehabilitation session and does not introduce a new clinical conclusion.",
    "The patient reviewed scheduling, gradual progress, and routine monitoring at this point in the session.",
    "This entry preserves neutral session history so the later request must rely on the important current fact.",
    "No unrelated body area or prior episode is relevant to this synthetic conversation entry.",
    "The rehabilitation discussion continued with ordinary progress notes and bounded activity reminders.",
)


def _generated_context_token_estimate(messages: list[Mapping[str, str]]) -> int:
    from app.ai.model_input_budget import model_messages_estimated_tokens
    from langchain_core.messages import HumanMessage, SystemMessage

    source = "\n\n".join(
        f"{_text(message.get('role'), 'user')} sequence={index}:\n"
        f"{message.get('content', '')}"
        for index, message in enumerate(messages, start=1)
    )
    return model_messages_estimated_tokens(
        [
            SystemMessage(
                content=(
                    "RehabFlow serialized context admission envelope. Preserve source labels, "
                    "tool arguments, tool results, and the active durable context."
                )
            ),
            HumanMessage(content="Active durable context and current-session sources:\n" + source),
        ]
    )


def _generated_context_messages(
    session_spec: Mapping[str, Any],
) -> tuple[list[dict[str, str]] | None, dict[str, Any] | None]:
    config = session_spec.get("generated_context")
    if not isinstance(config, Mapping):
        return None, None
    target_tokens = config.get("target_tokens")
    if not isinstance(target_tokens, int) or isinstance(target_tokens, bool) or target_tokens < 256001:
        raise ValueError("generated_context.target_tokens must be at least 256001")
    critical_fact = _text(config.get("critical_fact"))
    if not critical_fact:
        raise ValueError("generated_context.critical_fact is required")

    messages: list[dict[str, str]] = []
    estimated_tokens = 0
    index = 1
    while estimated_tokens < target_tokens:
        role = "user" if index % 2 else "assistant"
        parts: list[str] = []
        current_chars = 0
        detail = 0
        while current_chars < _GENERATED_CONTEXT_CHUNK_CHARS:
            template = _GENERATED_CONTEXT_TEMPLATES[(index + detail) % len(_GENERATED_CONTEXT_TEMPLATES)]
            fragment = f"Session entry {index}, detail {detail}: {template}"
            parts.append(fragment)
            current_chars += len(fragment) + 1
            detail += 1
        content = " ".join(parts)[:_GENERATED_CONTEXT_CHUNK_CHARS].rstrip()
        if index == 1:
            suffix = f" Critical fact: {critical_fact}"
            content = content[: max(0, _GENERATED_CONTEXT_CHUNK_CHARS - len(suffix))].rstrip() + suffix
        messages.append({"role": role, "content": content})
        estimated_tokens = _generated_context_token_estimate(messages)
        index += 1
        if index > _GENERATED_CONTEXT_MAX_MESSAGES:
            raise ValueError("generated_context exceeded the message-count safety bound")

    return messages, {
        "target_tokens": target_tokens,
        "estimated_tokens": estimated_tokens,
        "message_count": len(messages),
        "critical_fact_sha256": hashlib.sha256(critical_fact.encode("utf-8")).hexdigest(),
        "report_capture": config.get("report_capture"),
    }


@dataclass
class ProvisionedFixture:
    patient_id: str
    token: str
    episode_ids: dict[str, str]
    memory_ids: dict[str, str]
    memory_scopes: dict[str, str]
    source_id_map: dict[str, str]
    fixture_setup: dict[str, Any]
    case_manifest: CaseManifest
    case_resources: CaseResources
    evaluator_db_control: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    record_memory_item: Callable[[str, str], None]
    record_ai_session: Callable[[str], None]
    record_triage_summary: Callable[[str, str], None]
    load_persisted_messages: Callable[[], Mapping[str, Any]]


class FixtureStore:
    """Own exact per-case resources and evaluator DB controls."""

    def __init__(
        self,
        *,
        harness: Any,
        manifest: RunManifest,
        database_url: str,
        catalog_dir: str | Path,
        qdrant_path: str | Path | None = None,
        session_factory: Callable[[], Session] | None = None,
        store_factory: Callable[..., Any] | None = None,
        progress_hook: Callable[[], None] | None = None,
        activate_case_resources: bool = True,
    ) -> None:
        self.harness = harness
        self.manifest = manifest
        self.database_url = database_url
        self.catalog_dir = Path(catalog_dir).expanduser().resolve()
        self.qdrant_path = Path(qdrant_path).expanduser().resolve() if qdrant_path is not None else None
        self._session_factory = session_factory
        self._store_factory = store_factory
        self._progress_hook = progress_hook
        self._activate_case_resources = bool(activate_case_resources)
        self._engine = None
        self._previous_resources: list[CaseResources] = []

    def _db(self) -> Session:
        if self._session_factory:
            return self._session_factory()
        if self._engine is None:
            self._engine = create_engine(self.database_url, pool_pre_ping=True)
        return sessionmaker(bind=self._engine, autoflush=False, expire_on_commit=False)()

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
        from app.vector.qdrant_store import close_shared_qdrant_clients
        close_shared_qdrant_clients()

    def _persist_progress(self) -> None:
        if getattr(self, "_progress_hook", None) is not None:
            self._progress_hook()

    def _register(self, case_id: str) -> tuple[str, str]:
        nonce = uuid.uuid4().hex
        status, payload = self.harness.request_json(
            "/auth/register/patient",
            method="POST",
            payload={
                "patient_name": f"eval-{case_id}-{nonce[:12]}",
                "password": f"Eval-{nonce}-password",
                "real_info": {"evaluation_namespace": f"eval-{case_id}"},
            },
        )
        user = payload.get("user", {}) if isinstance(payload, Mapping) else {}
        patient_id = str(user.get("user_id") or "") if isinstance(user, Mapping) else ""
        token = str(payload.get("access_token") or "") if isinstance(payload, Mapping) else ""
        if status not in {200, 201} or not patient_id or not token:
            raise RuntimeError(f"evaluator patient provisioning failed with HTTP {status}")
        return patient_id, token

    def _active_episode(self, case_id: str, logical_id: str, body_area: str, token: str) -> str:
        status, payload = self.harness.request_json(
            "/care-episodes",
            method="POST",
            payload={
                "issue_title": f"Evaluation {case_id} {logical_id}",
                "body_area": body_area[:128] or "synthetic",
                "goal": "Evaluate the real backend conversation boundary",
                "short_description": "Synthetic non-clinical evaluator Care Episode.",
            },
            token=token,
        )
        episode_id = str(payload.get("care_episode_id") or "") if isinstance(payload, Mapping) else ""
        if status not in {200, 201} or not episode_id:
            raise RuntimeError(f"evaluator Care Episode provisioning failed with HTTP {status}")
        return episode_id

    def _seed(
        self,
        scenario: ScenarioV2 | None,
        patient_id: str,
        episode_ids: dict[str, str],
        manifest: CaseManifest,
        source_id_map: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
        from app.db.models import AISession, AIMessage, CareEpisode, CareEpisodeTriageSummary, CareEpisodeTriageSummaryDraft
        from app.services.memory_documents import ensure_episode_memory_document, ensure_patient_memory_document, refresh_compiled_text
        from app.services.triage_summary_publication import mark_latest_summary_snapshot

        if scenario is None:
            return {}, {}, {
                "inactive_episodes": 0,
                "memory_items": 0,
                "sessions": 0,
                "triage_summaries": 0,
                "generated_contexts": 0,
                "generated_context_setup": {},
            }
        state = scenario.initial_state
        source_id_map = source_id_map if source_id_map is not None else {}
        current = _time(state.get("current_time"), datetime.now(timezone.utc))
        db = self._db()
        memory_ids: dict[str, str] = {}
        memory_scopes: dict[str, str] = {}
        counts: dict[str, Any] = {
            "inactive_episodes": 0,
            "memory_items": 0,
            "sessions": 0,
            "triage_summaries": 0,
            "generated_contexts": 0,
            "generated_context_setup": {},
        }
        session_id_map: dict[str, str] = {}
        episode_memory_specs: list[tuple[Any, Mapping[str, Any]]] = []
        try:
            patient_uuid = uuid.UUID(patient_id)
            for spec in state.get("care_episodes", []) or []:
                if not isinstance(spec, Mapping) or _text(spec.get("status"), "active") == "active":
                    continue
                episode = CareEpisode(
                    patient_id=patient_uuid,
                    issue_title=f"Evaluation {scenario.scenario_id} {_text(spec.get('id'))}",
                    body_area=_text(spec.get("body_area"), "synthetic")[:128],
                    goal="Evaluate the real backend conversation boundary",
                    short_description="Synthetic non-clinical evaluator Care Episode.",
                    status=_text(spec.get("status"), "inactive"),
                    origin="evaluation",
                )
                _stamp(episode, current)
                db.add(episode)
                db.flush()
                logical_episode_id = _text(spec.get("id"))
                physical_episode_id = str(episode.care_episode_id)
                episode_ids[logical_episode_id] = physical_episode_id
                manifest.record_care_episode(logical_episode_id, physical_episode_id)
                self._persist_progress()
                source_id_map[f"care_episode:{physical_episode_id}"] = f"care_episode:{logical_episode_id}"
                counts["inactive_episodes"] += 1

            docs: dict[str, Any] = {"patient": ensure_patient_memory_document(db, patient_uuid)}
            manifest.record_memory_document(str(docs["patient"].document_id))
            self._persist_progress()
            for logical_id, physical_id in episode_ids.items():
                episode = db.get(CareEpisode, uuid.UUID(physical_id))
                if episode is not None:
                    docs[logical_id] = ensure_episode_memory_document(db, episode)
                    manifest.record_memory_document(str(docs[logical_id].document_id))
                    self._persist_progress()

            memory_specs = [
                ("patient", item) for item in state.get("patient_memory", []) or [] if isinstance(item, Mapping)
            ] + [
                (_text(item.get("care_episode_id")), item)
                for item in state.get("episode_memory", []) or []
                if isinstance(item, Mapping)
            ]
            for scope, spec in memory_specs:
                document = docs.get(scope)
                if document is None:
                    raise RuntimeError("memory fixture references an unprovisioned Care Episode")
                logical_memory_id = _text(spec.get("id"))
                status = _text(spec.get("status"), "active")
                content = str(spec.get("content") or "Synthetic evaluator memory.")
                if status not in {"deleted", "archived", "inactive"} and spec.get("stale") is not True:
                    if scope == "patient":
                        fields = dict(document.editable_fields or {})
                        fields[logical_memory_id] = content
                        document.editable_fields = fields
                    else:
                        keywords = list(document.level1_keywords or [])
                        title = _text(spec.get("title"), _text(spec.get("item_type"), "evaluation"))
                        if title and title not in keywords:
                            keywords.append(title)
                        document.level1_keywords = keywords
                        document.level1_description = content
                        level2_content = str(spec.get("level2_content") or content)
                        prior = str(document.level2_summary or "").strip()
                        document.level2_summary = "\n\n".join(
                            value for value in (prior, level2_content) if value
                        )
                        episode_memory_specs.append((document, spec))
                physical_memory_id = str(document.document_id)
                memory_ids[logical_memory_id] = physical_memory_id
                memory_scopes[logical_memory_id] = "patient" if scope == "patient" else "episode"
                manifest.record_memory_item(logical_memory_id, physical_memory_id)
                manifest.record_memory_document(physical_memory_id)
                self._persist_progress()
                source_id_map[f"memory_document:{physical_memory_id}"] = f"memory_document:{logical_memory_id}"
                source_id_map[f"memory_item:{physical_memory_id}"] = f"memory_item:{logical_memory_id}"
                counts["memory_items"] += 1
            for document in docs.values():
                refresh_compiled_text(document)

            for spec in state.get("sessions", []) or []:
                if not isinstance(spec, Mapping):
                    continue
                episode_logical_id = _text(spec.get("care_episode_id"))
                episode_id = episode_ids.get(episode_logical_id)
                if episode_logical_id and episode_id is None:
                    raise RuntimeError("session fixture references an unprovisioned Care Episode")
                session = AISession(
                    patient_id=patient_uuid,
                    care_episode_id=uuid.UUID(episode_id) if episode_id else None,
                    title=f"Evaluation session {_text(spec.get('id'))}"[:255],
                )
                _stamp(session, current)
                db.add(session)
                db.flush()
                logical_session_id = _text(spec.get("id"))
                physical_session_id = str(session.session_id)
                session_id_map[logical_session_id] = physical_session_id
                manifest.record_ai_session(logical_session_id, physical_session_id)
                self._persist_progress()
                source_id_map[f"conversation:{physical_session_id}"] = f"conversation:{logical_session_id}"
                generated_messages, generated_setup = _generated_context_messages(spec)
                seed_messages = (
                    generated_messages
                    if generated_messages is not None
                    else list(spec.get("messages", []) or [])
                )
                if generated_setup is not None:
                    counts["generated_contexts"] += 1
                    counts["generated_context_setup"][logical_session_id] = generated_setup
                for sequence, message in enumerate(seed_messages, start=1):
                    if not isinstance(message, Mapping):
                        continue
                    row = AIMessage(
                        session_id=session.session_id,
                        sender_role=_text(message.get("role"), "user"),
                        content=str(message.get("content") or ""),
                        message_metadata={"sequence": sequence},
                    )
                    _stamp(row, _time(message.get("created_at"), current))
                    db.add(row)
                    db.flush()
                    logical_message_id = _text(message.get("id"))
                    if logical_message_id:
                        source_id_map[f"ai_message:{row.message_id}"] = f"ai_message:{logical_message_id}"
                counts["sessions"] += 1

            for document, spec in episode_memory_specs:
                raw_session_ids = spec.get("session_ids", spec.get("level1_session_ids", []))
                if not isinstance(raw_session_ids, (list, tuple)):
                    continue
                resolved_session_ids = [
                    session_id_map.get(_text(raw_session_id), _text(raw_session_id))
                    for raw_session_id in raw_session_ids
                    if _text(raw_session_id)
                ]
                document.level1_session_ids = list(dict.fromkeys(resolved_session_ids))
                refresh_compiled_text(document)

            latest: dict[str, list[tuple[int, str, Any]]] = {}
            for spec in state.get("triage_summaries", []) or []:
                if not isinstance(spec, Mapping):
                    continue
                episode_id = episode_ids.get(_text(spec.get("care_episode_id")))
                if not episode_id:
                    raise RuntimeError("triage fixture references an unprovisioned Care Episode")
                content = str(spec.get("content") or "")
                common = {
                    "care_episode_id": uuid.UUID(episode_id),
                    "patient_id": patient_uuid,
                    "concern": content[:255] or "Evaluation triage summary",
                    "relevant_context": content,
                    "safety_signals": [],
                    "limitations": [],
                    "recommendation": content,
                    "missing_information": [],
                    "unresolved_questions": [],
                    "clinician_review_needed": True,
                    "additional_context": "",
                    "source_ai_session_id": None,
                    "source_conversation_transcript": "",
                }
                status = _text(spec.get("status"), "saved")
                summary = (
                    CareEpisodeTriageSummaryDraft(**common)
                    if status == "draft"
                    else CareEpisodeTriageSummary(version=int(spec.get("version") or 1), **common)
                )
                _stamp(summary, _time(spec.get("recorded_at"), current))
                db.add(summary)
                db.flush()
                summary_id = str(getattr(summary, "triage_summary_id", None) or getattr(summary, "triage_summary_draft_id"))
                logical_summary_id = _text(spec.get("id"))
                manifest.record_triage_summary(logical_summary_id, summary_id)
                source_id_map[f"triage_summary:{summary_id}"] = f"triage_summary:{logical_summary_id}"
                self._persist_progress()
                latest.setdefault(episode_id, []).append((int(spec.get("version") or 1), status, summary))
                counts["triage_summaries"] += 1
            for episode_id, candidates in latest.items():
                saved = [candidate for candidate in candidates if candidate[1] == "saved"]
                if saved:
                    episode = db.get(CareEpisode, uuid.UUID(episode_id))
                    if episode is not None:
                        mark_latest_summary_snapshot(episode, max(saved, key=lambda candidate: candidate[0])[2])
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return memory_ids, memory_scopes, counts

    def _control(
        self,
        step: Mapping[str, Any],
        *,
        patient_id: str,
        episode_ids: dict[str, str],
        memory_ids: dict[str, str],
        manifest: CaseManifest,
    ) -> Mapping[str, Any]:
        from app.db.models import CareEpisode, MemoryDocument
        from app.services.memory_documents import ensure_episode_memory_document, ensure_patient_memory_document, refresh_compiled_text

        operation = _text(step.get("op"))
        logical_id = _text(step.get("memory_id"))
        patient_uuid = uuid.UUID(patient_id)
        db = self._db()
        try:
            if operation == "memory_create":
                scope = _text(step.get("scope"), "patient")
                if scope == "episode":
                    physical_episode = episode_ids.get(_text(step.get("care_episode_id")))
                    episode = db.get(CareEpisode, uuid.UUID(physical_episode)) if physical_episode else None
                    if episode is None or episode.patient_id != patient_uuid or episode.status != "active":
                        raise ValueError("evaluator episode memory authorization failed")
                    document = ensure_episode_memory_document(db, episode)
                else:
                    document = ensure_patient_memory_document(db, patient_uuid)
                manifest.record_memory_document(str(document.document_id))
                memory_ids[logical_id] = str(document.document_id)
                content = str(step.get("content") or "")
                if scope == "patient":
                    fields = dict(document.editable_fields or {})
                    fields[logical_id] = content
                    document.editable_fields = fields
                else:
                    document.level1_description = content
                    document.level2_summary = content
                refresh_compiled_text(document)
                db.commit()
                self._persist_progress()
                return {"transport": "evaluator_db_control", "outcome": "applied", "scope": scope}

            physical_id = memory_ids.get(logical_id)
            document = db.get(MemoryDocument, uuid.UUID(physical_id)) if physical_id else None
            if document is None or document.patient_id != patient_uuid:
                raise ValueError("evaluator memory authorization failed")
            if document.scope == "episode":
                physical_episode = episode_ids.get(_text(step.get("care_episode_id")))
                if str(document.care_episode_id) != str(physical_episode or ""):
                    raise ValueError("evaluator episode memory scope mismatch")
                episode = db.get(CareEpisode, document.care_episode_id)
                if episode is None or episode.status != "active":
                    raise ValueError("evaluator episode memory target is not active")
            if operation in {"memory_edit", "correct_fact"}:
                content = str(step.get("content") or "")
                if document.scope == "patient":
                    fields = dict(document.editable_fields or {})
                    fields[logical_id] = content
                    document.editable_fields = fields
                else:
                    document.level1_description = content
                    document.level2_summary = content
            elif operation in {"memory_archive", "memory_delete", "memory_supersede"}:
                if document.scope == "patient":
                    fields = dict(document.editable_fields or {})
                    fields.pop(logical_id, None)
                    document.editable_fields = fields
                else:
                    document.level1_description = ""
                    document.level2_summary = ""
            else:
                raise ValueError(f"unsupported evaluator memory operation: {operation}")
            refresh_compiled_text(document)
            db.commit()
            return {"transport": "evaluator_db_control", "outcome": "applied", "scope": document.scope}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def load_persisted_messages(
        self,
        case_manifest: CaseManifest,
        *,
        bounded_oversized_session: bool = False,
    ) -> Mapping[str, Any]:
        """Read the durable human-readable messages for one evaluator case."""
        from app.db.models import AISessionCompactionState, AIInternalMessage, AIMessage, ai_message_ordering

        session_aliases = {
            str(physical_id): str(logical_id)
            for logical_id, physical_id in case_manifest.ai_session_ids.items()
        }
        session_ids: list[uuid.UUID] = []
        for physical_id in case_manifest.ai_session_ids.values():
            try:
                session_ids.append(uuid.UUID(str(physical_id)))
            except (ValueError, AttributeError):
                continue
        if not session_ids:
            return {"ai_messages": [], "ai_internal_messages": []}

        db = self._db()
        try:
            message_rows = db.execute(
                select(AIMessage)
                .where(AIMessage.session_id.in_(session_ids))
                .order_by(*ai_message_ordering())
            ).scalars().all()
            internal_rows = db.execute(
                select(AIInternalMessage)
                .where(AIInternalMessage.session_id.in_(session_ids))
                .order_by(
                    AIInternalMessage.event_metadata["sequence"].astext.cast(Integer).asc(),
                    AIInternalMessage.internal_message_id.asc(),
                )
            ).scalars().all()
            # Message sequence numbers restart in every AI session. Preserve the
            # manifest's timeline across sessions, then the query's local order.
            message_rows = _rows_in_session_order(message_rows, session_ids)
            internal_rows = _rows_in_session_order(internal_rows, session_ids)

            if bounded_oversized_session:
                rows_by_session: dict[str, list[Any]] = {}
                for row in message_rows:
                    rows_by_session.setdefault(str(row.session_id), []).append(row)
                final_assistant_ids: set[Any] = set()
                captured_rows: list[Any] = []
                for rows in rows_by_session.values():
                    if rows:
                        captured_rows.append(rows[0])
                    final_assistant = next((row for row in reversed(rows) if str(row.sender_role) == "assistant"), None)
                    if final_assistant is not None:
                        final_assistant_ids.add(final_assistant.message_id)
                        if final_assistant not in captured_rows:
                            captured_rows.append(final_assistant)
                raw_digest = hashlib.sha256()
                raw_message_chars = 0
                for row in message_rows:
                    content = str(row.content or "")
                    raw_message_chars += len(content)
                    raw_digest.update(content.encode("utf-8"))
                messages = []
                for row in captured_rows:
                    content = str(row.content or "")
                    metadata = row.message_metadata if isinstance(row.message_metadata, Mapping) else {}
                    messages.append({
                        "session": session_aliases.get(str(row.session_id), "session"),
                        "role": str(row.sender_role),
                        "sequence": metadata.get("sequence"),
                        "content": content if row.message_id in final_assistant_ids else "[omitted oversized session message]",
                        "content_length": len(content),
                        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "created_at": row.created_at.isoformat() if row.created_at is not None else None,
                    })
                compaction_rows = db.execute(select(AISessionCompactionState).where(AISessionCompactionState.session_id.in_(session_ids))).scalars().all()
                states = {str(row.session_id): row for row in compaction_rows}
                compaction_evidence: list[dict[str, Any]] = []
                for physical_session_id in session_ids:
                    state = states.get(str(physical_session_id))
                    rolling_block = str(getattr(state, "rolling_block", "") or "") if state is not None else ""
                    raw_watermark = str(getattr(state, "source_watermark", "{}") or "{}") if state is not None else "{}"
                    try:
                        parsed_watermark = json.loads(raw_watermark)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed_watermark = {}
                    watermark = {
                        str(key): int(value)
                        for key, value in parsed_watermark.items()
                        if str(key) in {"ai", "internal"} and isinstance(value, int) and not isinstance(value, bool)
                    } if isinstance(parsed_watermark, Mapping) else {}
                    session_rows = rows_by_session.get(str(physical_session_id), [])
                    compaction_evidence.append({
                        "session": session_aliases.get(str(physical_session_id), "session"),
                        "state_present": state is not None,
                        "compacted": bool(rolling_block.strip() and watermark.get("ai", 0) > 0),
                        "raw_message_count": len(session_rows),
                        "raw_message_chars": sum(len(str(row.content or "")) for row in session_rows),
                        "rolling_block_length": len(rolling_block),
                        "rolling_block_sha256": hashlib.sha256(rolling_block.encode("utf-8")).hexdigest() if rolling_block else None,
                        "source_watermark": watermark,
                        "rolling_block_excerpt": rolling_block[:1600] if rolling_block else None,
                        "attempts": int(getattr(state, "attempts", 0) or 0) if state is not None else 0,
                        "last_error_present": bool(str(getattr(state, "last_error", "") or "").strip()) if state is not None else False,
                    })
                return {
                    "ai_messages": messages,
                    "ai_internal_messages": [],
                    "message_capture_status": "bounded",
                    "message_capture": {
                        "mode": "bounded_oversized_session",
                        "raw_ai_message_count": len(message_rows),
                        "raw_ai_message_chars": raw_message_chars,
                        "raw_ai_content_sha256": raw_digest.hexdigest(),
                        "captured_ai_message_count": len(messages),
                        "raw_internal_message_count": len(internal_rows),
                        "captured_internal_message_count": 0,
                    },
                    "compaction_evidence": compaction_evidence,
                }
            messages = [
                {
                    "session": session_aliases.get(str(row.session_id), "session"),
                    "role": str(row.sender_role),
                    "content": str(row.content),
                    "created_at": row.created_at.isoformat() if row.created_at is not None else None,
                }
                for row in message_rows
            ]
            internal_messages = [
                {
                    "session": session_aliases.get(str(row.session_id), "session"),
                    "agent": str(row.agent_name),
                    "event": str(row.event_type),
                    "content": str(row.content),
                    "model_input": str(row.model_input),
                    "metadata": dict(row.event_metadata or {}),
                    "created_at": row.created_at.isoformat() if row.created_at is not None else None,
                }
                for row in internal_rows
            ]
            return {
                "ai_messages": messages,
                "ai_internal_messages": internal_messages,
            }
        finally:
            db.close()

    def provision_case(self, case_id: str, scenario: ScenarioV2 | None) -> ProvisionedFixture:
        patient_id, token = self._register(case_id)
        current_time = _text((scenario.initial_state if scenario else {}).get("current_time")) or None
        case_manifest = self.manifest.begin_case(case_id, patient_id=patient_id, current_time=current_time)
        self._persist_progress()
        resources = build_case_resources(
            run_id=self.manifest.run_id,
            case_id=case_id,
            scenario=scenario,
            catalog_dir=self.catalog_dir,
            qdrant_path=self.qdrant_path,
        )
        if self.manifest.qdrant_endpoint_binding is None:
            raise RuntimeError("Qdrant evaluator endpoint ownership binding is required before resource registration")
        case_manifest.record_qdrant_collection(resources.collection_name)
        self._persist_progress()
        if resources.qdrant_path is not None:
            case_manifest.record_qdrant_path(resources.qdrant_path)
            self._persist_progress()
        case_manifest.record_catalog_path(str(resources.catalog_path))
        self._persist_progress()
        resource_setup = resources.provision(store_factory=self._store_factory)
        isolation_setup = {"status": "not_applicable", "transport": "qdrant_exact_point_probe", "previous_points_checked": 0}
        if self._previous_resources:
            isolation_setup = resources.verify_isolation_against(self._previous_resources[-1])
        self._previous_resources.append(resources)
        if self._activate_case_resources:
            activation_health = self.harness.activate_case_resources(resources)
            activation_setup = {
                "status": "applied",
                "transport": "evaluator_process_configuration_and_restart",
                "restart_observed": activation_health is not None,
            }
        else:
            activation_setup = {
                "status": "deferred",
                "transport": "per_case_service_process",
                "restart_observed": False,
            }
        self._persist_progress()
        episode_ids: dict[str, str] = {}
        source_id_map: dict[str, str] = {}
        specs = list(scenario.initial_state.get("care_episodes", [])) if scenario else []
        specs = specs or [{"id": "__default__", "body_area": "synthetic", "status": "active"}]
        for spec in specs:
            if isinstance(spec, Mapping) and _text(spec.get("status"), "active") == "active":
                logical_id = _text(spec.get("id"))
                episode_ids[logical_id] = self._active_episode(
                    case_id, logical_id, _text(spec.get("body_area"), "synthetic"), token
                )
                case_manifest.record_care_episode(logical_id, episode_ids[logical_id])
                source_id_map[f"care_episode:{episode_ids[logical_id]}"] = f"care_episode:{logical_id}"
                self._persist_progress()
        seeded_memory_ids, memory_scopes, counts = self._seed(
            scenario,
            patient_id,
            episode_ids,
            case_manifest,
            source_id_map,
        )
        memory_ids = dict(seeded_memory_ids)
        generated_setup = counts.get("generated_context_setup", {})
        bounded_message_capture = any(
            isinstance(value, Mapping) and value.get("report_capture") == "bounded"
            for value in generated_setup.values()
        ) if isinstance(generated_setup, Mapping) else False

        def record_memory_item(logical_id: str, physical_id: str) -> None:
            memory_ids[logical_id] = physical_id
            memory_scopes[logical_id] = "patient"
            case_manifest.record_memory_item(logical_id, physical_id)
            self._persist_progress()
            source_id_map[f"memory_item:{physical_id}"] = f"memory_item:{logical_id}"

        def record_ai_session(physical_id: str) -> None:
            logical_id = f"runtime:{len(case_manifest.ai_session_ids) + 1}"
            case_manifest.record_ai_session(logical_id, physical_id)
            self._persist_progress()
            source_id_map[f"conversation:{physical_id}"] = f"conversation:{logical_id}"

        def record_triage_summary(logical_id: str, physical_id: str) -> None:
            case_manifest.record_triage_summary(logical_id, physical_id)
            self._persist_progress()
            source_id_map[f"triage_summary:{physical_id}"] = f"triage_summary:{logical_id}"

        fixture_setup = {
            "status": "provisioned",
            "transport": "evaluator_case_resources_and_production_models",
            "current_time": case_manifest.current_time,
            "resource_activation": activation_setup,
            "care_episodes_created": len(episode_ids),
            "inactive_episodes_seeded": counts["inactive_episodes"],
            "memory_items_seeded": counts["memory_items"],
            "sessions_seeded": counts["sessions"],
            "triage_summaries_seeded": counts["triage_summaries"],
            "generated_contexts": counts.get("generated_contexts", 0),
            "generated_context_setup": dict(generated_setup) if isinstance(generated_setup, Mapping) else {},
            "catalog": {key: value for key, value in resource_setup.items() if key != "transport"},
            "authorized_evidence_count": len(resources.evidence_items),
            "authorized_catalog_count": len(resources.catalog_items),
            "isolation_probe": isolation_setup,
            "operations": {
                "patient_registration": {"transport": "authenticated_product_api", "status": "applied"},
                "active_care_episodes": {"transport": "authenticated_product_api", "status": "applied", "count": len(episode_ids)},
                "inactive_care_episodes": {"transport": "production_models", "status": "applied", "count": counts["inactive_episodes"]},
                "memory_documents_and_items": {"transport": "production_models", "status": "applied", "count": counts["memory_items"]},
                "prior_sessions_and_messages": {"transport": "production_models", "status": "applied", "count": counts["sessions"]},
                "triage_summaries": {"transport": "production_models", "status": "applied", "count": counts["triage_summaries"]},
                "qdrant_and_catalog": {"transport": "evaluator_case_resources", "status": resource_setup.get("status", "failed")},
            },
            "failures": [],
        }
        return ProvisionedFixture(
            patient_id=patient_id,
            token=token,
            episode_ids=episode_ids,
            memory_ids=memory_ids,
            memory_scopes=memory_scopes,
            source_id_map=source_id_map,
            fixture_setup=fixture_setup,
            case_manifest=case_manifest,
            case_resources=resources,
            evaluator_db_control=lambda step: self._control(
                step, patient_id=patient_id, episode_ids=episode_ids, memory_ids=memory_ids, manifest=case_manifest
            ),
            record_memory_item=record_memory_item,
            record_ai_session=record_ai_session,
            record_triage_summary=record_triage_summary,
            load_persisted_messages=lambda: self.load_persisted_messages(
                case_manifest,
                bounded_oversized_session=bounded_message_capture,
            ),
        )
