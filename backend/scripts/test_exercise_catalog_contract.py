from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.exercise_catalog import _catalog_path

assert _catalog_path().exists(), _catalog_path()

from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import app

assert (Path(__file__).resolve().parents[1] / "data").exists()


REQUIRED_EXERCISE_FIELDS = {
	"exercise_id",
	"slug",
	"title",
	"introduction",
	"structures_involved",
	"related_conditions",
	"source_url",
	"video_url",
	"video_provider",
	"video_id",
	"video_available",
}


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "catalog-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _register_doctor(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/doctor",
		json={"doctor_name": name, "password": "catalog-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _assert_exercise_shape(exercise: dict) -> None:
	assert REQUIRED_EXERCISE_FIELDS <= set(exercise), exercise
	assert exercise["exercise_id"]
	assert exercise["slug"]
	assert exercise["title"]
	assert isinstance(exercise["structures_involved"], list)
	assert isinstance(exercise["related_conditions"], list)
	assert isinstance(exercise["video_available"], bool)


def main() -> None:
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"catalog_patient_{suffix}")
	doctor_token = _register_doctor(client, f"catalog_doctor_{suffix}")

	search_response = client.get("/exercise-catalog?query=ankle&limit=5", headers=_auth(patient_token))
	assert search_response.status_code == 200, search_response.text
	search = search_response.json()
	assert search["total"] >= 1
	assert search["limit"] == 5
	assert search["offset"] == 0
	assert 1 <= len(search["exercises"]) <= 5
	first_exercise = search["exercises"][0]
	_assert_exercise_shape(first_exercise)

	detail_response = client.get(f"/exercise-catalog/{first_exercise['exercise_id']}", headers=_auth(patient_token))
	assert detail_response.status_code == 200, detail_response.text
	detail = detail_response.json()
	_assert_exercise_shape(detail)
	assert detail["exercise_id"] == first_exercise["exercise_id"]

	missing_detail = client.get("/exercise-catalog/not-a-real-exercise-id", headers=_auth(patient_token))
	assert missing_detail.status_code == 404, missing_detail.text

	facets_response = client.get("/exercise-catalog/facets", headers=_auth(patient_token))
	assert facets_response.status_code == 200, facets_response.text
	facets = facets_response.json()
	assert facets["structures"], facets
	assert facets["conditions"], facets
	assert any("ankle" in structure.casefold() for structure in facets["structures"]), facets
	assert "Ankle Sprains" in facets["conditions"], facets

	filtered_response = client.get(
		"/exercise-catalog?structure=Ankle&condition=Ankle%20Sprains&limit=10",
		headers=_auth(patient_token),
	)
	assert filtered_response.status_code == 200, filtered_response.text
	filtered = filtered_response.json()
	assert filtered["total"] >= 1
	assert filtered["limit"] == 10
	for exercise in filtered["exercises"]:
		_assert_exercise_shape(exercise)
		structure_text = " ".join([exercise["slug"], exercise["title"], exercise["introduction"], *exercise["structures_involved"]])
		assert "ankle" in structure_text.casefold()
		condition_text = " ".join(exercise["related_conditions"])
		assert "ankle sprain" in condition_text.casefold()

	anonymous_response = client.get("/exercise-catalog?query=ankle")
	assert anonymous_response.status_code in {401, 403}, anonymous_response.text

	doctor_response = client.get("/exercise-catalog?query=ankle", headers=_auth(doctor_token))
	assert doctor_response.status_code == 403, doctor_response.text

	invalid_limit = client.get("/exercise-catalog?limit=51", headers=_auth(patient_token))
	assert invalid_limit.status_code == 422, invalid_limit.text

	print("exercise catalog contract ok")


if __name__ == "__main__":
	main()
