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


def main() -> None:
    test_disabled_by_default_adds_nothing()
    test_refused_in_production_without_override()
    test_missing_keys_disable_tracing()
    test_mask_redacts_secrets_recursively()
    test_graph_llm_calls_nest_under_one_session_trace()
    print("langfuse tracing contract: ok")


if __name__ == "__main__":
    main()
