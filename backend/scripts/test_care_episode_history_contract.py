from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("REHAB_TRIAGE_SUMMARY_AGENT", "fallback")

from fastapi.testclient import TestClient

from app.db.models import AIMessage, AISession
from app.db.session import SessionLocal, init_db
from app.main import app


def _register_patient(client: TestClient, name: str) -> tuple[str, str]:
    response = client.post(
        "/auth/register/patient",
        json={"patient_name": name, "password": "history-pass-123", "real_info": {}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["access_token"], body["user"]["user_id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_episode(client: TestClient, token: str) -> str:
    response = client.post(
        "/care-episodes",
        json={
            "issue_title": "Low back strain",
            "body_area": "Low back",
            "goal": "Return to desk work comfortably",
            "short_description": "Back feels strained after lifting boxes and sitting gets sore.",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["care_episode_id"]


def _create_chat_session(patient_id: str) -> str:
    with SessionLocal() as db:
        session = AISession(patient_id=patient_id, title="Episode history intake")
        db.add(session)
        db.flush()
        db.add_all(
            [
                AIMessage(
                    session_id=session.session_id,
                    sender_role="user",
                    content="Back pain is about 3/10 and easing with rest.",
                ),
                AIMessage(
                    session_id=session.session_id,
                    sender_role="assistant",
                    content="Any numbness, leg weakness, or trouble walking?",
                ),
                AIMessage(
                    session_id=session.session_id,
                    sender_role="user",
                    content="No numbness, no weakness, and I can walk normally.",
                ),
            ]
        )
        db.commit()
        return str(session.session_id)


def main() -> None:
    init_db()
    client = TestClient(app)
    suffix = str(time.time_ns())
    patient_token, patient_id = _register_patient(client, f"history_patient_{suffix}")
    other_patient_token, _ = _register_patient(client, f"history_other_{suffix}")
    episode_id = _create_episode(client, patient_token)
    ai_session_id = _create_chat_session(patient_id)

    draft_response = client.post(
        f"/care-episodes/{episode_id}/triage-summary-drafts",
        json={"ai_session_id": ai_session_id, "additional_context": "Create a history-ready draft."},
        headers=_auth(patient_token),
    )
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()

    select_response = client.post(
        f"/care-episodes/{episode_id}/triage-summary-drafts/{draft['triage_summary_draft_id']}/select",
        headers=_auth(patient_token),
    )
    assert select_response.status_code == 201, select_response.text

    response = client.get(f"/care-episodes/{episode_id}/history", headers=_auth(patient_token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["episode"]["care_episode_id"] == episode_id
    assert body["triage_summaries"]
    assert body["triage_summaries"][0]["source_conversation_transcript"]
    assert "Patient:" in body["triage_summaries"][0]["source_conversation_transcript"]
    assert body["triage_summary_drafts"] == []
    assert body["patient_memory"] is not None
    assert "compiled_text" in body["patient_memory"]
    assert body["episode_memory"] is not None
    assert "compiled_memory_context" in body
    assert isinstance(body["rehab_sessions"], list)
    assert isinstance(body["ai_care_summaries"], list)

    forbidden = client.get(f"/care-episodes/{episode_id}/history", headers=_auth(other_patient_token))
    assert forbidden.status_code == 404, forbidden.text

    print("care episode history contract ok")


if __name__ == "__main__":
    main()
