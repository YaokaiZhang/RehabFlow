"""Claude chat model used by every RehabFlow LLM call.

``RehabChatAnthropic`` is a thin ``ChatAnthropic`` subclass that adapts the
LangChain integration to the current Claude API (``claude-haiku-5-5`` by
default; any current model via ``ANTHROPIC_MODEL``):

* Thinking is adaptive and on by default. Depth is controlled with
  ``output_config.effort`` (``ANTHROPIC_EFFORT``); ``temperature`` is never
  sent because current models reject sampling parameters.
* Structured output uses native JSON-schema output
  (``output_config.format``). The langchain-anthropic default forces a tool
  call, which Opus 5.5 / Sonnet 5.5 reject and which skips thinking on
  Haiku 5.5.
* Responses are normalised to plain-text ``AIMessage.content`` (thinking and
  fallback blocks dropped) so callers that treat content as a string keep
  working.
* Prompt caching: breakpoints go on the static system prompt, on the end of
  the replayed history (the turn before the per-request user message), and on
  a trailing ``tool_result`` turn so each tool-loop iteration reads the
  previous one. The unique per-request tail is left unmarked, so it never pays
  the cache-write premium.
* Server-side refusal fallbacks are enabled for models that support them
  (not Haiku). A refused turn raises ``ModelRefusalError`` so the graph's existing
  failure handling takes over instead of releasing an empty answer.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda, RunnableMap, RunnablePassthrough
from pydantic import BaseModel

from app.ai.structured_output import attach_usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
CACHE_CONTROL = {"type": "ephemeral"}
# Block types that accept ``cache_control`` (thinking blocks do not).
_CACHEABLE_BLOCKS = frozenset({"text", "image", "document", "tool_use", "tool_result"})


class ModelRefusalError(RuntimeError):
    """Claude declined the request (``stop_reason == "refusal"``) even after fallback."""

    def __init__(self, category: str | None = None) -> None:
        self.category = category
        super().__init__(f"model refused the request (category={category or 'unspecified'})")


def message_text(message: Any) -> str:
    """Return the text of a LangChain message whose content may be blocks."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


def _as_blocks(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content or [])


def _mark_last_cacheable(content: Any) -> tuple[Any, bool]:
    """Return ``content`` as blocks with ``cache_control`` on the last eligible block."""
    blocks = _as_blocks(content)
    for index in range(len(blocks) - 1, -1, -1):
        block = blocks[index]
        if isinstance(block, Mapping) and block.get("type") in _CACHEABLE_BLOCKS:
            if block.get("type") == "text" and not block.get("text"):
                continue
            blocks[index] = {**block, "cache_control": dict(CACHE_CONTROL)}
            return blocks, True
    return content, False


def _is_tool_result_turn(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and any(isinstance(b, Mapping) and b.get("type") == "tool_result" for b in content)
    )


def apply_cache_breakpoints(payload: dict[str, Any]) -> dict[str, Any]:
    """Place prompt-cache breakpoints at the payload's stability boundaries.

    Prompts are built as static system prompt, replayed history, then one
    per-request user message (context and question). Caching that last
    message would write bytes no later request reads, so the breakpoints sit
    on the system prompt and on the turn before the final message. In a tool
    loop the final message is a ``tool_result`` turn the next iteration
    replays, so it is marked too. At most three of the four allowed
    breakpoints are used.
    """
    if any(
        isinstance(block, Mapping) and "cache_control" in block
        for message in payload.get("messages") or ()
        if isinstance(message, Mapping) and isinstance(message.get("content"), list)
        for block in message["content"]
    ):
        return payload  # Caller placed its own breakpoints.

    system = payload.get("system")
    if system:
        marked, ok = _mark_last_cacheable(system)
        if ok:
            payload["system"] = marked

    messages = [dict(m) if isinstance(m, Mapping) else m for m in payload.get("messages") or ()]
    targets: list[int] = []
    if len(messages) >= 2:
        targets.append(len(messages) - 2)
    if messages and isinstance(messages[-1], Mapping) and _is_tool_result_turn(messages[-1]):
        targets.append(len(messages) - 1)
    for index in targets:
        message = messages[index]
        if isinstance(message, dict):
            marked, ok = _mark_last_cacheable(message.get("content"))
            if ok:
                message["content"] = marked
    if messages:
        payload["messages"] = messages
    return payload


