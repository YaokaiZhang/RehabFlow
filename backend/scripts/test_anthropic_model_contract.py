"""Offline contract checks for the Claude chat-model adapter and web search parsing.

No network: requests are inspected via ``_get_request_payload`` and responses are
built from real ``anthropic`` SDK types. Run with pytest or directly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://rehab:rehab@localhost:5432/rehab_agent")

from anthropic.types import Message, TextBlock, ThinkingBlock, Usage
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.ai.anthropic_model import (
    FALLBACK_BETA,
    ModelRefusalError,
    RehabChatAnthropic,
    message_text,
    supports_server_side_fallback,
)
from app.ai.reliable_web_search import _response_text_and_citations, _web_search_tool_type


class Plan(BaseModel):
    concern: str = Field(min_length=1)
    urgent: bool


def _model(**overrides) -> RehabChatAnthropic:
    kwargs = {
        "model": "claude-opus-5-5",
        "api_key": "sk-ant-test",
        "max_tokens": 16000,
        "effort": "low",
        "betas": [FALLBACK_BETA],
        "model_kwargs": {"fallbacks": "default"},
    }
    kwargs.update(overrides)
    return RehabChatAnthropic(**kwargs)


def _message(content, stop_reason="end_turn") -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-opus-5-5",
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class message_text_holder:  # minimal message-like object
    def __init__(self, text: str) -> None:
        self.content = text


def test_payload_sends_effort_and_fallbacks_but_no_sampling_params() -> None:
    payload = _model(temperature=0.4)._get_request_payload([HumanMessage(content="hi")])
    assert payload["model"] == "claude-opus-5-5"
    assert payload["output_config"] == {"effort": "low"}
    assert payload["fallbacks"] == "default"
    assert payload["betas"] == [FALLBACK_BETA]
    for key in ("temperature", "top_p", "top_k"):
        assert key not in payload


def test_haiku_gets_no_fallback_and_basic_web_search() -> None:
    assert supports_server_side_fallback("claude-haiku-5-5") is False
    assert supports_server_side_fallback("claude-opus-5-5") is True
    assert _web_search_tool_type("claude-haiku-5-5") == "web_search_20250305"
    assert _web_search_tool_type("claude-opus-5-5") == "web_search_20260209"


def test_default_effort_is_medium() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["anthropic_effort"].default == "medium"


def test_build_chat_model_defaults_to_haiku_without_fallback() -> None:
    from app.ai import anthropic_model
    from app.core import config

    class _Settings:
        anthropic_api_key = "sk-ant-test"
        anthropic_model = "claude-haiku-5-5"
        anthropic_max_tokens = 16000
        anthropic_effort = "medium"
        anthropic_base_url = None
        anthropic_fallbacks_enabled = True

    original = config.get_settings
    config.get_settings = lambda: _Settings()  # type: ignore[assignment]
    try:
        model = anthropic_model.build_chat_model()
    finally:
        config.get_settings = original  # type: ignore[assignment]
    payload = model._get_request_payload([HumanMessage(content="hi")])
    assert payload["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in payload and "betas" not in payload
    assert payload["output_config"] == {"effort": "medium"}


def test_structured_output_uses_json_schema_format_not_forced_tool() -> None:
    model = _model()
    bound = model.with_structured_output(Plan)
    binding = bound.first  # RunnableBinding carrying the bound kwargs
    payload = model._get_request_payload([HumanMessage(content="hi")], **binding.kwargs)
    assert "tool_choice" not in payload and "tools" not in payload
    output_config = payload["output_config"]
    assert output_config["effort"] == "low"
    assert output_config["format"]["type"] == "json_schema"
    schema = output_config["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"concern", "urgent"}


def test_structured_output_parses_text_json() -> None:
    model = _model()
    parse = model.with_structured_output(Plan).last
    result = parse.invoke(message_text_holder('{"concern": "Knee stiffness", "urgent": false}'))
    assert result == Plan(concern="Knee stiffness", urgent=False)


def test_thinking_blocks_are_dropped_and_content_is_plain_text() -> None:
    result = _model()._format_output(
        _message([
            ThinkingBlock(type="thinking", thinking="", signature="sig"),
            TextBlock(type="text", text="Try gentle heel slides."),
        ])
    )
    message = result.generations[0].message
    assert message.content == "Try gentle heel slides."
    assert message.usage_metadata["output_tokens"] == 5


def test_refusal_raises() -> None:
    try:
        _model()._format_output(_message([], stop_reason="refusal"))
    except ModelRefusalError:
        return
    raise AssertionError("refusal should raise ModelRefusalError")


def test_message_text_handles_blocks_and_strings() -> None:
    assert message_text("plain") == "plain"
    assert message_text([{"type": "thinking", "thinking": ""}, {"type": "text", "text": "a"}, "b"]) == "ab"


def test_web_search_parser_collects_citations_and_results() -> None:
    content = [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": [{"type": "web_search_result", "url": "https://www.nhs.uk/a", "title": "NHS A"}],
        },
        {
            "type": "text",
            "text": "Heel slides help.",
            "citations": [{
                "type": "web_search_result_location",
                "url": "https://medlineplus.gov/b",
                "title": "Medline B",
                "cited_text": "...",
            }],
        },
    ]
    text, citations = _response_text_and_citations(content)
    assert text == "Heel slides help."
    assert ("Medline B", "https://medlineplus.gov/b") in citations
    assert ("NHS A", "https://www.nhs.uk/a") in citations


def test_web_search_parser_ignores_error_result() -> None:
    content = [{
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"},
    }]
    assert _response_text_and_citations(content) == ("", [])


def main() -> None:
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
    print("anthropic model contract: ok")


if __name__ == "__main__":
    main()
