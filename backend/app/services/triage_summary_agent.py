from __future__ import annotations

import os
from uuid import UUID
from typing import Any

from sqlalchemy import Integer, func, select, text
from sqlalchemy.orm import Session

from app.ai.runtime_dependencies import openai_client_options
from app.core.config import get_settings
from app.db.models import AIInternalMessage, AIMessage, AISession, CareEpisode, CareEpisodeTriageSummary, ai_message_ordering
from app.services.rehab_session_context import load_latest_rehab_session_context


class TriageSummarySessionNotFound(Exception):
	"""Raised when a requested AI triage session is not owned by the episode patient."""


class TriageSummaryGenerationFailed(Exception):
	"""Raised when the required LLM Triage Summary path cannot produce a summary."""


def _mentions_any(text: str, terms: list[str]) -> bool:
	lowered = text.lower()
	return any(term in lowered for term in terms)


def _mentions_negated(text: str, terms: list[str]) -> bool:
	lowered = text.lower()
	for term in terms:
		phrases = [
			f"no {term}",
			f"no new {term}",
			f"no sudden {term}",
			f"without {term}",
			f"denies {term}",
			f"denied {term}",
			f"not have {term}",
			f"did not have {term}",
			f"didn't have {term}",
		]
		if any(phrase in lowered for phrase in phrases):
			return True
	return False


def _has_positive_swelling(text: str) -> bool:
	return _mentions_any(text, ["swelling", "swollen", "puffy"]) and not _mentions_negated(text, ["swelling", "swollen", "puffy"])


def _has_positive_surgery(text: str) -> bool:
	if _mentions_any(text, ["post-op", "postoperative"]):
		return True
	if _mentions_any(text, ["did not do surgery", "didn't do surgery", "no surgery", "without surgery", "no surgery history"]):
		return False
	return "surgery" in text.lower()


def _has_pain_context(text: str) -> bool:
	return _mentions_any(
		text,
		["no pain", "without pain", "pain is", "pain level", "pain score", "/10", "mild", "moderate", "severe"],
	)


def _has_weight_bearing_context(text: str) -> bool:
	return _mentions_any(
		text,
		["can bear weight", "cannot bear weight", "weight-bearing", "weight bearing normally", "walking normally", "walk normally", "no walking changes"],
	)


def _has_bruising_context(text: str) -> bool:
	return _mentions_any(text, ["no bruising", "no bruise", "bruising is", "visible bruise", "purple", "black-and-blue"])


def _has_neurologic_context(text: str) -> bool:
	return _mentions_any(
		text,
		["no numbness", "no tingling", "numb", "tingling", "sensation", "pins and needles", "neurologic"],
	)


def _has_chronic_stable_context(text: str) -> bool:
	return (
		_mentions_any(text, ["six years ago", "years ago", "old", "chronic", "long-standing"])
		and _has_pain_context(text)
		and _mentions_negated(text, ["swelling", "swollen", "puffy"])
		and _mentions_any(text, ["no changes", "no change", "walking", "stairs", "range of motion"])
	)


def _trim_text(text: str, limit: int) -> str:
	if len(text) <= limit:
		return text
	return f"{text[:limit].rstrip()}..."


def _format_ai_session_transcript(rows: list[AIMessage]) -> str:
	role_labels = {"user": "Patient", "assistant": "RehabFlow"}
	return "\n".join(
		f"{role_labels.get(row.sender_role, row.sender_role)}: {row.content.strip()}"
		for row in rows
		if row.content.strip()
	)


def _load_ai_session_rows(db: Session, patient_id: UUID, ai_session_id: UUID | None) -> list[AIMessage]:
	if ai_session_id is None:
		return []
	session = db.get(AISession, ai_session_id)
	if session is None or session.patient_id != patient_id:
		raise TriageSummarySessionNotFound("AI triage session not found")
	return list(
		db.execute(
			select(AIMessage)
			.where(AIMessage.session_id == ai_session_id)
			.where(AIMessage.sender_role.in_(["user", "assistant"]))
			.order_by(*ai_message_ordering())
		).scalars().all()
	)


def _load_ai_session_transcript(db: Session, patient_id: UUID, ai_session_id: UUID | None) -> str:
	rows = _load_ai_session_rows(db, patient_id, ai_session_id)
	# Bound by characters after formatting complete messages. Line-based slicing lets
	# one long assistant response evict the patient's original concern.
	return _trim_text(_format_ai_session_transcript(rows), 6000)