def _schema_for(schema: type[BaseModel] | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    from anthropic import transform_schema

    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return transform_schema(schema), schema.__name__
    if isinstance(schema, Mapping):
        raw = dict(schema)
        # Accept Anthropic/OpenAI tool-style wrappers as well as bare JSON Schema.
        if "input_schema" in raw:
            name = str(raw.get("name") or "output")
            raw = dict(raw["input_schema"])
        elif "parameters" in raw and "type" not in raw:
            name = str(raw.get("name") or "output")
            raw = dict(raw["parameters"])
        else:
            name = str(raw.get("title") or "output")
        return transform_schema(raw), name
    raise TypeError(f"unsupported structured output schema: {schema!r}")


class RehabChatAnthropic(ChatAnthropic):
    """``ChatAnthropic`` adapted to current Claude models (see module docstring)."""

    effort: str | None = None
    prompt_caching: bool = True

    def _effort_config(self) -> dict[str, Any]:
        return {"effort": self.effort} if self.effort else {}

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # Current models return 400 for non-default sampling parameters.
        for key in ("temperature", "top_p", "top_k"):
            payload.pop(key, None)
        output_config = dict(self._effort_config())
        output_config.update(payload.get("output_config") or {})
        if output_config:
            payload["output_config"] = output_config
        if self.prompt_caching:
            apply_cache_breakpoints(payload)
        return payload

    def _format_output(self, data: Any, **kwargs: Any) -> ChatResult:
        stop_reason = getattr(data, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(data, "stop_details", None)
            raise ModelRefusalError(getattr(details, "category", None))
        result = super()._format_output(data, **kwargs)
        generation = result.generations[0]
        message = generation.message
        if isinstance(message.content, list) and not getattr(message, "tool_calls", None):
            normalised = AIMessage(
                content=message_text(message),
                usage_metadata=message.usage_metadata,
                response_metadata=message.response_metadata,
                id=message.id,
            )
            result = ChatResult(
                generations=[ChatGeneration(message=normalised)],
                llm_output=result.llm_output,
            )
        return result

    def with_structured_output(
        self,
        schema: type[BaseModel] | Mapping[str, Any],
        *,
        include_raw: bool = False,
        **_kwargs: Any,
    ) -> Runnable:
        json_schema, _name = _schema_for(schema)
        output_config = {
            **self._effort_config(),
            "format": {"type": "json_schema", "schema": json_schema},
        }
        llm = self.bind(output_config=output_config)
        is_model = isinstance(schema, type) and issubclass(schema, BaseModel)

        def parse(message: Any) -> Any:
            text = message_text(message).strip()
            if is_model:
                # Usage-reporting schemas keep the call's token usage.
                return attach_usage(schema.model_validate_json(text), message)  # type: ignore[union-attr]
            return json.loads(text)

        if not include_raw:
            return llm | RunnableLambda(parse)

        parser = RunnablePassthrough.assign(
            parsed=lambda value: parse(value["raw"]),
            parsing_error=lambda _: None,
        ).with_fallbacks(
            [RunnablePassthrough.assign(parsed=lambda _: None)],
            exception_key="parsing_error",
        )
        return RunnableMap(raw=llm) | parser


def supports_server_side_fallback(model: str) -> bool:
    """Haiku models have no server-side refusal fallback; sending it is a no-op or a 400."""
    return not model.startswith("claude-haiku")


def build_chat_model(
    *,
    model: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
) -> RehabChatAnthropic:
    """Build the configured Claude chat model."""
    from app.ai.runtime_dependencies import openai_client_options
    from app.core.config import get_settings

    settings = get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        raise ValueError(
            "LLM_KEY is not set. Set LLM_KEY (or ANTHROPIC_API_KEY) to an Anthropic API key "
            "in the environment or .env."
        )
    options = openai_client_options(timeout_seconds=timeout_seconds, max_retries=max_retries)
    model_name = model or settings.anthropic_model
    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "max_tokens": settings.anthropic_max_tokens,
        "effort": settings.anthropic_effort,
        "timeout": options["timeout"],
        "max_retries": options["max_retries"],
    }
    if settings.anthropic_base_url:
        kwargs["base_url"] = settings.anthropic_base_url
    if settings.anthropic_fallbacks_enabled and supports_server_side_fallback(model_name):
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["model_kwargs"] = {"fallbacks": "default"}
    return RehabChatAnthropic(**kwargs)
