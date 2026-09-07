"""Grounding review over a consultant draft and authorized evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from app.ai.active_turn_context import active_triaged_context
from app.ai.runtime_dependencies import structured_output_model
from app.ai.workflow_state import ReviewStatus, ReviewerDecision


class GroundingReviewerOutput(BaseModel):
    """Minimal model output; clinical critique remains natural language."""

    model_config = ConfigDict(extra="forbid")

    grounding_passed: bool = Field(description="Whether the draft is grounded in authorized evidence.")
    grounding_feedback: str = Field(description="Natural-language grounding feedback.")


_SYSTEM_PROMPT = """You are RehabFlow's grounding reviewer.
Review the consultant draft against only the authorized evidence packets, validated exercise-catalog entries, and explicit evidence gaps supplied below.
The frozen Consultant-visible context is also authorized evidence for facts it explicitly contains, including active Patient or Episode Memory and current-session messages. A patient-specific claim may rely on that context only when it directly paraphrases the fact or follows an explicit boundary in it; do not infer a treatment rule that the context does not state.
Direct restatement of an explicit current-session fact or monitoring boundary is authorized. A conditional reminder to follow the user's own surgeon, clinician, or therapist instructions, without inventing those instructions, is an evidence-boundary statement rather than an unsupported treatment prescription.
Require direct semantic support for every material patient fact, diagnosis, exercise choice, exercise instruction, dose, progression, clearance, treatment action, causal claim, and comparative claim. Do not treat common knowledge, plausibility, generic titles, placeholders, or unstated clinical equivalence as support.
A concise, non-diagnostic safety-net boundary does not need to repeat source wording verbatim. It may conditionally tell the patient to pause or stop if symptoms materially worsen, or to seek care for a severe or new concerning change relevant to the visible presentation. Treat this as a boundary on the advice, not as evidence of a diagnosis or as an unsupported treatment claim.
Reject specific unsupported thresholds, treatment details, and unrelated warning checklists.
Allow clearly labeled general education without evidence when it remains non-individualized and high-level. Do not approve personalized clearance, exact dosing, named exercise selection, or progression thresholds under that exception.
For evidence-backed details, require the material qualifiers in the draft to match the source. A validated catalog title may appear in patient-facing prose without its internal ID, but the underlying exercise selection must be authorized.
When an authorized source pairs an allowance with a restriction or deferral, treat the restriction as material when the draft uses that allowance. If the draft omits or conflicts with that restriction, reject it and state the missing source fact and concrete repair needed in grounding_feedback. Feedback must be actionable for a repair pass.
Preserve the latest timing, symptom, goal, allowance, restriction, and lifecycle state in authorized context. Do not approve inactive memory as current evidence or infer a broader treatment rule from a narrow fact.
Regardless of packet availability, reject invented patient facts, diagnoses, nonexistent exercise IDs, citations to unavailable sources, deleted or archived memory content reused as current, and certainty stronger than the evidence supports.
Clinical meaning and grounding are your judgment. An empty evidence packet is valid, not a grounding failure by itself.
Do not reject a draft solely because evidence is absent when it is bounded general education. If a draft mixes scopes, identify only the unsupported patient-specific clauses in grounding_feedback and allow the clearly framed general-education remainder to survive repair. Return only the requested structured result with concise natural-language feedback."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _manifest_from_state(state: Mapping[str, Any]) -> tuple[list[Any], tuple[str, ...], tuple[str, ...], list[dict[str, Any]]]:
    triaged = active_triaged_context(state.get("evidence_turn_id"))
    if triaged is None:
        return [], (), (), []
    return (
        list(getattr(triaged, "packets", ()) or ()),
        tuple(getattr(triaged, "unavailable_evidence", ()) or ()),
        tuple(getattr(triaged, "omitted_source_ids", ()) or ()),
        [dict(item) for item in (getattr(triaged, "catalog_exercise_suggestions", ()) or ())],
    )


