"""Initial workflow-control state projection and legacy request identity.

State contract: writes the existing workflow version, budget, repair/evidence and
review control fields with their original types and nullability. The legacy hash
continues to bind message, session, optional Care Episode, and workflow version.
No content-bearing field is persisted by this module.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from app.ai.workflow_policy import WorkflowPolicy
from app.ai.workflow_state import WORKFLOW_VERSION


def _initial_workflow_control_state(
    policy: WorkflowPolicy,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    return {
        "workflow_version": WORKFLOW_VERSION,
        "budget": policy.build_budget(now=now),
        "repair_count": 0,
        "repair_action_count": 0,
        "repair_requires_revalidation": False,
        "force_revalidate_context": False,
        "discard_previous_evidence": False,
        "exercise_request": False,
        "evidence_results": [],
        "retrieval_status": {},
        "reviewer_decisions": [],
        "reviewer_decision_history": [],
        "review_attempt": 0,
        "review_turn_id": "",
        "clarification_field_reasons": {},
    }


def _legacy_request_hash(
    *,
    user_input: str,
    session_id: uuid.UUID,
    care_episode_id: str | None,
) -> str:
    payload = {
        "care_episode_id": care_episode_id,
        "message": user_input,
        "session_id": str(session_id),
        "workflow_version": WORKFLOW_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
