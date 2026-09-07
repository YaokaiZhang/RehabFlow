from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import app


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post("/auth/register/patient", json={"patient_name": name, "password": "conversation-pass-123", "real_info": {}})
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _register_doctor(client: TestClient, name: str) -> tuple[str, str]:
	response = client.post("/auth/register/doctor", json={"doctor_name": name, "password": "conversation-pass-123", "real_info": {"specialty": "PT"}})
	assert response.status_code == 200, response.text
	data = response.json()
	return data["access_token"], data["user"]["user_id"]


def _create_active_relationship(client: TestClient, patient_token: str, doctor_token: str, doctor_id: str) -> dict:
	episode_response = client.post(
		"/care-episodes",
		json={"issue_title": "Shoulder stiffness", "body_area": "Right shoulder", "goal": "Reach overhead", "short_description": "Stiffness after rotator cuff rehab."},
		headers=_auth(patient_token),
	)
	assert episode_response.status_code == 201, episode_response.text
	episode = episode_response.json()
	episode_id = episode["care_episode_id"]
	assert client.post("/professional-care/subscribe", headers=_auth(patient_token)).status_code == 200
	request_response = client.post(
		f"/care-episodes/{episode_id}/connection-requests",
		json={"doctor_id": doctor_id, "request_reason": "Please help with my shoulder."},
		headers=_auth(patient_token),
	)
	assert request_response.status_code == 201, request_response.text
	request_id = request_response.json()["request_id"]
	accept_response = client.post(f"/doctor/care-requests/{request_id}/accept", headers=_auth(doctor_token))
	assert accept_response.status_code == 200, accept_response.text
	select_response = client.post(f"/care-episodes/{episode_id}/care-relationship/select", json={"doctor_id": doctor_id}, headers=_auth(patient_token))
	assert select_response.status_code == 200, select_response.text
	return {"episode_id": episode_id, "relationship": select_response.json()}


def main() -> None:
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"conversation_patient_{suffix}")
	doctor_token, doctor_id = _register_doctor(client, f"conversation_doctor_{suffix}")
	other_doctor_token, _ = _register_doctor(client, f"conversation_other_{suffix}")
	context = _create_active_relationship(client, patient_token, doctor_token, doctor_id)
	episode_id = context["episode_id"]

	patient_message = client.post(
		f"/care-episodes/{episode_id}/care-conversation/messages",
		json={"content": "My shoulder felt sore after the wall slides."},
		headers=_auth(patient_token),
	)
	assert patient_message.status_code == 201, patient_message.text
	assert patient_message.json()["sender_role"] == "patient"

	doctor_message = client.post(
		f"/care-episodes/{episode_id}/care-conversation/messages",
		json={"content": "Reduce range today and note pain after the session."},
		headers=_auth(doctor_token),
	)
	assert doctor_message.status_code == 201, doctor_message.text
	assert doctor_message.json()["sender_role"] == "doctor"

	messages_response = client.get(f"/care-episodes/{episode_id}/care-conversation/messages", headers=_auth(patient_token))
	assert messages_response.status_code == 200, messages_response.text
	messages = messages_response.json()["messages"]
	assert [message["sender_role"] for message in messages] == ["patient", "doctor"]
	assert "wall slides" in messages[0]["content"]

	unauthorized = client.get(f"/care-episodes/{episode_id}/care-conversation/messages", headers=_auth(other_doctor_token))
	assert unauthorized.status_code == 404, unauthorized.text

	summary_response = client.post(f"/care-episodes/{episode_id}/ai-care-summary", headers=_auth(doctor_token))
	assert summary_response.status_code == 200, summary_response.text
	summary = summary_response.json()
	assert summary["care_episode_id"] == episode_id
	assert "wall slides" in summary["conversation_digest"]
	assert "Reduce range" in summary["plan_digest"]
	assert summary["raw_score_trend"] is None

	print("care conversation contract ok")


if __name__ == "__main__":
	main()
