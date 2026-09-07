from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import Integer, func, select, text
from sqlalchemy.orm import Session

from app.ai.context_types import ContextSourceCard, MAX_CONTEXT_MODEL_TOKENS
from app.ai.model_input_budget import model_messages_estimated_tokens
from app.db.models import (
    AIInternalMessage,
    AIMessage,
    AISessionCompactionState,
    ai_internal_message_ordering,
    ai_message_ordering,
)


LATEST_PROTECTED_COUNT = 5
EPISODE_MEMORY_TARGET_TOKENS = 32_000


class SessionCompactionError(RuntimeError):
    """Raised when the required LLM session compaction did not complete."""


@dataclass(frozen=True)
class SessionContextView:
    cards: tuple[ContextSourceCard, ...]
    compacted: bool
    estimated_tokens: int
    source_watermark: str


def _sequence(row: object, metadata_name: str) -> int | None:
    metadata = getattr(row, metadata_name, None)
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("sequence")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ordered_rows(rows: list[object], metadata_name: str) -> list[object]:
    def sort_key(row: object) -> tuple[bool, int, str]:
        sequence = _sequence(row, metadata_name)
        identifier = getattr(row, "message_id", None) or getattr(row, "internal_message_id", None)
        return (
            sequence is None,
            sequence if sequence is not None else 0,
            str(identifier or ""),
        )

    return sorted(rows, key=sort_key)


def _latest_protected_rows(rows: list[object], metadata_name: str) -> list[object]:
    ordered = _ordered_rows(rows, metadata_name)
    sequenced = [row for row in ordered if _sequence(row, metadata_name) is not None]
    if sequenced:
        return sequenced[-LATEST_PROTECTED_COUNT:]
    return ordered[-LATEST_PROTECTED_COUNT:]


def _watermark_value(raw: str | None) -> dict[str, int]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("ai", "internal"):
        try:
            if value.get(key) is not None:
                result[key] = int(value[key])
        except (TypeError, ValueError):
            continue
    return result


def _watermark_text(ai_sequence: int | None, internal_sequence: int | None) -> str:
    return json.dumps(
        {
            "ai": ai_sequence if ai_sequence is not None else -1,
            "internal": internal_sequence if internal_sequence is not None else -1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _after_watermark(row: object, metadata_name: str, watermark: int | None) -> bool:
    sequence = _sequence(row, metadata_name)
    if watermark is None:
        return True
    return sequence is not None and sequence > watermark


def _max_sequence(rows: list[object], metadata_name: str, default: int | None) -> int | None:
    values = [
        sequence
        for row in rows
        if (sequence := _sequence(row, metadata_name)) is not None
    ]
    return max(values) if values else default


def _internal_is_tool_event(row: AIInternalMessage) -> bool:
    event_type = str(getattr(row, "event_type", "") or "").strip().casefold()
    return event_type in {"tool_call", "tool_result"} or event_type.endswith("_tool_call") or event_type.endswith("_tool_result")


def _message_text(row: object) -> str:
    return str(getattr(row, "content", "") or "")


def _source_text(row: object, *, internal: bool = False) -> str:
    sequence = _sequence(row, "event_metadata" if internal else "message_metadata")
    if internal:
        metadata = getattr(row, "event_metadata", None)
        tool_arguments = metadata.get("tool_arguments") if isinstance(metadata, Mapping) else None
        arguments_text = (
            "\nTool call arguments: "
            + json.dumps(tool_arguments, ensure_ascii=False, sort_keys=True, default=str)
            if tool_arguments
            else ""
        )
        return (
            f"Internal event sequence={sequence if sequence is not None else 'unknown'} "
            f"agent={getattr(row, 'agent_name', '')} "
            f"type={getattr(row, 'event_type', '')}:\nTool result content:\n{_message_text(row)}{arguments_text}"
        ).strip()
    return (
        f"{getattr(row, 'sender_role', '')} sequence={sequence if sequence is not None else 'unknown'}:\n"
        f"{_message_text(row)}"
    ).strip()


def _context_tokens(
    rolling_block: str,
    ai_rows: list[AIMessage],
    internal_rows: list[AIInternalMessage],
    *,
    current_user_message: str = "",
    durable_cards: list[ContextSourceCard] | None = None,
) -> int:
    source_parts: list[str] = []
    if rolling_block.strip():
        source_parts.append(
            f"Session compaction source id=session_compaction:rolling:\n{rolling_block.strip()}"
        )
    source_parts.extend(_source_text(row, internal=False) for row in ai_rows)
    source_parts.extend(_source_text(row, internal=True) for row in internal_rows)
    source_parts.extend(card.to_prompt_text() for card in (durable_cards or []))
    messages = [
        SystemMessage(
            content=(
                "RehabFlow serialized context admission envelope. Preserve source labels, "
                "tool arguments, tool results, and the active durable context."
            )
        ),
        HumanMessage(
            content=(
                "Active durable context and current-session sources:\n"
                + "\n\n".join(source_parts)
                + (f"\n\nCurrent user message:\n{current_user_message.strip()}" if current_user_message.strip() else "")
            )
        ),
    ]
    return model_messages_estimated_tokens(messages)


def _invoke_with_timeout(model: Any, messages: list[Any], timeout_seconds: float) -> Any:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rehab-session-compaction")
    future = executor.submit(model.invoke, messages)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"session compaction timed out after {timeout_seconds} seconds") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response.strip()
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, Mapping) else str(item)
            for item in content
        ).strip()
    return str(content or "").strip()


