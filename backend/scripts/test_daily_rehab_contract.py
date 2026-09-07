from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import app
from app.services.exercise_catalog import load_exercise_catalog
from app.services.rehab_recommendations import set_rehab_recommendation_searcher_for_tests


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
    response = client.post("/auth/register/patient", json={"patient_name": name, "password": "rehab-pass-123", "real_info": {}})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _create_episode(client: TestClient, token: str) -> dict:
    response = client.post(
        "/care-episodes",
        json={"issue_title": "Ankle sprain", "body_area": "Left ankle", "goal": "Walk normally", "short_description": "Rolled ankle last week."},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def main() -> None:
    init_db()
    client = TestClient(app)
    suffix = str(time.time_ns())
    patient_token = _register_patient(client, f"rehab_patient_{suffix}")
    other_patient_token = _register_patient(client, f"rehab_other_{suffix}")
    episode = _create_episode(client, patient_token)
    episode_id = episode["care_episode_id"]

    catalog_exercises = list(load_exercise_catalog())
    assert len(catalog_exercises) >= 2
    first_exercise = catalog_exercises[0]
    second_exercise = catalog_exercises[1]

    gated_response = client.post(
        f"/care-episodes/{episode_id}/rehab-sessions",
        json={"patient_notes": "Trying to start before triage."},
        headers=_auth(patient_token),
    )
    assert gated_response.status_code == 409, gated_response.text

    def fake_searcher(query_text: str, limit: int) -> list[dict]:
        return [
            {"score": 0.92, "payload": {"document_type": "rehab_exercise", "exercise_id": first_exercise.exercise_id}},
            {"score": 0.88, "payload": {"document_type": "rehab_exercise", "exercise_id": second_exercise.exercise_id}},
        ]

    set_rehab_recommendation_searcher_for_tests(fake_searcher)
    try:
        summary_response = client.post(
            f"/care-episodes/{episode_id}/triage-summary",
            json={"additional_context": "I can bear weight, no bruising, no numbness, pain is mild."},
            headers=_auth(patient_token),
        )
        assert summary_response.status_code == 201, summary_response.text
    finally:
        set_rehab_recommendation_searcher_for_tests(None)

    list_response = client.put(
        f"/care-episodes/{episode_id}/rehab-list",
        json={"exercise_ids": [first_exercise.exercise_id, second_exercise.exercise_id]},
        headers=_auth(patient_token),
    )
    assert list_response.status_code == 200, list_response.text
    rehab_list = list_response.json()
    assert [item["exercise_id"] for item in rehab_list["items"]] == [first_exercise.exercise_id, second_exercise.exercise_id]

    session_response = client.post(
        f"/care-episodes/{episode_id}/rehab-sessions",
        json={"patient_notes": "Felt stiff before starting."},
        headers=_auth(patient_token),
    )
    assert session_response.status_code == 201, session_response.text
    session = session_response.json()
    assert session["care_episode_id"] == episode_id
    assert session["recommended_exercises"][0]["exercise_id"] == first_exercise.exercise_id
    assert session["checklist"][0]["label"] == first_exercise.title
    assert session["checklist"][0]["completed"] is False

    update_response = client.patch(
        f"/rehab-sessions/{session['session_id']}",
        json={"completed_items": [first_exercise.exercise_id], "patient_notes": "Mild soreness after first item."},
        headers=_auth(patient_token),
    )
    assert update_response.status_code == 200, update_response.text
    updated = update_response.json()
    assert updated["checklist"][0]["completed"] is True
    assert updated["patient_notes"] == "Mild soreness after first item."

    summary = client.post(f"/rehab-sessions/{session['session_id']}/summary", headers=_auth(patient_token))
    assert summary.status_code == 200, summary.text
    session_summary = summary.json()
    assert first_exercise.title in session_summary["session_summary"]
    assert session_summary["completion_status"] == "summarized"

    sessions_response = client.get(f"/care-episodes/{episode_id}/rehab-sessions", headers=_auth(patient_token))
    assert sessions_response.status_code == 200, sessions_response.text
    assert sessions_response.json()["sessions"][0]["session_id"] == session["session_id"]

    other_access = client.get(f"/care-episodes/{episode_id}/rehab-sessions", headers=_auth(other_patient_token))
    assert other_access.status_code == 404, other_access.text

    print("daily rehab contract ok")


if __name__ == "__main__":
    main()
