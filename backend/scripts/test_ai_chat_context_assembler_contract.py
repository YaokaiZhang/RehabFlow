#!/usr/bin/env python3
"""Contract tests for AI chat context assembly data types.

Usage:
    cd backend
    python3 scripts/test_ai_chat_context_assembler_contract.py
"""
from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

os.environ.setdefault("QWEN_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Keep this data-contract test independent of app.ai.__init__, which imports the
# graph stack and its optional runtime dependencies.
ai_package = types.ModuleType("app.ai")
ai_package.__path__ = [str(BACKEND_DIR / "app" / "ai")]
sys.modules.setdefault("app.ai", ai_package)
context_types = importlib.import_module("app.ai.context_types")
context_assembler = importlib.import_module("app.ai.context_assembler")
node_specs = importlib.import_module("app.ai.node_specs")
ContextAssemblyResult = context_types.ContextAssemblyResult
ContextPlan = context_types.ContextPlan
ContextScope = context_types.ContextScope
ContextSourceCard = context_types.ContextSourceCard
NodeContextPacket = context_types.NodeContextPacket
NodeSpec = context_types.NodeSpec
CONSULTANT_SPEC = node_specs.CONSULTANT_SPEC
NODE_SPECS = node_specs.NODE_SPECS
ROUTER_SPEC = node_specs.ROUTER_SPEC
SAFETY_REVIEWER_SPEC = node_specs.SAFETY_REVIEWER_SPEC
GROUNDING_REVIEWER_SPEC = node_specs.GROUNDING_REVIEWER_SPEC
ContextAssembler = context_assembler.ContextAssembler
cards_from_memory_documents = context_assembler.cards_from_memory_documents
cards_from_session_messages = context_assembler.cards_from_session_messages
cards_from_triage_summary = context_assembler.cards_from_triage_summary
estimate_tokens = context_assembler.estimate_tokens
latest_triage_summary_for_episode = context_assembler.latest_triage_summary_for_episode
TRANSCRIPT_ROLES = context_assembler.TRANSCRIPT_ROLES


class ContextSourceCollectionTests(unittest.TestCase):
    def test_estimate_tokens_is_zero_for_empty_text_and_positive_for_content(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreater(estimate_tokens("Patient has knee pain"), 0)

    def test_structured_memory_documents_become_source_cards(self):
        patient_document = types.SimpleNamespace(
            document_id="patient-doc",
            scope="patient",
            status="active",
            summary_text="Prefers morning mobility.",
            editable_fields={"exercise_timing": "morning"},
        )
        episode_document = types.SimpleNamespace(
            document_id="episode-doc",
            scope="episode",
            status="active",
            level1_keywords=["Stairs"],
            level1_description="Pain increases when descending stairs.",
            level1_session_ids=["session-1"],
        )

        cards = cards_from_memory_documents(patient_document, episode_document)

        self.assertEqual([card.source_id for card in cards], ["memory:patient", "memory:episode:level1"])
        self.assertEqual(cards[0].scope, "patient")
        self.assertEqual(cards[1].scope, "episode")
        self.assertIn("Prefers morning mobility.", cards[0].text)
        self.assertIn("Relevant session IDs: session-1", cards[1].text)

    def test_inactive_structured_memory_documents_are_excluded(self):
        patient_document = types.SimpleNamespace(
            document_id="patient-doc",
            scope="patient",
            status="inactive",
            summary_text="Drop",
            editable_fields={"old": "Drop"},
        )

        cards = cards_from_memory_documents(patient_document, None)

        self.assertEqual(cards, [])


    def test_latest_triage_summary_helper_returns_scalar_result(self):
        summary = object()

        class FakeScalarResult:
            def scalar_one_or_none(self):
                return summary

        class FakeDb:
            statement = None

            def execute(self, statement):
                self.statement = statement
                return FakeScalarResult()

        db = FakeDb()

        result = latest_triage_summary_for_episode(db, "episode-1")

        self.assertIs(result, summary)
        statement_text = str(db.statement)
        self.assertIn("care_episode_triage_summaries", statement_text)
        self.assertIn("ORDER BY", statement_text)
        self.assertIn("version DESC", statement_text)
        self.assertIn("created_at DESC", statement_text)
        self.assertIn("LIMIT", statement_text)

    def test_saved_triage_summary_fields_become_one_episode_source_card(self):
        summary = types.SimpleNamespace(
            triage_summary_id="summary-1",
            care_episode_id="episode-1",
            patient_id="patient-1",
            version=3,
            concern="Left knee pain",
            relevant_context="Pain on stairs",
            safety_signals=["swelling"],
            limitations=["stairs"],
            recommendation="Book clinician review",
            missing_information=["pain score"],
            unresolved_questions=["Any locking?"],
            clinician_review_needed=True,
            additional_context="Symptoms started last week.",
        )

        cards = cards_from_triage_summary(summary)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].source_id, "triage_summary:summary-1")
        self.assertEqual(cards[0].scope, "episode")
        self.assertIn("Concern: Left knee pain", cards[0].text)
        self.assertIn('Unresolved questions: ["Any locking?"]', cards[0].text)

    def test_session_messages_become_verbatim_transcript_cards_only(self):
        messages = [
            types.SimpleNamespace(message_id="user-1", sender_role="user", content="My knee hurts", created_at="2026-07-06T10:00:00+00:00"),
            types.SimpleNamespace(message_id="assistant-1", sender_role="assistant", content="Tell me more", created_at="2026-07-06T10:01:00+00:00"),
            types.SimpleNamespace(
                message_id="internal-1",
                sender_role="tool",
                content="Tool output",
                created_at="2026-07-06T09:00:00+00:00",
            ),
        ]

        cards = cards_from_session_messages(messages)

        self.assertEqual(TRANSCRIPT_ROLES, {"user", "assistant"})
        self.assertEqual([card.source_id for card in cards], ["ai_message:user-1", "ai_message:assistant-1"])
        self.assertEqual(cards[0].scope, "session")
        self.assertEqual(cards[0].metadata["sender_role"], "user")


    def test_saved_rehab_session_becomes_episode_source_card(self):
        session = types.SimpleNamespace(
            session_id="session-1",
            care_episode_id="episode-1",
            patient_id="patient-1",
            completion_status="summarized",
            checklist=[
                {"label": "Air Angels", "completed": True},
                {"label": "Wall slides", "completed": False},
            ],
            patient_notes="Shoulder hurts at the highest stretch.",
            session_summary="Completed: Air Angels. Remaining: Wall slides.",
        )

        cards = context_assembler.cards_from_rehab_session(session)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].source_id, "rehab_activity:session-1")
        self.assertEqual(cards[0].scope, "episode")
        self.assertIn("Air Angels", cards[0].text)
        self.assertIn("Shoulder hurts at the highest stretch.", cards[0].text)


