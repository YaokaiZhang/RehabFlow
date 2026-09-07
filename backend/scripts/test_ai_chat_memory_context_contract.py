#!/usr/bin/env python3
"""Offline contract for feeding Memory Documents into AI chat graph state."""
from __future__ import annotations

import os
import sys
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QWEN_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ai import context_types
from app.ai import graph_execution
from app.ai import graph_prompt_context
from app.ai import node_specs
from app.db.models import AIInternalMessage, AIMessage, AISession, CareEpisode, Patient


class FakeQuery:
    def __init__(self, db, model):
        self.db = db
        self.model = model

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        if self.model is Patient:
            return self.db.patient
        if self.model is AISession:
            return None
        return None

    def all(self):
        if self.model is AIMessage:
            return self.db.messages
        return []


class FakeDb:
    def __init__(self, *, patient_id, episode_id=None, episode_patient_id=None, messages=None):
        self.patient = Patient(patient_id=patient_id, patient_name="Memory Context Patient", password_hash="hash", real_info={})
        self.episode = None
        if episode_id is not None:
            self.episode = CareEpisode(
                care_episode_id=episode_id,
                patient_id=episode_patient_id or patient_id,
                issue_title="Shoulder rehab",
                body_area="shoulder",
                goal="Return to overhead reach",
                short_description="Post-op stiffness and overhead limits.",
            )
        self.messages = list(messages or [])
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        for obj in self.added:
            if isinstance(obj, AISession) and obj.session_id is None:
                obj.session_id = uuid.uuid4()
            if isinstance(obj, Patient) and obj.patient_id is None:
                obj.patient_id = uuid.uuid4()

    def rollback(self):
        pass

    def close(self):
        pass

    def query(self, model):
        return FakeQuery(self, model)

    def execute(self, _statement):
        class EmptyResult:
            def scalar_one_or_none(self):
                return None

            def scalars(self):
                return self

            def all(self):
                return []

        return EmptyResult()

    def get(self, model, item_id):
        if model is CareEpisode and self.episode is not None and self.episode.care_episode_id == item_id:
            return self.episode
        return None


class FakeVectorStore:
    def close(self):
        pass


class CapturingGraph:
    def __init__(self):
        self.initial_state = None

    def invoke(self, state):
        self.initial_state = dict(state)
        return {
            "session_id": state["session_id"],
            "final_response": "Safe memory-aware rehab reply.",
            "conversation_history": [],
            "debug_trace": state.get("debug_trace", []),
        }


class ContextPlannerContractTests(unittest.TestCase):
    def test_deterministic_context_plan_uses_only_candidate_source_ids(self):
        card = context_types.ContextSourceCard(
            source_id="memory:patient",
            source_type="patient_memory_summary",
            scope="patient",
            title="Goal",
            text="Return to hiking.",
        )

        plan = graph_prompt_context._source_order_context_plan(
            candidate_cards=[card],
            current_user_message="Can I progress squats?",
            node_specs=node_specs.NODE_SPECS,
        )

        self.assertEqual(plan.turn_summary, "Deterministic source-order context plan.")
        self.assertEqual(plan.ranked_source_ids, ("memory:patient",))
        self.assertEqual(plan.node_hints["consultant"], ("memory:patient",))
        self.assertFalse(plan.summary_update_needed)