def load_triage_conversation_context(db: Session, patient_id: UUID, ai_session_id: UUID | None) -> str:
	return _load_ai_session_transcript(db, patient_id, ai_session_id)


def load_full_triage_conversation_context(db: Session, patient_id: UUID, ai_session_id: UUID | None) -> str:
	return _format_ai_session_transcript(_load_ai_session_rows(db, patient_id, ai_session_id))


def _patient_triage_lines(conversation_context: str) -> list[str]:
	patient_lines: list[str] = []
	for raw_line in conversation_context.splitlines():
		line = raw_line.strip()
		if not line.startswith("Patient:"):
			continue
		content = line.removeprefix("Patient:").strip()
		if content:
			patient_lines.append(content)
	return patient_lines


def _goal_body_area(body_area: str) -> str:
	words = body_area.lower().split()
	if words and words[0] in {"left", "right"}:
		words = words[1:]
	return " ".join(words) or "rehab concern"


def _derive_patient_goal(body_area: str, modifier: str) -> str:
	if body_area == "General rehab concern":
		return "Clarify and improve the rehab concern"
	goal_area = _goal_body_area(body_area)
	if modifier == "stiffness":
		return f"Relieve {goal_area} stiffness"
	if modifier == "pain":
		return f"Reduce {goal_area} pain"
	return f"Improve {goal_area} function"


def derive_episode_fields_from_triage_context(additional_context: str, conversation_context: str) -> dict[str, str]:
	combined = " ".join([additional_context, conversation_context]).strip()
	patient_context = " ".join(_patient_triage_lines(conversation_context)).strip()
	episode_context = " ".join([patient_context, additional_context]).strip()
	# Patient-reported episode fields should not be inferred from assistant suggestions,
	# which may mention unrelated body areas or exercises.
	inference_text = episode_context or combined
	lowered = inference_text.lower()
	body_terms = [
		"shoulder",
		"ankle",
		"knee",
		"back",
		"hip",
		"neck",
		"elbow",
		"wrist",
		"foot",
		"hamstring",
		"calf",
	]
	body_area = next((term for term in body_terms if term in lowered), "General rehab concern")
	if body_area != "General rehab concern":
		prefix = "Right " if f"right {body_area}" in lowered else "Left " if f"left {body_area}" in lowered else ""
		body_area = f"{prefix}{body_area}".strip().title()

	modifier = "stiffness" if "stiff" in lowered else "pain" if "pain" in lowered else "rehab concern"
	issue_title = f"{body_area} {modifier}" if body_area != "General rehab concern" else "AI triage rehab concern"
	goal = _derive_patient_goal(body_area, modifier)
	short_description = _trim_text(episode_context or "Created from AI triage intake.", 800)
	if len(short_description) < 5:
		short_description = "Created from AI triage intake."
	return {
		"issue_title": issue_title[:255],
		"body_area": body_area[:128],
		"goal": goal[:255],
		"short_description": short_description[:4000],
	}


def _build_fallback_summary(
	episode: CareEpisode,
	additional_context: str,
	conversation_context: str,
	version: int,
	rehab_context: str = "",
) -> CareEpisodeTriageSummary:
	combined = " ".join(
		[
			episode.issue_title,
			episode.body_area,
			episode.goal,
			episode.short_description,
			additional_context,
			conversation_context,
			rehab_context,
		]
	).lower()
	stable_chronic_context = _has_chronic_stable_context(combined)
	missing_information: list[str] = []
	if not stable_chronic_context and not _has_weight_bearing_context(combined):
		missing_information.append("weight-bearing tolerance")
	if not stable_chronic_context and not _has_bruising_context(combined):
		missing_information.append("bruising")
	if not stable_chronic_context and not _has_neurologic_context(combined):
		missing_information.append("numbness or tingling")
	if not _has_pain_context(combined):
		missing_information.append("pain intensity and behavior")
	if _mentions_any(combined, ["shoulder", "overhead", "reaching"]) and not _mentions_any(
		combined, ["range of motion", "range limits", "rom", "can reach", "cannot reach", "no changes"]
	):
		missing_information.append("range of motion limits")

	positive_swelling = _has_positive_swelling(combined)
	positive_surgery = _has_positive_surgery(combined)
	safety_signals: list[str] = []
	if positive_swelling:
		safety_signals.append("Swelling is present or recently reported.")
	if positive_surgery:
		safety_signals.append("Post-operative context may require clinician restrictions.")
	if not safety_signals:
		safety_signals.append("No explicit red-flag symptoms were captured in the available episode context.")

	limitations = [
		"This summary is generated from the currently saved episode details and patient-provided triage context.",
	]
	if missing_information:
		limitations.append("Important triage fields are still missing, so recommendations should stay conservative.")

	unresolved_questions = [f"Please clarify {item}." for item in missing_information]
	clinician_review_needed = bool(missing_information) or positive_swelling or positive_surgery or _mentions_any(combined, ["severe"]) or (
		_mentions_any(combined, ["numb", "tingling"]) and not _mentions_negated(combined, ["numbness", "tingling", "numb"])
	)
	recommendation = (
		"Use gentle, low-risk activity only and complete the missing triage details before AI Daily Rehab progression."
		if missing_information
		else "Episode context is sufficient for cautious AI Daily Rehab guidance, while monitoring for symptom changes."
	)
	context_parts = [
		f"Body area: {episode.body_area}.",
		f"Goal: {episode.goal}.",
		f"Episode description: {episode.short_description}.",
		f"Additional context: {additional_context or 'None provided.'}",
	]
	if conversation_context:
		context_parts.append(f"AI triage transcript:\n{conversation_context}")
	if rehab_context:
		context_parts.append(rehab_context)

	return CareEpisodeTriageSummary(
		care_episode_id=episode.care_episode_id,
		patient_id=episode.patient_id,
		version=version,
		concern=episode.issue_title,
		relevant_context=" ".join(context_parts),
		safety_signals=safety_signals,
		limitations=limitations,
		recommendation=recommendation,
		missing_information=missing_information,
		unresolved_questions=unresolved_questions,
		clinician_review_needed=clinician_review_needed,
		additional_context=additional_context,
	)