class FakePlanner:
    def __init__(self, plan: object):
        self.plan_result = plan
        self.calls: list[dict[str, object]] = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.plan_result, Exception):
            raise self.plan_result
        return self.plan_result


class ContextAssemblerTests(unittest.TestCase):
    def _cards(self):
        return [
            ContextSourceCard(
                source_id="memory:patient",
                source_type="patient_memory_summary",
                scope="patient",
                title="Patient Memory",
                text="Patient wants to return to hiking.",
            ),
            ContextSourceCard(
                source_id="memory:episode:level1",
                source_type="episode_memory_level_1",
                scope="episode",
                title="Episode Memory Level 1",
                text="Pain when descending stairs.",
            ),
            ContextSourceCard(
                source_id="triage_summary:summary-1",
                source_type="triage_summary",
                scope="episode",
                title="Saved triage summary",
                text="Concern: left knee pain",
            ),
            ContextSourceCard(
                source_id="ai_message:user-1",
                source_type="session_message",
                scope="session",
                title="user",
                text="Can I progress squats?",
            ),
        ]

    def test_assembler_calls_planner_once_and_discards_unknown_source_ids(self):
        plan = ContextPlan(
            turn_summary="Patient asks about progression.",
            ranked_source_ids=("memory:patient", "unknown:old", "triage_summary:summary-1"),
            node_hints={"consultant": ("unknown:old", "memory:episode:level1")},
            unresolved_questions=(),
            stale_source_ids=("unknown:old", "memory:patient"),
            summary_update_needed=True,
            summary_notes="May need updated progression summary.",
        )
        planner = FakePlanner(plan)

        result = ContextAssembler(planner).assemble_for_turn(
            candidate_cards=self._cards(),
            current_user_message="Can I progress squats?",
        )

        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(result.plan.ranked_source_ids, ("memory:patient", "triage_summary:summary-1"))
        self.assertEqual(result.plan.node_hints["consultant"], ("memory:episode:level1",))
        self.assertEqual(result.plan.stale_source_ids, ("memory:patient",))
        self.assertTrue(result.plan.summary_update_needed)
        self.assertEqual(result.assembly_metadata["candidate_count"], 4)

    def test_assembler_builds_different_packets_by_node_spec(self):
        source_ids = tuple(card.source_id for card in self._cards())
        plan = ContextPlan(
            turn_summary="Patient asks about progression.",
            ranked_source_ids=source_ids,
            node_hints={node_id: source_ids for node_id in NODE_SPECS},
            unresolved_questions=(),
            stale_source_ids=(),
            summary_update_needed=False,
            summary_notes="No summary update needed.",
        )

        result = ContextAssembler(FakePlanner(plan)).assemble_for_turn(
            candidate_cards=self._cards(),
            current_user_message="Can I progress squats?",
            node_required_fields={"consultant_response": "Suggested gentle progression.", "safety_feedback": "None"},
        )

        router_packet = result.packets["router"]
        consultant_packet = result.packets["consultant"]
        safety_packet = result.packets["safety_reviewer"]
        self.assertIn("recent_turns", router_packet.sections)
        self.assertNotIn("patient_memory", router_packet.sections)
        self.assertIn("patient_memory", consultant_packet.sections)
        self.assertIn("episode_memory", consultant_packet.sections)
        self.assertIn("triage_summary", consultant_packet.sections)
        self.assertIn("consultant_response", safety_packet.sections)
        self.assertIn("safety_feedback", safety_packet.sections)
        self.assertNotEqual(set(router_packet.sections), set(consultant_packet.sections))
        self.assertIn("consultant", result.assembly_metadata["packet_tokens"])

    def test_assembler_maps_retrieval_cards_by_source_id_prefix(self):
        card = ContextSourceCard(
            source_id="retrieved_doc:exercise-1",
            source_type="unexpected_source_type",
            scope="session",
            title="Exercise catalog result",
            text="Catalog-backed squat progression guidance.",
        )
        plan = ContextPlan(
            turn_summary="Patient asks about progression.",
            ranked_source_ids=("retrieved_doc:exercise-1",),
            node_hints={"consultant": ("retrieved_doc:exercise-1",)},
            unresolved_questions=(),
            stale_source_ids=(),
            summary_update_needed=False,
            summary_notes="Retrieval prefix routing test.",
        )

        result = ContextAssembler(FakePlanner(plan)).assemble_for_turn(
            candidate_cards=[card],
            current_user_message="Can I progress squats?",
        )

        self.assertIn("retrieval_context", result.packets["consultant"].sections)
        self.assertEqual(result.packets["consultant"].included_source_ids, ("retrieved_doc:exercise-1",))

    def test_assembler_keeps_full_sections_without_fixed_budgets(self):
        tiny_spec = NodeSpec(node_id="tiny", context_sections=("patient_memory",))
        card = ContextSourceCard(
            source_id="memory:patient",
            source_type="patient_memory_summary",
            scope="patient",
            title="Patient Memory",
            text="This source card is deliberately far too long for one token.",
        )
        plan = ContextPlan(
            turn_summary="Full source assembly.",
            ranked_source_ids=("memory:patient",),
            node_hints={"tiny": ("memory:patient",)},
            unresolved_questions=(),
            stale_source_ids=(),
            summary_update_needed=False,
            summary_notes="No per-section budget.",
        )

        result = ContextAssembler(FakePlanner(plan)).assemble_for_turn(
            candidate_cards=[card],
            current_user_message="",
            node_specs={"tiny": tiny_spec},
        )

        self.assertEqual(result.packets["tiny"].included_source_ids, ("memory:patient",))
        self.assertEqual(result.packets["tiny"].omitted_source_ids, ())

    def test_assembler_falls_back_when_planner_fails(self):
        result = ContextAssembler(FakePlanner(RuntimeError("planner unavailable"))).assemble_for_turn(
            candidate_cards=self._cards(),
            current_user_message="Can I progress squats?",
        )

        self.assertIn("planner_error", result.assembly_metadata)
        self.assertEqual(result.plan.ranked_source_ids, tuple(card.source_id for card in self._cards()))
        self.assertIn("recent_turns", result.packets["router"].sections)


    def test_node_specs_do_not_embed_fixed_section_budgets(self):
        for spec in NODE_SPECS.values():
            self.assertTrue(spec.context_sections)
            self.assertFalse(hasattr(spec, "budgets"))

