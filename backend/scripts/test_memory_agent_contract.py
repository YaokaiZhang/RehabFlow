#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.models import CareEpisode, CareEpisodeTriageSummary, MemoryDocument, Patient
from app.db.session import SessionLocal
from app.services.memory_agent import _agent_messages, build_memory_snapshot


def main() -> None:
	suffix = time.time_ns()
	with SessionLocal() as db:
		patient = Patient(
			patient_name=f"memory_agent_patient_{suffix}",
			password_hash="contract",
			real_info={},
		)
		db.add(patient)
		db.flush()
		episode = CareEpisode(
			patient_id=patient.patient_id,
			issue_title="Left knee stiffness",
			body_area="Left Knee",
			goal="Relieve knee stiffness",
			short_description="Cold-weather left knee stiffness after prior healed fracture.",
		)
		db.add(episode)
		db.flush()
		summary = CareEpisodeTriageSummary(
			care_episode_id=episode.care_episode_id,
			patient_id=patient.patient_id,
			version=1,
			concern="Left knee stiffness",
			relevant_context=(
				"Patient reports left knee stiffness only in cold weather, no pain, swelling, redness, warmth, "
				"or functional limitation. History of healed left knee fracture six years ago. "
				"No recent injury or surgery."
			),
			safety_signals=["No pain, swelling, redness, warmth, or functional limitation."],
			limitations=[],
			recommendation="Continue conservative warmth and gentle movement.",
			missing_information=[],
			unresolved_questions=[],
			clinician_review_needed=False,
			additional_context="Patient broke the left knee six years ago and recovered.",
		)
		db.add(summary)
		db.flush()

		snapshot = build_memory_snapshot(
			db,
			patient_id=patient.patient_id,
			care_episode_id=episode.care_episode_id,
			session_id=None,
			trigger="triage_summary_generation",
		).value
		episode_prompt = "\n".join(
			str(getattr(message, "content", ""))
			for message in _agent_messages("episode", snapshot, "")
		)
		patient_prompt = "\n".join(
			str(getattr(message, "content", ""))
			for message in _agent_messages("patient", snapshot, "")
		)
		assert "healed left knee fracture six years ago" not in episode_prompt
		assert "healed left knee fracture six years ago" not in patient_prompt
		assert "previous_episode_memory" in episode_prompt
		assert "previous_patient_memory" in patient_prompt

		patient_document = db.execute(
			select(MemoryDocument).where(
				MemoryDocument.patient_id == patient.patient_id,
				MemoryDocument.scope == "patient",
				MemoryDocument.care_episode_id.is_(None),
			)
		).scalars().one()
		assert patient_document.editable_fields == {}
		assert patient_document.summary_text == ""


if __name__ == "__main__":
	main()
