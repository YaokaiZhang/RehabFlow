from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db.models import (
    CareAppointment,
    CareConnectionRequest,
    CareEpisode,
    CareRelationship,
    DoctorIntelligenceArtifact,
)
from app.db.session import SessionLocal, init_db
from app.main import app
from app.services.memory_documents import ensure_episode_memory_document, ensure_patient_memory_document, refresh_compiled_text


WORKFLOW_MUTATION_MARKERS = [
    "accept",
    "reject",
    "select_relationship",
    "send_message",
    "/doctor/care-requests",
    "/care-relationship/select",
    "/care-conversation/messages",
]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
    response = client.post(
        "/auth/register/patient",
        json={"patient_name": name, "password": "dashboard-pass-123", "real_info": {"display_name": "Panel Patient"}},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _register_doctor(client: TestClient, name: str) -> tuple[str, str]:
    response = client.post(
        "/auth/register/doctor",
        json={"doctor_name": name, "password": "dashboard-pass-123", "real_info": {"specialty": "Sports PT"}},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return data["access_token"], data["user"]["user_id"]


def _create_episode(client: TestClient, token: str, *, issue_title: str = "Shoulder mobility plan") -> dict:
    response = client.post(
        "/care-episodes",
        json={
            "issue_title": issue_title,
            "body_area": "Shoulder",
            "goal": "Return to overhead lifting",
            "short_description": "Persistent stiffness after a training flare-up.",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_active_relationship_and_appointment(
    *,
    patient_id: str,
    doctor_id: str,
    care_episode_id: str,
) -> tuple[str, str]:
    with SessionLocal() as db:
        request = CareConnectionRequest(
            care_episode_id=care_episode_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            request_reason="Please review my durable memory and next appointment prep.",
            status="accepted",
            responded_at=datetime.now(timezone.utc),
        )
        db.add(request)
        db.flush()
        relationship = CareRelationship(
            care_episode_id=care_episode_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            source_request_id=request.request_id,
            status="active",
        )
        db.add(relationship)
        db.flush()

        episode = db.get(CareEpisode, care_episode_id)
        assert episode is not None
        episode.selected_doctor_id = doctor_id

        patient_document = ensure_patient_memory_document(db, patient_id)
        patient_document.editable_fields = {
            "care_preference": "Prefers concise home instructions and early warning signs.",
        }
        episode_document = ensure_episode_memory_document(db, episode)
        episode_document.level1_keywords = ["clinical_context"]
        episode_document.level1_description = "Overhead reach remains limited late in the day."
        db.flush()
        refresh_compiled_text(patient_document)
        refresh_compiled_text(episode_document)

        appointment = CareAppointment(
            relationship_id=relationship.relationship_id,
            care_episode_id=care_episode_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            scheduled_start=datetime.now(timezone.utc) + timedelta(days=2),
            scheduled_end=datetime.now(timezone.utc) + timedelta(days=2, minutes=45),
            status="scheduled",
            purpose="Prep appointment: review shoulder progression",
        )
        db.add(appointment)
        db.commit()
        return str(relationship.relationship_id), str(appointment.appointment_id)


def _seed_malformed_dashboard_rows(
    *,
    relationship_id: str,
    patient_id: str,
    doctor_id: str,
    other_doctor_id: str,
    care_episode_id: str,
    other_patient_id: str,
    other_episode_id: str,
) -> list[str]:
    with SessionLocal() as db:
        unrelated_episode = db.get(CareEpisode, other_episode_id)
        assert unrelated_episode is not None
        unrelated_document = ensure_episode_memory_document(db, unrelated_episode)
        unrelated_document.level1_keywords = ["private_context"]
        unrelated_document.level1_description = "Do not leak this unrelated patient memory."
        db.flush()
        refresh_compiled_text(unrelated_document)

        malformed_request = CareConnectionRequest(
            care_episode_id=other_episode_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            request_reason="Malformed mismatched relationship should not be dashboard-readable.",
            status="accepted",
            responded_at=datetime.now(timezone.utc),
        )
        db.add(malformed_request)
        db.flush()
        db.add(
            CareRelationship(
                care_episode_id=other_episode_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                source_request_id=malformed_request.request_id,
                status="active",
            )
        )

        invalid_appointments = [
            CareAppointment(
                relationship_id=relationship_id,
                care_episode_id=care_episode_id,
                patient_id=other_patient_id,
                doctor_id=doctor_id,
                scheduled_start=datetime.now(timezone.utc) + timedelta(days=3),
                status="scheduled",
                purpose="Invalid appointment: mismatched patient",
            ),
            CareAppointment(
                relationship_id=relationship_id,
                care_episode_id=other_episode_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                scheduled_start=datetime.now(timezone.utc) + timedelta(days=4),
                status="scheduled",
                purpose="Invalid appointment: mismatched episode",
            ),
            CareAppointment(
                relationship_id=relationship_id,
                care_episode_id=care_episode_id,
                patient_id=patient_id,
                doctor_id=other_doctor_id,
                scheduled_start=datetime.now(timezone.utc) + timedelta(days=5),
                status="scheduled",
                purpose="Invalid appointment: other doctor",
            ),
            CareAppointment(
                relationship_id=relationship_id,
                care_episode_id=care_episode_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                scheduled_start=datetime.now(timezone.utc) - timedelta(days=1),
                status="scheduled",
                purpose="Invalid appointment: past appointment",
            ),
            CareAppointment(
                relationship_id=relationship_id,
                care_episode_id=care_episode_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                scheduled_start=datetime.now(timezone.utc) + timedelta(days=6),
                status="cancelled",
                purpose="Invalid appointment: cancelled appointment",
            ),
        ]
        db.add_all(invalid_appointments)
        db.commit()
        return [str(appointment.appointment_id) for appointment in invalid_appointments]


def _add_valid_dashboard_appointment(
    *,
    relationship_id: str,
    patient_id: str,
    doctor_id: str,
    care_episode_id: str,
) -> str:
    with SessionLocal() as db:
        appointment = CareAppointment(
            relationship_id=relationship_id,
            care_episode_id=care_episode_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            scheduled_start=datetime.now(timezone.utc) + timedelta(days=7),
            scheduled_end=datetime.now(timezone.utc) + timedelta(days=7, minutes=30),
            status="confirmed",
            purpose="Progress check after updated memory review",
        )
        db.add(appointment)
        db.commit()
        return str(appointment.appointment_id)


def _assert_no_workflow_mutations(payload: dict) -> None:
    assert "actions" not in payload
    serialized = json.dumps(payload).lower()
    for marker in WORKFLOW_MUTATION_MARKERS:
        assert marker not in serialized


def main() -> None:
    init_db()
    client = TestClient(app)
    suffix = str(time.time_ns())
    patient_token = _register_patient(client, f"dashboard_patient_{suffix}")
    other_patient_token = _register_patient(client, f"dashboard_other_patient_{suffix}")
    doctor_token, doctor_id = _register_doctor(client, f"dashboard_doctor_{suffix}")
    other_doctor_token, other_doctor_id = _register_doctor(client, f"dashboard_other_doctor_{suffix}")
    episode = _create_episode(client, patient_token)
    other_episode = _create_episode(client, other_patient_token, issue_title="Unrelated private episode")
    episode_id = episode["care_episode_id"]
    patient_id = episode["patient_id"]
    other_episode_id = other_episode["care_episode_id"]
    other_patient_id = other_episode["patient_id"]

    triage_response = client.post(
        f"/care-episodes/{episode_id}/triage-summary",
        json={"additional_context": "Stiffness is improving, but overhead reach is still limited."},
        headers=_auth(patient_token),
    )
    assert triage_response.status_code == 201, triage_response.text

    relationship_id, appointment_id = _seed_active_relationship_and_appointment(
        patient_id=patient_id,
        doctor_id=doctor_id,
        care_episode_id=episode_id,
    )
    invalid_appointment_ids = _seed_malformed_dashboard_rows(
        relationship_id=relationship_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        other_doctor_id=other_doctor_id,
        care_episode_id=episode_id,
        other_patient_id=other_patient_id,
        other_episode_id=other_episode_id,
    )

    patient_denied = client.get("/doctor/dashboard", headers=_auth(patient_token))
    assert patient_denied.status_code == 403, patient_denied.text

    missing_panel = client.get("/doctor/dashboard", headers=_auth(other_doctor_token))
    assert missing_panel.status_code == 200, missing_panel.text
    assert missing_panel.json()["care_relationships"] == []
    assert missing_panel.json()["upcoming_appointments"] == []

    dashboard_response = client.get("/doctor/dashboard", headers=_auth(doctor_token))
    assert dashboard_response.status_code == 200, dashboard_response.text
    dashboard = dashboard_response.json()
    assert dashboard["care_worklist_href"] == "/doctor"
    _assert_no_workflow_mutations(dashboard)
    serialized_dashboard = json.dumps(dashboard)
    assert "Unrelated private episode" not in serialized_dashboard
    assert "Do not leak this unrelated patient memory" not in serialized_dashboard
    for invalid_appointment_id in invalid_appointment_ids:
        assert invalid_appointment_id not in serialized_dashboard

    briefing = dashboard["patient_panel_briefing"]
    assert briefing["artifact_type"] == "patient_panel_briefing"
    assert briefing["doctor_id"] == doctor_id
    assert briefing["visibility"] == "doctor_private"
    assert briefing["version"] >= 1
    assert briefing["status"] == "active"
    assert "Shoulder mobility plan" in briefing["content"]
    assert any(section["key"] == "memory_highlights" for section in briefing["sections"])
    assert relationship_id in briefing["input_refs"].get("relationship_ids", [])
    assert other_episode_id not in briefing["input_refs"].get("care_episode_ids", [])

    care_relationships = dashboard["care_relationships"]
    assert len(care_relationships) == 1
    assert care_relationships[0]["relationship_id"] == relationship_id
    assert care_relationships[0]["latest_triage_summary"]["concern"] == "Shoulder mobility plan"
    assert "Prefers concise home instructions" in care_relationships[0]["memory_context"]
    assert "Overhead reach remains limited" in care_relationships[0]["memory_context"]

    attention_map = dashboard["attention_map"]
    bucket_names = {bucket["bucket"] for bucket in attention_map["buckets"]}
    assert "clinician_review" in bucket_names
    assert "upcoming_appointments" in bucket_names
    assert attention_map["summary"]

    appointments = dashboard["upcoming_appointments"]
    assert len(appointments) == 1
    assert appointments[0]["appointment_id"] == appointment_id
    assert appointments[0]["relationship_id"] == relationship_id
    assert appointments[0]["purpose"] == "Prep appointment: review shoulder progression"
    assert appointments[0]["patient_id"] == patient_id
    assert appointments[0]["care_episode_id"] == episode_id
    assert appointment_id in briefing["input_refs"].get("appointment_ids", [])

    with SessionLocal() as db:
        initial_artifacts = db.query(DoctorIntelligenceArtifact).filter_by(doctor_id=doctor_id).count()
    assert initial_artifacts >= 1

    refresh_response = client.post("/doctor/dashboard/briefing/refresh", headers=_auth(doctor_token))
    assert refresh_response.status_code == 201, refresh_response.text
    refreshed = refresh_response.json()
    assert refreshed["artifact_type"] == "patient_panel_briefing"
    assert refreshed["doctor_id"] == doctor_id
    assert refreshed["version"] > briefing["version"] or refreshed["artifact_id"] != briefing["artifact_id"]
    assert refreshed["visibility"] == "doctor_private"

    new_appointment_id = _add_valid_dashboard_appointment(
        relationship_id=relationship_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        care_episode_id=episode_id,
    )
    changed_dashboard_response = client.get("/doctor/dashboard", headers=_auth(doctor_token))
    assert changed_dashboard_response.status_code == 200, changed_dashboard_response.text
    changed_dashboard = changed_dashboard_response.json()
    changed_briefing = changed_dashboard["patient_panel_briefing"]
    assert len(changed_dashboard["upcoming_appointments"]) == 2
    assert new_appointment_id in {appointment["appointment_id"] for appointment in changed_dashboard["upcoming_appointments"]}
    assert new_appointment_id in changed_briefing["input_refs"].get("appointment_ids", [])
    assert "Progress check after updated memory review" in changed_briefing["content"]
    assert changed_briefing["version"] > refreshed["version"] or changed_briefing["artifact_id"] != refreshed["artifact_id"]

    stable_dashboard_response = client.get("/doctor/dashboard", headers=_auth(doctor_token))
    assert stable_dashboard_response.status_code == 200, stable_dashboard_response.text
    stable_briefing = stable_dashboard_response.json()["patient_panel_briefing"]
    assert stable_briefing["artifact_id"] == changed_briefing["artifact_id"]
    assert stable_briefing["version"] == changed_briefing["version"]

    prep_response = client.post(
        "/doctor/dashboard/artifacts",
        json={
            "artifact_type": "care_appointment_prep",
            "title": "Prep for shoulder appointment",
            "content": "Review memory highlights and ask about late-day overhead reach.",
            "sections": [{"key": "questions", "title": "Questions", "items": ["Any swelling after exercises?"]}],
            "attention_map": {"buckets": [{"bucket": "upcoming_appointments", "label": "Upcoming appointments", "items": [appointment_id]}]},
            "input_refs": {"appointment_ids": [appointment_id], "relationship_ids": [relationship_id]},
        },
        headers=_auth(doctor_token),
    )
    assert prep_response.status_code == 201, prep_response.text
    prep = prep_response.json()
    assert prep["artifact_type"] == "care_appointment_prep"
    assert prep["visibility"] == "doctor_private"
    assert prep["version"] == 1
    assert prep["input_refs"]["appointment_ids"] == [appointment_id]

    print("doctor dashboard contract ok")


if __name__ == "__main__":
    main()