class ContextScopeTests(unittest.TestCase):
    def test_context_scope_allows_patient_episode_and_session(self):
        self.assertEqual(set(ContextScope.__args__), {"patient", "episode", "session"})


class ContextSourceCardTests(unittest.TestCase):
    def test_prompt_text_includes_source_id_and_content(self):
        card = ContextSourceCard(
            source_id="episode:triage-summary",
            source_type="triage_summary",
            scope="episode",
            title="Current episode summary",
            text="Patient reports left knee pain when descending stairs.",
            metadata={"episode_id": "episode-123"},
            priority_hint="high",
        )

        prompt_text = card.to_prompt_text()

        self.assertIn("Source: episode:triage-summary", prompt_text)
        self.assertIn("Type: triage_summary", prompt_text)
        self.assertIn("Scope: episode", prompt_text)
        self.assertIn("Priority: high", prompt_text)
        self.assertIn("Metadata: episode_id=episode-123", prompt_text)
        self.assertIn("Patient reports left knee pain when descending stairs.", prompt_text)

    def test_empty_metadata_still_has_stable_prompt_field(self):
        card = ContextSourceCard(
            source_id="patient:memory",
            source_type="patient_memory_document",
            scope="patient",
            title="Patient Memory",
            text="Prefers morning exercises.",
        )

        self.assertIn("Metadata: {}", card.to_prompt_text())

    def test_source_card_is_frozen(self):
        card = ContextSourceCard(
            source_id="patient:memory",
            source_type="patient_memory_document",
            scope="patient",
            title="Patient Memory",
            text="Prefers morning exercises.",
        )

        with self.assertRaises(FrozenInstanceError):
            card.title = "Changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            card.metadata["new"] = "value"  # type: ignore[index]


