from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import app
from app.services.exercise_catalog import load_exercise_catalog
from app.services.rehab_recommendations import set_rehab_recommendation_searcher_for_tests


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post("/auth/register/patient", json={"patient_name": name, "password": "workspace-pass-123", "real_info": {}})
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _register_doctor(client: TestClient, name: str) -> tuple[str, str]:
	response = client.post("/auth/register/doctor", json={"doctor_name": name, "password": "workspace-pass-123", "real_info": {"specialty": "Rehab"}})
	assert response.status_code == 200, response.text
	data = response.json()
	return data["access_token"], data["user"]["user_id"]


def _create_episode(client: TestClient, token: str) -> dict:
	response = client.post(
		"/care-episodes",
		json={
			"issue_title": "Shoulder stiffness",
			"body_area": "Right shoulder",
			"goal": "Reach overhead comfortably",
			"short_description": "Stiff after rotator cuff rehab.",
		},
		headers=_auth(token),
	)
	assert response.status_code == 201, response.text
	return response.json()


def main() -> None:
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"workspace_patient_{suffix}")
	other_patient_token = _register_patient(client, f"workspace_other_{suffix}")
	doctor_token, doctor_id = _register_doctor(client, f"workspace_doctor_{suffix}")
	unselected_doctor_token, _ = _register_doctor(client, f"workspace_unselected_doctor_{suffix}")
	episode = _create_episode(client, patient_token)
	episode_id = episode["care_episode_id"]

	initial = client.get(f"/care-episodes/{episode_id}/workspace-summary", headers=_auth(patient_token))
	assert initial.status_code == 200, initial.text
	initial_data = initial.json()
	assert initial_data["episode"]["care_episode_id"] == episode_id
	assert initial_data["latest_triage_summary"] is None
	assert initial_data["latest_rehab_session"] is None
	assert initial_data["latest_ai_care_summary"] is None
	assert initial_data["professional_care"]["selected_doctor_id"] is None
	assert initial_data["professional_care"]["has_active_relationship"] is False
	assert initial_data["professional_care"]["show_live_monitor"] is False

	catalog_exercises = list(load_exercise_catalog())
	assert len(catalog_exercises) >= 2
	first_exercise = catalog_exercises[0]
	second_exercise = catalog_exercises[1]

	def fake_searcher(query_text: str, limit: int) -> list[dict]:
		return [
			{"score": 0.92, "payload": {"document_type": "rehab_exercise", "exercise_id": first_exercise.exercise_id}},
			{"score": 0.88, "payload": {"document_type": "rehab_exercise", "exercise_id": second_exercise.exercise_id}},
		]

	set_rehab_recommendation_searcher_for_tests(fake_searcher)
	try:
		triage = client.post(
			f"/care-episodes/{episode_id}/triage-summary",
			json={"additional_context": "Pain is mild, no numbness, no bruising, can bear weight equivalent through arm support."},
			headers=_auth(patient_token),
		)
	finally:
		set_rehab_recommendation_searcher_for_tests(None)
	assert triage.status_code == 201, triage.text

	rehab_list = client.put(
		f"/care-episodes/{episode_id}/rehab-list",
		json={"exercise_ids": [first_exercise.exercise_id, second_exercise.exercise_id]},
		headers=_auth(patient_token),
	)
	assert rehab_list.status_code == 200, rehab_list.text
	assert [item["exercise_id"] for item in rehab_list.json()["items"]] == [
		first_exercise.exercise_id,
		second_exercise.exercise_id,
	]

	rehab = client.post(
		f"/care-episodes/{episode_id}/rehab-sessions",
		json={"patient_notes": "Stiff before session."},
		headers=_auth(patient_token),
	)
	assert rehab.status_code == 201, rehab.text
	session_id = rehab.json()["session_id"]
	summarized = client.post(f"/rehab-sessions/{session_id}/summary", headers=_auth(patient_token))
	assert summarized.status_code == 200, summarized.text

	subscribe = client.post("/professional-care/subscribe", headers=_auth(patient_token))
	assert subscribe.status_code == 200, subscribe.text
	care_request = client.post(
		f"/care-episodes/{episode_id}/connection-requests",
		json={"doctor_id": doctor_id, "request_reason": "Please review shoulder progression."},
		headers=_auth(patient_token),
	)
	assert care_request.status_code == 201, care_request.text
	request_id = care_request.json()["request_id"]
	accepted = client.post(f"/doctor/care-requests/{request_id}/accept", headers=_auth(doctor_token))
	assert accepted.status_code == 200, accepted.text
	selected = client.post(f"/care-episodes/{episode_id}/care-relationship/select", json={"doctor_id": doctor_id}, headers=_auth(patient_token))
	assert selected.status_code == 200, selected.text
	message = client.post(f"/care-episodes/{episode_id}/care-conversation/messages", json={"content": "Can I progress wall slides?"}, headers=_auth(patient_token))
	assert message.status_code == 201, message.text
	care_summary = client.post(f"/care-episodes/{episode_id}/ai-care-summary", headers=_auth(patient_token))
	assert care_summary.status_code == 200, care_summary.text

	set_rehab_recommendation_searcher_for_tests(fake_searcher)
	try:
		draft = client.post(
			f"/care-episodes/{episode_id}/triage-summary-drafts",
			json={"additional_context": "Draft note: shoulder clicks after the latest session."},
			headers=_auth(patient_token),
		)
	finally:
		set_rehab_recommendation_searcher_for_tests(None)
	assert draft.status_code == 201, draft.text
	draft_data = draft.json()
	assert first_exercise.title in draft_data["relevant_context"]
	assert "Stiff before session." in draft_data["relevant_context"]
	draft_id = draft_data["triage_summary_draft_id"]

	workspace = client.get(f"/care-episodes/{episode_id}/workspace-summary", headers=_auth(patient_token))
	assert workspace.status_code == 200, workspace.text
	data = workspace.json()
	assert data["latest_triage_summary"]["concern"] == "Shoulder stiffness"
	assert data["latest_rehab_session"]["session_id"] == session_id
	assert first_exercise.title in data["latest_rehab_session"]["session_summary"]
	assert data["latest_ai_care_summary"]["care_summary_id"] == care_summary.json()["care_summary_id"]
	assert [item["triage_summary_draft_id"] for item in data["unsaved_triage_summaries"]] == [draft_id]
	assert data["professional_care"]["subscription_tier"] == "professional_care"
	assert data["professional_care"]["selected_doctor_id"] == doctor_id
	assert data["professional_care"]["selected_doctor_name"] == "Professional Care clinician"
	assert data["professional_care"]["has_active_relationship"] is True
	assert data["professional_care"]["show_live_monitor"] is True
	assert data["professional_care"]["request_counts"]["accepted"] == 1

	selected_doctor_summary = client.get(
		f"/care-episodes/{episode_id}/workspace-summary",
		headers=_auth(doctor_token),
	)
	assert selected_doctor_summary.status_code == 200, selected_doctor_summary.text
	brief = selected_doctor_summary.json()
	assert brief["episode"]["care_episode_id"] == episode_id
	assert brief["episode"]["issue_title"] == "Shoulder stiffness"
	assert brief["latest_triage_summary"]["concern"] == "Shoulder stiffness"
	assert brief["latest_rehab_session"]["session_summary"]
	assert brief["unsaved_triage_summaries"] == []
	assert brief["professional_care"]["has_active_relationship"] is True
	assert brief["professional_care"]["relationship_id"]

	unselected_doctor_summary = client.get(
		f"/care-episodes/{episode_id}/workspace-summary",
		headers=_auth(unselected_doctor_token),
	)
	assert unselected_doctor_summary.status_code == 404, unselected_doctor_summary.text

	other_access = client.get(f"/care-episodes/{episode_id}/workspace-summary", headers=_auth(other_patient_token))
	assert other_access.status_code == 404, other_access.text

	print("workspace summary contract ok")


if __name__ == "__main__":
	main()
