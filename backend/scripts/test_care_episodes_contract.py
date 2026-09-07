from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import app


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "episode-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def main() -> None:
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"episode_patient_{suffix}")
	other_patient_token = _register_patient(client, f"episode_other_{suffix}")

	payload = {
		"issue_title": "Right shoulder stiffness",
		"body_area": "Right shoulder",
		"goal": "Return to overhead reaching safely",
		"short_description": "Stiffness after rotator cuff rehab, worse in the morning.",
		"symptom_started_on": "2026-06-01",
	}
	create_response = client.post("/care-episodes", json=payload, headers=_auth(patient_token))
	assert create_response.status_code == 201, create_response.text
	created = create_response.json()
	assert created["issue_title"] == payload["issue_title"]
	assert created["origin"] == "manual"
	assert created["safety_gate_status"] == "needs_triage"
	assert created["latest_triage_summary"] is None

	list_response = client.get("/care-episodes", headers=_auth(patient_token))
	assert list_response.status_code == 200, list_response.text
	episodes = list_response.json()["episodes"]
	assert any(item["care_episode_id"] == created["care_episode_id"] for item in episodes)

	other_list_response = client.get("/care-episodes", headers=_auth(other_patient_token))
	assert other_list_response.status_code == 200, other_list_response.text
	assert all(item["care_episode_id"] != created["care_episode_id"] for item in other_list_response.json()["episodes"])

	episode_id = created["care_episode_id"]
	detail_response = client.get(f"/care-episodes/{episode_id}", headers=_auth(patient_token))
	assert detail_response.status_code == 200, detail_response.text
	assert detail_response.json()["care_episode_id"] == episode_id

	forbidden_response = client.get(f"/care-episodes/{episode_id}", headers=_auth(other_patient_token))
	assert forbidden_response.status_code == 404, forbidden_response.text

	print("care episode contract ok")


if __name__ == "__main__":
	main()