def _structured_block(response: Any) -> str:
    value = getattr(response, "rolling_block", None)
    if value is not None:
        return str(value).strip()
    if isinstance(response, Mapping) and response.get("rolling_block") is not None:
        return str(response["rolling_block"]).strip()
    text = _response_text(response)
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping) and parsed.get("rolling_block") is not None:
            return str(parsed["rolling_block"]).strip()
    return text


class SessionCompactionAgent:
    def __init__(self, model: Any, *, timeout_seconds: float = 120.0) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds

    def compact(
        self,
        *,
        rolling_block: str,
        source_chunk: str,
        previous_error: str = "",
        reserve_attempt: Callable[[], int] | None = None,
        record_error: Callable[[str], None] | None = None,
    ) -> str:
        if not source_chunk.strip():
            raise SessionCompactionError("session compaction had no eligible source chunk")
        retry_note = (
            f"\nThe previous attempt failed with this error. Correct it and return only the compacted block:\n{previous_error}\n"
            if previous_error
            else ""
        )
        messages = [
            SystemMessage(
                content=(
                    "You are the RehabFlow Session Compaction Agent. Compact the supplied older "
                    "current-session AI messages and tool-call/tool-result events as one coherent "
                    "rolling context block. Preserve clinically relevant facts, decisions, "
                    "uncertainties, safety boundaries, and chronology. Do not add facts. The "
                    "output is advisory and may be approximately 32,000 tokens; do not truncate "
                    "or reject it because of a soft target. Return only the compacted block."
                )
            ),
            HumanMessage(
                content=(
                    f"Existing rolling block:\n{rolling_block.strip() or '(none)'}\n\n"
                    f"New older source chunk:\n{source_chunk.strip()}\n"
                    f"{retry_note}"
                )
            ),
        ]
        last_error = ""
        local_attempt = 0
        while local_attempt < 3:
            attempt = reserve_attempt() if reserve_attempt is not None else local_attempt + 1
            local_attempt = attempt
            try:
                response = _invoke_with_timeout(self.model, messages, self.timeout_seconds)
                block = _structured_block(response)
                if not block:
                    raise ValueError("session compaction returned an empty block")
                return block
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if record_error is not None:
                    record_error(last_error)
                if attempt >= 3:
                    break
                messages[-1] = HumanMessage(
                    content=(
                        f"Existing rolling block:\n{rolling_block.strip() or '(none)'}\n\n"
                        f"New older source chunk:\n{source_chunk.strip()}\n\n"
                        f"Previous attempt error:\n{last_error}\n"
                        "Return only a non-empty compacted block."
                    )
                )
        raise SessionCompactionError(
            f"session compaction failed after 3 attempts: {last_error}"
        )


def _active_message_type(message: object) -> str:
    return str(getattr(message, "type", "") or "").strip().lower()


def _active_message_metadata(message: object) -> Mapping[str, Any]:
    value = getattr(message, "additional_kwargs", None)
    return value if isinstance(value, Mapping) else {}


def _active_is_history_message(message: object) -> bool:
    return _active_message_metadata(message).get("rehab_session_history") is True


def _active_is_compaction_message(message: object) -> bool:
    return _active_message_metadata(message).get("rehab_session_compaction") is True


def _active_message_content(message: object) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)