class NodeSpecTests(unittest.TestCase):
    def test_rejects_duplicate_context_sections(self):
        with self.assertRaisesRegex(ValueError, "Duplicate context section"):
            NodeSpec(
                node_id="consultant",
                context_sections=("episode", "patient", "episode"),
            )

    def test_node_spec_has_no_section_budget_contract(self):
        spec = NodeSpec(
            node_id="router",
            context_sections=("session",),
        )
        self.assertFalse(hasattr(spec, "budgets"))


class NodeSpecCatalogTests(unittest.TestCase):
    def test_declares_expected_graph_node_specs(self):
        self.assertEqual(ROUTER_SPEC.node_id, "router")
        self.assertEqual(CONSULTANT_SPEC.node_id, "consultant")
        self.assertEqual(SAFETY_REVIEWER_SPEC.node_id, "safety_reviewer")
        self.assertEqual(GROUNDING_REVIEWER_SPEC.node_id, "grounding_reviewer")
        self.assertEqual(
            NODE_SPECS,
            {
                "router": ROUTER_SPEC,
                "consultant": CONSULTANT_SPEC,
                "safety_reviewer": SAFETY_REVIEWER_SPEC,
                "grounding_reviewer": GROUNDING_REVIEWER_SPEC,
            },
        )

    def test_node_specs_include_important_context_sections(self):
        self.assertIn("current_user_message", ROUTER_SPEC.context_sections)
        self.assertIn("patient_memory", CONSULTANT_SPEC.context_sections)
        self.assertIn("episode_memory", CONSULTANT_SPEC.context_sections)
        self.assertIn("triage_summary", CONSULTANT_SPEC.context_sections)
        self.assertIn("consultant_response", SAFETY_REVIEWER_SPEC.context_sections)
        self.assertIn("safety_feedback", SAFETY_REVIEWER_SPEC.context_sections)
        self.assertIn("consultant_draft", GROUNDING_REVIEWER_SPEC.context_sections)
        self.assertIn("evidence_manifest", GROUNDING_REVIEWER_SPEC.context_sections)

    def test_node_specs_have_context_sections(self):
        for spec in NODE_SPECS.values():
            self.assertTrue(spec.context_sections)

    def test_node_specs_do_not_embed_version_marker_names(self):
        marker = "v" + "1"
        for spec in NODE_SPECS.values():
            self.assertNotIn(marker, spec.node_id)
            for section in spec.context_sections:
                self.assertNotIn(marker, section)

    def test_node_spec_registry_is_frozen(self):
        with self.assertRaises(TypeError):
            NODE_SPECS["new"] = ROUTER_SPEC  # type: ignore[index]


