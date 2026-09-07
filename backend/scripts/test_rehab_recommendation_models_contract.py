from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AIDailyRehabList, AIDailyRehabRecommendation
from app.db.session import SessionLocal, init_db
from app.main import app


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "recommendation-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _create_episode(client: TestClient, token: str) -> dict:
	response = client.post(
		"/care-episodes",
		json={
			"issue_title": "Morning shoulder stiffness",
			"body_area": "Right shoulder",
			"goal": "Reach overhead with less pain",
			"short_description": "Stiffness after sleep and while dressing.",
		},
		headers=_auth(token),
	)
	assert response.status_code == 201, response.text
	return response.json()


def main() -> None:
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"recommendation_patient_{suffix}")
	episode = _create_episode(client, patient_token)
	patient_id = episode["patient_id"]
	episode_id = episode["care_episode_id"]

	recommendation_items = [
		{
			"exercise_id": "wall-slide",
			"title": "Wall slide",
			"reason": "Gentle shoulder mobility for morning stiffness.",
			"dosage": "2 sets of 8 slow reps",
		},
		{
			"exercise_id": "scapular-setting",
			"title": "Scapular setting",
			"reason": "Builds awareness before overhead reaching.",
			"dosage": "Hold 5 seconds for 10 reps",
		},
	]
	list_items = [
		{"exercise_id": "wall-slide", "title": "Wall slide", "completed": False},
		{"exercise_id": "scapular-setting", "title": "Scapular setting", "completed": True},
	]

	with SessionLocal() as db:
		recommendation = AIDailyRehabRecommendation(
			care_episode_id=episode_id,
			patient_id=patient_id,
			source_triage_summary_version=2,
			status="ready",
			query_text="right shoulder morning stiffness",
			items=recommendation_items,
		)
		db.add(recommendation)
		db.flush()

		default_status_recommendation = AIDailyRehabRecommendation(
			care_episode_id=episode_id,
			patient_id=patient_id,
			items=[],
		)
		db.add(default_status_recommendation)
		db.flush()

		rehab_list = AIDailyRehabList(
			care_episode_id=episode_id,
			patient_id=patient_id,
			source_recommendation_id=recommendation.recommendation_id,
			items=list_items,
		)
		db.add(rehab_list)
		db.commit()

	with SessionLocal() as db:
		stored_recommendation = db.get(AIDailyRehabRecommendation, recommendation.recommendation_id)
		stored_default_status_recommendation = db.get(
			AIDailyRehabRecommendation, default_status_recommendation.recommendation_id
		)
		stored_list = db.scalars(select(AIDailyRehabList).where(AIDailyRehabList.care_episode_id == episode_id)).one()

	assert stored_recommendation is not None
	assert stored_default_status_recommendation is not None

	assert stored_recommendation.patient_id == stored_list.patient_id
	assert stored_recommendation.status == "ready"
	assert stored_default_status_recommendation.status == "empty"
	assert stored_recommendation.empty_reason == ""
	assert stored_recommendation.active is True
	assert stored_recommendation.items == recommendation_items
	assert stored_list.source_recommendation_id == stored_recommendation.recommendation_id
	assert stored_list.items == list_items

	print("rehab recommendation model contract ok")


if __name__ == "__main__":
	main()
