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
    response = client.post(
        "/auth/register/patient",
        json={"patient_name": name, "password": "daily-list-pass-123", "real_info": {}},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _register_doctor(client: TestClient, name: str) -> str:
    response = client.post(
        "/auth/register/doctor",
        json={"doctor_name": name, "password": "daily-list-pass-123", "real_info": {}},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _create_episode(client: TestClient, token: str) -> dict:
    response = client.post(
        "/care-episodes",
        json={
            "issue_title": "Ankle sprain",
            "body_area": "Left ankle",
            "goal": "Walk normally",
            "short_description": "Rolled ankle last week.",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _complete_triage(client: TestClient, episode_id: str, token: str) -> None:
    response = client.post(
        f"/care-episodes/{episode_id}/triage-summary",
        json={"additional_context": "I can bear weight, no bruising, no numbness, pain is mild."},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text


def main() -> None:
    init_db()
    client = TestClient(app)
    suffix = str(time.time_ns())
    patient_token = _register_patient(client, f"daily_list_patient_{suffix}")
    other_patient_token = _register_patient(client, f"daily_list_other_{suffix}")
    doctor_token = _register_doctor(client, f"daily_list_doctor_{suffix}")
    episode = _create_episode(client, patient_token)
    episode_id = episode["care_episode_id"]

    before_triage_recommendation = client.get(
        f"/care-episodes/{episode_id}/rehab-recommendation",
        headers=_auth(patient_token),
    )
    assert before_triage_recommendation.status_code == 409, before_triage_recommendation.text

    before_triage_list = client.get(f"/care-episodes/{episode_id}/rehab-list", headers=_auth(patient_token))
    assert before_triage_list.status_code == 409, before_triage_list.text

    before_triage_update = client.put(
        f"/care-episodes/{episode_id}/rehab-list",
        json={"exercise_ids": ["does-not-matter"]},
        headers=_auth(patient_token),
    )
    assert before_triage_update.status_code == 409, before_triage_update.text

    catalog = list(load_exercise_catalog())
    assert len(catalog) >= 2
    first_exercise = catalog[0]
    second_exercise = catalog[1]

    def fake_searcher(query_text: str, limit: int) -> list[dict]:
        return [
            {"score": 0.92, "payload": {"document_type": "rehab_exercise", "exercise_id": first_exercise.exercise_id}},
            {"score": 0.88, "payload": {"document_type": "rehab_exercise", "exercise_id": second_exercise.exercise_id}},
        ]

    set_rehab_recommendation_searcher_for_tests(fake_searcher)
    try:
        _complete_triage(client, episode_id, patient_token)
    finally:
        set_rehab_recommendation_searcher_for_tests(None)

    recommendation_response = client.get(f"/care-episodes/{episode_id}/rehab-recommendation", headers=_auth(patient_token))
    assert recommendation_response.status_code == 200, recommendation_response.text
    recommendation = recommendation_response.json()
    assert recommendation is not None
    assert recommendation["care_episode_id"] == episode_id
    assert recommendation["active"] is True
    assert "items" in recommendation

    empty_list_response = client.get(f"/care-episodes/{episode_id}/rehab-list", headers=_auth(patient_token))
    assert empty_list_response.status_code == 200, empty_list_response.text
    empty_list = empty_list_response.json()
    assert empty_list["care_episode_id"] == episode_id
    assert empty_list["items"] == []

    empty_session = client.post(
        f"/care-episodes/{episode_id}/rehab-sessions",
        json={"patient_notes": "Ready to start."},
        headers=_auth(patient_token),
    )
    assert empty_session.status_code == 409, empty_session.text

    list_update = client.put(
        f"/care-episodes/{episode_id}/rehab-list",
        json={"exercise_ids": [first_exercise.exercise_id, first_exercise.exercise_id, second_exercise.exercise_id]},
        headers=_auth(patient_token),
    )
    assert list_update.status_code == 200, list_update.text
    updated_list = list_update.json()
    expected_exercise_ids = [first_exercise.exercise_id, second_exercise.exercise_id]
    assert [item["exercise_id"] for item in updated_list["items"]] == expected_exercise_ids
    assert updated_list["items"][0]["label"] == first_exercise.title
    assert updated_list["items"][0]["snapshot"]["exercise_id"] == first_exercise.exercise_id
    assert updated_list["items"][0]["snapshot"]["title"] == first_exercise.title

    invalid_list_update = client.put(
        f"/care-episodes/{episode_id}/rehab-list",
        json={"exercise_ids": ["not-in-catalog", first_exercise.exercise_id]},
        headers=_auth(patient_token),
    )
    assert invalid_list_update.status_code == 422, invalid_list_update.text

    after_invalid_update = client.get(f"/care-episodes/{episode_id}/rehab-list", headers=_auth(patient_token))
    assert after_invalid_update.status_code == 200, after_invalid_update.text
    assert [item["exercise_id"] for item in after_invalid_update.json()["items"]] == expected_exercise_ids

    malformed_extra_session = client.post(
        f"/care-episodes/{episode_id}/rehab-sessions",
        json={"unexpected_field": "not legacy", "patient_notes": "malformed payload"},
        headers=_auth(patient_token),
    )
    assert malformed_extra_session.status_code == 422, malformed_extra_session.text

    legacy_session = client.post(
        f"/care-episodes/{episode_id}/rehab-sessions",
        json={"recommended_exercises": ["free text exercise"], "patient_notes": "legacy payload"},
        headers=_auth(patient_token),
    )
    assert legacy_session.status_code == 409, legacy_session.text

    session_response = client.post(
        f"/care-episodes/{episode_id}/rehab-sessions",
        json={"patient_notes": "Felt stiff before starting."},
        headers=_auth(patient_token),
    )
    assert session_response.status_code == 201, session_response.text
    session = session_response.json()
    assert [item["exercise_id"] for item in session["recommended_exercises"]] == [
        first_exercise.exercise_id,
        second_exercise.exercise_id,
    ]
    assert session["checklist"][0]["exercise_id"] == first_exercise.exercise_id
    assert session["checklist"][0]["label"] == first_exercise.title
    assert session["checklist"][0]["snapshot"]["exercise_id"] == first_exercise.exercise_id
    assert session["checklist"][0]["completed"] is False

    patch_response = client.patch(
        f"/rehab-sessions/{session['session_id']}",
        json={"completed_items": [first_exercise.exercise_id], "patient_notes": "Mild soreness after first item."},
        headers=_auth(patient_token),
    )
    assert patch_response.status_code == 200, patch_response.text
    patched = patch_response.json()
    assert patched["checklist"][0]["completed"] is True
    assert patched["checklist"][1]["completed"] is False

    summary_response = client.post(f"/rehab-sessions/{session['session_id']}/summary", headers=_auth(patient_token))
    assert summary_response.status_code == 200, summary_response.text
    summary = summary_response.json()
    assert first_exercise.title in summary["session_summary"]
    assert second_exercise.title in summary["session_summary"]

    other_patient_list = client.get(f"/care-episodes/{episode_id}/rehab-list", headers=_auth(other_patient_token))
    assert other_patient_list.status_code == 404, other_patient_list.text

    doctor_recommendation = client.get(f"/care-episodes/{episode_id}/rehab-recommendation", headers=_auth(doctor_token))
    assert doctor_recommendation.status_code == 403, doctor_recommendation.text

    print("daily rehab list contract ok")


if __name__ == "__main__":
    main()