class ContextPlanTests(unittest.TestCase):
    def test_restrict_to_source_ids_removes_unknown_ids(self):
        plan = ContextPlan(
            turn_summary="Patient is asking about knee rehab progression.",
            ranked_source_ids=("episode:summary", "patient:goals", "unknown:old"),
            node_hints={
                "router": ("unknown:old", "episode:summary"),
                "consultant": ("patient:goals", "missing:note"),
            },
            unresolved_questions=("Confirm pain intensity.",),
            stale_source_ids=("unknown:old", "patient:goals", "missing:note"),
            summary_update_needed=True,
            summary_notes="Possible new limitation around stairs.",
        )

        restricted = plan.restrict_to_source_ids({"episode:summary", "patient:goals"})

        self.assertEqual(restricted.ranked_source_ids, ("episode:summary", "patient:goals"))
        self.assertEqual(restricted.node_hints["router"], ("episode:summary",))
        self.assertEqual(restricted.node_hints["consultant"], ("patient:goals",))
        self.assertEqual(restricted.stale_source_ids, ("patient:goals",))
        self.assertIsInstance(restricted.summary_notes, str)
        with self.assertRaises(TypeError):
            restricted.node_hints["new-node"] = ("patient:goals",)  # type: ignore[index]

    def test_summary_notes_must_be_string(self):
        with self.assertRaisesRegex(TypeError, "summary_notes must be a string"):
            ContextPlan(
                turn_summary="Patient asks about pain.",
                ranked_source_ids=("episode:summary",),
                node_hints={"consultant": ("episode:summary",)},
                unresolved_questions=(),
                stale_source_ids=(),
                summary_update_needed=False,
                summary_notes=("not", "a", "string"),  # type: ignore[arg-type]
            )


class NodeContextPacketTests(unittest.TestCase):
    def test_prompt_text_renders_section_headings_and_source_ids(self):
        packet = NodeContextPacket(
            node_id="consultant",
            sections={
                "Episode Memory": "Source: episode:summary\nPain on stairs.",
                "Patient Memory": "Source: patient:goals\nWants to return to hiking.",
            },
            included_source_ids=("episode:summary", "patient:goals"),
            omitted_source_ids=("session:raw-transcript",),
            estimated_tokens=42,
        )

        prompt_text = packet.to_prompt_text()

        self.assertIn("## Episode Memory", prompt_text)
        self.assertIn("## Patient Memory", prompt_text)
        self.assertIn("episode:summary", prompt_text)
        self.assertIn("patient:goals", prompt_text)
        self.assertIn("Included sources: episode:summary, patient:goals", prompt_text)
        self.assertIsInstance(packet.sections["Episode Memory"], str)
        with self.assertRaises(TypeError):
            packet.sections["Session"] = "mutated"  # type: ignore[index]

    def test_packet_sections_must_be_strings(self):
        with self.assertRaisesRegex(TypeError, "section content must be strings"):
            NodeContextPacket(
                node_id="consultant",
                sections={"Episode Memory": ("Pain on stairs.",)},  # type: ignore[dict-item]
                included_source_ids=("episode:summary",),
                omitted_source_ids=(),
                estimated_tokens=12,
            )

        with self.assertRaisesRegex(TypeError, "section names must be strings"):
            NodeContextPacket(
                node_id="consultant",
                sections={1: "Pain on stairs."},  # type: ignore[dict-item]
                included_source_ids=("episode:summary",),
                omitted_source_ids=(),
                estimated_tokens=12,
            )


class ContextAssemblyResultTests(unittest.TestCase):
    def test_result_keeps_plan_packets_and_metadata(self):
        plan = ContextPlan(
            turn_summary="Patient wants plan progression.",
            ranked_source_ids=("patient:goals",),
            node_hints={"consultant": ("patient:goals",)},
            unresolved_questions=(),
            stale_source_ids=(),
            summary_update_needed=False,
            summary_notes="No compaction needed.",
        )
        packet = NodeContextPacket(
            node_id="consultant",
            sections={"Patient Memory": "Source: patient:goals\nReturn to hiking."},
            included_source_ids=("patient:goals",),
            omitted_source_ids=(),
            estimated_tokens=16,
        )

        result = ContextAssemblyResult(
            plan=plan,
            packets={"consultant": packet},
            assembly_metadata={"budget_tokens": 800},
        )

        self.assertIs(result.plan, plan)
        self.assertEqual(result.packets["consultant"], packet)
        self.assertEqual(result.assembly_metadata["budget_tokens"], 800)

        with self.assertRaises(FrozenInstanceError):
            result.plan = plan  # type: ignore[misc]
        with self.assertRaises(TypeError):
            result.packets["router"] = packet  # type: ignore[index]
        with self.assertRaises(TypeError):
            result.assembly_metadata["new"] = "value"  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