def _active_numeric_sequence(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _active_message_sequence(message: object) -> int | None:
    for metadata_name in ("additional_kwargs", "response_metadata"):
        metadata = getattr(message, metadata_name, None)
        if not isinstance(metadata, Mapping):
            continue
        for key in ("sequence", "rehab_session_sequence"):
            sequence = _active_numeric_sequence(metadata.get(key))
            if sequence is not None:
                return sequence
    return None


def _active_internal_sequence(event: object) -> int | None:
    if not isinstance(event, Mapping):
        return None
    metadata = event.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    return _active_numeric_sequence(metadata.get("sequence"))


def _active_internal_event_type(event: object) -> str:
    if not isinstance(event, Mapping):
        return ""
    return str(event.get("event_type") or "").strip().casefold()


def _active_internal_is_tool_event(event: object) -> bool:
    event_type = _active_internal_event_type(event)
    return (
        event_type in {"tool_call", "tool_result"}
        or event_type.endswith("_tool_call")
        or event_type.endswith("_tool_result")
    )


def _active_event_with_sequence(
    event: Mapping[str, Any],
    sequence: int,
) -> Mapping[str, Any]:
    metadata = event.get("metadata")
    copied = dict(event)
    copied["metadata"] = {
        **(dict(metadata) if isinstance(metadata, Mapping) else {}),
        "sequence": sequence,
    }
    return copied


def _active_persisted_internal_sequence_base(
    session_factory: Callable[[], Any] | None,
    session_id: UUID | None,
) -> int:
    if session_factory is None or session_id is None:
        return 0
    db = session_factory()
    try:
        execute = getattr(db, "execute", None)
        if not callable(execute):
            return 0
        current = execute(
            select(
                func.max(
                    AIInternalMessage.event_metadata["sequence"].astext.cast(Integer)
                )
            ).where(AIInternalMessage.session_id == session_id)
        ).scalar()
        return int(current or 0)
    except Exception:
        rollback = getattr(db, "rollback", None)
        if callable(rollback):
            rollback()
        return 0
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


def _active_internal_stream_needs_ordered_sequences(
    events: list[Mapping[str, Any]],
) -> bool:
    return any(
        _active_internal_is_tool_event(event)
        and _active_internal_sequence(event) is None
        for event in events
    )


def _active_folded_watermark(
    messages: list[Any],
    eligible_positions: list[int],
    internal_events: list[Mapping[str, Any]],
    previous_watermark: str,
) -> str:
    """Advance only over numeric sources included in this compaction call."""
    previous = _watermark_value(previous_watermark)
    ai_sequences: list[int] = []
    internal_sequences: list[int] = []
    for index in eligible_positions:
        message = messages[index]
        sequence = _active_message_sequence(message)
        if sequence is None:
            continue
        if _active_message_type(message) == "tool":
            internal_sequences.append(sequence)
        else:
            ai_sequences.append(sequence)
    internal_sequences.extend(
        sequence
        for event in internal_events
        if _active_internal_is_tool_event(event)
        and (sequence := _active_internal_sequence(event)) is not None
        and _active_internal_source_text(event)
    )
    if not ai_sequences and not internal_sequences:
        return previous_watermark
    ai_candidates = list(ai_sequences)
    internal_candidates = list(internal_sequences)
    if previous.get("ai") is not None:
        ai_candidates.append(previous["ai"])
    if previous.get("internal") is not None:
        internal_candidates.append(previous["internal"])
    return _watermark_text(
        max(ai_candidates, default=previous.get("ai")),
        max(internal_candidates, default=previous.get("internal")),
    )


def _active_tool_calls(message: object) -> object:
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        return tool_calls
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, Mapping):
        return additional_kwargs.get("tool_calls") or []
    return []


def _active_source_text(message: object) -> str:
    message_type = _active_message_type(message)
    if message_type == "ai":
        tool_calls = _active_tool_calls(message)
        tool_call_text = (
            "\nTool call arguments:\n"
            + json.dumps(tool_calls, ensure_ascii=False, sort_keys=True, default=str)
            if tool_calls
            else ""
        )
        return (
            "Assistant message:\n"
            + _active_message_content(message)
            + tool_call_text
        ).strip()
    if message_type == "tool":
        return "Tool result content:\n" + _active_message_content(message)
    if message_type == "human" and _active_is_history_message(message):
        prefix = "Session compaction block:\n" if _active_is_compaction_message(message) else "User message:\n"
        return prefix + _active_message_content(message)
    return ""


