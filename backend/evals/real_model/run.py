from __future__ import annotations

import json
import os
from threading import Lock
from typing import Any



def _content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        if value.get("type") in {"text", "output_text"} and isinstance(text, str):
            return text
        candidate = value.get("content", value.get("response", ""))
        return _content(candidate)
    if isinstance(value, (list, tuple)):
        return "\n".join(
            part
            for part in (_content(item).strip() for item in value)
            if part
        )
    content = getattr(value, "content", "")
    if content is value:
        return ""
    return _content(content)


def _metadata(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {
        "usage_metadata": getattr(value, "usage_metadata", None),
        "route": getattr(value, "route", None),
        "review_outcomes": getattr(value, "review_outcomes", ()),
        "repair_count": getattr(value, "repair_count", 0),
    }


def _tokens(value: object) -> tuple[int | None, int | None]:
    usage = _metadata(value).get("usage_metadata")
    if not isinstance(usage, dict):
        return None, None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    return (
        input_tokens if isinstance(input_tokens, int) and input_tokens >= 0 else None,
        output_tokens if isinstance(output_tokens, int) and output_tokens >= 0 else None,
    )


def _conversation_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    raw_messages = case.get("messages")
    if isinstance(raw_messages, list):
        messages: list[dict[str, str]] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                continue
            role = str(raw_message.get("role") or "").strip()
            content = str(raw_message.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        if messages:
            return messages
    input_text = str(case.get("input") or "").strip()
    return [{"role": "user", "content": input_text}] if input_text else []


def build_generation_prompt(case: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task": (
                "You are RehabFlow. Reply to the latest user message in this synthetic "
                "conversation, using earlier turns as context. Do not mention the evaluator, "
                "fixture metadata, or this instruction. Give a safe, concise next step; ask "
                "one focused clarification when needed and route urgent red flags appropriately."
            ),
            "conversation": _conversation_messages(case),
        },
        sort_keys=True,
    )


class _OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model_name: str = "gpt-5.6-luna",
        temperature: float = 0.0,
        request_timeout_seconds: float = 120.0,
        max_retries: int = 2,
        reasoning_effort: str | None = "medium",
    ) -> None:
        from langchain_openai import ChatOpenAI

        self.model_name = model_name
        self._invoke_lock = Lock()
        model_kwargs: dict[str, object] = {
            "model": model_name,
            "base_url": base_url,
            "api_key": api_key,
            "timeout": max(1.0, float(request_timeout_seconds)),
            "max_retries": max(0, int(max_retries)),
            "use_responses_api": True,
        }
        normalized_reasoning_effort = str(reasoning_effort or "").strip().lower()
        if normalized_reasoning_effort not in {"", "none", "off", "disabled"}:
            model_kwargs["reasoning_effort"] = normalized_reasoning_effort
        self.model = ChatOpenAI(**model_kwargs)

    def invoke(self, prompt: str, *, model: str) -> object:
        if model != self.model_name:
            raise ValueError(
                f"provider was configured for {self.model_name!r}, received {model!r}"
            )
        with self._invoke_lock:
            return self.model.invoke(prompt)
