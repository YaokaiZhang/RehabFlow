from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("REHAB_TRIAGE_SUMMARY_AGENT", "fallback")

from fastapi.testclient import TestClient

from app.db.models import AIMessage, AISession, CareEpisode
from app.db.session import SessionLocal, init_db
from app.main import app


def _register_patient(client: TestClient, name: str) -> tuple[str, str]:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "summary-create-episode-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	body = response.json()
	return body["access_token"], body["user"]["user_id"]


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _create_chat_session(patient_id: str) -> str:
	with SessionLocal() as db:
		session = AISession(patient_id=patient_id, title="New episode triage intake")
		db.add(session)
		db.flush()
		db.add_all(
			[
				AIMessage(
					session_id=session.session_id,
					sender_role="user",
					content="My right shoulder is stiff after rotator cuff rehab and overhead reaching is limited.",
				),
				AIMessage(
					session_id=session.session_id,
					sender_role="assistant",
					content="What is the pain level and are there new neurologic symptoms?",
				),
				AIMessage(
					session_id=session.session_id,
					sender_role="user",
					content="Pain is mild around 2/10, no numbness or tingling, no sudden swelling.",
				),
			]
		)
		db.commit()
		return str(session.session_id)


def main() -> None:
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token, patient_id = _register_patient(client, f"summary_episode_patient_{suffix}")
	other_patient_token, other_patient_id = _register_patient(client, f"summary_episode_other_{suffix}")
	ai_session_id = _create_chat_session(patient_id)
	other_ai_session_id = _create_chat_session(other_patient_id)

	response = client.post(
		"/care-episodes/triage-summary-request",
		json={"ai_session_id": ai_session_id, "additional_context": "I am done with triage."},
		headers=_auth(patient_token),
	)
	assert response.status_code == 201, response.text
	body = response.json()
	episode = body["episode"]
	draft = body["draft"]
	assert episode["origin"] == "triage"
	assert episode["safety_gate_status"] == "needs_triage"
	assert episode["care_episode_id"] == draft["care_episode_id"]
	assert draft["saved"] is False
	assert "shoulder" in episode["body_area"].lower()
	assert episode["goal"] == "Relieve shoulder stiffness"
	assert "Patient:" not in episode["short_description"]
	assert "RehabFlow:" not in episode["short_description"]
	assert "rotator cuff rehab" in episode["short_description"].lower()
	assert "Patient: My right shoulder" in draft["relevant_context"]
	assert "numbness or tingling" not in draft["missing_information"]
	assert "pain intensity and behavior" not in draft["missing_information"]

	with SessionLocal() as db:
		stored_episode = db.get(CareEpisode, episode["care_episode_id"])
		assert stored_episode is not None
		assert stored_episode.origin == "triage"
		assert stored_episode.latest_triage_summary is None

	wrong_session_response = client.post(
		"/care-episodes/triage-summary-request",
		json={"ai_session_id": other_ai_session_id, "additional_context": "Use another patient's triage."},
		headers=_auth(patient_token),
	)
	assert wrong_session_response.status_code == 404, wrong_session_response.text

	print("triage summary request creates episode contract ok")


if __name__ == "__main__":
	main()
