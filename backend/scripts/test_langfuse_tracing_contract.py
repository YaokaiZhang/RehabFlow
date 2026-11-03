"""Contract checks for optional Langfuse tracing.

Runs without a Langfuse server: spans are captured with an in-memory OTel
exporter. Run with ``python scripts/test_langfuse_tracing_contract.py`` or pytest.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://rehab:rehab@localhost:5432/rehab_agent")

from app.core import config as config_module
from app.observability import langfuse_tracing


class _Settings:
    environment = "development"
    build_revision = None
    langfuse_enabled = True
    langfuse_public_key = "pk-lf-test"
    langfuse_secret_key = "sk-lf-test"
    langfuse_base_url = "http://langfuse.invalid"
    langfuse_allow_production = False


def _with_settings(settings: Any):
    original = config_module.get_settings
    config_module.get_settings = lambda: settings  # type: ignore[assignment]
    langfuse_tracing._reset_for_tests()
    return original


def _restore(original) -> None:
    config_module.get_settings = original  # type: ignore[assignment]
    langfuse_tracing._reset_for_tests()


def test_disabled_by_default_adds_nothing() -> None:
    settings = _Settings()
    settings.langfuse_enabled = False
    original = _with_settings(settings)
    try:
        assert langfuse_tracing.langfuse_callbacks() == []
        config = {"configurable": {"thread_id": "t"}}
        assert langfuse_tracing.with_langfuse_callbacks(config) == config
        with langfuse_tracing.langfuse_trace_attributes(trace_name="x", session_id="s"):
            pass
    finally:
        _restore(original)


def test_refused_in_production_without_override() -> None:
    settings = _Settings()
    settings.environment = "production"
    original = _with_settings(settings)
    try:
        assert langfuse_tracing.langfuse_enabled() is False
    finally:
        _restore(original)


def test_missing_keys_disable_tracing() -> None:
    settings = _Settings()
    settings.langfuse_secret_key = None
    original = _with_settings(settings)
    try:
        assert langfuse_tracing.langfuse_enabled() is False
    finally:
        _restore(original)


def test_mask_redacts_secrets_recursively() -> None:
    masked = langfuse_tracing._mask(
        data={"messages": [{"content": "my api_key=abc123 and sk-livesecret"}], "n": 3}
    )
    text = masked["messages"][0]["content"]
    assert "abc123" not in text and "sk-livesecret" not in text
    assert "[REDACTED]" in text
    assert masked["n"] == 3


def test_graph_llm_calls_nest_under_one_session_trace() -> None:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.graph import END, START, StateGraph
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from langfuse import Langfuse

    exporter = InMemorySpanExporter()
    settings = _Settings()
    original = _with_settings(settings)
    real_init = langfuse_tracing._init_client

    def _init_with_memory_exporter():
        langfuse_tracing._public_key = settings.langfuse_public_key
        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
            mask=langfuse_tracing._mask,
            span_exporter=exporter,
        )

    langfuse_tracing._init_client = _init_with_memory_exporter
    try:
        model = FakeListChatModel(responses=["first answer", "second answer"])

        class State(TypedDict):
            question: str
            answer: str

        def node_a(state: State) -> dict[str, str]:
            # Nested invoke without passing config, like the real graph nodes.
            return {"answer": model.invoke(state["question"]).content}

        def node_b(state: State) -> dict[str, str]:
            return {"answer": state["answer"] + " | " + model.invoke("token=hunter2").content}

        builder = StateGraph(State)
        builder.add_node("node_a", node_a)
        builder.add_node("node_b", node_b)
        builder.add_edge(START, "node_a")
        builder.add_edge("node_a", "node_b")
        builder.add_edge("node_b", END)
        graph = builder.compile()

        config = langfuse_tracing.with_langfuse_callbacks({"configurable": {"thread_id": "s-1"}})
        assert config["callbacks"], "callbacks should be attached when enabled"

        async def run() -> dict[str, Any]:
            with langfuse_tracing.langfuse_trace_attributes(trace_name="rehab_turn", session_id="s-1"):
                return await graph.ainvoke({"question": "knee hurts", "answer": ""}, config=config)

        result = asyncio.run(run())
        assert result["answer"] == "first answer | second answer"

        langfuse_tracing.get_langfuse_client().flush()
        spans = exporter.get_finished_spans()
        assert spans, "expected spans to be exported"
        trace_ids = {span.context.trace_id for span in spans}
        assert len(trace_ids) == 1, f"all spans should share one trace, got {len(trace_ids)}"

        generations = [
            span for span in spans if span.attributes.get("langfuse.observation.type") == "generation"
        ]
        assert len(generations) == 2, [span.name for span in spans]
        assert all(span.attributes.get("session.id") == "s-1" for span in spans)
        exported_text = " ".join(str(dict(span.attributes)) for span in spans)
        assert "knee hurts" in exported_text, "prompts should be exported"
        assert "hunter2" not in exported_text
    finally:
        langfuse_tracing._init_client = real_init
        _restore(original)


def test_web_search_call_is_a_generation_in_the_turn_trace() -> None:
    from types import SimpleNamespace

    from langgraph.graph import END, START, StateGraph
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from langfuse import Langfuse

    from app.ai import reliable_web_search as web_module
    from app.ai.consultant_tools import _search_adapter_result

    exporter = InMemorySpanExporter()
    settings = _Settings()
    # Langfuse caches one client per public key; use a fresh key so this
    # test gets its own in-memory exporter.
    settings.langfuse_public_key = "pk-lf-test-web-search"
    original = _with_settings(settings)
    real_init = langfuse_tracing._init_client

    def _init_with_memory_exporter():
        langfuse_tracing._public_key = settings.langfuse_public_key
        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
            mask=langfuse_tracing._mask,
            span_exporter=exporter,
        )

    class _Block:
        type = "text"
        text = "- NHS knee pain https://www.nhs.uk/conditions/knee-pain/ rest and ice"
        citations = [SimpleNamespace(
            type="web_search_result_location",
            url="https://www.nhs.uk/conditions/knee-pain/",
            title="Knee pain - NHS",
            cited_text="rest and ice",
        )]

        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return {"type": self.type, "text": self.text}

    class _Messages:
        def create(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                content=[_Block()],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=120,
                    output_tokens=40,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    server_tool_use=SimpleNamespace(web_search_requests=1),
                ),
            )

    import anthropic

    real_client = anthropic.Anthropic
    anthropic.Anthropic = lambda **_kwargs: SimpleNamespace(messages=_Messages())  # type: ignore[assignment]
    langfuse_tracing._init_client = _init_with_memory_exporter
    try:
        search = web_module.ReliableWebSearch(api_key="test-key", model="claude-haiku-5-5")

        class State(TypedDict):
            answer: str

        def search_node(state: State) -> dict[str, Any]:
            result = _search_adapter_result(search, "knee pain after running")
            return {"answer": str(result)}

        builder = StateGraph(State)
        builder.add_node("search_node", search_node)
        builder.add_edge(START, "search_node")
        builder.add_edge("search_node", END)
        graph = builder.compile()
        config = langfuse_tracing.with_langfuse_callbacks({"configurable": {"thread_id": "s-2"}})

        async def run() -> dict[str, Any]:
            with langfuse_tracing.langfuse_trace_attributes(trace_name="rehab_turn", session_id="s-2"):
                return await graph.ainvoke({"answer": ""}, config=config)

        result = asyncio.run(run())
        assert "nhs.uk" in result["answer"], result

        langfuse_tracing.get_langfuse_client().flush()
        spans = exporter.get_finished_spans()
        trace_ids = {span.context.trace_id for span in spans}
        assert len(trace_ids) == 1, f"web search should join the turn trace, got {len(trace_ids)}"
        generations = [
            span for span in spans
            if span.attributes.get("langfuse.observation.type") == "generation"
        ]
        assert [span.name for span in generations] == ["reliable_web_search"], [s.name for s in spans]
        attributes = dict(generations[0].attributes)
        exported = str(attributes)
        assert "knee pain after running" in exported, "prompt should be exported"
        assert "rest and ice" in exported, "output should be exported"
        assert "claude-haiku-5-5" in exported
        assert "120" in exported and "web_search_requests" in exported, "usage should be exported"
        assert all(span.attributes.get("session.id") == "s-2" for span in spans)
    finally:
        anthropic.Anthropic = real_client  # type: ignore[assignment]
        langfuse_tracing._init_client = real_init
        _restore(original)


def main() -> None:
    test_disabled_by_default_adds_nothing()
    test_refused_in_production_without_override()
    test_missing_keys_disable_tracing()
    test_mask_redacts_secrets_recursively()
    test_graph_llm_calls_nest_under_one_session_trace()
    test_web_search_call_is_a_generation_in_the_turn_trace()
    print("langfuse tracing contract: ok")


if __name__ == "__main__":
    main()
