"""Model-input token accounting shared by Consultant and reviewers."""
from __future__ import annotations

import json
from typing import Any

from app.ai.context_types import MAX_CONTEXT_MODEL_TOKENS


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _message_content(message: Any) -> str:
    return _content_text(getattr(message, "content", ""))


def estimated_text_tokens(value: Any) -> int:
    compact = " ".join(_content_text(value).split())
    return max(1, (len(compact) + 3) // 4) if compact else 0


def model_messages_estimated_tokens(messages: list[Any] | tuple[Any, ...]) -> int:
    """Estimate the exact serialized message envelope sent to a provider.

    LangChain wrappers carry tool calls and tool results outside ``content``.
    Serializing the complete message payload before counting keeps Router and
    Consultant on the same ceiling and avoids independent section budgets.
    """
    serialized_messages: list[Any] = []
    for message in messages:
        model_dump = getattr(message, "model_dump", None)
        if callable(model_dump):
            try:
                serialized_messages.append(model_dump(mode="json"))
                continue
            except Exception:
                pass
        serialized_messages.append(
            {
                "type": getattr(message, "type", None),
                "role": getattr(message, "role", None),
                "content": getattr(message, "content", ""),
                "name": getattr(message, "name", None),
                "tool_call_id": getattr(message, "tool_call_id", None),
                "tool_calls": getattr(message, "tool_calls", None),
                "additional_kwargs": getattr(message, "additional_kwargs", None),
                "response_metadata": getattr(message, "response_metadata", None),
            }
        )
    envelope = {"format": "langchain_messages_v1", "messages": serialized_messages}
    return estimated_text_tokens(json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")))

def assert_model_messages_within_limit(messages: list[Any] | tuple[Any, ...]) -> int:
    estimated = model_messages_estimated_tokens(messages)
    if estimated > MAX_CONTEXT_MODEL_TOKENS:
        raise ValueError(
            f"model input exceeds the {MAX_CONTEXT_MODEL_TOKENS}-token ceiling"
        )
    return estimated
