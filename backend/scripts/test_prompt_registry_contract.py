"""Contract checks for Langfuse-managed graph prompts.

Runs without a Langfuse server. Run with
``python scripts/test_prompt_registry_contract.py`` or pytest.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://rehab:rehab@localhost:5432/rehab_agent")

from app.ai import prompt_registry
from app.ai.prompt_defaults import DEFAULT_PROMPTS, PATIENT_RESPONSE_STYLE
from app.core import config as config_module
from app.observability import langfuse_tracing

EXPORT_DIR = Path(__file__).resolve().parents[1] / "prompts" / "langfuse"


class _Settings:
    environment = "development"
    build_revision = None
    langfuse_enabled = True
    langfuse_public_key = "pk-lf-test-prompts"
    langfuse_secret_key = "sk-lf-test"
    langfuse_base_url = "http://langfuse.invalid"
    langfuse_allow_production = False
    langfuse_tracing_environment = None
    langfuse_trace_tags = None
    langfuse_prompts_enabled = True
    langfuse_prompt_label = "production"
    langfuse_prompt_cache_ttl_seconds = 60


class _FakeClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_prompt(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        response = self.responses.get(name)
        if isinstance(response, Exception):
            raise response
        if response is None:
            return SimpleNamespace(prompt=kwargs["fallback"], version=None, is_fallback=True)
        return response


def _use(settings: Any, client: Any | None):
    original_settings = config_module.get_settings
    original_client = langfuse_tracing.get_langfuse_client
    config_module.get_settings = lambda: settings  # type: ignore[assignment]
    langfuse_tracing.get_langfuse_client = lambda: client  # type: ignore[assignment]
    prompt_registry._reset_for_tests()

    def restore() -> None:
        config_module.get_settings = original_settings  # type: ignore[assignment]
        langfuse_tracing.get_langfuse_client = original_client  # type: ignore[assignment]
        prompt_registry._reset_for_tests()

    return restore


def _langfuse_prompt(text: str, version: int = 7) -> Any:
    return SimpleNamespace(prompt=text, version=version, is_fallback=False)


def test_defaults_are_used_when_prompt_management_is_disabled() -> None:
    settings = _Settings()
    settings.langfuse_prompts_enabled = False
    client = _FakeClient({"rehab-grounding-reviewer": _langfuse_prompt("remote")})
    restore = _use(settings, client)
    try:
        resolved = prompt_registry.get_prompt("rehab-grounding-reviewer")
        assert resolved.source == "default"
        assert resolved.text == DEFAULT_PROMPTS["rehab-grounding-reviewer"].text
        assert resolved.run_config() == {}
        assert client.calls == []
    finally:
        restore()


def test_defaults_are_used_when_langfuse_client_is_off() -> None:
    restore = _use(_Settings(), None)
    try:
        assert prompt_registry.get_prompt("rehab-router").source == "default"
    finally:
        restore()


def test_langfuse_version_is_compiled_with_variables() -> None:
    client = _FakeClient({
        "rehab-consultant-retrieval-plan-level3": _langfuse_prompt(
            "Search session {{ session_id }} now.", version=4
        ),
    })
    restore = _use(_Settings(), client)
    try:
        resolved = prompt_registry.get_prompt(
            "rehab-consultant-retrieval-plan-level3", session_id="{{not_a_var}} s-1"
        )
        assert resolved.source == "langfuse"
        assert resolved.version == 4
        assert resolved.label == "production"
        # Values are inserted verbatim and never re-parsed as template syntax.
        assert resolved.text == "Search session {{not_a_var}} s-1 now."
        assert resolved.run_config()["metadata"]["langfuse_prompt"] is client.responses[
            "rehab-consultant-retrieval-plan-level3"
        ]
        name, kwargs = client.calls[0]
        assert name == "rehab-consultant-retrieval-plan-level3"
        assert kwargs["label"] == "production"
        assert kwargs["type"] == "text"
        assert kwargs["cache_ttl_seconds"] == 60
        assert kwargs["fallback"] == DEFAULT_PROMPTS[name].text
    finally:
        restore()


def test_missing_prompt_falls_back_and_is_not_refetched_every_call() -> None:
    client = _FakeClient({})
    restore = _use(_Settings(), client)
    try:
        first = prompt_registry.get_prompt("rehab-safety-reviewer")
        second = prompt_registry.get_prompt("rehab-safety-reviewer")
        assert first.source == second.source == "default"
        assert first.text == DEFAULT_PROMPTS["rehab-safety-reviewer"].text
        assert len(client.calls) == 1
    finally:
        restore()


def test_fetch_error_falls_back_to_default() -> None:
    client = _FakeClient({"rehab-router": RuntimeError("connection refused")})
    restore = _use(_Settings(), client)
    try:
        resolved = prompt_registry.get_prompt("rehab-router")
        assert resolved.source == "default"
        assert resolved.text == DEFAULT_PROMPTS["rehab-router"].text
    finally:
        restore()


def test_langfuse_version_with_wrong_variables_is_rejected() -> None:
    client = _FakeClient({
        # Drops {{authorized_tools}}: the planner would lose the tool list.
        "rehab-consultant-retrieval-plan": _langfuse_prompt("Plan retrieval."),
        # Adds an unknown variable that would reach the model as literal braces.
        "rehab-grounding-reviewer": _langfuse_prompt("Review {{draft}}."),
    })
    restore = _use(_Settings(), client)
    try:
        plan = prompt_registry.get_prompt("rehab-consultant-retrieval-plan", authorized_tools="[tools]")
        assert plan.source == "default"
        assert plan.text.endswith("The authorized tools are:\n[tools]")
        assert prompt_registry.get_prompt("rehab-grounding-reviewer").source == "default"
    finally:
        restore()


def test_label_and_ttl_come_from_settings() -> None:
    settings = _Settings()
    settings.langfuse_prompt_label = "staging"
    settings.langfuse_prompt_cache_ttl_seconds = 5
    client = _FakeClient({"rehab-router": _langfuse_prompt("staging router")})
    restore = _use(settings, client)
    try:
        resolved = prompt_registry.get_prompt("rehab-router")
        assert resolved.text == "staging router"
        assert resolved.label == "staging"
        assert client.calls[0][1]["label"] == "staging"
        assert client.calls[0][1]["cache_ttl_seconds"] == 5
    finally:
        restore()


def test_required_variables_must_be_supplied() -> None:
    restore = _use(_Settings(), None)
    try:
        try:
            prompt_registry.get_prompt("rehab-router-checkpoint", checkpoint_json="{}")
        except KeyError as exc:
            assert "remaining_fields_json" in str(exc)
        else:
            raise AssertionError("missing variables must raise")
    finally:
        restore()


def test_composed_prompts_match_the_previous_inline_text() -> None:
    restore = _use(_Settings(), None)
    try:
        consultant = prompt_registry.get_prompt(
            "rehab-consultant",
            patient_response_style=prompt_registry.prompt_text("rehab-patient-response-style"),
        ).text
        assert consultant.startswith("You are the RehabFlow Consultant.")
        assert consultant.endswith("Do not mention internal agents, tools, or prompts. " + PATIENT_RESPONSE_STYLE)
        assert "{{" not in consultant
        repair = prompt_registry.get_prompt(
            "rehab-consultant-repair",
            patient_response_style=PATIENT_RESPONSE_STYLE,
        ).text
        assert repair.endswith("from. " + PATIENT_RESPONSE_STYLE)
    finally:
        restore()


def test_every_default_has_unique_name_and_exported_copy() -> None:
    for name, default in DEFAULT_PROMPTS.items():
        assert default.name == name
        assert default.text.strip()
        exported = EXPORT_DIR / f"{name}.txt"
        assert exported.is_file(), f"run scripts/export_langfuse_prompts.py: missing {exported}"
        assert exported.read_text(encoding="utf-8") == default.text, (
            f"{exported} is stale; run scripts/export_langfuse_prompts.py"
        )


def test_link_prompt_leaves_defaults_and_test_doubles_unchanged() -> None:
    double = SimpleNamespace(invoke=lambda messages: "ok")
    default = prompt_registry.ResolvedPrompt(name="rehab-router", text="x", source="default")
    linked = prompt_registry.ResolvedPrompt(
        name="rehab-router", text="x", source="langfuse", langfuse_prompt=object()
    )
    assert prompt_registry.link_prompt(double, default) is double
    assert prompt_registry.link_prompt(double, linked) is double


def test_langfuse_generation_is_linked_to_the_prompt_version() -> None:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import HumanMessage, SystemMessage
    from langgraph.graph import END, START, StateGraph
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from langfuse import Langfuse
    from langfuse.api import Prompt_Text
    from langfuse.model import TextPromptClient

    exporter = InMemorySpanExporter()
    settings = _Settings()
    settings.langfuse_public_key = "pk-lf-test-prompt-link"
    original_settings = config_module.get_settings
    config_module.get_settings = lambda: settings  # type: ignore[assignment]
    langfuse_tracing._reset_for_tests()
    prompt_registry._reset_for_tests()
    real_init = langfuse_tracing._init_client
    remote = TextPromptClient(
        Prompt_Text(
            name="rehab-safety-reviewer",
            version=3,
            config={},
            labels=["production"],
            tags=[],
            prompt="Remote safety reviewer prompt.",
            type="text",
        )
    )

    def _init_with_memory_exporter():
        langfuse_tracing._public_key = settings.langfuse_public_key
        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
            span_exporter=exporter,
        )
        client.get_prompt = lambda name, **kwargs: remote  # type: ignore[method-assign]
        return client

    langfuse_tracing._init_client = _init_with_memory_exporter
    try:
        model = FakeListChatModel(responses=["linked answer", "unlinked answer"])

        class State(TypedDict):
            answer: str

        async def node(state: State) -> dict[str, str]:
            prompt = prompt_registry.get_prompt("rehab-safety-reviewer")
            assert prompt.source == "langfuse"
            # Async + retry wrapper, as used by the reviewers and the consultant.
            linked_model = prompt_registry.link_prompt(model.with_retry(stop_after_attempt=2), prompt)
            linked = (await linked_model.ainvoke(
                [SystemMessage(content=prompt.text), HumanMessage(content="draft")]
            )).content
            # A later call in the same node must not inherit the prompt link.
            plain = (await model.ainvoke("no prompt")).content
            return {"answer": f"{linked} | {plain}"}

        builder = StateGraph(State)
        builder.add_node("node", node)
        builder.add_edge(START, "node")
        builder.add_edge("node", END)
        graph = builder.compile()

        async def run() -> dict[str, Any]:
            return await graph.ainvoke({"answer": ""}, config=langfuse_tracing.with_langfuse_callbacks({}))

        assert asyncio.run(run())["answer"] == "linked answer | unlinked answer"
        langfuse_tracing.get_langfuse_client().flush()
        generations = [
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("langfuse.observation.type") == "generation"
        ]
        assert len(generations) == 2, [span.name for span in exporter.get_finished_spans()]
        linked_spans = [
            span for span in generations
            if span.attributes.get("langfuse.observation.prompt.name") == "rehab-safety-reviewer"
        ]
        assert len(linked_spans) == 1
        assert linked_spans[0].attributes.get("langfuse.observation.prompt.version") == 3
        assert "Remote safety reviewer prompt." in str(dict(linked_spans[0].attributes))
    finally:
        langfuse_tracing._init_client = real_init
        config_module.get_settings = original_settings  # type: ignore[assignment]
        langfuse_tracing._reset_for_tests()
        prompt_registry._reset_for_tests()


if __name__ == "__main__":
    for _name, _test in sorted(globals().items()):
        if _name.startswith("test_") and callable(_test):
            _test()
            print(f"ok {_name}")
