#!/usr/bin/env python3
"""Offline contract for catalog-priority AI triage guidance."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QWEN_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ai import consultant_invocation
from app.ai import graph_dependencies
from app.ai import workflow_policy
from app.ai import workflow_state
from app.services.exercise_catalog import load_exercise_catalog

CATALOG_EXERCISES = list(load_exercise_catalog())
assert len(CATALOG_EXERCISES) >= 3
CATALOG_EXERCISE = CATALOG_EXERCISES[0]
IGNORED_DOCUMENT_TYPE_EXERCISE = CATALOG_EXERCISES[1]
BAD_SCORE_EXERCISE = CATALOG_EXERCISES[2]


class FakeMessage:
    def __init__(self, content: str = "", tool_calls: list[dict] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage_metadata = {"total_tokens": 1}


class DirectPromptLLM:
    def __init__(self) -> None:
        self.prompt = ""

    def invoke(self, messages):
        self.prompt = "\n".join(str(getattr(message, "content", message)) for message in messages)
        assert "Catalog-backed exercise suggestions" not in self.prompt
        assert CATALOG_EXERCISE.title not in self.prompt
        assert "Do not invent exercise names" not in self.prompt
        return FakeMessage(f"Try the catalog-backed exercise {CATALOG_EXERCISE.title}.")


class EmptySuggestionDirectPromptLLM:
    def __init__(self) -> None:
        self.prompt = ""

    def invoke(self, messages):
        self.prompt = "\n".join(str(getattr(message, "content", message)) for message in messages)
        assert "Catalog-backed exercise suggestions" not in self.prompt
        assert CATALOG_EXERCISE.title not in self.prompt
        assert "Do not invent exercise names" not in self.prompt
        return FakeMessage("No database-backed suggestion is available yet.")


class ToolCallingLLM:
    """Scripted planner/answer model for the structured retrieval-plan loop."""

    def __init__(self, answer: str | None = None) -> None:
        self.prompts: list[str] = []
        self.plans = 0
        self.answer = answer or f"Use {CATALOG_EXERCISE.title} because it is database-backed."

    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, messages):
        prompt = "\n".join(str(getattr(message, "content", message)) for message in messages)
        self.prompts.append(prompt)
        last = str(getattr(messages[-1], "content", "")) if messages else ""
        if last.startswith("Plan the next retrieval step"):
            self.plans += 1
            if self.plans == 1:
                plan = {
                    "action": "retrieve",
                    "reason": "Catalog lookup for exercise selection.",
                    "calls": [
                        {
                            "tool_name": "rehab_exercise_kb_search",
                            "arguments": {"query": "ankle mobility", "queries": [], "limit": 5, "session_id": ""},
                            "reason": "Find catalog exercises.",
                        }
                    ],
                }
            else:
                plan = {"action": "none", "reason": "Enough evidence.", "calls": []}
            return FakeMessage(
                tool_calls=[{"id": f"plan-{self.plans}", "name": "consultant_retrieval_plan", "args": plan}]
            )
        assert "Catalog-backed exercise suggestions" not in prompt
        assert "Do not invent exercise names" not in prompt
        return FakeMessage(self.answer)


def _docs() -> list[dict]:
    return [
        {
            "score": 0.92,
            "payload": {
                "document_type": "rehab_exercise",
                "exercise_id": CATALOG_EXERCISE.exercise_id,
                "title": CATALOG_EXERCISE.title,
                "structures_involved": ["Ankle"],
                "related_conditions": ["Ankle Sprains"],
                "introduction": "Move the ankle gently.",
            },
        },
        {
            "score": 0.99,
            "payload": {
                "document_type": "rehab_article",
                "exercise_id": IGNORED_DOCUMENT_TYPE_EXERCISE.exercise_id,
                "title": "Mystery Exercise",
            },
        },
        {
            "score": "not-a-number",
            "payload": {
                "document_type": "rehab_exercise",
                "exercise_id": BAD_SCORE_EXERCISE.exercise_id,
                "title": "Bad Score",
            },
        },
        {"score": 0.88, "payload": "not-a-dict"},
        {"score": 0.87, "payload": {"document_type": "rehab_exercise", "exercise_id": "missing-title"}},
    ]


def _state() -> workflow_state.RehabGraphState:
    return {
        "user_input": "What catalog-backed exercise can I do for a mild ankle sprain?",
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
        "authorized_tool_names": ["rehab_exercise_kb_search"],
        "allowed_catalog_ids": [],
        "budget": workflow_policy.WorkflowPolicy(use_tool_calling=False, debug=False).build_budget(),
        "model_calls": 0,
        "tool_calls": 0,
        "total_tokens": 0,
        "idempotency_key": None,
        "request_hash": "catalog-contract-request",
        "prior_request_hash": None,
        "repair_count": 0,
    }


def _deps(
    model: object | None = None,
    *,
    use_tool_calling: bool = False,
    catalog_suggestions=None,
) -> graph_dependencies.RehabGraphDeps:
    return graph_dependencies.RehabGraphDeps(
        consultant_model=model,
        vector_store=None,
        rehab_exercise_kb_search=lambda query, limit=5: _docs(),
        use_tool_calling=use_tool_calling,
        catalog_suggestions=catalog_suggestions,
    )


def _empty_deps(model: object | None = None) -> graph_dependencies.RehabGraphDeps:
    return graph_dependencies.RehabGraphDeps(
        consultant_model=model,
        vector_store=None,
        rehab_exercise_kb_search=lambda query, limit=5: [],
        use_tool_calling=False,
    )


def test_direct_consultant_does_not_inject_catalog_suggestions() -> None:
    fake_llm = DirectPromptLLM()
    state = _state()
    result = consultant_invocation.consultant_agent(state=state, deps=_deps(fake_llm))
    assert result["catalog_exercise_suggestions"] == []
    assert CATALOG_EXERCISE.title in result["consult_response"]


def test_direct_consultant_keeps_empty_catalog_policy_fields() -> None:
    fake_llm = EmptySuggestionDirectPromptLLM()
    state = _state()
    result = consultant_invocation.consultant_agent(state=state, deps=_empty_deps(fake_llm))
    assert result["catalog_exercise_suggestions"] == []
    assert "No database-backed suggestion" in result["consult_response"]


def test_bound_tool_consultant_uses_model_visible_search_without_catalog_prefetch() -> None:
    fake_llm = ToolCallingLLM()
    callback_calls = []

    def forbidden_catalog_callback(_docs, _limit):
        callback_calls.append(True)
        raise AssertionError("legacy automatic catalog prefetch must not run")

    state = _state()
    result = consultant_invocation.consultant_agent(
        state=state,
        deps=_deps(
            fake_llm,
            use_tool_calling=True,
            catalog_suggestions=forbidden_catalog_callback,
        ),
    )
    assert callback_calls == []
    assert result["retrieved_docs"] == _docs()
    # Only rehab_exercise documents whose IDs resolve to catalog rows become
    # suggestions, and their content comes from the catalog row, not the payload.
    suggestions = result["catalog_exercise_suggestions"]
    assert [item["exercise_id"] for item in suggestions] == [CATALOG_EXERCISE.exercise_id]
    assert suggestions[0]["title"] == CATALOG_EXERCISE.title
    assert suggestions[0]["introduction"] == CATALOG_EXERCISE.introduction
    assert suggestions[0]["structures_involved"] == CATALOG_EXERCISE.structures_involved
    assert result["allowed_catalog_ids"] == [CATALOG_EXERCISE.exercise_id]
    assert CATALOG_EXERCISE.title in result["consult_response"]


def test_bound_tool_consultant_suggestions_pass_policy_gate_catalog_check() -> None:
    from app.ai.policy_gate import evaluate_policy_gate

    state = _state()
    result = consultant_invocation.consultant_agent(
        state=state,
        deps=_deps(ToolCallingLLM(), use_tool_calling=True),
    )
    catalog_ids = tuple(item["exercise_id"] for item in result["catalog_exercise_suggestions"])
    gate = evaluate_policy_gate(
        {"catalog_ids": catalog_ids, "allowed_catalog_ids": result["allowed_catalog_ids"]},
        policy=workflow_policy.WorkflowPolicy(),
    )
    assert catalog_ids
    assert "catalog_id_forbidden" not in gate.reason_codes


def test_bound_tool_consultant_fails_closed_when_catalog_unreadable() -> None:
    def broken_catalog(_exercise_id):
        raise RuntimeError("catalog unavailable")

    with patch("app.services.catalog_exercise_suggestions.get_exercise", broken_catalog):
        result = consultant_invocation.consultant_agent(
            state=_state(),
            deps=_deps(ToolCallingLLM(), use_tool_calling=True),
        )
    assert result["catalog_exercise_suggestions"] == []
    assert result["allowed_catalog_ids"] == []
    assert result["consult_response"]


def main() -> None:
    test_direct_consultant_does_not_inject_catalog_suggestions()
    test_direct_consultant_keeps_empty_catalog_policy_fields()
    test_bound_tool_consultant_uses_model_visible_search_without_catalog_prefetch()
    test_bound_tool_consultant_suggestions_pass_policy_gate_catalog_check()
    test_bound_tool_consultant_fails_closed_when_catalog_unreadable()
    print("ai triage catalog priority contract ok")


if __name__ == "__main__":
    main()
