#!/usr/bin/env python3
"""Offline contract: the grounding verdict follows the clauses the reviewer names."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("QWEN_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ai import review_nodes
from app.ai.grounding_reviewer import GroundingReviewerOutput, grounding_reviewer_agent, grounding_verdict
from app.ai.runtime_dependencies import ReviewerRuntimeConfig
from app.ai.workflow_state import ReviewStatus

DRAFT = "You've told me your lower back is stiff, and we went over the two stretches we discussed."
# Attempt 4 of the traced turn: feedback approves, flag rejects, nothing named.
TRACE_ATTEMPT_4 = {
    "grounding_feedback": (
        "The draft restates the stiffness the patient reported. The phrase 'the two stretches "
        "we discussed' refers to stretches in the session. Overall the draft is acceptable as grounded."
    ),
    "unsupported_clauses": [],
    "grounding_passed": False,
}


class FakeStructured:
    def __init__(self, output: dict) -> None:
        self.output = output
        self.schemas: list[type] = []

    def with_structured_output(self, schema, **_kwargs):
        self.schemas.append(schema)
        return self

    def invoke(self, _messages):
        return dict(self.output)


def _node_status(output: dict) -> tuple[ReviewStatus, str, dict]:
    review_input = review_nodes.ReviewInput(
        draft=DRAFT,
        evidence_manifest=review_nodes.EvidenceManifest(),
        attempt=4,
        review_turn_id="turn-1",
        context_snapshot="Patient: my lower back is stiff in the mornings.",
    )
    result = asyncio.run(
        review_nodes.grounding_reviewer_node(
            {"review_input": review_input},
            model=FakeStructured(output),
            runtime=ReviewerRuntimeConfig(),
        )
    )
    decision = result["reviewer_decisions"][0]
    return decision.status, decision.feedback, result["internal_log_events"][0]["metadata"]


def test_schema_asks_for_reasoning_and_clauses_before_the_flag() -> None:
    fields = list(GroundingReviewerOutput.model_fields)
    assert fields.index("grounding_feedback") < fields.index("unsupported_clauses") < fields.index("grounding_passed")
    assert "unsupported_clauses" in GroundingReviewerOutput.model_json_schema()["required"]


def test_false_flag_with_no_named_clause_passes() -> None:
    status, feedback, metadata = _node_status(TRACE_ATTEMPT_4)
    assert status is ReviewStatus.APPROVE
    assert feedback == TRACE_ATTEMPT_4["grounding_feedback"]
    assert metadata["grounding_flag_disagreed"] is True


def test_named_clause_rejects_and_reaches_the_repair_feedback() -> None:
    output = {
        "grounding_feedback": "The draft claims the patient denied leg pain; the context does not say so.",
        "unsupported_clauses": ["'your pain isn't spreading into your leg' -> delete"],
        "grounding_passed": False,
    }
    status, feedback, metadata = _node_status(output)
    assert status is ReviewStatus.REJECT
    assert "'your pain isn't spreading into your leg' -> delete" in feedback
    assert metadata["grounding_flag_disagreed"] is False


def test_named_clause_rejects_even_when_the_flag_says_passed() -> None:
    output = {
        "grounding_feedback": "One clause is unsupported.",
        "unsupported_clauses": ["'hold each stretch for 60 seconds' -> delete"],
        "grounding_passed": True,
    }
    status, _feedback, metadata = _node_status(output)
    assert status is ReviewStatus.REJECT
    assert metadata["grounding_flag_disagreed"] is True


def test_blank_clauses_count_as_none() -> None:
    passed, _ = grounding_verdict(
        GroundingReviewerOutput(grounding_feedback="Grounded.", unsupported_clauses=["  "], grounding_passed=False)
    )
    assert passed is True


def test_standalone_reviewer_uses_the_same_verdict() -> None:
    result = grounding_reviewer_agent({"consult_response": DRAFT}, FakeStructured(TRACE_ATTEMPT_4))
    assert result["grounding_passed"] is True
    assert result["grounding_review_status"] == ReviewStatus.APPROVE.value


def main() -> None:
    test_schema_asks_for_reasoning_and_clauses_before_the_flag()
    test_false_flag_with_no_named_clause_passes()
    test_named_clause_rejects_and_reaches_the_repair_feedback()
    test_named_clause_rejects_even_when_the_flag_says_passed()
    test_blank_clauses_count_as_none()
    test_standalone_reviewer_uses_the_same_verdict()
    print("grounding verdict contract ok")


if __name__ == "__main__":
    main()