def _active_internal_source_text(event: object) -> str:
    if not _active_internal_is_tool_event(event):
        return ""
    assert isinstance(event, Mapping)
    event_type = _active_internal_event_type(event)
    metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
    arguments = metadata.get("tool_arguments")
    argument_text = (
        "\nTool call arguments:\n"
        + json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        if arguments
        else ""
    )
    return (
        f"Internal {event_type} result content:\n"
        f"{str(event.get('content') or '')}{argument_text}"
    ).strip()


def _active_compaction_session_id(session_id: object) -> UUID | None:
    if session_id is None:
        return None
    try:
        return UUID(str(session_id))
    except (TypeError, ValueError):
        return None


def _active_folded_positions(
    messages: list[Any],
    watermark: dict[str, int],
) -> set[int]:
    folded: set[int] = set()
    for index, message in enumerate(messages):
        message_type = _active_message_type(message)
        if message_type == "tool":
            stream = "internal"
        elif message_type in {"ai", "human"} and _active_is_history_message(message):
            stream = "ai"
        else:
            continue
        sequence = _active_message_sequence(message)
        boundary = watermark.get(stream)
        if sequence is not None and boundary is not None and sequence <= boundary:
            folded.add(index)
            continue
        if message_type != "tool" or sequence is not None:
            continue
        preceding_ai = [
            ai_index
            for ai_index, candidate in enumerate(messages[:index])
            if _active_message_type(candidate) == "ai"
        ]
        if preceding_ai:
            ai_sequence = _active_message_sequence(messages[preceding_ai[-1]])
            if ai_sequence is not None and ai_sequence <= watermark.get("ai", -1):
                folded.add(index)
    return folded


def _active_unfolded_internal_events(
    events: list[Mapping[str, Any]],
    watermark: dict[str, int],
    *,
    sequence_base: int | None = None,
) -> list[Mapping[str, Any]]:
    boundary = watermark.get("internal")
    unfolded: list[Mapping[str, Any]] = []
    ordered = sequence_base is not None
    for index, event in enumerate(events, start=1):
        if ordered:
            candidate = _active_event_with_sequence(event, sequence_base + index)
            sequence = sequence_base + index
        else:
            candidate = event
            sequence = _active_internal_sequence(event)
        if boundary is None or sequence is None or sequence > boundary:
            unfolded.append(candidate)
    return unfolded


