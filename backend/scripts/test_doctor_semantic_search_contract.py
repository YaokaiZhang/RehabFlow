from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db.models import CareEpisode
from app.db.session import SessionLocal, verify_schema_revision
from app.main import app
from app.schemas.professional_care import DoctorDirectoryResponse
from app.services import professional_care_workflow, semantic_doctor_search


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, suffix: str) -> tuple[str, str]:
    response = client.post(
        "/auth/register/patient",
        json={"patient_name": f"semantic_patient_{suffix}", "password": "semantic-pass-123", "real_info": {}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["access_token"], body["user"]["user_id"]


def _register_doctor(client: TestClient, name: str, display_name: str, specialty: str, expertise_tags: list[str]) -> str:
    response = client.post(
        "/auth/register/doctor",
        json={
            "doctor_name": name,
            "password": "semantic-pass-123",
            "real_info": {
                "display_name": display_name,
                "specialty": specialty,
                "expertise_tags": expertise_tags,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]["user_id"]


def _register_doctor_without_public_display_name(client: TestClient, name: str) -> str:
    response = client.post(
        "/auth/register/doctor",
        json={
            "doctor_name": name,
            "password": "semantic-pass-123",
            "real_info": {"specialty": "Rehabilitation medicine", "expertise_tags": ["Knee"]},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]["user_id"]


def _register_ui_doctor(client: TestClient, name: str) -> str:
    response = client.post(
        "/auth/register/doctor",
        json={
            "doctor_name": name,
            "password": "semantic-pass-123",
            "real_info": {"source": "mvp-register-page"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]["user_id"]


def _create_knee_episode(client: TestClient, token: str) -> str:
    private_empty_query_narrative = "PRIVATE_EMPTY_QUERY_NARRATIVE " * 120
    response = client.post(
        "/care-episodes",
        json={
            "issue_title": "Knee stiffness",
            "body_area": "Knee",
            "goal": "Return to basketball",
            "short_description": f"Knee feels stiff in cold weather after an old basketball injury. {private_empty_query_narrative}",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["care_episode_id"]


def main() -> None:
    verify_schema_revision()
    client = TestClient(app)
    suffix = str(time.time_ns())
    patient_token, patient_id = _register_patient(client, suffix)
    knee_doctor_id = _register_doctor(
        client,
        f"semantic_knee_specialist_{suffix}",
        "Dr. Knee Sports",
        "Sports physical therapy",
        ["Knee", "Sports injury", "Basketball return", "Post-operative rehab"],
    )
    _register_doctor(
        client,
        f"semantic_shoulder_specialist_{suffix}",
        "Dr. Shoulder Mobility",
        "Upper limb rehabilitation",
        ["Shoulder", "Mobility", "Rotator cuff"],
    )
    raw_public_name = f"Dr. Search Raw Fallback {suffix}"
    raw_public_doctor_id = _register_doctor_without_public_display_name(client, raw_public_name)
    ui_registered_name = f"Dr. UI Registered {suffix}"
    ui_registered_doctor_id = _register_ui_doctor(client, ui_registered_name)
    episode_id = _create_knee_episode(client, patient_token)
    with SessionLocal() as db:
        episode = db.get(CareEpisode, UUID(episode_id))
        assert episode is not None
        episode.latest_triage_summary = {
            "concern": "Knee stiffness",
            "relevant_context": "PRIVATE_TRIAGE_CONTEXT " * 120,
            "safety_signals": ["No emergency symptoms", "Knee swelling"],
            "recommendation": "Work with a knee sports physical therapist.",
            "unresolved_questions": ["PRIVATE_UNRESOLVED_QUESTION " * 40],
            "missing_information": ["PRIVATE_MISSING_INFORMATION " * 40],
        }
        db.commit()
    subscribe = client.post("/professional-care/subscribe", headers=_auth(patient_token))
    assert subscribe.status_code == 200, subscribe.text

    class ReadOnlyVectorClient:
        def get_collection(self, *, collection_name: str) -> object:
            return SimpleNamespace(collection_name=collection_name)

        def query_points(
            self,
            *,
            collection_name: str,
            query: list[float],
            limit: int,
            with_payload: bool,
            query_filter: object | None = None,
        ) -> object:
            ReadOnlyVectorStore.query_filters.append(query_filter)
            return SimpleNamespace(
                points=[
                    SimpleNamespace(score=10000.0, payload={"doctor_id": knee_doctor_id}),
                    SimpleNamespace(score=9999.0, payload={"doctor_id": raw_public_doctor_id}),
                ]
            )

        def upsert(self, **kwargs: object) -> None:
            ReadOnlyVectorStore.write_calls.append("upsert")

    class ReadOnlyVectorStore:
        embedded_texts: list[str] = []
        query_filters: list[object | None] = []
        write_calls: list[str] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.client = ReadOnlyVectorClient()

        def ensure_collection(self, recreate: bool = False) -> None:
            self.write_calls.append("ensure_collection")

        def upsert_documents(self, *args: object, **kwargs: object) -> None:
            self.write_calls.append("upsert_documents")

        def embed_text(self, text: str) -> list[float]:
            self.embedded_texts.append(text)
            return [1.0, 0.0, 0.0]

        def search(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            return [{"score": 2.5, "payload": {"doctor_id": knee_doctor_id}}]

        def close(self) -> None:
            return None

    original_store = semantic_doctor_search.QdrantKnowledgeStore
    semantic_doctor_search.QdrantKnowledgeStore = ReadOnlyVectorStore
    original_seed_demo_doctors = professional_care_workflow._seed_demo_doctors
    seed_calls: list[str] = []

    def fail_if_semantic_search_seeds_demo_doctors(*args: object, **kwargs: object) -> list[object]:
        seed_calls.append("seed")
        raise AssertionError("semantic doctor search GET must not seed demo doctors")

    professional_care_workflow._seed_demo_doctors = fail_if_semantic_search_seeds_demo_doctors
    try:
        search = client.get(f"/care-episodes/{episode_id}/doctor-search?query=basketball stiffness", headers=_auth(patient_token))
        assert search.status_code == 200, search.text
        results = search.json()["results"]
        assert results
        assert results[0]["doctor"]["doctor_id"] == knee_doctor_id
        assert results[0]["doctor"]["display_name"] == "Dr. Knee Sports"
        assert "Knee" in results[0]["doctor"]["expertise_tags"]
        assert "doctor_name" not in results[0]["doctor"]
        assert "real_info" not in results[0]["doctor"]
        raw_public_result = next(result for result in results if result["doctor"]["doctor_id"] == raw_public_doctor_id)
        assert raw_public_result["doctor"]["display_name"] == "Professional Care clinician"
        raw_public_json = str(raw_public_result["doctor"])
        assert raw_public_name not in raw_public_json
        assert "doctor_name" not in raw_public_result["doctor"]
        assert "real_info" not in raw_public_result["doctor"]
        ui_registered_result = next(result for result in results if result["doctor"]["doctor_id"] == ui_registered_doctor_id)
        assert ui_registered_result["doctor"]["display_name"] == ui_registered_name
        name_search = client.get(
            f"/care-episodes/{episode_id}/doctor-search?query={ui_registered_name.replace(' ', '%20')}",
            headers=_auth(patient_token),
        )
        assert name_search.status_code == 200, name_search.text
        name_search_result = next(
            result for result in name_search.json()["results"] if result["doctor"]["doctor_id"] == ui_registered_doctor_id
        )
        assert name_search_result["doctor"]["display_name"] == ui_registered_name
        assert name_search_result["score"] > 0
        assert results[0]["match_reason"]
        assert isinstance(results[0]["score"], float)

        empty_query = client.get(f"/care-episodes/{episode_id}/doctor-search", headers=_auth(patient_token))
        assert empty_query.status_code == 200, empty_query.text
        empty_results = empty_query.json()["results"]
        assert empty_results
        assert empty_results[0]["doctor"]["doctor_id"] == knee_doctor_id
        assert empty_results[0]["match_reason"]
        assert isinstance(empty_results[0]["score"], float)
        assert seed_calls == []
        assert ReadOnlyVectorStore.query_filters, "doctor search should constrain vector retrieval to visible doctors"
        for query_filter in ReadOnlyVectorStore.query_filters:
            assert query_filter is not None
            filter_text = repr(query_filter)
            assert knee_doctor_id in filter_text
            assert raw_public_doctor_id in filter_text
        empty_query_text = ReadOnlyVectorStore.embedded_texts[-1]
        assert len(empty_query_text) <= 1200
        assert "PRIVATE_EMPTY_QUERY_NARRATIVE" not in empty_query_text
        assert "PRIVATE_TRIAGE_CONTEXT" not in empty_query_text
        assert "PRIVATE_UNRESOLVED_QUESTION" not in empty_query_text
        assert "PRIVATE_MISSING_INFORMATION" not in empty_query_text
        assert "Knee stiffness" in empty_query_text
        assert "Knee" in empty_query_text
        assert "Return to basketball" in empty_query_text
        assert "No emergency symptoms" in empty_query_text
        assert "Work with a knee sports physical therapist" in empty_query_text

        professional_care_workflow._seed_demo_doctors = original_seed_demo_doctors
        with SessionLocal() as db:
            directory = professional_care_workflow.list_available_doctors(db, UUID(patient_id), UUID(episode_id))
            authorized_doctor = next(entry for entry in directory.doctors if str(entry.doctor_id) == knee_doctor_id)
            original_directory = professional_care_workflow.list_available_doctors

            def limited_available_doctors(*args: object, **kwargs: object) -> DoctorDirectoryResponse:
                return DoctorDirectoryResponse(doctors=[authorized_doctor])

            professional_care_workflow.list_available_doctors = limited_available_doctors
            try:
                limited_results = semantic_doctor_search.search_doctors_for_episode(
                    db, UUID(patient_id), UUID(episode_id), "shoulder mobility", limit=6
                )
            finally:
                professional_care_workflow.list_available_doctors = original_directory
            assert [str(result.doctor.doctor_id) for result in limited_results] == [knee_doctor_id]
    finally:
        semantic_doctor_search.QdrantKnowledgeStore = original_store
        professional_care_workflow._seed_demo_doctors = original_seed_demo_doctors

    assert ReadOnlyVectorStore.write_calls == [], f"doctor search request wrote to vector store: {ReadOnlyVectorStore.write_calls}"
    print("doctor semantic search contract ok")


if __name__ == "__main__":
    main()
