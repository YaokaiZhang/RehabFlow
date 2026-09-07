from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("REHAB_TRIAGE_SUMMARY_AGENT", "fallback")

from fastapi.testclient import TestClient

from app.db.models import AIInternalMessage, AIMessage, AISession, CareEpisode
from app.db.session import SessionLocal, init_db
from app.main import app


def _register_patient(client: TestClient, name: str) -> tuple[str, str]:
    response = client.post(
        "/auth/register/patient",
        json={"patient_name": name, "password": "summary-chat-pass-123", "real_info": {}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["access_token"], body["user"]["user_id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_episode(client: TestClient, token: str) -> dict:
    response = client.post(
        "/care-episodes",
        json={
            "issue_title": "Right ankle sprain",
            "body_area": "Right ankle",
            "goal": "Return to walking without limping",
            "short_description": "Rolled the ankle last week and want to know if gentle rehab is safe.",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_knee_stiffness_episode(client: TestClient, token: str) -> dict:
    response = client.post(
        "/care-episodes",
        json={
            "issue_title": "Knee stiffness",
            "body_area": "Knee",
            "goal": "Relieve knee stiffness",
            "short_description": "Knee gets stiff in cold weather after an old basketball fracture. No pain, no swelling, and no surgery history.",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _no_forbidden_signal(summary: dict, forbidden: str) -> bool:
    return forbidden not in " ".join(summary["safety_signals"]).lower()


def _create_chat_session(patient_id: str) -> str:
    with SessionLocal() as db:
        session = AISession(patient_id=patient_id, title="Episode triage intake")
        db.add(session)
        db.flush()
        rows = [
            AIMessage(
                session_id=session.session_id,
                sender_role="user",
                content="FIRST SOURCE TURN: pain is mild, about 2/10. I can bear weight and walk slowly.",
            ),
            AIMessage(
                session_id=session.session_id,
                sender_role="assistant",
                content="Thanks. Any bruising, swelling, numbness, or tingling?",
            ),
        ]
        for turn in range(1, 11):
            rows.append(
                AIMessage(
                    session_id=session.session_id,
                    sender_role="user",
                    content=f"Turn {turn}: I still have no bruising, no numbness, and swelling keeps improving.",
                )
            )
            rows.append(
                AIMessage(
                    session_id=session.session_id,
                    sender_role="assistant",
                    content=f"Turn {turn}: Understood. Any pain changes, walking changes, or instability?",
                )
            )
        db.add_all(rows)
        db.commit()
        return str(session.session_id)


def _create_reassuring_knee_chat_session(patient_id: str) -> str:
    with SessionLocal() as db:
        session = AISession(patient_id=patient_id, title="Reassuring knee stiffness intake")
        db.add(session)
        db.flush()
        db.add_all(
            [
                AIMessage(
                    session_id=session.session_id,
                    sender_role="user",
                    content="My knee feels stiff when it gets cold. What can I do?",
                ),
                AIMessage(
                    session_id=session.session_id,
                    sender_role="assistant",
                    content="How long has this been present, and is there pain, swelling, injury, or surgery history?",
                ),
                AIMessage(
                    session_id=session.session_id,
                    sender_role="user",
                    content="Since I broke my knees playing basketball six years ago. I did not do surgery and recovered with plasters. No pain and no swelling.",
                ),
                AIMessage(
                    session_id=session.session_id,
                    sender_role="user",
                    content="Mostly only in cold weather. No changes in walking, stairs, or range of motion.",
                ),
            ]
        )
        db.commit()
        return str(session.session_id)


def _create_knee_chat_with_assistant_ankle_text(patient_id: str) -> str:
    with SessionLocal() as db:
        session = AISession(patient_id=patient_id, title="Knee triage with generic assistant suggestions")
        db.add(session)
        db.flush()
        db.add_all(
            [
                AIMessage(
                    session_id=session.session_id,
                    sender_role="user",
                    content="My left knee feels stiff when it gets cold. No pain and no swelling.",
                ),
                AIMessage(
                    session_id=session.session_id,
                    sender_role="assistant",
                    content="Gentle warmups sometimes include ankle circles, but tell me about the knee duration and injury history.",
                ),
                AIMessage(
                    session_id=session.session_id,
                    sender_role="user",
                    content="I broke my left knee 6 years ago and recovered. It improves when it warms up.",
                ),
            ]
        )
        db.commit()
        return str(session.session_id)


def main() -> None:
    init_db()
    client = TestClient(app)
    suffix = str(time.time_ns())
    patient_token, patient_id = _register_patient(client, f"summary_chat_patient_{suffix}")
    other_patient_token, other_patient_id = _register_patient(client, f"summary_chat_other_{suffix}")
    derived_ai_session_id = _create_knee_chat_with_assistant_ankle_text(patient_id)
    create_from_chat_response = client.post(
        "/care-episodes/triage-summary-request",
        json={"ai_session_id": derived_ai_session_id, "additional_context": "Patient requested a Triage Summary from the AI intake chat."},
        headers=_auth(patient_token),
    )
    assert create_from_chat_response.status_code == 201, create_from_chat_response.text
    created_from_chat = create_from_chat_response.json()
    assert created_from_chat["episode"]["body_area"] == "Left Knee"
    assert created_from_chat["episode"]["issue_title"] == "Left Knee stiffness"
    assert created_from_chat["draft"]["concern"] == "Left Knee stiffness"
    assert "knee" in created_from_chat["draft"]["relevant_context"].lower()
    assert "ankle" not in created_from_chat["draft"]["concern"].lower()
    episode = _create_episode(client, patient_token)
    episode_id = episode["care_episode_id"]
    ai_session_id = _create_chat_session(patient_id)
    other_ai_session_id = _create_chat_session(other_patient_id)

    draft_response = client.post(
        f"/care-episodes/{episode_id}/triage-summary-drafts",
        json={"ai_session_id": ai_session_id, "additional_context": "I am done with triage and want a draft summary."},
        headers=_auth(patient_token),
    )
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()
    assert draft["saved"] is False
    assert draft["care_episode_id"] == episode_id
    assert "Patient: FIRST SOURCE TURN" in draft["relevant_context"]
    assert "weight-bearing tolerance" not in draft["missing_information"]
    assert "bruising" not in draft["missing_information"]
    assert "numbness or tingling" not in draft["missing_information"]
    assert "pain intensity and behavior" not in draft["missing_information"]
    assert draft["source_ai_session_id"] == ai_session_id
    assert "FIRST SOURCE TURN" in draft["source_conversation_transcript"]
    assert "Patient:" in draft["source_conversation_transcript"]
    assert "RehabFlow:" in draft["source_conversation_transcript"]
    assert draft["source_conversation_transcript"].count("Patient:") >= 10

    detail_response = client.get(f"/care-episodes/{episode_id}", headers=_auth(patient_token))
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["latest_triage_summary"] is None
    assert detail["safety_gate_status"] == "needs_triage"

    wrong_session_response = client.post(
        f"/care-episodes/{episode_id}/triage-summary-drafts",
        json={"ai_session_id": other_ai_session_id, "additional_context": "Use someone else's session."},
        headers=_auth(patient_token),
    )
    assert wrong_session_response.status_code == 404, wrong_session_response.text

    select_response = client.post(
        f"/care-episodes/{episode_id}/triage-summary-drafts/{draft['triage_summary_draft_id']}/select",
        headers=_auth(patient_token),
    )
    assert select_response.status_code == 201, select_response.text
    saved = select_response.json()
    assert saved["source_ai_session_id"] == ai_session_id
    assert saved["source_conversation_transcript"] == draft["source_conversation_transcript"]

    knee_episode = _create_knee_stiffness_episode(client, patient_token)
    knee_ai_session_id = _create_reassuring_knee_chat_session(patient_id)
    knee_draft_response = client.post(
        f"/care-episodes/{knee_episode['care_episode_id']}/triage-summary-drafts",
        json={"ai_session_id": knee_ai_session_id, "additional_context": "Patient requested a Triage Summary from the AI intake chat."},
        headers=_auth(patient_token),
    )
    assert knee_draft_response.status_code == 201, knee_draft_response.text
    knee_draft = knee_draft_response.json()
    assert knee_draft["saved"] is False
    assert "pain intensity and behavior" not in knee_draft["missing_information"]
    assert "bruising" not in knee_draft["missing_information"]
    assert _no_forbidden_signal(knee_draft, "swelling")
    assert _no_forbidden_signal(knee_draft, "post-operative")
    assert knee_draft["clinician_review_needed"] is False
    assert "complete the missing triage details" not in knee_draft["recommendation"].lower()

    with SessionLocal() as db:
        knee_internal_logs = db.query(AIInternalMessage).filter(AIInternalMessage.session_id == knee_ai_session_id).all()
        assert any(log.agent_name == "triage_summary_agent" for log in knee_internal_logs)
        assert any(log.event_type == "summary_fallback_output" for log in knee_internal_logs)
        assert all(
            "swelling is present" not in " ".join(log.event_metadata.get("safety_signals", [])).lower()
            for log in knee_internal_logs
        )
        stored_episode = db.get(CareEpisode, episode_id)
        assert stored_episode is not None
        assert stored_episode.latest_triage_summary["source_ai_session_id"] == ai_session_id
        assert "FIRST SOURCE TURN" in stored_episode.latest_triage_summary["source_conversation_transcript"]
        internal_logs = db.query(AIInternalMessage).filter(AIInternalMessage.session_id == ai_session_id).all()
        assert any(log.agent_name == "triage_summary_agent" for log in internal_logs)
        assert any(log.event_type == "summary_fallback_output" for log in internal_logs)
        assert not any(log.content == "I am done with triage and want a draft summary." for log in internal_logs)

    print("triage summary from chat contract ok")


if __name__ == "__main__":
    main()
