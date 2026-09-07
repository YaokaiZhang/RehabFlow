#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import MemoryMaintenanceRun
from app.db.session import SessionLocal
from app.main import app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, suffix: str) -> tuple[str, str]:
    response = client.post(
        "/auth/register/patient",
        json={
            "patient_name": f"structured_memory_patient_{suffix}",
            "password": "memory-pass-123",
            "real_info": {},
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return data["access_token"], data["user"]["user_id"]


def _create_episode(client: TestClient, token: str) -> str:
    response = client.post(
        "/care-episodes",
        json={
            "issue_title": "Shoulder stiffness",
            "body_area": "Shoulder",
            "goal": "Sleep comfortably",
            "short_description": "Shoulder stiffness after home exercise.",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["care_episode_id"]


def main() -> None:
    client = TestClient(app)
    suffix = str(time.time_ns())
    patient_token, patient_id = _register_patient(client, suffix)
    episode_id = _create_episode(client, patient_token)

    empty = client.get("/memory/patient-document", headers=_auth(patient_token))
    assert empty.status_code == 200, empty.text
    empty_document = empty.json()
    assert empty_document["editable_fields"] == {}
    assert empty_document["summary_text"] == ""
    assert empty_document["level1_keywords"] == []
    assert empty_document["level2_summary"] == ""

    edit = client.patch(
        "/memory/patient-document/fields",
        json={"fields": {"preferred_activity": "morning mobility"}},
        headers={**_auth(patient_token), "Idempotency-Key": "structured-memory-edit-1"},
    )
    assert edit.status_code == 200, edit.text
    edited = edit.json()
    assert edited["editable_fields"] == {"preferred_activity": "morning mobility"}
    assert edited["summary_text"] == ""

    with SessionLocal() as db:
        runs = db.execute(
            select(MemoryMaintenanceRun)
            .where(
                MemoryMaintenanceRun.patient_id == patient_id,
                MemoryMaintenanceRun.trigger == "patient_edit",
            )
        ).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert run.care_episode_id is None
        assert run.episode_status == "skipped"
        assert run.snapshot["patient_change_set"]["changed_keys"] == ["preferred_activity"]
        assert run.snapshot["previous_patient_memory"]["editable_fields"] == {}

    repeat = client.patch(
        "/memory/patient-document/fields",
        json={"fields": {"preferred_activity": "morning mobility"}},
        headers={**_auth(patient_token), "Idempotency-Key": "structured-memory-edit-1"},
    )
    assert repeat.status_code == 200, repeat.text
    with SessionLocal() as db:
        count = db.execute(
            select(MemoryMaintenanceRun)
            .where(
                MemoryMaintenanceRun.patient_id == patient_id,
                MemoryMaintenanceRun.trigger == "patient_edit",
            )
        ).scalars().all()
        assert len(count) == 1

    context = client.get(
        f"/memory/care-episodes/{episode_id}/context-pack",
        headers=_auth(patient_token),
    )
    assert context.status_code == 200, context.text
    assert context.json()["patient_document"]["editable_fields"] == {
        "preferred_activity": "morning mobility"
    }

    print("structured memory contract ok")


if __name__ == "__main__":
    main()
