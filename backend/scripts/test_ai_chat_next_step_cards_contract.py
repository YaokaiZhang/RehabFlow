#!/usr/bin/env python3
"""Offline contract for AI chat ``next_step`` / ``exercise_cards`` and the
clinician-review safety gate."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QWEN_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest

from app.api import ws as ws_api
from app.core.security import Principal
from app.db.models import AIChatTurnReceipt, AIMessage, AISession, Patient
from app.services import ai_chat_presentation as presentation
from app.services.ai_turn_persistence import AIChatTurnPersistence

BRIDGE = {
    "exercise_id": "ex-bridge",
    "title": "Glute Bridge",
    "introduction": (
        "Lie on your back with knees bent and feet flat. Squeeze your glutes and lift your hips "
        "until your body forms a straight line from shoulders to knees, then lower slowly with control "
        "and repeat for the prescribed number of repetitions."
    ),
    "structures_involved": ["Gluteus Maximus", "Hamstrings", "Lumbar Spine", "Core"],
    "related_conditions": ["Low Back Pain"],
    "video_url": "https://videos.example.com/bridge.mp4",
    "source_url": "https://www.rehabhero.ca/exercise/glute-bridge",
    "score": 0.9,
}
CLAMSHELL = {
    "exercise_id": "ex-clam",
    "title": "Side-Lying Clamshell",
    "introduction": "Short intro.",
    "structures_involved": ["Gluteus Medius"],
    "related_conditions": [],
    "video_url": "",
    "source_url": "https://www.rehabhero.ca/exercise/clamshell",
    "score": 0.8,
}
PLANK = {**CLAMSHELL, "exercise_id": "ex-plank", "title": "Front Plank", "source_url": "https://www.rehabhero.ca/exercise/plank"}
DEADBUG = {**CLAMSHELL, "exercise_id": "ex-deadbug", "title": "Dead Bug", "source_url": "https://www.rehabhero.ca/exercise/dead-bug"}
UNMENTIONED = {**CLAMSHELL, "exercise_id": "ex-unused", "title": "Wall Angel"}

READY_TEXT = (
    "Start with glute bridges, then add the Side‑Lying Clamshell. A front plank and a dead bug "
    "can follow once these feel easy. Glute Bridge sets should stay pain-free."
)


def ready_result(**overrides):
    result = {
        "status": "completed",
        "routing_decision": "continuation",
        "needs_more_info": False,
        "if_emergency": False,
        "review_release": True,
        "consult_response": READY_TEXT,
        "final_response": READY_TEXT,
        "catalog_exercise_suggestions": [UNMENTIONED, BRIDGE, CLAMSHELL, dict(BRIDGE), PLANK, DEADBUG],
    }
    result.update(overrides)
    return result


# --- next_step gating -------------------------------------------------------


def test_ready_turn_emits_ready_and_cards_matching_prose():
    payload = ws_api._build_ai_chat_response("s-1", ready_result(), include_debug=False)
    assert payload["next_step"] == "ready"
    cards = payload["exercise_cards"]
    # Max 3, deduped by id, ordered by first mention, unmentioned titles dropped.
    assert [card["exercise_id"] for card in cards] == ["ex-bridge", "ex-clam", "ex-plank"]
    bridge = cards[0]
    assert set(bridge) == {"exercise_id", "title", "summary", "targets", "video_url", "source_url"}
    assert bridge["title"] == "Glute Bridge"
    assert bridge["targets"] == ["Gluteus Maximus", "Hamstrings", "Lumbar Spine"]
    assert bridge["video_url"] == "https://videos.example.com/bridge.mp4"
    assert bridge["source_url"] == "https://www.rehabhero.ca/exercise/glute-bridge"
    assert len(bridge["summary"]) <= 160
    assert bridge["summary"].endswith("…")
    assert BRIDGE["introduction"].startswith(bridge["summary"][:-1])
    # Summary ends on a word boundary.
    assert BRIDGE["introduction"][len(bridge["summary"]) - 1] in {" ", ","}
    assert cards[1]["video_url"] is None
    assert cards[1]["summary"] == "Short intro."


def test_urgent_turn_emits_urgent_and_never_cards():
    for overrides in (
        {"routing_decision": "urgent_end"},
        {"routing_decision": "continuation", "if_emergency": True},
    ):
        result = ready_result(
            final_response="This needs urgent medical attention. Please get medical care now.",
            **overrides,
        )
        payload = ws_api._build_ai_chat_response("s-1", result, include_debug=False)
        assert payload["next_step"] == "urgent"
        assert payload["exercise_cards"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"routing_decision": "ask_clarification", "final_response": "Where exactly is the pain?"},
        {"needs_more_info": True},
        {"status": "clarification_required"},
        {"status": "failed"},
        {"review_release": False},
        {"review_release": None},
        {"safety_passed": False},
        # unsafe / unsupported fallback replaces the reviewed draft
        {"final_response": "This request is outside the rehabilitation support I can provide."},
        {"routing_decision": None},
        {"final_response": ""},
    ],
)
def test_non_ready_turns_emit_null_next_step_and_no_cards(overrides):
    payload = ws_api._build_ai_chat_response("s-1", ready_result(**overrides), include_debug=False)
    assert payload["next_step"] is None
    assert payload["exercise_cards"] == []


def test_old_or_replayed_results_default_to_empty():
    payload = ws_api._build_ai_chat_response(
        "s-1", {"final_response": "Hello", "status": "completed"}, include_debug=False
    )
    assert payload["next_step"] is None
    assert payload["exercise_cards"] == []


def test_precomputed_fields_are_revalidated():
    cards = presentation.chat_turn_presentation(ready_result())["exercise_cards"]
    replay = {"final_response": READY_TEXT, "next_step": "ready", "exercise_cards": cards}
    payload = ws_api._build_ai_chat_response("s-1", replay, include_debug=False)
    assert payload["next_step"] == "ready"
    assert payload["exercise_cards"] == cards

    tampered = {"final_response": "x", "next_step": "bogus", "exercise_cards": cards}
    payload = ws_api._build_ai_chat_response("s-1", tampered, include_debug=False)
    assert payload["next_step"] is None
    assert payload["exercise_cards"] == []


def test_trim_summary_word_boundary():
    assert presentation.trim_summary("short") == "short"
    text = "word " * 60
    trimmed = presentation.trim_summary(text)
    assert len(trimmed) <= 160
    assert trimmed.endswith("word…")


# --- persistence / history round trip --------------------------------------


class _RecordingDb:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        return None


class _Query:
    def __init__(self, items):
        self.items = list(items)

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.items[0] if self.items else None

    def all(self):
        return list(self.items)


class _HistoryDb:
    def __init__(self, patient, session, messages, receipts):
        self.patient, self.session, self.messages, self.receipts = patient, session, messages, receipts

    def query(self, model):
        return _Query(
            {
                Patient: [self.patient],
                AISession: [self.session],
                AIMessage: self.messages,
                AIChatTurnReceipt: self.receipts,
            }.get(model, [])
        )


def test_persistence_and_history_round_trip():
    patient_id = uuid.uuid4()
    session_id = uuid.uuid4()
    db = _RecordingDb()
    receipt = AIChatTurnReceipt(
        session_id=session_id,
        patient_id=patient_id,
        idempotency_key="cards-turn-0001",
        request_hash="hash",
        status="processing",
        workflow_version="test",
    )
    output = ready_result(internal_log_events=[])
    output.update(presentation.chat_turn_presentation(output))
    AIChatTurnPersistence(db).persist_turn(
        session_id=session_id,
        patient_id=patient_id,
        user_input="Which exercises help my hip?",
        output=output,
        internal_trace={"internal_log_events": []},
        receipt=receipt,
        status="completed",
    )
    messages = [item for item in db.added if isinstance(item, AIMessage)]
    assert [m.sender_role for m in messages] == ["user", "assistant"]
    user_message, assistant_message = messages
    assert "next_step" not in user_message.message_metadata
    assert assistant_message.message_metadata["next_step"] == "ready"
    assert [c["exercise_id"] for c in assistant_message.message_metadata["exercise_cards"]] == [
        "ex-bridge", "ex-clam", "ex-plank",
    ]
    # Idempotent replay carries the same fields.
    assert receipt.response_payload["next_step"] == "ready"
    assert receipt.response_payload["exercise_cards"] == assistant_message.message_metadata["exercise_cards"]
    replayed = ws_api._build_ai_chat_response(str(session_id), receipt.response_payload, include_debug=False)
    assert replayed["exercise_cards"] == assistant_message.message_metadata["exercise_cards"]

    base = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    old_assistant = AIMessage(session_id=session_id, sender_role="assistant", content="Older reply")
    old_assistant.message_metadata = {"sequence": 0}
    for index, message in enumerate([old_assistant, user_message, assistant_message]):
        message.message_id = uuid.uuid4()
        message.created_at = base
    patient = Patient(patient_id=patient_id, patient_name="cards-patient", password_hash="hash")
    session = AISession(session_id=session_id, patient_id=patient_id, title="Hip chat")
    session.created_at = session.updated_at = base
    history_db = _HistoryDb(patient, session, [old_assistant, user_message, assistant_message], [])
    principal = Principal(subject="cards-patient", role="patient", user_id=patient_id)
    payload = ws_api.get_ai_chat_session(str(session_id), principal=principal, db=history_db)
    old_row, user_row, assistant_row = payload["messages"]
    assert old_row["next_step"] is None and old_row["exercise_cards"] == []
    assert "next_step" not in user_row and "exercise_cards" not in user_row
    assert assistant_row["next_step"] == "ready"
    assert assistant_row["exercise_cards"] == assistant_message.message_metadata["exercise_cards"]


def test_persisted_urgent_turn_has_no_cards():
    db = _RecordingDb()
    output = ready_result(routing_decision="urgent_end", final_response="Get medical care now.")
    AIChatTurnPersistence(db).persist_turn(
        session_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        user_input="Chest pain and numb arm",
        output=output,
        internal_trace={"internal_log_events": []},
        receipt=None,
        status="completed",
    )
    assistant = [m for m in db.added if isinstance(m, AIMessage) and m.sender_role == "assistant"][0]
    assert assistant.message_metadata["next_step"] == "urgent"
    assert assistant.message_metadata["exercise_cards"] == []
