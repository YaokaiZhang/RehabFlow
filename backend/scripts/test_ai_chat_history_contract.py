#!/usr/bin/env python3
"""Offline checks for patient-scoped AI chat history and deletion."""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

if __name__ == "__main__":
	os.environ.setdefault("QWEN_API_KEY", "test-key")
	os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
	sys.path.insert(0, str(BACKEND_DIR))

from app.api import ws as ws_api
from app.core.security import Principal
from app.db.models import AIChatTurnReceipt, AIMessage, AISession, Patient
from app.services.ai_turn_persistence import safe_chat_sources


class FakeQuery:
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


class FakeDb:
	def __init__(self, patient, sessions, messages, receipts=None):
		self.patient = patient
		self.sessions = list(sessions)
		self.messages = list(messages)
		self.receipts = list(receipts or [])
		self.deleted = []
		self.commits = 0

	def query(self, model):
		if model is Patient:
			return FakeQuery([self.patient])
		if model is AISession:
			return FakeQuery(self.sessions)
		if model is AIMessage:
			return FakeQuery(self.messages)
		if model is AIChatTurnReceipt:
			return FakeQuery(self.receipts)
		return FakeQuery([])

	def rollback(self):
		return None

	def delete(self, value):
		self.deleted.append(value)

	def commit(self):
		self.commits += 1


class HistoryContractTests(unittest.TestCase):
	def setUp(self):
		self.patient_id = uuid.uuid4()
		self.episode_id = uuid.uuid4()
		self.unassigned_id = uuid.uuid4()
		self.assigned_id = uuid.uuid4()
		base = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
		self.patient = Patient(patient_id=self.patient_id, patient_name="history-patient", password_hash="hash")
		unassigned = AISession(session_id=self.unassigned_id, patient_id=self.patient_id, title="New Rehab Session")
		unassigned.created_at = base
		unassigned.updated_at = base
		assigned = AISession(session_id=self.assigned_id, patient_id=self.patient_id, title="Back chat", care_episode_id=self.episode_id)
		assigned.created_at = base
		assigned.updated_at = base
		self.unassigned = unassigned
		self.assigned = assigned
		self.user_message = AIMessage(session_id=self.unassigned_id, sender_role="user", content="My back feels stiff")
		self.user_message.message_id = uuid.uuid4()
		self.user_message.created_at = base
		self.assistant_message = AIMessage(session_id=self.unassigned_id, sender_role="assistant", content="Let's clarify the change.")
		self.assistant_message.message_id = uuid.uuid4()
		self.assistant_message.created_at = base
		self.assistant_message.message_metadata = {
			"sequence": 2,
			"sources": [
				{"label": "NHS", "title": "Back pain", "url": "https://www.nhs.uk/conditions/back-pain/"},
				{"label": "Insecure", "title": "Drop this", "url": "http://example.com/insecure"},
			],
		}
		self.receipt = AIChatTurnReceipt(
			session_id=self.unassigned_id,
			patient_id=self.patient_id,
			idempotency_key="history-turn-0001",
			request_hash="history-request-hash",
			status="completed",
			workflow_version="history-contract",
			response_payload={
				"final_response": "Let's clarify the change.",
				"web_sources": [
					{"label": "NHS", "title": "Back pain", "url": "https://www.nhs.uk/conditions/back-pain/"},
				],
			},
		)
		self.receipt.created_at = base
		self.db = FakeDb(self.patient, [unassigned, assigned], [self.user_message, self.assistant_message], [self.receipt])
		self.principal = Principal(subject="history-patient", role="patient", user_id=self.patient_id)

	def test_list_exposes_unassigned_and_episode_ownership(self):
		payload = ws_api.list_ai_chat_sessions(principal=self.principal, db=self.db)
		self.assertEqual(len(payload["sessions"]), 2)
		by_id = {item["session_id"]: item for item in payload["sessions"]}
		self.assertIsNone(by_id[str(self.unassigned_id)]["care_episode_id"])
		self.assertEqual(by_id[str(self.unassigned_id)]["preview"], "My back feels stiff")
		self.assertEqual(by_id[str(self.assigned_id)]["care_episode_id"], str(self.episode_id))

	def test_get_returns_persisted_transcript(self):
		payload = ws_api.get_ai_chat_session(str(self.unassigned_id), principal=self.principal, db=self.db)
		self.assertEqual([message["sender_role"] for message in payload["messages"]], ["user", "assistant"])
		self.assertEqual(payload["messages"][0]["content"], "My back feels stiff")
		self.assertNotIn("sources", payload["messages"][0])
		self.assertEqual(
			payload["messages"][1]["sources"],
			[{"label": "NHS", "title": "Back pain", "url": "https://www.nhs.uk/conditions/back-pain/"}],
		)

	def test_source_sanitizer_keeps_only_unique_https_sources(self):
		self.assertEqual(
			safe_chat_sources([
				{"label": "NHS", "title": "Back pain", "url": "https://www.nhs.uk/conditions/back-pain/"},
				{"label": "Insecure", "title": "Drop this", "url": "http://example.com/insecure"},
				{"label": "Duplicate", "title": "Drop this duplicate", "url": "https://www.nhs.uk/conditions/back-pain/"},
			]),
			[{"label": "NHS", "title": "Back pain", "url": "https://www.nhs.uk/conditions/back-pain/"}],
		)

	def test_get_recovers_sources_from_replayable_receipt_for_older_message(self):
		self.assistant_message.message_metadata = {"sequence": 2}
		payload = ws_api.get_ai_chat_session(str(self.unassigned_id), principal=self.principal, db=self.db)
		self.assertEqual(
			payload["messages"][1]["sources"],
			[{"label": "NHS", "title": "Back pain", "url": "https://www.nhs.uk/conditions/back-pain/"}],
		)

	def test_delete_removes_session_and_agent_checkpoint(self):
		class FakeRuntime:
			def __init__(self):
				self.deleted = []

			def delete_thread(self, session_id):
				self.deleted.append(session_id)

		runtime = FakeRuntime()
		request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_runtime=runtime)))
		response = ws_api.delete_ai_chat_session(str(self.unassigned_id), http_request=request, principal=self.principal, db=self.db)
		self.assertEqual(response.status_code, 204)
		self.assertEqual(runtime.deleted, [str(self.unassigned_id)])
		self.assertEqual(self.db.deleted, [self.unassigned])
		self.assertEqual(self.db.commits, 1)


if __name__ == "__main__":
	unittest.main(verbosity=2)