class MemoryContextContractTests(unittest.TestCase):
    def test_run_rehab_graph_loads_source_labeled_memory_context_for_matching_episode(self):
        patient_id = uuid.uuid4()
        episode_id = uuid.uuid4()
        fake_db = FakeDb(patient_id=patient_id, episode_id=episode_id)
        graph = CapturingGraph()
        compiled_context = (
            "PATIENT MEMORY\n"
            "- Baseline precautions: Avoid loaded overhead press\n"
            "  Source: patient_edit\n\n"
            "EPISODE MEMORY\n"
            "- Triage summary: Right shoulder stiffness with overhead reach\n"
            "  Source: triage_summary 1"
        )

        def fake_build_memory_context_pack(db, episode):
            self.assertIs(db, fake_db)
            self.assertEqual(episode.care_episode_id, episode_id)
            return object(), object(), compiled_context

        with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
            graph_execution, "QdrantKnowledgeStore", FakeVectorStore
        ), patch.object(graph_execution, "build_rehab_graph", lambda _deps: graph), patch.object(
            graph_prompt_context, "build_memory_context_pack", fake_build_memory_context_pack, create=True
        ), patch.object(graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan):
            output = graph_execution.run_rehab_graph(
                "What should I do today?", None, str(patient_id), care_episode_id=str(episode_id), principal_id=str(patient_id)
            )

        self.assertEqual(output["final_response"], "Safe memory-aware rehab reply.")
        self.assertIsNotNone(graph.initial_state)
        self.assertEqual(graph.initial_state["care_episode_id"], str(episode_id))
        self.assertEqual(graph.initial_state["memory_context"], compiled_context)
        self.assertIn("Source: patient_edit", graph.initial_state["memory_context"])
        self.assertIn("Source: triage_summary 1", graph.initial_state["memory_context"])






    def test_context_assembly_log_does_not_disable_legacy_agent_output_fallback(self):
        patient_id = uuid.uuid4()
        fake_db = FakeDb(patient_id=patient_id)

        class AgentOutputOnlyGraph:
            def invoke(self, state):
                return {
                    "session_id": state["session_id"],
                    "final_response": "Safe final reply.",
                    "conversation_history": [],
                    "debug_trace": state.get("debug_trace", []),
                    "consult_response": "Internal consultant draft.",
                    "safety_feedback": "Safety reviewer approved.",
                    "safety_passed": True,
                    "internal_log_events": [
                        {
                            "agent_name": "consultant",
                            "event_type": "agent_output",
                            "content": "Internal consultant draft.",
                            "model_input": "consultant prompt",
                            "metadata": {},
                        },
                        {
                            "agent_name": "safety_reviewer",
                            "event_type": "agent_output",
                            "content": "Safety reviewer approved.",
                            "model_input": "safety prompt",
                            "metadata": {},
                        },
                    ],
                }

        with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
            graph_execution, "QdrantKnowledgeStore", FakeVectorStore
        ), patch.object(graph_execution, "build_rehab_graph", lambda _deps: AgentOutputOnlyGraph()), patch.object(
            graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan
        ):
            graph_execution.run_rehab_graph("My knee is stiff", None, str(patient_id), principal_id=str(patient_id))

        internal_messages = [obj for obj in fake_db.added if isinstance(obj, AIInternalMessage)]
        by_agent = {msg.agent_name for msg in internal_messages}
        self.assertIn("context_assembler", by_agent)
        self.assertIn("consultant", by_agent)
        self.assertIn("safety_reviewer", by_agent)
        self.assertTrue(any(msg.agent_name == "consultant" and msg.content == "Internal consultant draft." for msg in internal_messages))
        self.assertTrue(any(msg.agent_name == "safety_reviewer" and msg.content == "Safety reviewer approved." for msg in internal_messages))
        self.assertTrue(any(msg.agent_name == "consultant" and msg.model_input == "consultant prompt" for msg in internal_messages))
        self.assertTrue(all("Internal consultant draft." not in repr(msg.event_metadata) for msg in internal_messages))
        self.assertTrue(all("Safety reviewer approved." not in repr(msg.event_metadata) for msg in internal_messages))

    def test_run_rehab_graph_persists_context_assembly_internal_log(self):
        patient_id = uuid.uuid4()
        episode_id = uuid.uuid4()
        fake_db = FakeDb(
            patient_id=patient_id,
            episode_id=episode_id,
            messages=[
                AIMessage(message_id=uuid.uuid4(), session_id=uuid.uuid4(), sender_role="user", content="Old user turn"),
            ],
        )
        graph = CapturingGraph()
        patient_document = types.SimpleNamespace(document_id="patient-doc", scope="patient", status="active", items=[])
        episode_document = types.SimpleNamespace(document_id="episode-doc", scope="episode", status="active", items=[])

        def fake_build_memory_context_pack(_db, _episode):
            return patient_document, episode_document, "Compiled Memory Documents Context"

        with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
            graph_execution, "QdrantKnowledgeStore", FakeVectorStore
        ), patch.object(graph_execution, "build_rehab_graph", lambda _deps: graph), patch.object(
            graph_prompt_context, "build_memory_context_pack", fake_build_memory_context_pack, create=True
        ), patch.object(graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan):
            output = graph_execution.run_rehab_graph(
                "Can I progress squats?", None, str(patient_id), care_episode_id=str(episode_id), principal_id=str(patient_id)
            )

        self.assertEqual(output["final_response"], "Safe memory-aware rehab reply.")
        internal_messages = [obj for obj in fake_db.added if isinstance(obj, AIInternalMessage)]
        context_logs = [
            msg
            for msg in internal_messages
            if msg.agent_name == "context_assembler" and msg.event_type == "context_assembly"
        ]
        self.assertEqual(len(context_logs), 1)
        self.assertEqual(context_logs[0].content, "Deterministic source-order context plan.")
        metadata = context_logs[0].event_metadata
        self.assertEqual(metadata["candidate_count"], 1)
        self.assertIn("router", metadata["packet_tokens"])
        assert metadata["selected_sources"]["router"] == []
        assert metadata["omitted_sources"]["router"] == []

    def test_run_rehab_graph_does_not_persist_model_context_summary(self):
        patient_id = uuid.uuid4()
        episode_id = uuid.uuid4()
        fake_db = FakeDb(patient_id=patient_id, episode_id=episode_id)
        graph = CapturingGraph()
        patient_document = types.SimpleNamespace(document_id="patient-doc", scope="patient", status="active", items=[])
        episode_document = types.SimpleNamespace(document_id="episode-doc", scope="episode", status="active", items=[])

        def fake_build_memory_context_pack(_db, _episode):
            return patient_document, episode_document, "Compiled Memory Documents Context"

        with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
            graph_execution, "QdrantKnowledgeStore", FakeVectorStore
        ), patch.object(graph_execution, "build_rehab_graph", lambda _deps: graph), patch.object(
            graph_prompt_context, "build_memory_context_pack", fake_build_memory_context_pack, create=True
        ):
            output = graph_execution.run_rehab_graph(
                "Can I progress squats?", None, str(patient_id), care_episode_id=str(episode_id), principal_id=str(patient_id)
            )

        self.assertEqual(output["final_response"], "Safe memory-aware rehab reply.")
        transcript_messages = [obj for obj in fake_db.added if isinstance(obj, AIMessage)]


    def test_run_rehab_graph_includes_saved_triage_summary_in_consultant_packet(self):
        patient_id = uuid.uuid4()
        episode_id = uuid.uuid4()
        fake_db = FakeDb(patient_id=patient_id, episode_id=episode_id)
        graph = CapturingGraph()
        summary = types.SimpleNamespace(
            triage_summary_id="summary-1",
            care_episode_id=str(episode_id),
            patient_id=str(patient_id),
            version=2,
            concern="Left knee pain",
            relevant_context="Pain with stairs",
            safety_signals=["swelling"],
            limitations=["stairs"],
            recommendation="Clinician review before progression",
            missing_information=[],
            unresolved_questions=["Any locking?"],
            clinician_review_needed=True,
            additional_context="Saved by patient.",
        )
        patient_document = types.SimpleNamespace(document_id="patient-doc", scope="patient", status="active", items=[])
        episode_document = types.SimpleNamespace(document_id="episode-doc", scope="episode", status="active", items=[])

        def fake_build_memory_context_pack(_db, _episode):
            return patient_document, episode_document, "Compiled Memory Documents Context"

        with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
            graph_execution, "QdrantKnowledgeStore", FakeVectorStore
        ), patch.object(graph_execution, "build_rehab_graph", lambda _deps: graph), patch.object(
            graph_prompt_context, "build_memory_context_pack", fake_build_memory_context_pack, create=True
        ), patch.object(graph_execution, "latest_triage_summary_for_episode", lambda _db, _episode_id: summary), patch.object(
            graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan
        ):
            output = graph_execution.run_rehab_graph(
                "What should I do today?", None, str(patient_id), care_episode_id=str(episode_id), principal_id=str(patient_id)
            )

        self.assertEqual(output["final_response"], "Safe memory-aware rehab reply.")
        self.assertIsNotNone(graph.initial_state)
        consultant_packet = graph.initial_state["context_packets"]["consultant"]
        self.assertTrue(consultant_packet["available"])
        self.assertIn("triage_summary:summary-1", consultant_packet["included_source_ids"])
        self.assertNotIn("Saved by patient.", repr(graph.initial_state))
        self.assertNotIn("Concern: Left knee pain", repr(graph.initial_state))
        self.assertNotIn("Clinician review before progression", repr(graph.initial_state))

    def test_run_rehab_graph_assembles_context_packets_without_injecting_full_transcript(self):
        patient_id = uuid.uuid4()
        episode_id = uuid.uuid4()
        fake_db = FakeDb(
            patient_id=patient_id,
            episode_id=episode_id,
            messages=[
                AIMessage(message_id=uuid.uuid4(), session_id=uuid.uuid4(), sender_role="user", content="Old user turn"),
                AIMessage(message_id=uuid.uuid4(), session_id=uuid.uuid4(), sender_role="assistant", content="Old assistant turn"),
            ],
        )
        graph = CapturingGraph()
        build_calls = []
        patient_document = type(
            "FakeDocument",
            (),
            {
                "document_id": "patient-doc",
                "scope": "patient",
                "status": "active",
                "summary_text": "Patient baseline",
                "editable_fields": {"preference": "Avoid loaded overhead press"},
            },
        )()
        episode_document = type(
            "FakeDocument",
            (),
            {
                "document_id": "episode-doc",
                "scope": "episode",
                "status": "active",
                "level1_keywords": ["shoulder", "overhead reach"],
                "level1_description": "Return to overhead reach.",
                "level1_session_ids": [],
            },
        )()

        def fake_build_memory_context_pack(db, episode):
            build_calls.append((db, episode))
            return patient_document, episode_document, "Compiled Memory Documents Context"

        with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
            graph_execution, "QdrantKnowledgeStore", FakeVectorStore
        ), patch.object(graph_execution, "build_rehab_graph", lambda _deps: graph), patch.object(
            graph_prompt_context, "build_memory_context_pack", fake_build_memory_context_pack, create=True
        ), patch.object(graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan):
            output = graph_execution.run_rehab_graph(
                "What should I do today?", None, str(patient_id), care_episode_id=str(episode_id), principal_id=str(patient_id)
            )

        self.assertEqual(output["final_response"], "Safe memory-aware rehab reply.")
        self.assertEqual(len(build_calls), 1)
        self.assertIsNotNone(graph.initial_state)
        self.assertEqual(graph.initial_state["conversation_history"], [])
        self.assertEqual(set(graph.initial_state["context_packets"]), {"router", "consultant", "safety_reviewer", "grounding_reviewer"})
        self.assertTrue(graph.initial_state["context_packets"]["router"]["available"])
        self.assertEqual(
            len(graph.initial_state["context_packets"]["consultant"]["included_source_ids"]),
            2,
        )
        self.assertNotIn("Old user turn", repr(graph.initial_state))
        self.assertNotIn("Source-aware compact session summary.", repr(graph.initial_state))
        graph_start = graph.initial_state["debug_trace"][0]
        self.assertEqual(graph_start["node"], "graph_start")
        self.assertEqual(graph_start["history_count"], 0)
        self.assertEqual(graph_start["injected_history_count"], 0)
        self.assertEqual(graph_start["context_candidate_count"], 2)
        self.assertIn("router", graph_start["context_packet_tokens"])


    def test_run_rehab_graph_does_not_load_memory_for_inactive_episode(self):
        patient_id = uuid.uuid4()
        episode_id = uuid.uuid4()
        fake_db = FakeDb(patient_id=patient_id, episode_id=episode_id)
        fake_db.episode.status = "closed"
        graph = CapturingGraph()

        def fake_build_memory_context_pack(_db, _episode):
            raise AssertionError("memory context must not load for an inactive episode")

        with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
            graph_execution, "QdrantKnowledgeStore", FakeVectorStore
        ), patch.object(graph_execution, "build_rehab_graph", lambda _deps: graph), patch.object(
            graph_prompt_context, "build_memory_context_pack", fake_build_memory_context_pack, create=True
        ), patch.object(graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan):
            output = graph_execution.run_rehab_graph(
                "Can I progress today?", None, str(patient_id), care_episode_id=str(episode_id), principal_id=str(patient_id)
            )

        self.assertEqual(output["final_response"], "Safe memory-aware rehab reply.")
        self.assertIsNotNone(graph.initial_state)
        self.assertEqual(graph.initial_state["care_episode_id"], str(episode_id))
        self.assertEqual(graph.initial_state["memory_context"], "")
        self.assertEqual(
            graph.initial_state["context_packets"]["consultant"]["included_source_ids"],
            [],
        )

    def test_run_rehab_graph_does_not_load_memory_for_another_patients_episode(self):
        patient_id = uuid.uuid4()
        episode_id = uuid.uuid4()
        fake_db = FakeDb(patient_id=patient_id, episode_id=episode_id, episode_patient_id=uuid.uuid4())
        graph = CapturingGraph()

        def fake_build_memory_context_pack(_db, _episode):
            raise AssertionError("memory context must not load for a mismatched episode")

        with patch.object(graph_execution, "SessionLocal", lambda: fake_db), patch.object(
            graph_execution, "QdrantKnowledgeStore", FakeVectorStore
        ), patch.object(graph_execution, "build_rehab_graph", lambda _deps: graph), patch.object(
            graph_prompt_context, "build_memory_context_pack", fake_build_memory_context_pack, create=True
        ), patch.object(graph_execution, "_source_order_context_plan", graph_prompt_context._source_order_context_plan):
            output = graph_execution.run_rehab_graph(
                "Can I progress today?", None, str(patient_id), care_episode_id=str(episode_id), principal_id=str(patient_id)
            )

        self.assertEqual(output["final_response"], "Safe memory-aware rehab reply.")
        self.assertIsNotNone(graph.initial_state)
        self.assertEqual(graph.initial_state["care_episode_id"], str(episode_id))
        self.assertEqual(graph.initial_state["memory_context"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
