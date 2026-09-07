from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AIDailyRehabRecommendation
from app.db.session import SessionLocal, init_db
from app.main import app
from app.services.exercise_catalog import load_exercise_catalog
from app.services.rehab_recommendations import set_rehab_recommendation_searcher_for_tests


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "recommend-create-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _create_episode(client: TestClient, token: str) -> dict:
	response = client.post(
		"/care-episodes",
		json={
			"issue_title": "Left ankle sprain",
			"body_area": "Left ankle",
			"goal": "Walk normally without pain",
			"short_description": "Rolled ankle last week with mild pain and no numbness.",
		},
		headers=_auth(token),
	)
	assert response.status_code == 201, response.text
	return response.json()


def _active_recommendations(episode_id: str) -> list[AIDailyRehabRecommendation]:
	with SessionLocal() as db:
		return list(
			db.execute(
				select(AIDailyRehabRecommendation)
				.where(
					AIDailyRehabRecommendation.care_episode_id == episode_id,
					AIDailyRehabRecommendation.active == True,  # noqa: E712
				)
				.order_by(AIDailyRehabRecommendation.created_at.desc())
			).scalars()
		)


def _all_recommendations(episode_id: str) -> list[AIDailyRehabRecommendation]:
	with SessionLocal() as db:
		return list(
			db.execute(
				select(AIDailyRehabRecommendation)
				.where(AIDailyRehabRecommendation.care_episode_id == episode_id)
				.order_by(AIDailyRehabRecommendation.created_at.asc())
			).scalars()
		)