def _json_safe(value: Any) -> Any:
	if isinstance(value, (str, int, float, bool)) or value is None:
		return value
	if isinstance(value, dict):
		return {str(key): _json_safe(item) for key, item in value.items()}
	if isinstance(value, list):
		return [_json_safe(item) for item in value]
	return str(value)


def _summary_log_metadata(summary: CareEpisodeTriageSummary) -> dict[str, Any]:
	return {
		"version": summary.version,
		"clinician_review_needed": summary.clinician_review_needed,
		"safety_signal_count": len(summary.safety_signals or []),
		"limitation_count": len(summary.limitations or []),
		"missing_information_count": len(summary.missing_information or []),
		"unresolved_question_count": len(summary.unresolved_questions or []),
	}


def _log_summary_agent_output(
	db: Session,
	*,
	patient_id: UUID,
	ai_session_id: UUID | None,
	event_type: str,
	summary: CareEpisodeTriageSummary,
) -> None:
	if ai_session_id is None:
		return
	metadata = _json_safe(_summary_log_metadata(summary))
	try:
		bind = getattr(db, "bind", None)
		if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
			db.execute(
				text(
					"SELECT pg_advisory_xact_lock("
					"hashtextextended(CAST(:session_id AS text), 0))"
				),
				{"session_id": str(ai_session_id)},
			)
		current_sequence = db.execute(
			select(func.max(AIInternalMessage.event_metadata["sequence"].astext.cast(Integer)))
			.where(AIInternalMessage.session_id == ai_session_id)
		).scalar()
	except Exception:
		current_sequence = 0
	metadata["sequence"] = int(current_sequence or 0) + 1
	db.add(
		AIInternalMessage(
			session_id=ai_session_id,
			patient_id=patient_id,
			agent_name="triage_summary_agent",
			event_type=event_type,
			content="",
			event_metadata=metadata,
		)
	)


def _ai_summary_enabled() -> bool:
	mode = os.getenv("REHAB_TRIAGE_SUMMARY_AGENT", "llm").lower()
	return mode not in {"fallback", "heuristic", "local", "off", "false", "0"}


