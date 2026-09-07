"""Fail-closed response selection and state projection."""
from __future__ import annotations

import json
import re
from typing import Mapping

from app.ai.active_turn_context import active_context_packets
from app.ai.consultant_tools import _MEMORY_TOOL_NAMES
from app.ai.invocation_support import (
    _debug_event,
    _internal_log_events,
    _make_internal_event,
    _safe_str,
)
from app.ai.workflow_state import (
    RehabGraphState,
    Route,
    replace_reviewer_decisions,
)


def _fallback_response_for_state(state: RehabGraphState) -> tuple[str, str]:
    reasons = {
        str(value) for value in state.get("policy_gate_reason_codes", ()) or ()
    }
    feedback = _safe_str(state.get("safety_feedback", ""), 500).strip()

    def _nonempty_retrieval_available() -> bool:
        if state.get("retrieved_docs"):
            return True
        for event in state.get("tool_events", ()) or ():
            if not isinstance(event, Mapping):
                continue
            if event.get("outcome") == "ok_nonempty":
                return True
            result_count = event.get("result_count")
            if (
                isinstance(result_count, int)
                and not isinstance(result_count, bool)
                and result_count > 0
            ):
                return True
        return False

    def _review_status(reviewer: str) -> str:
        for decision in reversed(state.get("reviewer_decisions") or ()):
            value = (
                decision.get("reviewer")
                if isinstance(decision, Mapping)
                else getattr(decision, "reviewer", "")
            )
            if str(value or "") != reviewer:
                continue
            status = (
                decision.get("status")
                if isinstance(decision, Mapping)
                else getattr(decision, "status", "")
            )
            return str(getattr(status, "value", status) or "")
        return ""

    def _retrieval_states() -> set[str]:
        states: set[str] = set()
        raw_status = state.get("retrieval_status")
        if isinstance(raw_status, Mapping):
            states.update(
                str(value or "").strip().lower()
                for value in raw_status.values()
                if str(value or "").strip()
            )
        for event in state.get("tool_events", ()) or ():
            if isinstance(event, Mapping):
                outcome = str(event.get("outcome") or "").strip().lower()
                if outcome:
                    states.add(outcome)
        if "query_not_searchable" in states:
            states.discard("query_not_searchable")
            states.add("failed")
        return states

    retrieval_states = _retrieval_states()
    retrieval_failed = bool(
        retrieval_states & {"failed", "timeout", "blocked", "invalid"}
    )
    retrieval_empty = "ok_empty" in retrieval_states
    grounding_status = _review_status("grounding")
    safety_status = _review_status("safety")

    if state.get("if_emergency") or state.get("route") in {
        Route.URGENT,
        Route.URGENT.value,
    }:
        return (
            f"This may need urgent medical attention: {_safe_str(state.get('emergency_reason', ''), 500)} "
            "Please seek immediate care rather than following an exercise plan here.",
            "emergency",
        )
    if any(
        reason in {"model_budget_exceeded", "tool_budget_exceeded"}
        for reason in reasons
    ):
        return (
            "I could not finish processing this request reliably. Please try the same question again; "
            "I have not provided a recommendation.",
            "processing_limit",
        )
    if "deadline_exceeded" in reasons or "time_unavailable" in reasons:
        return (
            "I could not finish this request in time. Please try again, and seek clinical care "
            "if the symptom is severe, worsening, or concerning.",
            "review_timeout",
        )
    if any(
        reason in {"provider_failure", "model_provider_failure"}
        for reason in reasons
    ):
        return (
            "The recommendation service is temporarily unavailable, so I cannot safely generate advice right now. "
            "Please try again shortly or consult a licensed professional.",
            "provider_unavailable",
        )
    if grounding_status in {"error", "timeout"} or any(
        reason in {"review_grounding_error", "review_grounding_timeout"}
        for reason in reasons
    ):
        return (
            "I encountered an error and cannot provide a reliable recommendation right now. "
            "Please try again or consult a licensed professional before changing your plan.",
            "grounding_review_incomplete",
        )

    def _bounded_context_fallback() -> tuple[str, str] | None:
        packets = active_context_packets(state.get("evidence_turn_id"))
        candidates = [
            str(state.get("memory_context") or ""),
            str(packets.get("consultant") or ""),
            str(packets.get("router") or ""),
        ]
        facts: list[str] = []
        goal_key_pattern = re.compile(
            r"(?:^|_)(?:goal|preference|prefer|constraint|limit)(?:$|_)",
            re.IGNORECASE,
        )

        def add_editable_facts(editable: object) -> None:
            if not isinstance(editable, Mapping):
                return
            for key, value in editable.items():
                key_text = re.sub(
                    r"[^a-z0-9]+", "_", str(key or "").strip().lower()
                ).strip("_")
                value_text = str(value or "").strip()
                if value_text and goal_key_pattern.search(key_text):
                    facts.append(value_text)

        for candidate_index, text in enumerate(candidates):
            source_blocks = re.findall(
                r"(?ms)(?:^|\n)Source:[^\n]*\n.*?(?=\n\nSource:|\Z)",
                text,
            )
            if not source_blocks and candidate_index == 0 and text:
                editable_match = re.search(
                    r"(?is)Editable fields:\s*(\{.*?\})(?:\n\n|\Z)",
                    text,
                )
                if editable_match:
                    try:
                        editable = json.loads(editable_match.group(1))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        editable = {}
                    add_editable_facts(editable)
            for raw_block in source_blocks:
                block = raw_block.lstrip()
                source_type = ""
                for line in block.splitlines():
                    if line.startswith("Type: "):
                        source_type = line[6:].strip()
                content = block.split("\nContent:\n", 1)[-1]
                content = content.split("\n\nIncluded sources:", 1)[0].strip()
                if source_type == "patient_memory_summary":
                    editable_match = re.search(
                        r"(?is)Editable fields:\s*(\{.*?\})(?:\n\n|\Z)",
                        content,
                    )
                    editable_line = editable_match.group(1) if editable_match else ""
                    try:
                        editable = json.loads(editable_line) if editable_line else {}
                    except (TypeError, ValueError, json.JSONDecodeError):
                        editable = {}
                    add_editable_facts(editable)
                    if not editable_line and content:
                        facts.append(content)
        unique_facts = list(
            dict.fromkeys(value.strip() for value in facts if value.strip())
        )
        fact = "; ".join(unique_facts)[:900]
        if not fact:
            return None
        retrieval_note = ""
        if retrieval_failed:
            retrieval_note = (
                "\n\nThe requested evidence provider was unavailable, so I cannot verify "
                "a rehabilitation plan from that search."
            )
        elif retrieval_empty:
            retrieval_note = (
                "\n\nThe evidence search completed without usable evidence, so I cannot "
                "verify a rehabilitation plan from that search."
            )
        return (
            "I can keep this within the authorized goal or preference already available: "
            f"{fact}\n\n"
            "I cannot safely name an exercise or add dosing, repetitions, progression, or clearance "
            "without an authorized rehabilitation plan. Use the plan you already have or share its "
            "relevant instruction before changing it."
            f"{retrieval_note}",
            "bounded_context_only",
        )

    if (
        safety_status == "approve"
        and grounding_status == "approve"
        and not (
            state.get("needs_more_info")
            or state.get("route") in {Route.CLARIFY, Route.CLARIFY.value}
        )
    ):
        bounded_context = _bounded_context_fallback()
        if bounded_context is not None:
            return bounded_context
    if safety_status == "reject" and grounding_status == "approve":
        return (
            "I cannot provide a reliable recommendation right now. Please try again or consult a licensed professional.",
            "safety_review_rejected",
        )
    if grounding_status == "reject" and safety_status == "approve":
        if retrieval_failed:
            return (
                "The requested evidence provider was unavailable, so I cannot verify a "
                "recommendation. Please try again or consult a licensed "
                "professional before changing the plan.",
                "grounding_retrieval_failed",
            )
        if retrieval_empty:
            return (
                "The evidence search completed without usable evidence, so I cannot verify a "
                "recommendation. Please share the relevant "
                "rehabilitation instructions or consult a licensed professional before changing the plan.",
                "grounding_retrieval_empty",
            )
        return (
            "I do not have enough supporting evidence to provide a reliable recommendation. Please try again or consult a licensed professional.",
            "grounding_review_rejected",
        )
    if feedback:
        return (
            "I cannot verify that recommendation reliably, so I will not provide it as advice. Please try again or consult a licensed professional.",
            "safety_review_rejected",
        )
    if state.get("needs_more_info") or state.get("route") in {Route.CLARIFY, Route.CLARIFY.value}:
        question = _safe_str(state.get("clarification_question") or state.get("follow_up_question"), 700).strip()
        if question:
            return question, "clarification_required"
    if grounding_status == "reject" or "review_grounding_reject" in reasons:
        if retrieval_failed:
            return (
                "The requested evidence provider was unavailable, so I cannot verify a "
                "recommendation. Please try again or consult "
                "a licensed professional before changing the plan.",
                "grounding_retrieval_failed",
            )
        if retrieval_empty:
            return (
                "The evidence search completed without usable evidence, so I cannot verify a "
                "recommendation. Please share the relevant "
                "rehabilitation instructions or consult a licensed professional before changing the plan.",
                "grounding_retrieval_empty",
            )
        if _nonempty_retrieval_available():
            return (
                "I could not form a reliable recommendation from the available evidence. Please try again or "
                "consult a licensed professional before changing the plan.",
                "grounding_review_rejected_with_evidence",
            )
        return (
            "I do not have enough authorized rehabilitation evidence to give a grounded exercise "
            "recommendation. Please share the relevant rehabilitation instructions, plan, or "
            "episode details, or consult a licensed professional before changing the plan.",
            "grounding_review_rejected",
        )
    if state.get("route") in {Route.UNSUPPORTED, Route.UNSUPPORTED.value}:
        return (
            "This request is outside the rehabilitation support I can provide. Please consult an appropriate "
            "licensed professional for guidance.",
            "unsupported_request",
        )
    tool_names = {
        str(value) for value in (state.get("tool_call_names") or ()) if str(value)
    }
    if tool_names.intersection(_MEMORY_TOOL_NAMES):
        return (
            "I could not complete the earlier-visit lookup right now. Please try again, "
            "or tell me the detail you want recalled in your own words.",
            "memory_recall_incomplete",
        )
    return (
        "I encountered an error and cannot provide a reliable exercise plan right now. "
        "Please try again or consult a licensed professional before changing your plan.",
        "safety_review_incomplete",
    )


def unsafe_fallback_agent(state: RehabGraphState) -> RehabGraphState:
    msg, fallback_category = _fallback_response_for_state(state)
    return {
        "final_response": msg,
        "user_input": "",
        "reviewer_decisions": replace_reviewer_decisions(),
        "debug_trace": _debug_event(
            state,
            "unsafe_fallback",
            {
                "attempts": state.get("consult_attempts", 0),
                "last_feedback": _safe_str(state.get("safety_feedback", ""), 300),
                "fallback_category": fallback_category,
                "policy_gate_reason_codes": list(
                    state.get("policy_gate_reason_codes") or ()
                ),
            },
        ),
        "internal_log_events": _internal_log_events(
            state,
            _make_internal_event(
                "unsafe_fallback",
                "agent_output",
                msg,
                attempts=state.get("consult_attempts", 0),
                last_feedback=state.get("safety_feedback", ""),
                fallback_category=fallback_category,
                policy_gate_reason_codes=list(
                    state.get("policy_gate_reason_codes") or ()
                ),
            ),
        ),
    }
