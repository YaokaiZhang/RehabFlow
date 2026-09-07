from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db.models import CareEpisode, CareEpisodeTriageSummaryDraft
from app.db.session import SessionLocal, init_db
from app.main import app


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post("/auth/register/patient", json={"patient_name": name, "password": "delete-pass-123", "real_info": {}})
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _create_episode(client: TestClient, token: str) -> dict:
	response = client.post(
		"/care-episodes",
		json={
			"issue_title": "Hip stiffness",
			"body_area": "Left hip",
			"goal": "Walk stairs comfortably",
			"short_description": "Hip gets stiff after longer walks.",
		},
		headers=_auth(token),
	)
	assert response.status_code == 201, response.text
	return response.json()


def main() -> None:
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"delete_episode_patient_{suffix}")
	other_patient_token = _register_patient(client, f"delete_episode_other_{suffix}")
	episode = _create_episode(client, patient_token)
	episode_id = episode["care_episode_id"]

	draft_response = client.post(
		f"/care-episodes/{episode_id}/triage-summary-drafts",
		json={"additional_context": "Pain is mild, no numbness, no bruising, can bear weight."},
		headers=_auth(patient_token),
	)
	assert draft_response.status_code == 201, draft_response.text
	draft_id = draft_response.json()["triage_summary_draft_id"]

	unauthorized = client.delete(f"/care-episodes/{episode_id}", headers=_auth(other_patient_token))
	assert unauthorized.status_code == 404, unauthorized.text

	deleted = client.delete(f"/care-episodes/{episode_id}", headers=_auth(patient_token))
	assert deleted.status_code == 204, deleted.text

	missing = client.get(f"/care-episodes/{episode_id}", headers=_auth(patient_token))
	assert missing.status_code == 404, missing.text

	with SessionLocal() as db:
		assert db.get(CareEpisode, episode_id) is None
		assert db.get(CareEpisodeTriageSummaryDraft, draft_id) is None

	print("care episode delete contract ok")


if __name__ == "__main__":
	main()
