#!/usr/bin/env python3
"""Deterministic AI chat tests with fake LLM, fake vector, and fake socket.

This script is intentionally offline. It verifies backend module contracts without
calling Qwen, Qdrant, PostgreSQL, or Redis.

Usage:
    cd backend
    python scripts/test_ai_chat_contract.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
import uuid
from pathlib import Path

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

if __name__ == "__main__":
    os.environ.setdefault("QWEN_API_KEY", "test-key")
    os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
	sys.path.insert(0, str(BACKEND_DIR))

from fastapi import WebSocketDisconnect

from app.ai import consultant_invocation
from app.ai import graph_dependencies
from app.ai import graph_execution
from app.ai import graph_prompt_context
from app.ai import graph_routes
from app.ai import router
from app.ai import workflow_state
from app.db import models as db_models
from app.ai.workflow_policy import WorkflowPolicy
from app.api import ws as ws_api
from app.core.security import Principal
from app.db.models import AIInternalMessage
from app.db.session import get_db
from app.services.ai_chat_access import AuthorizedAIChat
from app.vector.qdrant_store import HashingEmbedder


class FakeStructuredLLM:
	def __init__(self, result):
		self.result = result
		self.messages = []

	def invoke(self, messages):
		self.messages.append(messages)
		return self.result


class FakeLLM:
	def __init__(self, result):
		self.structured_llm = FakeStructuredLLM(result)

	def with_structured_output(self, _schema):
		return self.structured_llm


class FakeAIChatTurnService:
	def __init__(self, result):
		self.result = result
		self.calls = []

	def invoke_turn(self, user_input, access, *, idempotency_key=None, workflow_version=None):
		self.calls.append((user_input, str(access.session_id), str(access.patient_id), access.care_episode_id))
		return dict(self.result)


class FakeContentLLM:
	def __init__(self, content="Revised safe plan"):
		self.content = content
		self.messages = []

	def invoke(self, messages):
		self.messages.append(messages)
		return type("FakeMessage", (), {"content": self.content})()


def authorized_agent_state(**overrides):
	state = {
		"workflow_version": workflow_state.WORKFLOW_VERSION,
		"principal_authorized": True,
		"session_authorized": True,
		"care_episode_authorized": True,
		"requested_route": "consultant",
		"authorized_source_ids": [],
		"source_ids": [],
		"authorized_tool_names": ["rehab_exercise_kb_search"],
		"allowed_catalog_ids": [],
		"budget": WorkflowPolicy().build_budget(),
		"model_calls": 0,
		"tool_calls": 0,
		"total_tokens": 0,
		"idempotency_key": None,
		"request_hash": "legacy-contract-request",
		"prior_request_hash": None,
		"repair_count": 0,
		"debug_trace": [],
	}
	state.update(overrides)
	return state


class FakeWebSocket:
	def __init__(self, incoming_messages):
		self.incoming_messages = list(incoming_messages)
		self.accepted = False
		self.sent_json = []

	async def accept(self):
		self.accepted = True

	async def receive_json(self):
		if not self.incoming_messages:
			raise WebSocketDisconnect(code=1000)
		return self.incoming_messages.pop(0)

	async def send_json(self, payload):
		self.sent_json.append(payload)


class RouterContractTests(unittest.TestCase):
	def test_hash_embedder_is_deterministic_and_normalized(self):
		embedder = HashingEmbedder(vector_size=64)
		first = embedder.embed_text("knee pain squat rehab")
		second = embedder.embed_text("knee pain squat rehab")
		self.assertEqual(first, second)
		self.assertEqual(len(first), 64)
		self.assertAlmostEqual(sum(value * value for value in first), 1.0, places=6)

	def test_router_turns_emergency_into_terminal_warning(self):
		result = router.RouterOutput(
			routing_decision="urgent_end",
			if_emergency=True,
			emergency_reason="Possible stroke signs reported",
			needs_more_info=False,
			follow_up_question="",
			triage_notes="Red flag symptoms require urgent care.",
		)
		with patch.object(graph_dependencies, "QWEN_70B", FakeLLM(result)):
			updates = router.router_agent(
				authorized_agent_state(user_input="I suddenly cannot move my arm")
			)
		self.assertEqual(updates["routing_decision"], "urgent_end")
		self.assertTrue(updates["if_emergency"])
		self.assertIn("EMERGENCY WARNING", updates["final_response"])

	def test_router_returns_clarifying_question_for_vague_input(self):
		result = router.RouterOutput(
			routing_decision="ask_clarification",
			if_emergency=False,
			emergency_reason="",
			needs_more_info=True,
			follow_up_question="Which joint hurts, and when did the pain start?",
			triage_notes="The input is too vague for a plan.",
		)
		with patch.object(graph_dependencies, "QWEN_70B", FakeLLM(result)):
			updates = router.router_agent(
				authorized_agent_state(user_input="It hurts")
			)
		self.assertEqual(updates["final_response"], "Which joint hurts, and when did the pain start?")

	def test_router_prompt_treats_unknown_duration_as_answered_context(self):
		result = router.RouterOutput(
			routing_decision="ask_clarification",
			if_emergency=False,
			emergency_reason="",
			needs_more_info=True,
			follow_up_question="How long has this been happening?",
			triage_notes="Testing prompt guidance.",
		)
		fake_llm = FakeLLM(result)
		state = {
			"user_input": "I have already said that",
			"context_packets": {
				"router": "## recent_turns\nPatient: no pain, no swelling, slight difficulty bearing weight. Not sure how long. Cold weather only. No recent injury. Broke knee six years ago and recovered."
			},
			"debug_trace": [],
		}

		with patch.object(graph_dependencies, "QWEN_70B", fake_llm):
			router.router_agent({**authorized_agent_state(), **state})

		system_prompt = fake_llm.structured_llm.messages[-1][0].content
		user_prompt = fake_llm.structured_llm.messages[-1][1].content
		self.assertIn("Screen for emergencies first", system_prompt)
		self.assertIn("Do not create or retain a durable goal", system_prompt)
		self.assertIn("Cold weather only", user_prompt)
		self.assertIn("No recent injury", user_prompt)

	def test_router_prompt_requires_safety_basics_for_first_vague_joint_symptom(self):
		result = router.RouterOutput(
			routing_decision="continuation",
			if_emergency=False,
			emergency_reason="",
			needs_more_info=False,
			follow_up_question="",
			triage_notes="Testing initial symptom policy.",
		)
		fake_llm = FakeLLM(result)
		state = {"user_input": "my left knee feels stiff when its cold. what should i do?", "debug_trace": []}

		with patch.object(graph_dependencies, "QWEN_70B", fake_llm):
			router.router_agent({**authorized_agent_state(), **state})

		system_prompt = fake_llm.structured_llm.messages[-1][0].content
		self.assertIn("Screen for emergencies first", system_prompt)
		self.assertIn("Do not create or retain a durable goal", system_prompt)

	def test_router_prompt_treats_exact_duration_as_useful_not_blocking_when_user_is_unsure(self):
		result = router.RouterOutput(
			routing_decision="ask_clarification",
			if_emergency=False,
			emergency_reason="",
			needs_more_info=True,
			follow_up_question="Could you estimate how long this has been happening, weeks or months?",
			triage_notes="Testing router policy prompt.",
		)
		fake_llm = FakeLLM(result)
		state = {
			"user_input": "not sure how long. it has little effect on daily activities",
			"context_packets": {
				"router": (
					"## recent_turns\n"
					"Assistant: Could you tell me how long you have had this stiffness, whether pain or swelling, and any recent injury?\n"
					"Patient: not sure how long. no pain and no swelling. i broke my knee six years ago and recovered.\n"
					"Assistant: Could you clarify how long the stiffness has been present and whether it affects daily activities?\n"
					"Patient: not sure how long. it has little effect on daily activities."
				)
			},
			"debug_trace": [],
		}

		with patch.object(graph_dependencies, "QWEN_70B", fake_llm):
			router.router_agent({**authorized_agent_state(), **state})

		system_prompt = fake_llm.structured_llm.messages[-1][0].content
		user_prompt = fake_llm.structured_llm.messages[-1][1].content
		self.assertIn("Screen for emergencies first", system_prompt)
		self.assertIn("Do not create or retain a durable goal", system_prompt)
		self.assertIn("not sure how long", user_prompt)
		self.assertIn("little effect on daily activities", user_prompt)

	def test_route_helpers_select_expected_paths(self):
		self.assertEqual(graph_routes.route_after_router({"if_emergency": True}), "urgent_end")
		self.assertEqual(graph_routes.route_after_router({"needs_more_info": True}), "clarification")
		self.assertEqual(graph_routes.route_after_router({"routing_decision": "continuation"}), "consultant")
		self.assertEqual(graph_routes.route_after_router({"route": workflow_state.Route.URGENT}), "urgent_end")
		self.assertEqual(graph_routes.route_after_router({"route": workflow_state.Route.CLARIFY}), "clarification")
		self.assertEqual(graph_routes.route_after_router({"route": workflow_state.Route.UNSUPPORTED}), "unsafe_fallback")
		self.assertEqual(graph_routes.route_after_policy_gate({"policy_gate_severity": "repairable"}), "unsafe_fallback")
		self.assertEqual(graph_routes.route_after_safety({"safety_passed": True}), "safe_end")
		self.assertEqual(graph_routes.route_after_safety({"safety_passed": False, "consult_attempts": 1}), "consultant_retry")
		self.assertEqual(graph_routes.route_after_safety({"safety_passed": False, "consult_attempts": 2}), "unsafe_fallback")

	def test_consultant_retry_prompt_makes_safety_feedback_a_revision_requirement(self):
		class FakeDeps:
			consultant_model = None
			use_tool_calling = False
			session_compaction_model = None
			session_compaction_timeout_seconds = 120
			session_factory = None
			policy = WorkflowPolicy()
			catalog_suggestions = None

		fake_llm = FakeContentLLM()
		FakeDeps.consultant_model = fake_llm
		consultant_invocation.consultant_agent(
			state=authorized_agent_state(
				user_input="Cold-weather knee stiffness",
				consult_attempts=1,
				safety_feedback="Explicitly tell the patient to stop for any new pain.",
			),
			deps=FakeDeps(),
		)

		prompt = fake_llm.messages[-1][1].content
		self.assertIn("FAILED SAFETY REVIEW", prompt)
		self.assertIn("Explicitly tell the patient to stop for any new pain.", prompt)
		self.assertIn("Revise the plan to satisfy every safety concern", prompt)


class RunRehabGraphPersistenceTests(unittest.TestCase):
	def test_anonymous_chat_is_rejected_before_graph_execution(self):
		class FakeDb:
			def close(self):
				pass

		with patch.object(graph_execution, "SessionLocal", lambda: FakeDb()):
			with self.assertRaises(PermissionError) as error:
				graph_execution.run_rehab_graph("My knee is stiff", None, None)

		self.assertIn("principal ownership", str(error.exception))

	def test_run_rehab_graph_reports_provider_account_errors_without_internal_issue_copy(self):
		class FakeQuery:
			def __init__(self, first_result=None):
				self.first_result = first_result

			def filter(self, *_args, **_kwargs):
				return self

			def order_by(self, *_args, **_kwargs):
				return self

			def first(self):
				return self.first_result

			def all(self):
				return []

		class FakeDb:
			def __init__(self):
				self.added = []
				self.patient_id = uuid.uuid4()
				self.session = db_models.AISession(
					session_id=uuid.uuid4(),
					patient_id=self.patient_id,
				)

			def add(self, obj):
				self.added.append(obj)

			def commit(self):
				pass

			def rollback(self):
				pass

			def close(self):
				pass

			def query(self, model):
				if model is db_models.AISession:
					return FakeQuery(self.session)
				return FakeQuery()

			def execute(self, _statement):
				class Result:
					def scalars(self):
						return self

					def all(self):
						return []

					def scalar_one_or_none(self):
						return None

				return Result()

		class FakeVectorStore:
			def close(self):
				pass

		class FakeGraph:
			def invoke(self, _state):
				raise RuntimeError("BadRequestError: Error code: 400 - {error: {type: Arrearage, code: Arrearage}}")

		fake_db = FakeDb()
		with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
			graph_execution, "QdrantKnowledgeStore", FakeVectorStore
		), patch.object(graph_execution, "build_rehab_graph", lambda _deps: FakeGraph()), patch.object(
			graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan
		):
			output = graph_execution.run_rehab_graph("My knee is stiff", str(fake_db.session.session_id), str(fake_db.patient_id), principal_id=str(fake_db.patient_id))

		self.assertIn("AI provider", output["final_response"])
		self.assertIn("account", output["final_response"])
		self.assertNotIn("internal issue", output["final_response"].lower())

	def test_run_rehab_graph_persists_full_per_node_internal_outputs(self):
		class FakeQuery:
			def __init__(self, first_result=None):
				self.first_result = first_result

			def filter(self, *_args, **_kwargs):
				return self

			def order_by(self, *_args, **_kwargs):
				return self

			def first(self):
				return self.first_result

			def all(self):
				return []

		class FakeDb:
			def __init__(self):
				self.added = []
				self.patient_id = uuid.uuid4()
				self.session = db_models.AISession(
					session_id=uuid.uuid4(),
					patient_id=self.patient_id,
				)

			def add(self, obj):
				self.added.append(obj)

			def commit(self):
				pass

			def rollback(self):
				pass

			def close(self):
				pass

			def query(self, model):
				if model is db_models.AISession:
					return FakeQuery(self.session)
				return FakeQuery()

			def execute(self, _statement):
				class Result:
					def scalars(self):
						return self

					def all(self):
						return []

					def scalar_one_or_none(self):
						return None

				return Result()

		class FakeVectorStore:
			def close(self):
				pass

		first_full_feedback = "Safety reviewer full text. " + "Do not truncate this sentence. " * 80
		router_full_output = "Decision: continuation\nNeeds more info: false\nTriage notes: enough detail to continue."

		class FakeGraph:
			def invoke(self, state):
				return {
					"session_id": state["session_id"],
					"routing_decision": "continuation",
					"consult_response": "Second consultant answer.",
					"safety_feedback": "Final safety feedback.",
					"safety_passed": True,
					"final_response": "Second consultant answer.",
					"conversation_history": [],
					"debug_trace": [
						{"step": 1, "node": "router", "decision": "continuation"},
						{"step": 2, "node": "safety_reviewer", "feedback": "Safety reviewer full text. Do not truncate..."},
					],
					"internal_log_events": [
						{"agent_name": "router", "event_type": "agent_output", "content": router_full_output, "metadata": {"decision": "continuation"}},
						{"agent_name": "safety_reviewer", "event_type": "agent_output", "content": first_full_feedback, "metadata": {"attempt": 1, "safety_passed": False}},
					],
				}

		fake_db = FakeDb()
		with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
			graph_execution, "QdrantKnowledgeStore", FakeVectorStore
		), patch.object(graph_execution, "build_rehab_graph", lambda _deps: FakeGraph()), patch.object(
			graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan
		):
			graph_execution.run_rehab_graph("My ankle feels unstable", str(fake_db.session.session_id), str(fake_db.patient_id), principal_id=str(fake_db.patient_id))

		internal_messages = [obj for obj in fake_db.added if isinstance(obj, AIInternalMessage)]
		self.assertTrue(internal_messages)
		self.assertTrue(any(msg.agent_name == "router" and msg.content == router_full_output for msg in internal_messages))
		self.assertTrue(any(msg.agent_name == "safety_reviewer" and msg.content == first_full_feedback for msg in internal_messages))
		self.assertTrue(any(msg.agent_name == "router" and msg.event_metadata.get("decision") == "continuation" for msg in internal_messages))
		self.assertTrue(any(msg.agent_name == "safety_reviewer" and msg.event_metadata.get("attempt") == 1 and msg.event_metadata.get("safety_passed") is False for msg in internal_messages))

	def test_run_rehab_graph_persists_internal_agent_outputs_separately_from_transcript(self):
		class FakeQuery:
			def __init__(self, first_result=None):
				self.first_result = first_result

			def filter(self, *_args, **_kwargs):
				return self

			def order_by(self, *_args, **_kwargs):
				return self

			def first(self):
				return self.first_result

			def all(self):
				return []

		class FakeDb:
			def __init__(self):
				self.added = []
				self.patient_id = uuid.uuid4()
				self.session = db_models.AISession(
					session_id=uuid.uuid4(),
					patient_id=self.patient_id,
				)

			def add(self, obj):
				self.added.append(obj)

			def commit(self):
				pass

			def rollback(self):
				pass

			def close(self):
				pass

			def query(self, model):
				if model is db_models.AISession:
					return FakeQuery(self.session)
				return FakeQuery()

			def execute(self, _statement):
				class Result:
					def scalars(self):
						return self

					def all(self):
						return []

					def scalar_one_or_none(self):
						return None

				return Result()

		class FakeVectorStore:
			def close(self):
				pass

		class FakeGraph:
			def invoke(self, state):
				return {
					"session_id": state["session_id"],
					"routing_decision": "continuation",
					"consult_response": "Internal consultant draft before safety review.",
					"safety_feedback": "Plan is conservative and safe.",
					"safety_passed": True,
					"final_response": "Internal consultant draft before safety review.",
					"conversation_history": [],
					"debug_trace": [
						{"step": 1, "node": "router", "decision": "continuation"},
						{"step": 2, "node": "consultant", "response_preview": "Internal consultant draft before safety review."},
						{"step": 3, "node": "safety_reviewer", "safety_passed": True, "feedback": "Plan is conservative and safe."},
					],
					"internal_log_events": [
						{"agent_name": "consultant", "event_type": "agent_output", "content": "Internal consultant draft before safety review.", "model_input": "consultant prompt", "metadata": {"attempt": 1}},
						{"agent_name": "safety_reviewer", "event_type": "agent_output", "content": "Plan is conservative and safe.", "model_input": "safety prompt", "metadata": {"attempt": 1}},
					],
				}

		fake_db = FakeDb()
		with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
			graph_execution, "QdrantKnowledgeStore", FakeVectorStore
		), patch.object(graph_execution, "build_rehab_graph", lambda _deps: FakeGraph()), patch.object(
			graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan
		):
			graph_execution.run_rehab_graph("My ankle feels unstable", str(fake_db.session.session_id), str(fake_db.patient_id), principal_id=str(fake_db.patient_id))

		transcript_messages = [obj for obj in fake_db.added if isinstance(obj, db_models.AIMessage)]
		internal_messages = [obj for obj in fake_db.added if isinstance(obj, AIInternalMessage)]
		self.assertEqual([msg.sender_role for msg in transcript_messages], ["user", "assistant"])
		self.assertGreaterEqual(len(internal_messages), 3)
		self.assertIn("consultant", {msg.agent_name for msg in internal_messages})
		self.assertIn("safety_reviewer", {msg.agent_name for msg in internal_messages})
		self.assertTrue(any(msg.agent_name == "consultant" and msg.content == "Internal consultant draft before safety review." for msg in internal_messages))
		self.assertTrue(any(msg.agent_name == "safety_reviewer" and msg.content == "Plan is conservative and safe." for msg in internal_messages))
		self.assertTrue(any(msg.agent_name == "consultant" and msg.model_input == "consultant prompt" for msg in internal_messages))
		self.assertFalse(any(msg.content == "My ankle feels unstable" for msg in internal_messages))


class AiChatWebSocketContractTests(unittest.TestCase):
	def test_new_session_is_created_then_reused(self):
		websocket = FakeWebSocket(
			[
				{"message": ""},
				{"message": "My knee is stiff after ACL rehab", "patient_id": "patient-1", "debug": True},
				{"message": "Can I add squats today?"},
			]
		)
		calls = []

		def fake_run_rehab_graph(user_input, session_id=None, patient_id=None):
			calls.append((user_input, session_id, patient_id))
			return {
				"session_id": "session-123",
				"final_response": f"Reply: {user_input}",
				"debug_trace": [{"step": 1, "node": "router", "decision": "ask_clarification"}],
				"routing_decision": "ask_clarification",
				"needs_more_info": True,
			}

		with patch.object(ws_api, "DEBUG_GRAPH", True), patch.object(
			ws_api, "run_rehab_graph", fake_run_rehab_graph
		):
			asyncio.run(ws_api.ai_chat_stream(websocket, "new"))

		self.assertTrue(websocket.accepted)
		self.assertEqual(calls[0], ("My knee is stiff after ACL rehab", None, "patient-1"))
		self.assertEqual(calls[1], ("Can I add squats today?", "session-123", None))
		self.assertEqual([payload["event"] for payload in websocket.sent_json], ["response", "response"])
		self.assertEqual(websocket.sent_json[0]["debug_trace"][0]["node"], "router")
		self.assertEqual(websocket.sent_json[0]["routing_decision"], "ask_clarification")
		self.assertEqual(websocket.sent_json[1]["debug_trace"][0]["node"], "router")

	def test_graph_errors_are_sent_as_socket_errors(self):
		websocket = FakeWebSocket([{"message": "Please test error handling", "patient_id": "patient-1"}])

		def fake_run_rehab_graph(_user_input, session_id=None, patient_id=None):
			raise RuntimeError("graph exploded")

		with patch.object(ws_api, "run_rehab_graph", fake_run_rehab_graph):
			asyncio.run(ws_api.ai_chat_stream(websocket, "new"))

		self.assertEqual(websocket.sent_json[0]["event"], "error")
		self.assertEqual(websocket.sent_json[0]["detail"], ws_api.AI_CHAT_STREAM_ERROR_DETAIL)


class AiChatHttpContractTests(unittest.TestCase):
	def test_http_chat_turn_reuses_ai_chat_response_shape(self):
		patient_id, session_id = uuid.uuid4(), uuid.uuid4()
		access = AuthorizedAIChat(
			patient_id=patient_id,
			session_id=str(session_id),
			care_episode_id=None,
		)
		service = FakeAIChatTurnService(
			{
				"session_id": "session-http-123",
				"final_response": "HTTP reply",
				"debug_trace": [{"step": 1, "node": "router"}],
				"routing_decision": "ask_clarification",
				"needs_more_info": True,
				"safety_passed": None,
			}
		)

		app = FastAPI()
		app.include_router(ws_api.router)
		app.state.ai_chat_turn_service = service
		app.dependency_overrides[get_db] = lambda: object()
		with patch.object(ws_api, "authorize_ai_chat", return_value=access), patch.object(
			ws_api, "run_rehab_graph"
		) as legacy_graph, patch.object(ws_api, "DEBUG_GRAPH", True), patch(
			"app.core.security.decode_token",
			return_value={
				"sub": "offline-user",
				"role": "patient",
				"user_id": str(patient_id),
			},
		):
			response = TestClient(app).post(
				f"/ai/chat/{session_id}",
				headers={"Authorization": "Bearer offline-token"},
				json={
					"message": "My knee is stiff",
					"idempotency_key": "idempotency-key-1234",
				},
			)

		self.assertEqual(response.status_code, 200, response.text)
		payload = response.json()
		self.assertEqual(service.calls, [("My knee is stiff", str(session_id), str(patient_id), None)])
		legacy_graph.assert_not_called()
		self.assertEqual(payload["event"], "response")
		self.assertEqual(payload["session_id"], str(session_id))
		self.assertEqual(payload["response"], "HTTP reply")
		self.assertEqual(payload["routing_decision"], "ask_clarification")
		self.assertTrue(payload["needs_more_info"])
		self.assertEqual(payload["debug_trace"][0]["node"], "router")

	def test_http_chat_turn_serializes_debug_retrieval_payload(self):
		patient_id, session_id = uuid.uuid4(), uuid.uuid4()
		access = AuthorizedAIChat(
			patient_id=patient_id,
			session_id=str(session_id),
			care_episode_id=None,
		)
		service = FakeAIChatTurnService(
			{
				"session_id": "session-http-debug",
				"final_response": "Debug HTTP reply",
				"debug_trace": [
					{"step": 1, "node": "consultant", "latency_ms": np.float32(12.5)},
				],
				"retrieved_docs": [
					{"score": np.float32(0.5), "payload": {"text": "Exercise context"}},
				],
				"routing_decision": "continuation",
				"needs_more_info": False,
				"safety_passed": True,
			}
		)

		app = FastAPI()
		app.include_router(ws_api.router)
		app.state.ai_chat_turn_service = service
		app.dependency_overrides[get_db] = lambda: object()

		with patch.object(ws_api, "authorize_ai_chat", return_value=access), patch.object(
			ws_api, "run_rehab_graph"
		) as legacy_graph, patch.object(ws_api, "DEBUG_GRAPH", True), patch(
			"app.core.security.decode_token",
			return_value={
				"sub": "offline-user",
				"role": "patient",
				"user_id": str(patient_id),
			},
		):
			response = TestClient(app, raise_server_exceptions=False).post(
				f"/ai/chat/{session_id}",
				headers={"Authorization": "Bearer offline-token"},
				json={
					"message": "My knee is stiff",
					"idempotency_key": "idempotency-key-5678",
				},
			)

		self.assertEqual(response.status_code, 200, response.text)
		body = response.json()
		self.assertEqual(body["event"], "response")
		self.assertEqual(body["retrieved_doc_count"], 1)
		self.assertEqual(body["debug_trace"][0], {"step": 1, "node": "consultant"})
		self.assertEqual(service.calls, [("My knee is stiff", str(session_id), str(patient_id), None)])
		legacy_graph.assert_not_called()

	def test_http_chat_turn_rejects_empty_messages(self):
		request = ws_api.AIChatTurnRequest(message="   ", idempotency_key="idempotency-key-9999")
		with self.assertRaises(ws_api.HTTPException) as error:
			asyncio.run(
				ws_api.ai_chat_turn(
					"new",
					request,
					principal=Principal(subject="offline-user", role="patient", user_id=uuid.uuid4()),
					db=object(),
				)
			)
		self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
	unittest.main(verbosity=2)
