#!/usr/bin/env python3
"""Offline contract: Consultant tool-call markup never becomes the patient answer."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QWEN_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ai import consultant_invocation
from app.ai import consultant_tools
from app.ai import graph_dependencies
from app.ai import workflow_policy
from app.ai import workflow_state
from app.ai.prompt_defaults import CONSULTANT_TOOL_MARKUP_RETRY

# The reply that failed attempt 1 of the traced turn, verbatim.
TRACE_MARKUP = (
    "<invoke:read_episode_memory_level_2>\n"
    '<parameter name="session_id">bff06306-ae9b-4fc9-9d07-1429b5672809</parameter>\n'
    "</invoke>"
)
PLAIN_ANSWER = "You told me your lower back has been stiff in the mornings."


class FakeMessage:
    def __init__(self, content: str = "", tool_calls: list[dict] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage_metadata = {"total_tokens": 1}


class ScriptedLLM:
    """Plans no retrieval, then returns the scripted answers in order."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.answer_prompts: list[list[object]] = []

    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, messages):
        last = str(getattr(messages[-1], "content", "")) if messages else ""
        if last.startswith("Plan the next retrieval step"):
            plan = {"action": "none", "reason": "Context is enough.", "calls": []}
            return FakeMessage(
                tool_calls=[{"id": "plan", "name": "consultant_retrieval_plan", "args": plan}]
            )
        self.answer_prompts.append(list(messages))
        return FakeMessage(self.answers.pop(0))


def _state(tools: list[str]) -> workflow_state.RehabGraphState:
    return {
        "user_input": "hi. what do you remember about me?",
        "conversation_history": [],
        "consult_attempts": 0,
        "debug_trace": [],
        "workflow_version": workflow_state.WORKFLOW_VERSION,
        "principal_authorized": True,
        "session_authorized": True,
        "care_episode_authorized": True,
        "requested_route": workflow_state.Route.CONSULTANT,
        "authorized_source_ids": [],
        "source_ids": [],
        "authorized_tool_names": tools,
        "allowed_catalog_ids": [],
        "budget": workflow_policy.WorkflowPolicy(use_tool_calling=False, debug=False).build_budget(),
        "model_calls": 0,
        "tool_calls": 0,
        "total_tokens": 0,
        "idempotency_key": None,
        "request_hash": "tool-markup-contract",
        "prior_request_hash": None,
        "repair_count": 0,
    }


def _deps(model, *, use_tool_calling: bool, search=None) -> graph_dependencies.RehabGraphDeps:
    return graph_dependencies.RehabGraphDeps(
        consultant_model=model,
        vector_store=None,
        rehab_exercise_kb_search=search or (lambda query, limit=5: []),
        use_tool_calling=use_tool_calling,
    )


def _markup_events(result) -> list[dict]:
    return [
        event for event in result["internal_log_events"]
        if event.get("event_type") == "tool_markup"
        or (event.get("metadata") or {}).get("action") in {
            "request_plain_answer", "run_recovered_calls", "strip_markup",
        }
    ]


def test_trace_markup_is_detected_and_its_call_recovered() -> None:
    assert consultant_invocation._looks_like_tool_markup(TRACE_MARKUP)
    assert not consultant_invocation._looks_like_tool_markup(PLAIN_ANSWER)
    assert not consultant_invocation._looks_like_tool_markup("Keep the knee below 90 degrees <for now>.")
    calls = consultant_invocation._tool_calls_from_markup(
        TRACE_MARKUP, consultant_tools._CONSULTANT_TOOL_SCHEMAS, step=1
    )
    assert [(call["name"], call["args"]) for call in calls] == [
        ("read_episode_memory_level_2", {"session_id": "bff06306-ae9b-4fc9-9d07-1429b5672809"})
    ]


def test_markup_without_required_arguments_recovers_nothing() -> None:
    markup = "<invoke:read_episode_memory_level_2>\n</invoke>"
    assert consultant_invocation._tool_calls_from_markup(
        markup, consultant_tools._CONSULTANT_TOOL_SCHEMAS, step=1
    ) == []


def test_markup_answer_is_replaced_by_a_plain_answer() -> None:
    model = ScriptedLLM([TRACE_MARKUP, PLAIN_ANSWER])
    result = consultant_invocation.consultant_agent(
        state=_state([]), deps=_deps(model, use_tool_calling=False)
    )
    assert result["consult_response"] == PLAIN_ANSWER
    assert len(model.answer_prompts) == 2
    retry_prompt = model.answer_prompts[1]
    assert str(retry_prompt[-1].content) == CONSULTANT_TOOL_MARKUP_RETRY
    assert all("<invoke" not in str(message.content) for message in retry_prompt)
    assert "<invoke" not in result["consultant_context_snapshot"]
    assert _markup_events(result)


def test_markup_naming_an_authorized_tool_runs_that_tool() -> None:
    searches: list[str] = []

    def search(query, limit=5):
        searches.append(query)
        return []

    markup = (
        "<invoke:rehab_exercise_kb_search>\n"
        '<parameter name="queries">["lower back stretch"]</parameter>\n'
        "</invoke>"
    )
    model = ScriptedLLM([markup, PLAIN_ANSWER])
    result = consultant_invocation.consultant_agent(
        state=_state(["rehab_exercise_kb_search"]),
        deps=_deps(model, use_tool_calling=True, search=search),
    )
    assert result["consult_response"] == PLAIN_ANSWER
    assert result["tool_call_names"] == ["rehab_exercise_kb_search"]
    assert searches
    assert "<invoke" not in result["consultant_context_snapshot"]


def test_markup_in_the_retry_answer_is_stripped() -> None:
    model = ScriptedLLM([TRACE_MARKUP, PLAIN_ANSWER + "\n" + TRACE_MARKUP])
    result = consultant_invocation.consultant_agent(
        state=_state([]), deps=_deps(model, use_tool_calling=False)
    )
    assert result["consult_response"] == PLAIN_ANSWER


def main() -> None:
    test_trace_markup_is_detected_and_its_call_recovered()
    test_markup_without_required_arguments_recovers_nothing()
    test_markup_answer_is_replaced_by_a_plain_answer()
    test_markup_naming_an_authorized_tool_runs_that_tool()
    test_markup_in_the_retry_answer_is_stripped()
    print("consultant tool markup contract ok")


if __name__ == "__main__":
    main()
