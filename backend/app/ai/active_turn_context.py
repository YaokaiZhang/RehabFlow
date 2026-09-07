"""Ephemeral active-turn evidence storage.

Packet bodies, triage prompts, and evidence query text are execution-only. This
bounded process-local store lets concurrent evidence branches and the consultant
share those values without placing them in LangGraph checkpoints, ordinary
traces, or durable internal logs.
"""
from __future__ import annotations

from contextvars import ContextVar
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
from uuid import uuid4

from app.ai.evidence_types import EvidenceResult


_MAX_ACTIVE_TURNS = 256
_MAX_SESSION_EVIDENCE_SESSIONS = 256
_MAX_SESSION_TOOL_RECORDS = 8
_SESSION_EVIDENCE_TOOL_NAMES = frozenset(
    {"rehab_exercise_kb_search", "web_search"}
)


@dataclass
class _ActiveTurn:
    evidence_results: list[EvidenceResult]
    session_id: str = ""
    evidence_queries: dict[str, str] = field(default_factory=dict)
    context_packets: dict[str, str] = field(default_factory=dict)
    triaged_context: Any | None = None
    consultant_snapshot: str = ""
    tool_records: list[dict[str, Any]] = field(default_factory=list)
    web_sources: list[dict[str, str]] = field(default_factory=list)
    lock: RLock = field(default_factory=RLock)


_ACTIVE_TURNS: dict[str, _ActiveTurn] = {}
_ACTIVE_TURNS_LOCK = RLock()
_SESSION_TOOL_RECORDS: dict[str, list[dict[str, Any]]] = {}
_ACTIVE_TURN_TRACKER: ContextVar[set[str] | None] = ContextVar(
    "active_turn_tracker",
    default=None,
)


def begin_active_turn_tracking() -> tuple[set[str], Any]:
    tracked_turn_ids: set[str] = set()
    token = _ACTIVE_TURN_TRACKER.set(tracked_turn_ids)
    return tracked_turn_ids, token


def end_active_turn_tracking(token: Any) -> None:
    _ACTIVE_TURN_TRACKER.reset(token)


def create_active_turn(session_id: str | None = None) -> str:
    turn_id = str(uuid4())
    session_key = str(session_id or "").strip()
    with _ACTIVE_TURNS_LOCK:
        if len(_ACTIVE_TURNS) >= _MAX_ACTIVE_TURNS:
            oldest = next(iter(_ACTIVE_TURNS))
            _ACTIVE_TURNS.pop(oldest, None)
        prior_records = [
            dict(record)
            for record in _SESSION_TOOL_RECORDS.get(session_key, [])
        ] if session_key else []
        _ACTIVE_TURNS[turn_id] = _ActiveTurn(
            evidence_results=[],
            session_id=session_key,
            tool_records=prior_records,
        )
    tracked_turn_ids = _ACTIVE_TURN_TRACKER.get()
    if tracked_turn_ids is not None:
        tracked_turn_ids.add(turn_id)
    return turn_id


def _get_turn(turn_id: str) -> _ActiveTurn:
    with _ACTIVE_TURNS_LOCK:
        turn = _ACTIVE_TURNS.get(str(turn_id))
    if turn is None:
        raise KeyError(f"Unknown active turn: {turn_id}")
    return turn


def record_evidence_result(turn_id: str, result: EvidenceResult) -> None:
    turn = _get_turn(turn_id)
    with turn.lock:
        turn.evidence_results.append(result)


def active_evidence_results(turn_id: str | None) -> list[EvidenceResult]:
    if not turn_id:
        return []
    try:
        turn = _get_turn(turn_id)
    except KeyError:
        return []
    with turn.lock:
        return list(turn.evidence_results)


def store_evidence_query(turn_id: str, request_id: str, query: str) -> None:
    """Keep query text execution-only, keyed by metadata-only request identity."""
    turn = _get_turn(turn_id)
    with turn.lock:
        turn.evidence_queries[str(request_id)] = str(query)


def active_evidence_query(turn_id: str | None, request_id: str) -> str | None:
    """Resolve an execution-only evidence query from its active-turn reference."""
    if not turn_id:
        return None
    try:
        turn = _get_turn(turn_id)
    except KeyError:
        return None
    with turn.lock:
        return turn.evidence_queries.get(str(request_id))


