from __future__ import annotations

import json
import sys
import time
from uuid import uuid4
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db.models import Doctor
from app.db.session import SessionLocal, init_db
from app.main import app


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "professional-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _register_doctor(client: TestClient, name: str) -> tuple[str, str]:
	response = client.post(
		"/auth/register/doctor",
		json={"doctor_name": name, "password": "professional-pass-123", "real_info": {"specialty": "PT"}},
	)
	assert response.status_code == 200, response.text
	data = response.json()
	return data["access_token"], data["user"]["user_id"]


def _create_episode(client: TestClient, token: str) -> dict:
	response = client.post(
		"/care-episodes",
		json={
			"issue_title": "Knee stiffness",
			"body_area": "Left knee",
			"goal": "Return to stairs",
			"short_description": "Stiffness after ACL rehab exercises.",
		},
		headers=_auth(token),
	)
	assert response.status_code == 201, response.text
	return response.json()


def _seed_directory_contract_doctors() -> tuple[str, str, str, str, str, list[str]]:
	with SessionLocal() as db:
		uuid_like_name = str(uuid4())
		token_like_name = f"7f4a9c2e1d0b4a6f8c3e{time.time_ns()}"[:64]
		raw_metadata_display_name = str(uuid4())
		raw_specialty = str(uuid4())
		raw_tag_values = [str(uuid4()), f"7f4a9c2e1d0b4a6f8c3e{time.time_ns()}"[:64]]
		uuid_like = Doctor(
			doctor_name=uuid_like_name,
			password_hash="contract-unusable-password-hash",
			real_info=["not", "a", "dict"],
			verification_status="pending",
		)
		token_like = Doctor(
			doctor_name=token_like_name,
			password_hash="contract-unusable-password-hash",
			real_info={"expertise_tags": "not-a-list", "tags": {"bad": "shape"}, "specialty": "   "},
			verification_status="pending",
		)
		human_profile = Doctor(
			doctor_name=f"raw_human_profile_{time.time_ns()}",
			password_hash="contract-unusable-password-hash",
			real_info={
				"display_name": "Dr. Human Name",
				"specialty": "Sports PT",
				"expertise_tags": ["Knee", "Sports injury"],
			},
			verification_status="verified",
		)
		human_without_public_display_name = Doctor(
			doctor_name=f"Dr. Raw Fallback {time.time_ns()}",
			password_hash="contract-unusable-password-hash",
			real_info={"specialty": "Rehabilitation medicine", "expertise_tags": ["Knee"]},
			verification_status="verified",
		)
		raw_metadata_display = Doctor(
			doctor_name=f"metadata_display_{time.time_ns()}",
			password_hash="contract-unusable-password-hash",
			real_info={"display_name": raw_metadata_display_name, "expertise_tags": ["Knee"]},
			verification_status="pending",
		)
		raw_specialty_tags = Doctor(
			doctor_name=f"raw_specialty_tags_{time.time_ns()}",
			password_hash="contract-unusable-password-hash",
			real_info={
				"display_name": "Dr. Safe Tag Filter",
				"specialty": raw_specialty,
				"expertise_tags": raw_tag_values,
			},
			verification_status="pending",
		)
		db.add_all([uuid_like, token_like, human_profile, human_without_public_display_name, raw_metadata_display, raw_specialty_tags])
		db.commit()
		db.refresh(uuid_like)
		db.refresh(token_like)
		db.refresh(human_profile)
		db.refresh(human_without_public_display_name)
		db.refresh(raw_metadata_display)
		db.refresh(raw_specialty_tags)
		return (
			str(uuid_like.doctor_id),
			str(token_like.doctor_id),
			str(human_profile.doctor_id),
			str(human_without_public_display_name.doctor_id),
			str(raw_metadata_display.doctor_id),
			str(raw_specialty_tags.doctor_id),
			[uuid_like_name, token_like_name, human_without_public_display_name.doctor_name, raw_metadata_display_name, raw_specialty, *raw_tag_values],
		)