def _try_build_ai_summary(
	episode: CareEpisode,
	additional_context: str,
	conversation_context: str,
	version: int,
	rehab_context: str = "",
) -> CareEpisodeTriageSummary | None:
	if not _ai_summary_enabled():
		return None
	try:
		from langchain_core.messages import HumanMessage, SystemMessage
		from langchain_openai import ChatOpenAI
		from pydantic import BaseModel, Field
	
		class AgentOutput(BaseModel):
			concern: str = Field(min_length=1)
			relevant_context: str = Field(min_length=1)
			safety_signals: list[str]
			limitations: list[str]
			recommendation: str = Field(min_length=1)
			missing_information: list[str]
			unresolved_questions: list[str]
			clinician_review_needed: bool
	
		settings = get_settings()
		api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY")
		if not api_key:
			raise TriageSummaryGenerationFailed("OpenAI API key is not configured for Triage Summary generation")
		llm = ChatOpenAI(
			model=settings.openai_model,
			base_url=settings.openai_api_base,
			api_key=api_key,
			reasoning_effort="none",
			use_responses_api=True,
			**openai_client_options(),
		)
		structured = llm.with_structured_output(AgentOutput, method="json_schema")
		prompt = (
			"Create a concise patient-facing Triage Summary from the episode and triage chat. "
			"Use a two-to-six-word concern such as 'Shoulder stiffness'. "
			"Write relevant_context as one to three sentences centered on the main complaint, rehab stage, "
			"functional impact, and what changes the symptom. Safety negatives may appear only as a brief secondary clause; "
			"never make a denial checklist the main narrative. "
			"Keep missing information and safety signals in their structured fields for internal review. "
			"Prefer partial summaries over failure. Do not invent facts. "
			"Treat explicit patient denials such as no pain, no swelling, no bruising, no numbness, or no surgery as answered facts. "
			"Do not mark assistant questions as patient-reported symptoms, and do not list denied symptoms as safety signals.\n\n"
			f"Episode issue: {episode.issue_title}\n"
			f"Body area: {episode.body_area}\n"
			f"Goal: {episode.goal}\n"
			f"Description: {episode.short_description}\n"
			f"Additional context: {additional_context or 'None provided.'}\n"
			f"Latest saved rehab session:\n{rehab_context or 'None provided.'}\n"
			f"AI triage transcript:\n{conversation_context or 'None provided.'}"
		)
		output = structured.invoke([
			SystemMessage(content="You are RehabFlow's backend triage-summary agent."),
			HumanMessage(content=prompt),
		])
		derived_fields = derive_episode_fields_from_triage_context(additional_context, conversation_context)
		summary_concern = derived_fields["issue_title"]
		if summary_concern == "AI triage rehab concern" and episode.issue_title != "AI triage rehab concern":
			summary_concern = episode.issue_title
		# The title already anchors the body area and concern. Preserve the LLM's
		# patient-facing narrative instead of requiring it to repeat title words.
		relevant_context = output.relevant_context.strip()
		if rehab_context and rehab_context not in relevant_context:
			relevant_context = f"{relevant_context} {rehab_context}".strip()
		return CareEpisodeTriageSummary(
			care_episode_id=episode.care_episode_id,
			patient_id=episode.patient_id,
			version=version,
			concern=summary_concern,
			relevant_context=relevant_context,
			safety_signals=output.safety_signals or ["No explicit safety signals were returned by the summary agent."],
			limitations=output.limitations or ["Generated from available episode and triage context."],
			recommendation=output.recommendation,
			missing_information=output.missing_information,
			unresolved_questions=output.unresolved_questions,
			clinician_review_needed=output.clinician_review_needed or bool(output.missing_information),
			additional_context=additional_context,
		)
	except TriageSummaryGenerationFailed:
		raise
	except Exception as exc:
		raise TriageSummaryGenerationFailed("LLM Triage Summary generation failed") from exc


def build_triage_summary(
	episode: CareEpisode,
	additional_context: str,
	version: int,
	*,
	db: Session,
	ai_session_id: UUID | None = None,
) -> CareEpisodeTriageSummary:
	source_conversation_transcript = load_full_triage_conversation_context(db, episode.patient_id, ai_session_id)
	conversation_context = _load_ai_session_transcript(db, episode.patient_id, ai_session_id)
	rehab_context = load_latest_rehab_session_context(db, episode.care_episode_id, episode.patient_id)
	if _ai_summary_enabled():
		ai_summary = _try_build_ai_summary(
			episode,
			additional_context,
			conversation_context,
			version,
			rehab_context,
		)
		ai_summary.source_ai_session_id = ai_session_id
		ai_summary.source_conversation_transcript = source_conversation_transcript
		_log_summary_agent_output(
			db,
			patient_id=episode.patient_id,
			ai_session_id=ai_session_id,
			event_type="summary_llm_output",
			summary=ai_summary,
		)
		return ai_summary
	fallback_summary = _build_fallback_summary(
		episode,
		additional_context,
		conversation_context,
		version,
		rehab_context,
	)
	fallback_summary.source_ai_session_id = ai_session_id
	fallback_summary.source_conversation_transcript = source_conversation_transcript
	_log_summary_agent_output(
		db,
		patient_id=episode.patient_id,
		ai_session_id=ai_session_id,
		event_type="summary_fallback_output",
		summary=fallback_summary,
	)
	return fallback_summary