def main() -> None:
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"recommend_create_patient_{suffix}")
	episode = _create_episode(client, patient_token)
	episode_id = episode["care_episode_id"]

	catalog_exercises = list(load_exercise_catalog())
	assert len(catalog_exercises) >= 3
	first_exercise = catalog_exercises[0]
	second_exercise = catalog_exercises[1]
	missing_document_type_exercise = catalog_exercises[2]

	def fake_searcher(query_text: str, limit: int) -> list[dict]:
		assert "Left ankle sprain" in query_text
		assert "Walk normally without pain" in query_text
		return [
			{"score": 0.92, "payload": {"document_type": "rehab_exercise", "exercise_id": first_exercise.exercise_id}},
			{"score": 0.89, "payload": {"document_type": "rehab_exercise", "exercise_id": second_exercise.exercise_id}},
			{"score": 0.96, "payload": {"exercise_id": missing_document_type_exercise.exercise_id}},
			{"score": 0.99, "payload": {"document_type": "rehab_exercise", "exercise_id": "missing-catalog-id"}},
		]

	set_rehab_recommendation_searcher_for_tests(fake_searcher)
	try:
		draft_response = client.post(
			f"/care-episodes/{episode_id}/triage-summary-drafts",
			json={"additional_context": "I can bear weight, no bruising, no numbness, pain is mild."},
			headers=_auth(patient_token),
		)
		assert draft_response.status_code == 201, draft_response.text
		draft = draft_response.json()
		assert _all_recommendations(episode_id) == []

		select_response = client.post(
			f"/care-episodes/{episode_id}/triage-summary-drafts/{draft['triage_summary_draft_id']}/select",
			headers=_auth(patient_token),
		)
		assert select_response.status_code == 201, select_response.text
		selected_summary = select_response.json()
		active_after_select = _active_recommendations(episode_id)
		assert len(active_after_select) == 1
		selected_recommendation = active_after_select[0]
		assert str(selected_recommendation.source_triage_summary_id) == selected_summary["triage_summary_id"]
		assert selected_recommendation.source_triage_summary_version == selected_summary["version"]
		assert selected_recommendation.status == "ready"
		assert selected_recommendation.empty_reason == ""
		assert selected_recommendation.query_text
		assert [item["exercise_id"] for item in selected_recommendation.items] == [
			first_exercise.exercise_id,
			second_exercise.exercise_id,
		]
		assert selected_recommendation.items[0]["title"] == first_exercise.title
		assert selected_recommendation.items[0]["dosage"]

		direct_summary_response = client.post(
			f"/care-episodes/{episode_id}/triage-summary",
			json={"additional_context": "Still mild pain, can bear weight, no bruising, no numbness."},
			headers=_auth(patient_token),
		)
		assert direct_summary_response.status_code == 201, direct_summary_response.text
		direct_summary = direct_summary_response.json()
		all_after_direct_save = _all_recommendations(episode_id)
		active_after_direct_save = [item for item in all_after_direct_save if item.active]
		inactive_after_direct_save = [item for item in all_after_direct_save if not item.active]
		assert len(all_after_direct_save) == 2
		assert len(active_after_direct_save) == 1
		assert len(inactive_after_direct_save) == 1
		assert str(active_after_direct_save[0].source_triage_summary_id) == direct_summary["triage_summary_id"]
		assert active_after_direct_save[0].status == "ready"
		assert inactive_after_direct_save[0].recommendation_id == selected_recommendation.recommendation_id

		def failing_searcher(query_text: str, limit: int) -> list[dict]:
			raise RuntimeError("qdrant offline")

		set_rehab_recommendation_searcher_for_tests(failing_searcher)
		empty_episode = _create_episode(client, patient_token)
		empty_episode_id = empty_episode["care_episode_id"]
		empty_summary_response = client.post(
			f"/care-episodes/{empty_episode_id}/triage-summary",
			json={"additional_context": "Can bear weight, no bruising, no numbness, pain is mild."},
			headers=_auth(patient_token),
		)
		assert empty_summary_response.status_code == 201, empty_summary_response.text
		active_empty_recommendations = _active_recommendations(empty_episode_id)
		assert len(active_empty_recommendations) == 1
		empty_recommendation = active_empty_recommendations[0]
		assert empty_recommendation.status == "empty"
		assert empty_recommendation.empty_reason
		assert empty_recommendation.items == []

		def unresolved_catalog_searcher(query_text: str, limit: int) -> list[dict]:
			return [
				{"score": 0.91, "payload": {"document_type": "rehab_exercise", "exercise_id": "not-in-catalog"}},
				{"score": 0.88, "payload": {"document_type": "rehab_exercise", "exercise_id": "also-not-in-catalog"}},
			]

		set_rehab_recommendation_searcher_for_tests(unresolved_catalog_searcher)
		unresolved_episode = _create_episode(client, patient_token)
		unresolved_episode_id = unresolved_episode["care_episode_id"]
		unresolved_summary_response = client.post(
			f"/care-episodes/{unresolved_episode_id}/triage-summary",
			json={"additional_context": "Can bear weight, no bruising, no numbness, pain is mild."},
			headers=_auth(patient_token),
		)
		assert unresolved_summary_response.status_code == 201, unresolved_summary_response.text
		active_unresolved_recommendations = _active_recommendations(unresolved_episode_id)
		assert len(active_unresolved_recommendations) == 1
		unresolved_recommendation = active_unresolved_recommendations[0]
		assert unresolved_recommendation.status == "empty"
		assert unresolved_recommendation.empty_reason
		assert unresolved_recommendation.items == []

		def malformed_searcher(query_text: str, limit: int) -> list[dict]:
			return [
				None,
				"not-a-result",
				{"score": 0.72, "payload": "not-a-payload"},
				{"score": "not-a-number", "payload": {"document_type": "rehab_exercise", "exercise_id": first_exercise.exercise_id}},
			]

		set_rehab_recommendation_searcher_for_tests(malformed_searcher)
		malformed_episode = _create_episode(client, patient_token)
		malformed_episode_id = malformed_episode["care_episode_id"]
		malformed_summary_response = client.post(
			f"/care-episodes/{malformed_episode_id}/triage-summary",
			json={"additional_context": "Can bear weight, no bruising, no numbness, pain is mild."},
			headers=_auth(patient_token),
		)
		assert malformed_summary_response.status_code == 201, malformed_summary_response.text
		active_malformed_recommendations = _active_recommendations(malformed_episode_id)
		assert len(active_malformed_recommendations) == 1
		malformed_recommendation = active_malformed_recommendations[0]
		assert malformed_recommendation.status == "empty"
		assert malformed_recommendation.empty_reason
		assert malformed_recommendation.items == []
	finally:
		set_rehab_recommendation_searcher_for_tests(None)

	print("rehab recommendation creation contract ok")


if __name__ == "__main__":
	main()
