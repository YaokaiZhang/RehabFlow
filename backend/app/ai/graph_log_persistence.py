"""Redacted internal graph-log persistence boundary.

State contract: reads projected internal trace events from a completed graph
output and writes only redacted content plus allowlisted metadata. Session and
patient identifiers are required ownership keys; checkpoint state is untouched.
"""
from __future__ import annotations

import uuid
from typing import Any

def _add_internal_log(
    db,
    *,
    session_id: uuid.UUID | None,
    patient_id: uuid.UUID | None,
    agent_name: str,
    event_type: str,
    content: str = "",
    event_metadata: dict[str, Any] | None = None,
    preserve_content: bool = False,
) -> None:
    from app.db.models import AIInternalMessage
    from app.services.ai_turn_persistence import (
        redact_internal_text,
        safe_internal_metadata,
    )

    row = AIInternalMessage(
        session_id=session_id,
        patient_id=patient_id,
        agent_name=agent_name[:64],
        event_type=event_type[:64],
        content=redact_internal_text(content),
        model_input=redact_internal_text(str((event_metadata or {}).pop("model_input", "") or "")),
        event_metadata=safe_internal_metadata(event_metadata),
    )
    del preserve_content
    db.add(row)

def _persist_internal_graph_logs(db, session_id: uuid.UUID, patient_id: uuid.UUID, output: dict[str, Any], *, preserve_content: bool = False) -> None:
    from app.services.ai_turn_persistence import project_internal_trace_events
    from sqlalchemy import Integer, func, select, text
    from app.db.models import AIInternalMessage

    next_internal_sequence = 1
    execute = getattr(db, "execute", None)
    if callable(execute):
        try:
            bind = getattr(db, "bind", None)
            if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
                execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(CAST(:session_id AS text), 0))"
                    ),
                    {"session_id": str(session_id)},
                )
            current = execute(
                select(func.max(AIInternalMessage.event_metadata["sequence"].astext.cast(Integer))).where(
                    AIInternalMessage.session_id == session_id
                )
            ).scalar()
            next_internal_sequence = int(current or 0) + 1
        except Exception:
            pass

    for event in project_internal_trace_events(output, include_content=True):
        event_metadata = dict(event.get("metadata") or {})
        event_metadata["sequence"] = next_internal_sequence
        next_internal_sequence += 1
        _add_internal_log(
            db,
            session_id=session_id,
            patient_id=patient_id,
            agent_name=str(event["agent_name"]),
            event_type=str(event["event_type"]),
            event_metadata={
                **event_metadata,
                "model_input": str(event.get("model_input") or ""),
            },
            content=str(event.get("content") or ""),
            preserve_content=preserve_content,
        )
