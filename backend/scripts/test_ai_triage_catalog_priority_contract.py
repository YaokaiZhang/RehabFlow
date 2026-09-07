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
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        prompt = "\n".join(str(getattr(message, "content", message)) for message in messages)
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return FakeMessage(
                tool_calls=[
                    {
                        "id": "tool-call-1",
                        "name": "rehab_exercise_kb_search",
                        "args": {"query": "ankle mobility", "limit": 5},
                    }
                ]
            )
        assert "Catalog-backed exercise suggestions" not in prompt
        assert "Do not invent exercise names" not in prompt
        return FakeMessage(f"Use {CATALOG_EXERCISE.title} because it is database-backed.")


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
    assert result["catalog_exercise_suggestions"] == []
    assert CATALOG_EXERCISE.title in result["consult_response"]


def main() -> None:
    test_direct_consultant_does_not_inject_catalog_suggestions()
    test_direct_consultant_keeps_empty_catalog_policy_fields()
    test_bound_tool_consultant_uses_model_visible_search_without_catalog_prefetch()
    print("ai triage catalog priority contract ok")


if __name__ == "__main__":
    main()
