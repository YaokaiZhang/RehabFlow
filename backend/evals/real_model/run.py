from __future__ import annotations

from contextlib import nullcontext
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


JUDGE_DEFAULT_MODEL = "claude-haiku-5-5"
JUDGE_DEFAULT_EFFORT = "high"
JUDGE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_EFFORT_DISABLED = frozenset({"", "none", "off", "disabled", "default"})


def normalize_judge_effort(value: str | None) -> str | None:
    """Anthropic effort for the judge; ``None`` leaves the model default."""
    effort = str(value or "").strip().lower()
    if effort in _EFFORT_DISABLED:
        return None
    if effort not in JUDGE_EFFORTS:
        raise ValueError(
            f"judge effort must be one of {', '.join(sorted(JUDGE_EFFORTS))} or none; got {value!r}"
        )
    return effort


def _usage_details(usage: object) -> dict[str, int]:
    details: dict[str, int] = {}
    for source, target in (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_input_tokens", "cache_read_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
    ):
        value = getattr(usage, source, None)
        if isinstance(value, int):
            details[target] = value
    return details


class _AnthropicJudgeProvider:
    """LLM judge on the Anthropic Messages API (adaptive thinking + effort).

    Returns a LangChain ``AIMessage`` so the judge parser and the report code,
    which read ``content`` and ``usage_metadata``, need no provider branches.
    Each call is a Langfuse generation when a reporter is configured.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        model_name: str = JUDGE_DEFAULT_MODEL,
        effort: str | None = JUDGE_DEFAULT_EFFORT,
        max_tokens: int = 32000,
        request_timeout_seconds: float = 600.0,
        max_retries: int = 2,
        langfuse: object | None = None,
        client: object | None = None,
    ) -> None:
        self.model_name = model_name
        self.effort = normalize_judge_effort(effort)
        self.max_tokens = max(1024, int(max_tokens))
        self.langfuse = langfuse
        self._invoke_lock = Lock()
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                api_key=api_key,
                base_url=base_url or None,
                timeout=max(1.0, float(request_timeout_seconds)),
                max_retries=max(0, int(max_retries)),
            )
        self.client = client

    def _request(self, prompt: str) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        return request

    def _generation(self, prompt: str):
        if self.langfuse is None:
            return nullcontext(None)
        return self.langfuse.judge_generation(
            model=self.model_name,
            prompt=prompt,
            model_parameters={"max_tokens": self.max_tokens, "effort": self.effort or "default"},
        )

    def _call(self, prompt: str) -> object:
        from langchain_core.messages import AIMessage

        with self._generation(prompt) as generation:
            # Streaming keeps long high-effort judge calls clear of request timeouts.
            with self.client.messages.stream(**self._request(prompt)) as stream:
                message = stream.get_final_message()
            text = "\n".join(
                block.text
                for block in message.content
                if getattr(block, "type", None) == "text" and getattr(block, "text", "")
            )
            usage = _usage_details(getattr(message, "usage", None))
            if generation is not None:
                try:
                    generation.update(
                        output=text,
                        usage_details=usage,
                        metadata={"stop_reason": getattr(message, "stop_reason", None)},
                    )
                except Exception:
                    pass
        input_tokens = usage.get("input", 0)
        output_tokens = usage.get("output", 0)
        return AIMessage(
            content=text,
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            response_metadata={
                "model": getattr(message, "model", self.model_name),
                "stop_reason": getattr(message, "stop_reason", None),
            },
        )

    def invoke(self, prompt: str, *, model: str) -> object:
        if model != self.model_name:
            raise ValueError(
                f"provider was configured for {self.model_name!r}, received {model!r}"
            )
        with self._invoke_lock:
            return self._call(prompt)
