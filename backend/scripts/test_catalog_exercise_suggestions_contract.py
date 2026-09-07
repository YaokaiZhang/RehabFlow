#!/usr/bin/env python3
"""Service contract for catalog-backed exercise suggestions."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import catalog_exercise_suggestions as catalog_service
from app.services.catalog_exercise_suggestions import suggest_catalog_exercises
from app.services.exercise_catalog import ExerciseCatalogEntry, load_exercise_catalog


def _catalog_entries() -> list[ExerciseCatalogEntry]:
	entries = list(load_exercise_catalog())
	assert len(entries) >= 3
	return entries[:3]


def _search_doc(
	exercise_id: str,
	*,
	title: str = "Vector title should not be trusted",
	document_type: str | None = "rehab_exercise",
	score: Any = 0.91,
) -> dict[str, Any]:
	payload: dict[str, Any] = {
		"exercise_id": exercise_id,
		"title": title,
		"introduction": "Vector introduction should not be trusted.",
		"structures_involved": ["Vector structure"],
		"related_conditions": ["Vector condition"],
	}
	if document_type is not None:
		payload["document_type"] = document_type
	return {"score": score, "payload": payload}


def test_suggest_catalog_exercises_filters_to_rehab_exercise_documents() -> None:
	first, second, _ = _catalog_entries()

	def fake_searcher(query_text: str, limit: int) -> list[dict[str, Any]]:
		assert query_text == "ankle mobility"
		assert limit == 5
		return [
			_search_doc(first.exercise_id, document_type="rehab_article", score=0.99),
			_search_doc(second.exercise_id, document_type="rehab_exercise", score=0.88),
		]

	result = suggest_catalog_exercises(None, "ankle mobility", searcher=fake_searcher)
	assert result.empty_reason is None
	assert [suggestion.exercise_id for suggestion in result.suggestions] == [second.exercise_id]


def test_suggest_catalog_exercises_validates_ids_against_exercise_catalog() -> None:
	first, _, _ = _catalog_entries()

	def fake_searcher(query_text: str, limit: int) -> list[dict[str, Any]]:
		return [
			_search_doc("missing-catalog-id", score=0.99),
			_search_doc(first.exercise_id, score=0.9),
		]

	result = suggest_catalog_exercises(None, "ankle mobility", searcher=fake_searcher)
	assert result.empty_reason is None
	assert [suggestion.exercise_id for suggestion in result.suggestions] == [first.exercise_id]


def test_suggest_catalog_exercises_dedupes_while_preserving_rank_order() -> None:
	first, second, third = _catalog_entries()

	def fake_searcher(query_text: str, limit: int) -> list[dict[str, Any]]:
		return [
			_search_doc(second.exercise_id, score=0.91),
			_search_doc(first.exercise_id, score=0.89),
			_search_doc(second.exercise_id, score=0.97),
			_search_doc(third.exercise_id, score=0.86),
		]

	result = suggest_catalog_exercises(None, "ankle mobility", limit=3, searcher=fake_searcher)
	assert result.empty_reason is None
	assert [suggestion.exercise_id for suggestion in result.suggestions] == [
		second.exercise_id,
		first.exercise_id,
		third.exercise_id,
	]


def test_suggest_catalog_exercises_returns_empty_reason_when_no_catalog_matches() -> None:
	def fake_searcher(query_text: str, limit: int) -> list[Any]:
		return [
			_search_doc("missing-catalog-id", score=0.91),
			{"score": 0.9, "payload": {"document_type": "rehab_article", "exercise_id": "also-missing"}},
			{"score": "not-a-number", "payload": {"document_type": "rehab_exercise", "exercise_id": "bad-score"}},
		]

	result = suggest_catalog_exercises(None, "ankle mobility", searcher=fake_searcher)
	assert result.suggestions == []
	assert result.empty_reason == "No matching catalog exercises were found."


def test_suggest_catalog_exercises_returns_empty_when_limit_is_not_positive() -> None:
	def fake_searcher(query_text: str, limit: int) -> list[dict[str, Any]]:
		raise AssertionError("searcher should not be called when limit is not positive")

	for limit in (0, -1):
		result = suggest_catalog_exercises(None, "ankle mobility", limit=limit, searcher=fake_searcher)
		assert result.suggestions == []
		assert result.empty_reason == "No matching catalog exercises were found."


def test_suggest_catalog_exercises_fails_closed_when_catalog_lookup_raises() -> None:
	first, _, _ = _catalog_entries()

	def fake_searcher(query_text: str, limit: int) -> list[dict[str, Any]]:
		return [_search_doc(first.exercise_id, score=0.93)]

	with patch.object(catalog_service, "get_exercise", side_effect=RuntimeError("catalog malformed")):
		result = suggest_catalog_exercises(None, "ankle mobility", searcher=fake_searcher)

	assert result.suggestions == []
	assert result.empty_reason == "No matching catalog exercises were found."


def test_suggestion_payload_contains_catalog_id_title_and_patient_safe_rationale() -> None:
	first, _, _ = _catalog_entries()

	def fake_searcher(query_text: str, limit: int) -> list[dict[str, Any]]:
		return [_search_doc(first.exercise_id, title="Untrusted vector title", score=0.94)]

	result = suggest_catalog_exercises(None, "ankle mobility", searcher=fake_searcher)
	assert result.empty_reason is None
	assert len(result.suggestions) == 1
	suggestion = result.suggestions[0]
	assert suggestion.exercise_id == first.exercise_id
	assert suggestion.title == first.title
	assert suggestion.reason == f"Matched the saved clinical context for {first.title}."
	assert suggestion.introduction == first.introduction
	assert suggestion.structures_involved == first.structures_involved
	assert suggestion.related_conditions == first.related_conditions
	assert suggestion.score == 0.94


def main() -> None:
	test_suggest_catalog_exercises_filters_to_rehab_exercise_documents()
	test_suggest_catalog_exercises_validates_ids_against_exercise_catalog()
	test_suggest_catalog_exercises_dedupes_while_preserving_rank_order()
	test_suggest_catalog_exercises_returns_empty_reason_when_no_catalog_matches()
	test_suggest_catalog_exercises_returns_empty_when_limit_is_not_positive()
	test_suggest_catalog_exercises_fails_closed_when_catalog_lookup_raises()
	test_suggestion_payload_contains_catalog_id_title_and_patient_safe_rationale()
	print("catalog exercise suggestions contract ok")


if __name__ == "__main__":
	main()