def store_triaged_context(turn_id: str, triaged_context: Any) -> None:
    turn = _get_turn(turn_id)
    with turn.lock:
        turn.triaged_context = triaged_context


def active_triaged_context(turn_id: str | None) -> Any | None:
    if not turn_id:
        return None
    try:
        turn = _get_turn(turn_id)
    except KeyError:
        return None
    with turn.lock:
        return turn.triaged_context


def store_context_packets(turn_id: str, context_packets: Mapping[str, str]) -> None:
    turn = _get_turn(turn_id)
    with turn.lock:
        turn.context_packets = {
            str(node_id): str(prompt)
            for node_id, prompt in context_packets.items()
        }


def active_context_packets(turn_id: str | None) -> dict[str, str]:
    if not turn_id:
        return {}
    try:
        turn = _get_turn(turn_id)
    except KeyError:
        return {}
    with turn.lock:
        return dict(turn.context_packets)


def active_context_packet(turn_id: str | None, node_id: str) -> str | None:
    return active_context_packets(turn_id).get(str(node_id))


def store_consultant_snapshot(turn_id: str, snapshot: str) -> None:
    turn = _get_turn(turn_id)
    with turn.lock:
        turn.consultant_snapshot = str(snapshot)


def active_consultant_snapshot(turn_id: str | None) -> str:
    if not turn_id:
        return ""
    try:
        turn = _get_turn(turn_id)
    except KeyError:
        return ""
    with turn.lock:
        return turn.consultant_snapshot


def store_tool_record(turn_id: str, record: Mapping[str, Any]) -> None:
    turn = _get_turn(turn_id)
    with turn.lock:
        turn.tool_records.append(dict(record))


def _remember_session_tool_records(turn: _ActiveTurn) -> None:
    session_id = turn.session_id
    if not session_id:
        return
    with turn.lock:
        records = [
            dict(record)
            for record in turn.tool_records
            if str(record.get("tool_name") or "")
            in _SESSION_EVIDENCE_TOOL_NAMES
            and str(record.get("status") or "") == "ok_nonempty"
            and str(record.get("rendered") or "").strip()
        ]
    if not records:
        return
    with _ACTIVE_TURNS_LOCK:
        if session_id not in _SESSION_TOOL_RECORDS:
            if len(_SESSION_TOOL_RECORDS) >= _MAX_SESSION_EVIDENCE_SESSIONS:
                oldest = next(iter(_SESSION_TOOL_RECORDS))
                _SESSION_TOOL_RECORDS.pop(oldest, None)
            _SESSION_TOOL_RECORDS[session_id] = []
        existing = _SESSION_TOOL_RECORDS[session_id]
        for record in records:
            fingerprint = (
                str(record.get("tool_name") or ""),
                repr(record.get("request")),
                str(record.get("rendered") or ""),
            )
            if any(
                (
                    str(item.get("tool_name") or ""),
                    repr(item.get("request")),
                    str(item.get("rendered") or ""),
                ) == fingerprint
                for item in existing
            ):
                continue
            existing.append(dict(record))
        del existing[:-_MAX_SESSION_TOOL_RECORDS]


def active_tool_records(turn_id: str | None) -> list[dict[str, Any]]:
    if not turn_id:
        return []
    try:
        turn = _get_turn(turn_id)
    except KeyError:
        return []
    with turn.lock:
        return [dict(record) for record in turn.tool_records]


def store_web_sources(turn_id: str, sources: list[Mapping[str, str]]) -> None:
    turn = _get_turn(turn_id)
    with turn.lock:
        turn.web_sources = [dict(source) for source in sources]


def active_web_sources(turn_id: str | None) -> list[dict[str, str]]:
    if not turn_id:
        return []
    try:
        turn = _get_turn(turn_id)
    except KeyError:
        return []
    with turn.lock:
        return [dict(source) for source in turn.web_sources]


def clear_active_turn(turn_id: str | None) -> None:
    if not turn_id:
        return
    with _ACTIVE_TURNS_LOCK:
        turn = _ACTIVE_TURNS.pop(str(turn_id), None)
    if turn is None:
        return
    _remember_session_tool_records(turn)
