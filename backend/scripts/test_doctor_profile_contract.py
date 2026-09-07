from __future__ import annotations

import sys
import time
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db.models import Doctor
from app.db.session import SessionLocal, init_db
from app.main import app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, suffix: str) -> tuple[str, str]:
    response = client.post(
        "/auth/register/patient",
        json={"patient_name": f"doctor_profile_patient_{suffix}", "password": "doctor-profile-pass-123", "real_info": {}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["access_token"], body["user"]["user_id"]


def _register_doctor(client: TestClient, suffix: str, *, extra_real_info: dict | None = None) -> tuple[str, str]:
    response = client.post(
        "/auth/register/doctor",
        json={
            "doctor_name": f"doctor_profile_doctor_{suffix}",
            "password": "doctor-profile-pass-123",
            "verification_status": "verified",
            "real_info": {
                "display_name": "Dr. Profile Keeper",
                "specialty": "Initial specialty",
                "expertise_tags": ["Initial tag"],
                "source": "doctor-profile-contract",
                **(extra_real_info or {}),
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["access_token"], body["user"]["user_id"]


def _create_episode(client: TestClient, token: str, suffix: str) -> str:
    response = client.post(
        "/care-episodes",
        json={
            "issue_title": "Ankle recovery",
            "body_area": "Ankle",
            "goal": "Return to running",
            "short_description": f"Ankle stiffness after a sprain {suffix}.",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["care_episode_id"]


def main() -> None:
    init_db()
    client = TestClient(app)
    suffix = str(time.time_ns())

    patient_token, _ = _register_patient(client, suffix)
    doctor_token, doctor_id = _register_doctor(client, suffix)
    other_doctor_token, other_doctor_id = _register_doctor(client, f"other_{suffix}", extra_real_info={"display_name": "Dr. Other Profile"})

    missing_auth = client.get("/doctor/profile")
    assert missing_auth.status_code == 401, missing_auth.text

    patient_forbidden = client.get("/doctor/profile", headers=_auth(patient_token))
    assert patient_forbidden.status_code == 403, patient_forbidden.text

    initial_profile = client.get("/doctor/profile", headers=_auth(doctor_token))
    assert initial_profile.status_code == 200, initial_profile.text
    initial_body = initial_profile.json()
    assert initial_body["doctor_id"] == doctor_id
    assert initial_body["display_name"] == "Dr. Profile Keeper"
    assert initial_body["verification_status"] == "verified"
    assert initial_body["specialty"] == "Initial specialty"
    assert initial_body["expertise_tags"] == ["Initial tag"]
    assert "real_info" not in initial_body
    assert "doctor_name" not in initial_body

    invalid_tags = client.patch(
        "/doctor/profile",
        json={"specialty": "Rehab", "expertise_tags": ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]},
        headers=_auth(doctor_token),
    )
    assert invalid_tags.status_code == 200, invalid_tags.text
    assert invalid_tags.json()["expertise_tags"] == ["one", "two", "three", "four", "five", "six", "seven", "eight"]

    update = client.patch(
        "/doctor/profile",
        json={
            "specialty": "  Sports physical therapy  ",
            "expertise_tags": [
                " Knee ",
                "",
                "knee",
                "Sports injury",
                "Sports injury ",
                "  ",
                "Balance",
                "Running",
                "ACL",
                "Post-op",
                "Return to sport",
                "Strength",
            ],
        },
        headers=_auth(doctor_token),
    )
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["doctor_id"] == doctor_id
    assert body["specialty"] == "Sports physical therapy"
    assert body["expertise_tags"] == [
        "Knee",
        "Sports injury",
        "Balance",
        "Running",
        "ACL",
        "Post-op",
        "Return to sport",
        "Strength",
    ]
    assert body["display_name"] == "Dr. Profile Keeper"
    assert body["verification_status"] == "verified"
    assert "real_info" not in body

    with SessionLocal() as db:
        stored = db.get(Doctor, UUID(doctor_id))
        assert stored is not None
        assert stored.real_info["display_name"] == "Dr. Profile Keeper"
        assert stored.real_info["source"] == "doctor-profile-contract"
        assert stored.real_info["specialty"] == "Sports physical therapy"
        assert stored.real_info["expertise_tags"] == [
            "Knee",
            "Sports injury",
            "Balance",
            "Running",
            "ACL",
            "Post-op",
            "Return to sport",
            "Strength",
        ]

    refresh = client.get("/doctor/profile", headers=_auth(doctor_token))
    assert refresh.status_code == 200, refresh.text
    assert refresh.json()["expertise_tags"] == [
        "Knee",
        "Sports injury",
        "Balance",
        "Running",
        "ACL",
        "Post-op",
        "Return to sport",
        "Strength",
    ]

    other_profile = client.get("/doctor/profile", headers=_auth(other_doctor_token))
    assert other_profile.status_code == 200, other_profile.text
    assert other_profile.json()["doctor_id"] == other_doctor_id
    assert other_profile.json()["display_name"] == "Dr. Other Profile"
    assert other_profile.json()["specialty"] == "Initial specialty"

    subscribe = client.post("/professional-care/subscribe", headers=_auth(patient_token))
    assert subscribe.status_code == 200, subscribe.text
    episode_id = _create_episode(client, patient_token, suffix)

    directory = client.get("/professional-care/doctors", headers=_auth(patient_token))
    assert directory.status_code == 200, directory.text
    directory_doctor = next(item for item in directory.json()["doctors"] if item["doctor_id"] == doctor_id)
    assert directory_doctor["specialty"] == "Sports physical therapy"
    assert "Knee" in directory_doctor["expertise_tags"]
    assert "Sports injury" in directory_doctor["expertise_tags"]
    assert "doctor_name" not in directory_doctor
    assert "real_info" not in directory_doctor

    lexical_search = client.get(
        f"/care-episodes/{episode_id}/doctor-search?query=sports%20injury%20running",
        headers=_auth(patient_token),
    )
    assert lexical_search.status_code == 200, lexical_search.text
    search_doctor = next(item for item in lexical_search.json()["results"] if item["doctor"]["doctor_id"] == doctor_id)
    assert search_doctor["doctor"]["specialty"] == "Sports physical therapy"
    assert "Sports injury" in search_doctor["doctor"]["expertise_tags"]
    assert search_doctor["match_reason"]

    print("doctor professional profile contract ok")


if __name__ == "__main__":
    main()
