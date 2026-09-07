from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("REHAB_TRIAGE_SUMMARY_AGENT", "fallback")

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import MemoryMaintenanceRun
from app.db.session import SessionLocal
from app.main import app
from app.services.exercise_catalog import load_exercise_catalog
from app.services.rehab_recommendations import set_rehab_recommendation_searcher_for_tests


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "summary-select-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _create_episode(client: TestClient, token: str) -> dict:
	response = client.post(
		"/care-episodes",
		json={
			"issue_title": "Right shoulder stiffness",
			"body_area": "Right shoulder",
			"goal": "Reach overhead without pain",
			"short_description": "Stiff after a workout and not ready for heavy lifting.",
		},
		headers=_auth(token),
	)
	assert response.status_code == 201, response.text
	return response.json()


def main() -> None:
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"summary_select_patient_{suffix}")
	other_patient_token = _register_patient(client, f"summary_select_other_{suffix}")
	episode = _create_episode(client, patient_token)
	episode_id = episode["care_episode_id"]

	draft_response = client.post(
		f"/care-episodes/{episode_id}/triage-summary-drafts",
		json={"additional_context": "Pain is moderate and there is no numbness, but overhead reaching still feels uncertain."},
		headers=_auth(patient_token),
	)
	assert draft_response.status_code == 201, draft_response.text
	draft = draft_response.json()
	assert draft["care_episode_id"] == episode_id
	assert draft["triage_summary_draft_id"]
	assert draft["saved"] is False
	assert draft["concern"] == "Right shoulder stiffness"
	assert "range of motion limits" in draft["missing_information"]

	detail_response = client.get(f"/care-episodes/{episode_id}", headers=_auth(patient_token))
	assert detail_response.status_code == 200, detail_response.text
	detail = detail_response.json()
	assert detail["latest_triage_summary"] is None
	assert detail["safety_gate_status"] == "needs_triage"

	workspace_response = client.get(f"/care-episodes/{episode_id}/workspace-summary", headers=_auth(patient_token))
	assert workspace_response.status_code == 200, workspace_response.text
	workspace = workspace_response.json()
	assert workspace["latest_triage_summary"] is None
	assert [item["triage_summary_draft_id"] for item in workspace["unsaved_triage_summaries"]] == [draft["triage_summary_draft_id"]]

	drafts_response = client.get(f"/care-episodes/{episode_id}/triage-summary-drafts", headers=_auth(patient_token))
	assert drafts_response.status_code == 200, drafts_response.text
	assert [item["triage_summary_draft_id"] for item in drafts_response.json()["drafts"]] == [draft["triage_summary_draft_id"]]

	catalog_exercises = list(load_exercise_catalog())
	assert catalog_exercises
	first_exercise = catalog_exercises[0]

	def fake_searcher(query_text: str, limit: int) -> list[dict]:
		return [
			{"score": 0.91, "payload": {"document_type": "rehab_exercise", "exercise_id": first_exercise.exercise_id}},
		]

	set_rehab_recommendation_searcher_for_tests(fake_searcher)
	try:
		select_response = client.post(
			f"/care-episodes/{episode_id}/triage-summary-drafts/{draft['triage_summary_draft_id']}/select",
			headers=_auth(patient_token),
		)
	finally:
		set_rehab_recommendation_searcher_for_tests(None)
	assert select_response.status_code == 201, select_response.text
	summary = select_response.json()
	assert summary["version"] == 1
	assert summary["concern"] == draft["concern"]
	assert summary["recommendation"] == draft["recommendation"]

	after_select_workspace = client.get(f"/care-episodes/{episode_id}/workspace-summary", headers=_auth(patient_token)).json()
	assert after_select_workspace["latest_triage_summary"]["triage_summary_id"] == summary["triage_summary_id"]
	assert after_select_workspace["episode"]["safety_gate_status"] == "triage_complete"
	assert after_select_workspace["unsaved_triage_summaries"] == []

	memory_context = client.get(f"/memory/care-episodes/{episode_id}/context-pack", headers=_auth(patient_token)).json()
	episode_document = memory_context["episode_document"]
	assert isinstance(episode_document["level1_keywords"], list)
	assert isinstance(episode_document["level1_description"], str)
	assert isinstance(episode_document["level1_session_ids"], list)
	assert isinstance(episode_document["level2_summary"], str)
	with SessionLocal() as db:
		runs = db.execute(
			select(MemoryMaintenanceRun).where(
				MemoryMaintenanceRun.care_episode_id == episode_id,
				MemoryMaintenanceRun.trigger == "triage_summary_generation",
			)
		).scalars().all()
		assert len(runs) == 1
		run = runs[0]
		assert run is not None
		assert run.snapshot["trigger"] == "triage_summary_generation"
		assert run.snapshot["patient_id"] == episode["patient_id"]
		assert "current_session_ai_messages" in run.snapshot
		assert "relevant_context" not in str(run.snapshot)
	second_draft_response = client.post(
		f"/care-episodes/{episode_id}/triage-summary-drafts",
		json={"additional_context": "New soreness after reaching behind my back."},
		headers=_auth(patient_token),
	)
	assert second_draft_response.status_code == 201, second_draft_response.text
	second_draft = second_draft_response.json()
	delete_forbidden = client.delete(
		f"/care-episodes/{episode_id}/triage-summary-drafts/{second_draft['triage_summary_draft_id']}",
		headers=_auth(other_patient_token),
	)
	assert delete_forbidden.status_code == 404, delete_forbidden.text

	delete_response = client.delete(
		f"/care-episodes/{episode_id}/triage-summary-drafts/{second_draft['triage_summary_draft_id']}",
		headers=_auth(patient_token),
	)
	assert delete_response.status_code == 204, delete_response.text
	drafts_after_delete = client.get(f"/care-episodes/{episode_id}/triage-summary-drafts", headers=_auth(patient_token)).json()
	assert drafts_after_delete["drafts"] == []
	assert client.get(f"/care-episodes/{episode_id}", headers=_auth(patient_token)).json()["latest_triage_summary"]["triage_summary_id"] == summary["triage_summary_id"]

	print("triage summary selection contract ok")


if __name__ == "__main__":
	main()
