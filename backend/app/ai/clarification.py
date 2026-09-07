from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from langgraph.types import Command, interrupt


WORKFLOW_VERSION = "2026-07-18"


def clarification_node(state: Mapping[str, Any]) -> Command:
    question = str(state["clarification_question"])
    required_fields = tuple(
        str(value).strip()
        for value in (state.get("clarification_required_fields") or ())
        if str(value).strip()
    )
    prior_checkpoint = state.get("clarification_checkpoint")
    if isinstance(prior_checkpoint, Mapping):
        prior_required_fields = tuple(
            str(value).strip()
            for value in (prior_checkpoint.get("required_fields") or ())
            if str(value).strip()
        )
        if prior_required_fields:
            required_fields = prior_required_fields
        prior_question = str(prior_checkpoint.get("question") or "").strip()
        if prior_question:
            question = prior_question
    if not question:
        raise ValueError("clarification question is required")
    if re.search(
        r"(?<![a-z0-9])[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+(?![a-z0-9])",
        question,
        re.IGNORECASE,
    ):
        raise ValueError("clarification question contains an internal field identifier")
    answer = interrupt(
        {
            "kind": "clarification",
            "question": question,
            "required_fields": list(required_fields),
            "workflow_version": state["workflow_version"],
        }
    )
    if not isinstance(answer, Mapping):
        raise ValueError("Clarification resume payload must be an object")
    message = str(answer.get("message") or "").strip()
    if not message:
        raise ValueError("Clarification resume message is required")
    checkpoint = {
        "question": question,
        "required_fields": list(required_fields),
        "answer": message,
        "status": "answered",
    }
    field_reasons = state.get("clarification_field_reasons")
    if isinstance(field_reasons, Mapping) and field_reasons:
        checkpoint["field_reasons"] = {
            str(key).strip(): str(value).strip()
            for key, value in field_reasons.items()
            if str(key).strip() and str(value).strip()
        }
    structured_answers = answer.get("answered_fields")
    if isinstance(structured_answers, Mapping):
        checkpoint["answered_fields"] = {str(key).strip(): value for key, value in structured_answers.items() if str(key).strip() in required_fields and isinstance(value, str) and value.strip()}
    if isinstance(prior_checkpoint, Mapping) and "answered_fields" in prior_checkpoint:
        prior_answered_fields = prior_checkpoint.get("answered_fields")
        merged = dict(prior_answered_fields) if isinstance(prior_answered_fields, Mapping) else {}
        merged.update(checkpoint.get("answered_fields", {}))
        checkpoint["answered_fields"] = merged
    return Command(
        update={
            "user_input": message,
            "clarification_answered": True,
            "clarification_checkpoint": checkpoint,
        },
        goto="context_integrity",
    )


def clarification_pending(snapshot: Any, *, workflow_version: str = WORKFLOW_VERSION) -> bool:
    values = getattr(snapshot, "values", {}) or {}
    if values.get("workflow_version") != workflow_version:
        return False
    interrupts = getattr(snapshot, "interrupts", ()) or ()
    for pending in interrupts:
        value = getattr(pending, "value", pending)
        if (
            isinstance(value, Mapping)
            and value.get("kind") == "clarification"
            and value.get("workflow_version") == workflow_version
        ):
            return True
    return False


def clarification_value(snapshot: Any) -> dict[str, Any] | None:
    for pending in getattr(snapshot, "interrupts", ()) or ():
        value = getattr(pending, "value", pending)
        if isinstance(value, Mapping) and value.get("kind") == "clarification":
            return dict(value)
    return None