def _evidence_prompt(
    packets: list[Any],
    unavailable_evidence: tuple[str, ...],
    omitted_source_ids: tuple[str, ...],
    catalog_suggestions: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    packet_lines = ["Authorized evidence packets:"]
    packet_ids: list[str] = []
    for packet in packets:
        source_id = str(getattr(packet, "source_id", "")).strip()
        if not source_id:
            continue
        packet_ids.append(source_id)
        packet_lines.append(
            f"- Packet {source_id}: {_text(getattr(packet, 'content', ''))}"
        )

    catalog_ids = sorted(
        {
            str(item.get("exercise_id")).strip()
            for item in catalog_suggestions
            if item.get("exercise_id")
        }
    )
    packet_lines.append(
        "Validated catalog exercise IDs: " + (", ".join(catalog_ids) if catalog_ids else "none")
    )
    evidence_gaps = sorted({*unavailable_evidence, *omitted_source_ids})
    if not packet_ids:
        packet_lines.append(
            "Evidence availability: no validated evidence packets are available for this turn."
        )
        packet_lines.append(
            "Empty evidence rule: absence of packets is not itself a grounding failure. "
            "Approve cautious general guidance and clearly labeled, non-individualized educational examples. "
            "For an explicitly general-education request, allow high-level phases, goals, symptom-monitoring concepts, "
            "and concise warning-sign examples without treating ordinary educational verbs as prescriptions. "
            "If the frozen authorized context states a current memory or session boundary, a direct answer may use that fact "
            "without requiring a public packet. Do not convert examples into a personalized plan. Reject deleted or archived "
            "memory content reused as current, invented facts, diagnoses, or stronger certainty."
        )
    packet_lines.append(
        "Evidence gaps: "
        + (", ".join(evidence_gaps) if evidence_gaps else "none recorded")
    )
    return "\n".join(packet_lines), {
        "packet_ids": sorted(set(packet_ids)),
        "catalog_ids": catalog_ids,
        "evidence_available": bool(packet_ids),
        "unavailable_evidence": sorted(set(unavailable_evidence)),
        "omitted_source_ids": sorted(set(omitted_source_ids)),
    }


def _trace_event(state: Mapping[str, Any], status: ReviewStatus, passed: bool, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    trace = list(state.get("debug_trace", []) or [])
    trace.append(
        {
            "step": len(trace) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node": "grounding_reviewer",
            "reviewer": "grounding",
            "status": status.value,
            "grounding_passed": passed,
            "attempt": state.get("consult_attempts", 0),
            **dict(metadata),
        }
    )
    return trace


def grounding_reviewer_agent(
    state: Mapping[str, Any],
    model: Any,
) -> dict[str, Any]:
    """Review one consultant draft using an injected structured-output model."""
    draft = _text(state.get("consult_response", state.get("consultant_draft", "")))
    packets, unavailable_evidence, omitted_source_ids, catalog_suggestions = _manifest_from_state(state)
    evidence_prompt, metadata = _evidence_prompt(
        packets,
        unavailable_evidence,
        omitted_source_ids,
        catalog_suggestions,
    )
    user_prompt = (
        f"Consultant draft:\n{draft or 'none'}\n\n"
        f"{evidence_prompt}"
    )

    status = ReviewStatus.ERROR
    passed = False
    feedback = "Grounding review could not be completed. Approval is not available."
    try:
        if model is None:
            raise ValueError("grounding reviewer model is not configured")
        structured_model = structured_output_model(model, GroundingReviewerOutput)
        raw_result = structured_model.invoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ]
        )
        result = (
            raw_result
            if isinstance(raw_result, GroundingReviewerOutput)
            else GroundingReviewerOutput.model_validate(raw_result)
        )
        feedback = result.grounding_feedback.strip()
        if not feedback:
            raise ValueError("grounding reviewer feedback is blank")
        passed = result.grounding_passed is True
        status = ReviewStatus.APPROVE if passed else ReviewStatus.REJECT
    except Exception as exc:
        feedback = f"Grounding review failed ({type(exc).__name__}); approval is not available."

    decision = ReviewerDecision(
        reviewer="grounding",
        status=status,
        feedback=feedback,
    )
    return {
        "grounding_passed": passed if status is not ReviewStatus.ERROR else False,
        "grounding_feedback": feedback,
        "grounding_review_status": status.value,
        "reviewer_decisions": [decision],
        "debug_trace": _trace_event(state, status, passed if status is not ReviewStatus.ERROR else False, metadata),
    }
