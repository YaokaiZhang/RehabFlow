from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("REHAB_TRIAGE_SUMMARY_AGENT", "fallback")

from fastapi.testclient import TestClient
from app.main import app


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "summary-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _create_episode(client: TestClient, token: str) -> dict:
	response = client.post(
		"/care-episodes",
		json={
			"issue_title": "Left ankle swelling",
			"body_area": "Left ankle",
			"goal": "Walk without limping",
			"short_description": "Rolled the ankle last week and it still feels puffy after stairs.",
		},
		headers=_auth(token),
	)
	assert response.status_code == 201, response.text
	return response.json()


def main() -> None:
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"summary_patient_{suffix}")
	other_patient_token = _register_patient(client, f"summary_other_{suffix}")
	episode = _create_episode(client, patient_token)
	episode_id = episode["care_episode_id"]

	create_response = client.post(
		f"/care-episodes/{episode_id}/triage-summary",
		json={"additional_context": "Pain is mild, but I have not checked bruising or weight bearing limits."},
		headers=_auth(patient_token),
	)
	assert create_response.status_code == 201, create_response.text
	summary = create_response.json()
	assert summary["care_episode_id"] == episode_id
	assert summary["concern"] == "Left ankle swelling"
	assert summary["recommendation"]
	assert "weight-bearing tolerance" in summary["missing_information"]
	assert "bruising" in summary["missing_information"]
	assert summary["clinician_review_needed"] is True
	assert summary["version"] == 1

	detail_response = client.get(f"/care-episodes/{episode_id}", headers=_auth(patient_token))
	assert detail_response.status_code == 200, detail_response.text
	detail = detail_response.json()
	assert detail["latest_triage_summary"]["triage_summary_id"] == summary["triage_summary_id"]
	assert detail["safety_gate_status"] == "triage_complete"

	regenerate_response = client.post(
		f"/care-episodes/{episode_id}/triage-summary",
		json={"additional_context": "I can bear weight, swelling is improving, no numbness."},
		headers=_auth(patient_token),
	)
	assert regenerate_response.status_code == 201, regenerate_response.text
	regenerated = regenerate_response.json()
	assert regenerated["version"] == 2
	assert regenerated["triage_summary_id"] != summary["triage_summary_id"]

	list_response = client.get(f"/care-episodes/{episode_id}/triage-summaries", headers=_auth(patient_token))
	assert list_response.status_code == 200, list_response.text
	history = list_response.json()["summaries"]
	assert [item["version"] for item in history[:2]] == [2, 1]

	forbidden_response = client.post(
		f"/care-episodes/{episode_id}/triage-summary",
		json={"additional_context": "Trying to summarize someone else."},
		headers=_auth(other_patient_token),
	)
	assert forbidden_response.status_code == 404, forbidden_response.text

	print("triage summary contract ok")


if __name__ == "__main__":
	main()