def main() -> None:
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"pc_patient_{suffix}")
	doctor_one_token, doctor_one_id = _register_doctor(client, f"pc_doctor_one_{suffix}")
	doctor_two_token, doctor_two_id = _register_doctor(client, f"pc_doctor_two_{suffix}")
	doctor_three_token, doctor_three_id = _register_doctor(client, f"pc_doctor_three_{suffix}")
	_, doctor_four_id = _register_doctor(client, f"pc_doctor_four_{suffix}")
	episode = _create_episode(client, patient_token)
	episode_id = episode["care_episode_id"]

	unsubscribed = client.post(
		f"/care-episodes/{episode_id}/connection-requests",
		json={"doctor_id": doctor_one_id, "request_reason": "Please review my knee plan."},
		headers=_auth(patient_token),
	)
	assert unsubscribed.status_code == 403, unsubscribed.text

	subscribe_response = client.post("/professional-care/subscribe", headers=_auth(patient_token))
	assert subscribe_response.status_code == 200, subscribe_response.text
	assert subscribe_response.json()["subscription_tier"] == "professional_care"
	raw_uuid_doctor_id, token_like_doctor_id, human_profile_doctor_id, human_without_display_doctor_id, raw_metadata_display_doctor_id, raw_specialty_tags_doctor_id, raw_directory_values = _seed_directory_contract_doctors()

	doctors_response = client.get("/professional-care/doctors", headers=_auth(patient_token))
	assert doctors_response.status_code == 200, doctors_response.text
	doctors = doctors_response.json()["doctors"]
	assert any(doctor["doctor_id"] == doctor_one_id for doctor in doctors)
	raw_uuid_doctor = next(doctor for doctor in doctors if doctor["doctor_id"] == raw_uuid_doctor_id)
	assert raw_uuid_doctor["display_name"] == "Professional Care clinician"
	assert raw_uuid_doctor["expertise_tags"] == ["Rehabilitation clinician"]
	token_like_doctor = next(doctor for doctor in doctors if doctor["doctor_id"] == token_like_doctor_id)
	assert token_like_doctor["display_name"] == "Professional Care clinician"
	assert token_like_doctor["expertise_tags"] == ["Rehabilitation clinician"]
	human_profile_doctor = next(doctor for doctor in doctors if doctor["doctor_id"] == human_profile_doctor_id)
	assert human_profile_doctor["display_name"] == "Dr. Human Name"
	assert human_profile_doctor["specialty"] == "Sports PT"
	assert "Knee" in human_profile_doctor["expertise_tags"]
	assert "Sports injury" in human_profile_doctor["expertise_tags"]
	assert "Sports PT" in human_profile_doctor["expertise_tags"]
	human_without_display_doctor = next(doctor for doctor in doctors if doctor["doctor_id"] == human_without_display_doctor_id)
	assert human_without_display_doctor["display_name"] == "Professional Care clinician"
	raw_metadata_display_doctor = next(doctor for doctor in doctors if doctor["doctor_id"] == raw_metadata_display_doctor_id)
	assert raw_metadata_display_doctor["display_name"] == "Professional Care clinician"
	raw_specialty_tags_doctor = next(doctor for doctor in doctors if doctor["doctor_id"] == raw_specialty_tags_doctor_id)
	assert raw_specialty_tags_doctor["display_name"] == "Dr. Safe Tag Filter"
	assert raw_specialty_tags_doctor["specialty"] is None
	assert raw_specialty_tags_doctor["expertise_tags"] == ["Rehabilitation clinician"]
	for raw_doctor in [raw_uuid_doctor, token_like_doctor, human_without_display_doctor, raw_metadata_display_doctor, raw_specialty_tags_doctor]:
		assert "doctor_name" not in raw_doctor
		assert "real_info" not in raw_doctor
		raw_doctor_json = json.dumps(raw_doctor)
		for raw_value in raw_directory_values:
			assert raw_value not in raw_doctor_json

	raw_request_episode = _create_episode(client, patient_token)
	raw_request_response = client.post(
		f"/care-episodes/{raw_request_episode['care_episode_id']}/connection-requests",
		json={"doctor_id": raw_uuid_doctor_id, "request_reason": "Please review this raw doctor name case."},
		headers=_auth(patient_token),
	)
	assert raw_request_response.status_code == 201, raw_request_response.text
	assert raw_request_response.json()["doctor_name"] == "Professional Care clinician"
	raw_requests_response = client.get(f"/care-episodes/{raw_request_episode['care_episode_id']}/connection-requests", headers=_auth(patient_token))
	assert raw_requests_response.status_code == 200, raw_requests_response.text
	raw_request_list_item = next(item for item in raw_requests_response.json()["requests"] if item["doctor_id"] == raw_uuid_doctor_id)
	assert raw_request_list_item["doctor_name"] == "Professional Care clinician"

	summary_response = client.post(
		f"/care-episodes/{episode_id}/triage-summary",
		json={"additional_context": "Pain is mild but I still need help deciding progression."},
		headers=_auth(patient_token),
	)
	assert summary_response.status_code == 201, summary_response.text

	request_one = client.post(
		f"/care-episodes/{episode_id}/connection-requests",
		json={"doctor_id": doctor_one_id},
		headers=_auth(patient_token),
	)
	assert request_one.status_code == 201, request_one.text
	request_one_data = request_one.json()
	assert request_one_data["status"] == "pending"
	assert "Knee stiffness" in request_one_data["request_reason"]

	duplicate = client.post(
		f"/care-episodes/{episode_id}/connection-requests",
		json={"doctor_id": doctor_one_id, "request_reason": "Duplicate"},
		headers=_auth(patient_token),
	)
	assert duplicate.status_code == 409, duplicate.text

	for doctor_id in [doctor_two_id, doctor_three_id]:
		response = client.post(
			f"/care-episodes/{episode_id}/connection-requests",
			json={"doctor_id": doctor_id, "request_reason": "Please review this episode."},
			headers=_auth(patient_token),
		)
		assert response.status_code == 201, response.text

	limit_response = client.post(
		f"/care-episodes/{episode_id}/connection-requests",
		json={"doctor_id": doctor_four_id, "request_reason": "Fourth pending request."},
		headers=_auth(patient_token),
	)
	assert limit_response.status_code == 409, limit_response.text

	worklist_response = client.get("/doctor/care-worklist", headers=_auth(doctor_one_token))
	assert worklist_response.status_code == 200, worklist_response.text
	incoming = worklist_response.json()["incoming_requests"]
	assert any(item["request_id"] == request_one_data["request_id"] for item in incoming)
	preview = next(item for item in incoming if item["request_id"] == request_one_data["request_id"])
	assert preview["patient_id"]
	assert preview["latest_triage_summary"]["concern"] == "Knee stiffness"

	accept_response = client.post(f"/doctor/care-requests/{request_one_data['request_id']}/accept", headers=_auth(doctor_one_token))
	assert accept_response.status_code == 200, accept_response.text
	assert accept_response.json()["status"] == "accepted"

	detail_after_accept = client.get(f"/care-episodes/{episode_id}", headers=_auth(patient_token)).json()
	assert detail_after_accept["selected_doctor_id"] is None

	worklist_after_accept_response = client.get("/doctor/care-worklist", headers=_auth(doctor_one_token))
	assert worklist_after_accept_response.status_code == 200, worklist_after_accept_response.text
	worklist_after_accept = worklist_after_accept_response.json()
	assert worklist_after_accept["incoming_requests"] == []
	assert worklist_after_accept["active_relationships"] == []
	accepted_worklist_items = worklist_after_accept["accepted_requests"]
	assert any(item["request_id"] == request_one_data["request_id"] for item in accepted_worklist_items)
	accepted_preview = next(item for item in accepted_worklist_items if item["request_id"] == request_one_data["request_id"])
	assert accepted_preview["care_episode_id"] == episode_id
	assert accepted_preview["issue_title"] == "Knee stiffness"

	requests_response = client.get(f"/care-episodes/{episode_id}/connection-requests", headers=_auth(patient_token))
	assert requests_response.status_code == 200, requests_response.text
	accepted = [item for item in requests_response.json()["requests"] if item["status"] == "accepted"]
	assert accepted and accepted[0]["doctor_id"] == doctor_one_id

	select_response = client.post(
		f"/care-episodes/{episode_id}/care-relationship/select",
		json={"doctor_id": doctor_one_id},
		headers=_auth(patient_token),
	)
	assert select_response.status_code == 200, select_response.text
	relationship = select_response.json()
	assert relationship["status"] == "active"
	assert relationship["doctor_id"] == doctor_one_id

	doctor_message = client.post(
		f"/care-episodes/{episode_id}/care-conversation/messages",
		json={"content": "Continue gentle range work and report swelling tomorrow."},
		headers=_auth(doctor_one_token),
	)
	assert doctor_message.status_code == 201, doctor_message.text
	latest_doctor_message = client.post(
		f"/care-episodes/{episode_id}/care-conversation/messages",
		json={"content": "Latest update: keep reps low and stop if swelling increases."},
		headers=_auth(doctor_one_token),
	)
	assert latest_doctor_message.status_code == 201, latest_doctor_message.text

	active_worklist_response = client.get("/doctor/care-worklist", headers=_auth(doctor_one_token))
	assert active_worklist_response.status_code == 200, active_worklist_response.text
	active_items = active_worklist_response.json()["active_relationships"]
	assert len(active_items) == 1
	active_preview = active_items[0]
	assert active_preview["relationship_id"] == relationship["relationship_id"]
	assert active_preview["care_episode_id"] == episode_id
	assert active_preview["patient_id"]
	assert active_preview["patient_name"]
	assert active_preview["issue_title"] == "Knee stiffness"
	assert active_preview["latest_triage_summary"]["concern"] == "Knee stiffness"
	assert active_preview["last_activity_at"] == latest_doctor_message.json()["created_at"]
	assert active_preview["last_activity_label"] == "Latest Care Conversation message"

	detail_after_select = client.get(f"/care-episodes/{episode_id}", headers=_auth(patient_token)).json()
	assert detail_after_select["selected_doctor_id"] == doctor_one_id

	reject_response = client.post(f"/doctor/care-requests/{request_one_data['request_id']}/reject", headers=_auth(doctor_two_token))
	assert reject_response.status_code == 404, reject_response.text

	print("professional care contract ok")


if __name__ == "__main__":
	main()