def prepare_active_model_messages(
    messages: list[Any] | tuple[Any, ...],
    *,
    model: Any | None,
    timeout_seconds: float = 120.0,
    session_factory: Callable[[], Any] | None = None,
    session_id: object | None = None,
    internal_events: list[Mapping[str, Any]] | None = None,
) -> tuple[list[Any], int]:
    """Admit an active model payload, compacting one older AI/tool chunk when needed."""
    prepared = list(messages)
    active_session_id = _active_compaction_session_id(session_id)
    rolling_block = ""
    source_watermark = "{}"
    expected_rolling_block = rolling_block
    expected_watermark = source_watermark
    active_watermark: dict[str, int] = {}
    if session_factory is not None and active_session_id is not None:
        state_db = session_factory()
        try:
            state = _state_for_update(state_db, active_session_id)
            rolling_block = str(getattr(state, "rolling_block", "") or "")
            source_watermark = str(getattr(state, "source_watermark", "{}") or "{}")
            expected_rolling_block = rolling_block
            expected_watermark = source_watermark
            active_watermark = _watermark_value(source_watermark)
        finally:
            close = getattr(state_db, "close", None)
            if callable(close):
                close()
        folded_positions = _active_folded_positions(
            prepared,
            active_watermark,
        )
        if folded_positions:
            prepared = [
                message
                for index, message in enumerate(prepared)
                if index not in folded_positions
            ]
    raw_internal_events = list(internal_events or ())
    internal_sequence_base = (
        _active_persisted_internal_sequence_base(
            session_factory,
            active_session_id,
        )
        if _active_internal_stream_needs_ordered_sequences(raw_internal_events)
        else None
    )
    eligible_internal_events = _active_unfolded_internal_events(
        raw_internal_events,
        active_watermark,
        sequence_base=internal_sequence_base,
    )
    estimated = model_messages_estimated_tokens(prepared)
    if estimated <= MAX_CONTEXT_MODEL_TOKENS:
        return prepared, estimated
    if model is None:
        raise SessionCompactionError(
            "active model input exceeded the ceiling without a compaction model"
        )

    ai_positions = [
        index
        for index, message in enumerate(prepared)
        if _active_message_type(message) == "ai"
    ]
    protected_ai = set(ai_positions[-LATEST_PROTECTED_COUNT:])
    protected_tool: set[int] = set()
    protected_history: set[int] = set()
    for index, message in enumerate(prepared):
        if _active_message_type(message) != "tool":
            continue
        preceding_ai = [ai_index for ai_index in ai_positions if ai_index < index]
        if preceding_ai and preceding_ai[-1] in protected_ai:
            protected_tool.add(index)
    for ai_index in protected_ai:
        prior = ai_index - 1
        while prior >= 0 and _active_message_type(prepared[prior]) == "tool":
            prior -= 1
        if (
            prior >= 0
            and _active_message_type(prepared[prior]) == "human"
            and _active_is_history_message(prepared[prior])
            and not _active_is_compaction_message(prepared[prior])
        ):
            protected_history.add(prior)

    eligible_positions = [
        index
        for index, message in enumerate(prepared)
        if (
            (
                _active_message_type(message) in {"ai", "tool"}
                or _active_is_history_message(message)
            )
            and not _active_is_compaction_message(message)
        )
        and index not in protected_ai
        and index not in protected_tool
        and index not in protected_history
    ]
    source_parts = [_active_source_text(prepared[index]) for index in eligible_positions]
    source_parts.extend(
        text
        for event in eligible_internal_events
        if (text := _active_internal_source_text(event))
    )
    source_chunk = "\n\n".join(part for part in source_parts if part.strip())
    if not source_chunk:
        raise SessionCompactionError(
            "active model input exceeded the ceiling but has no older AI/tool source chunk"
        )

    def reserve_attempt() -> int:
        if session_factory is None or active_session_id is None:
            return 1
        attempt_db = session_factory()
        try:
            return _reserve_compaction_attempt(
                attempt_db,
                active_session_id,
                rolling_block=rolling_block,
                source_watermark=expected_watermark,
            )
        finally:
            close = getattr(attempt_db, "close", None)
            if callable(close):
                close()

    def record_error(error: str) -> None:
        if session_factory is None or active_session_id is None:
            return
        error_db = session_factory()
        try:
            _record_compaction_error(error_db, active_session_id, error)
        finally:
            close = getattr(error_db, "close", None)
            if callable(close):
                close()

    compacted_block = SessionCompactionAgent(
        model,
        timeout_seconds=timeout_seconds,
    ).compact(
        rolling_block=rolling_block,
        source_chunk=source_chunk,
        reserve_attempt=reserve_attempt if session_factory and active_session_id else None,
        record_error=record_error if session_factory and active_session_id else None,
    )
    rebuilt: list[Any] = []
    eligible_set = set(eligible_positions)
    replacement = HumanMessage(
        content="Session compaction source id=session_compaction:active:\n" + compacted_block,
        additional_kwargs={
            "rehab_session_history": True,
            "rehab_session_compaction": True,
        },
    )
    inserted = False
    for index, message in enumerate(prepared):
        if index in eligible_set:
            if not inserted:
                rebuilt.append(replacement)
                inserted = True
            continue
        rebuilt.append(message)

    final_estimated = model_messages_estimated_tokens(rebuilt)
    if final_estimated > MAX_CONTEXT_MODEL_TOKENS:
        raise SessionCompactionError(
            "active model input remains above the serialized ceiling after compaction"
        )
    if session_factory is not None and active_session_id is not None:
        next_watermark = _active_folded_watermark(
            prepared,
            eligible_positions,
            eligible_internal_events,
            expected_watermark,
        )
        state_db = session_factory()
        try:
            replaced = _replace_compaction_state(
                state_db,
                active_session_id,
                expected_rolling_block=expected_rolling_block,
                expected_watermark=expected_watermark,
                rolling_block=compacted_block,
                source_watermark=next_watermark,
            )
        finally:
            close = getattr(state_db, "close", None)
            if callable(close):
                close()
        if not replaced:
            raise SessionCompactionError(
                "active session compaction state changed concurrently; retry the active turn"
            )
    return rebuilt, final_estimated




def _load_session_rows(db: Session, session_id: UUID) -> tuple[list[AIMessage], list[AIInternalMessage]]:
    return _load_session_rows_with_exclusions(db, session_id)



def _load_session_rows_with_exclusions(
    db: Session,
    session_id: UUID,
    *,
    exclude_message_ids: set[UUID] | None = None,
    exclude_message_contents: set[str] | None = None,
) -> tuple[list[AIMessage], list[AIInternalMessage]]:
    excluded_ids = exclude_message_ids or set()
    execute = getattr(db, "execute", None)
    if not callable(execute):
        query = db.query(AIMessage)
        criteria = [
            AIMessage.session_id == session_id,
            AIMessage.sender_role.in_(("user", "assistant")),
        ]
        if excluded_ids:
            criteria.append(~AIMessage.message_id.in_(excluded_ids))
        query = query.filter(*criteria)
        ai_rows = list(query.order_by(*ai_message_ordering()).all())
        try:
            internal_query = db.query(AIInternalMessage).filter(
                AIInternalMessage.session_id == session_id
            )
            internal_rows = list(
                internal_query.order_by(*ai_internal_message_ordering()).all()
            )
        except Exception:
            internal_rows = []
        return _ordered_rows(ai_rows, "message_metadata"), _ordered_rows(
            internal_rows, "event_metadata"
        )
    ai_query = (
        select(AIMessage)
        .where(AIMessage.session_id == session_id)
        .where(AIMessage.sender_role.in_(("user", "assistant")))
    )
    if excluded_ids:
        ai_query = ai_query.where(~AIMessage.message_id.in_(excluded_ids))
    ai_rows = list(
        db.execute(
            ai_query.order_by(*ai_message_ordering())
        ).scalars().all()
    )
    internal_rows = list(
        db.execute(
            select(AIInternalMessage)
            .where(AIInternalMessage.session_id == session_id)
            .order_by(*ai_internal_message_ordering())
        ).scalars().all()
    )
    return _ordered_rows(ai_rows, "message_metadata"), _ordered_rows(internal_rows, "event_metadata")


def _state_for_update(db: Session, session_id: UUID) -> AISessionCompactionState | None:
    if not callable(getattr(db, "execute", None)):
        return None
    return db.execute(
        select(AISessionCompactionState)
        .where(AISessionCompactionState.session_id == session_id)
    ).scalar_one_or_none()


def _lock_compaction_session(db: Session, session_id: UUID) -> None:
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return
    db.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(CAST(:session_id AS text), 0))"
        ),
        {"session_id": str(session_id)},
    )


def _reserve_compaction_attempt(
    db: Session,
    session_id: UUID,
    *,
    rolling_block: str,
    source_watermark: str,
) -> int:
    """Reserve a lifetime attempt in an independent committed transaction."""
    bind = db.get_bind()
    with Session(bind=bind) as claim_db:
        _lock_compaction_session(claim_db, session_id)
        state = claim_db.execute(
            select(AISessionCompactionState)
            .where(AISessionCompactionState.session_id == session_id)
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            state = AISessionCompactionState(
                session_id=session_id,
                rolling_block=rolling_block,
                source_watermark=source_watermark,
                last_error="",
                attempts=1,
            )
            claim_db.add(state)
            attempt = 1
        else:
            current = int(state.attempts or 0)
            if current >= 3:
                raise SessionCompactionError(
                    "session compaction lifetime attempt budget exhausted"
                )
            state.attempts = current + 1
            attempt = state.attempts
        claim_db.commit()
        return attempt


def _record_compaction_error(db: Session, session_id: UUID, error: str) -> None:
    bind = db.get_bind()
    with Session(bind=bind) as error_db:
        _lock_compaction_session(error_db, session_id)
        state = error_db.execute(
            select(AISessionCompactionState)
            .where(AISessionCompactionState.session_id == session_id)
            .with_for_update()
        ).scalar_one_or_none()
        if state is not None:
            state.last_error = str(error)[:4000]
            error_db.commit()


def _card_for_ai(row: AIMessage) -> ContextSourceCard:
    sequence = _sequence(row, "message_metadata")
    return ContextSourceCard(
        source_id=f"ai_message:{row.message_id}",
        source_type="session_message",
        scope="session",
        title=str(row.sender_role),
        text=_message_text(row),
        metadata={"sender_role": str(row.sender_role), "sequence": sequence},
        priority_hint="normal",
    )


def _card_for_internal(row: AIInternalMessage) -> ContextSourceCard:
    sequence = _sequence(row, "event_metadata")
    event_metadata = getattr(row, "event_metadata", None)
    tool_arguments = (
        event_metadata.get("tool_arguments")
        if isinstance(event_metadata, Mapping)
        else None
    )
    return ContextSourceCard(
        source_id=f"ai_internal:{row.internal_message_id}",
        source_type="session_internal",
        scope="session",
        title=f"{row.agent_name}:{row.event_type}",
        text=_source_text(row, internal=True),
        metadata={
            "agent_name": str(row.agent_name),
            "event_type": str(row.event_type),
            "sequence": sequence,
            "tool_arguments": tool_arguments,
        },
        priority_hint="normal",
    )


def _build_cards(
    session_id: UUID,
    *,
    rolling_block: str,
    ai_rows: list[AIMessage],
    internal_rows: list[AIInternalMessage],
    watermark: dict[str, int],
    include_all_ai: bool,
) -> list[ContextSourceCard]:
    cards: list[ContextSourceCard] = []
    if rolling_block.strip():
        cards.append(
            ContextSourceCard(
                source_id=f"session_compaction:{session_id}",
                source_type="session_compacted_block",
                scope="session",
                title="Compacted older session context",
                text=rolling_block,
                metadata={"session_id": str(session_id)},
                priority_hint="high",
            )
        )
    ai_candidates = (
        ai_rows
        if include_all_ai
        else [
            row for row in ai_rows
            if _after_watermark(row, "message_metadata", watermark.get("ai"))
        ]
    )
    internal_candidates = [] if include_all_ai else [
        row for row in internal_rows
        if _internal_is_tool_event(row)
        and _after_watermark(row, "event_metadata", watermark.get("internal"))
    ]
    protected_ai = _latest_protected_rows(ai_rows, "message_metadata")
    protected_internal = _latest_protected_rows(internal_rows, "event_metadata")
    seen_ai: set[UUID] = set()
    seen_internal: set[UUID] = set()
    for row in [*ai_candidates, *protected_ai]:
        if row.message_id in seen_ai:
            continue
        seen_ai.add(row.message_id)
        cards.append(_card_for_ai(row))
    for row in [*internal_candidates, *protected_internal]:
        if row.internal_message_id in seen_internal:
            continue
        seen_internal.add(row.internal_message_id)
        cards.append(_card_for_internal(row))
    return cards


def _replace_compaction_state(
    db: Session,
    session_id: UUID,
    *,
    expected_rolling_block: str,
    expected_watermark: str,
    rolling_block: str,
    source_watermark: str,
) -> bool:
    """Replace the rolling block only if the state read before provider work is unchanged."""
    bind = db.get_bind()
    with Session(bind=bind) as replace_db:
        _lock_compaction_session(replace_db, session_id)
        state = replace_db.execute(
            select(AISessionCompactionState)
            .where(AISessionCompactionState.session_id == session_id)
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            if expected_rolling_block or expected_watermark != "{}":
                return False
            state = AISessionCompactionState(
                session_id=session_id,
                rolling_block=rolling_block,
                source_watermark=source_watermark,
                last_error="",
                attempts=0,
            )
            replace_db.add(state)
        else:
            if (
                str(state.rolling_block or "") != expected_rolling_block
                or str(state.source_watermark or "{}") != expected_watermark
            ):
                return False
            state.rolling_block = rolling_block
            state.source_watermark = source_watermark
            state.last_error = ""
            state.attempts = 0
        replace_db.commit()
        return True


def prepare_session_context(
    db: Session,
    session_id: UUID,
    *,
    model: Any | None,
    timeout_seconds: float = 120.0,
    current_user_message: str = "",
    exclude_message_ids: set[UUID] | None = None,
    exclude_message_contents: set[str] | None = None,
    durable_cards: list[ContextSourceCard] | None = None,
    _retry_on_state_conflict: bool = True,
) -> SessionContextView:
    ai_rows, internal_rows = _load_session_rows_with_exclusions(
        db,
        session_id,
        exclude_message_ids=exclude_message_ids,
        exclude_message_contents=exclude_message_contents,
    )
    state = _state_for_update(db, session_id)
    watermark = _watermark_value(getattr(state, "source_watermark", None))
    rolling_block = str(getattr(state, "rolling_block", "") or "")
    include_all_ai = state is None
    ai_for_size = (
        ai_rows
        if include_all_ai
        else [
            row for row in ai_rows
            if _after_watermark(row, "message_metadata", watermark.get("ai"))
        ]
    )
    protected_ai = _latest_protected_rows(ai_rows, "message_metadata")
    protected_internal = _latest_protected_rows(internal_rows, "event_metadata")
    internal_for_size = [
        row
        for row in internal_rows
        if _internal_is_tool_event(row)
        and _after_watermark(row, "event_metadata", watermark.get("internal"))
        and row not in protected_internal
    ]
    estimated = _context_tokens(
        rolling_block,
        ai_for_size,
        [*internal_for_size, *protected_internal],
        current_user_message=current_user_message,
        durable_cards=durable_cards,
    )
    if estimated <= MAX_CONTEXT_MODEL_TOKENS:
        cards = _build_cards(
            session_id,
            rolling_block=rolling_block,
            ai_rows=ai_rows,
            internal_rows=internal_rows,
            watermark=watermark,
            include_all_ai=include_all_ai,
        )
        return SessionContextView(
            cards=tuple(cards),
            compacted=bool(rolling_block),
            estimated_tokens=estimated,
            source_watermark=str(getattr(state, "source_watermark", "") or ""),
        )

    eligible_ai = [
        row for row in ai_for_size
        if row not in protected_ai
    ]
    eligible_internal = [
        row for row in internal_for_size
        if _internal_is_tool_event(row) and row not in protected_internal
    ]
    if not eligible_ai and not eligible_internal:
        raise SessionCompactionError(
            "session compaction required but no older AIMessage or tool event is eligible"
        )
    if model is None:
        raise SessionCompactionError("session compaction model is not configured")
    source_rows: list[tuple[str, object]] = [
        *[("ai", row) for row in eligible_ai],
        *[("internal", row) for row in eligible_internal],
    ]
    source_rows.sort(
        key=lambda item: (
            _sequence(
                item[1],
                "event_metadata" if item[0] == "internal" else "message_metadata",
            ) is None,
            _sequence(
                item[1],
                "event_metadata" if item[0] == "internal" else "message_metadata",
            ) or 0,
            item[0],
        )
    )
    source_chunk = "\n\n".join(
        _source_text(row, internal=kind == "internal")
        for kind, row in source_rows
    )
    previous_error = str(getattr(state, "last_error", "") or "")
    expected_rolling_block = rolling_block
    expected_watermark = str(getattr(state, "source_watermark", "{}") or "{}")
    agent = SessionCompactionAgent(model, timeout_seconds=timeout_seconds)
    rolling_block = agent.compact(
        rolling_block=rolling_block,
        source_chunk=source_chunk,
        previous_error=previous_error,
        reserve_attempt=lambda: _reserve_compaction_attempt(
            db,
            session_id,
            rolling_block=rolling_block,
            source_watermark=str(getattr(state, "source_watermark", "{}") or "{}"),
        ),
        record_error=lambda error: _record_compaction_error(db, session_id, error),
    )
    ai_watermark = _max_sequence(
        eligible_ai,
        "message_metadata",
        watermark.get("ai"),
    )
    internal_watermark = _max_sequence(
        eligible_internal,
        "event_metadata",
        watermark.get("internal"),
    )
    next_watermark = _watermark_text(ai_watermark, internal_watermark)
    if not _replace_compaction_state(
        db,
        session_id,
        expected_rolling_block=expected_rolling_block,
        expected_watermark=expected_watermark,
        rolling_block=rolling_block,
        source_watermark=next_watermark,
    ):
        if _retry_on_state_conflict:
            return prepare_session_context(
                db,
                session_id,
                model=model,
                timeout_seconds=timeout_seconds,
                current_user_message=current_user_message,
                exclude_message_ids=exclude_message_ids,
                exclude_message_contents=exclude_message_contents,
                durable_cards=durable_cards,
                _retry_on_state_conflict=False,
            )
        raise SessionCompactionError(
            "session compaction state changed concurrently; retry the active turn"
        )
    cards = _build_cards(
        session_id,
        rolling_block=rolling_block,
        ai_rows=ai_rows,
        internal_rows=internal_rows,
        watermark=_watermark_value(next_watermark),
        include_all_ai=False,
    )
    return SessionContextView(
        cards=tuple(cards),
        compacted=True,
        estimated_tokens=_context_tokens(
            rolling_block,
            [
                row for row in ai_rows
                if _after_watermark(row, "message_metadata", _watermark_value(next_watermark).get("ai"))
            ],
            [
                row
                for row in internal_rows
                if _internal_is_tool_event(row)
                and _after_watermark(row, "event_metadata", _watermark_value(next_watermark).get("internal"))
            ],
            current_user_message=current_user_message,
            durable_cards=durable_cards,
        ),
        source_watermark=next_watermark,
    )

