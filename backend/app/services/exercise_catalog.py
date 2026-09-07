from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings


CATALOG_FILENAME = "rehab_exercises.catalog.jsonl"


@dataclass(frozen=True)
class ExerciseCatalogEntry:
	exercise_id: str
	slug: str
	title: str
	introduction: str
	structures_involved: list[str]
	related_conditions: list[str]
	source_url: str
	video_url: str
	video_provider: str
	video_id: str

	@property
	def video_available(self) -> bool:
		return bool(self.video_url)

	def to_response(self) -> dict[str, Any]:
		return {
			"exercise_id": self.exercise_id,
			"slug": self.slug,
			"title": self.title,
			"introduction": self.introduction,
			"structures_involved": self.structures_involved,
			"related_conditions": self.related_conditions,
			"source_url": self.source_url,
			"video_url": self.video_url,
			"video_provider": self.video_provider,
			"video_id": self.video_id,
			"video_available": self.video_available,
		}


def _clean_text(value: Any) -> str:
	return "" if value is None else " ".join(str(value).split())


def _clean_list(value: Any) -> list[str]:
	if not isinstance(value, list):
		return []

	seen: set[str] = set()
	items: list[str] = []
	for raw_item in value:
		item = _clean_text(raw_item)
		key = item.casefold()
		if item and key not in seen:
			seen.add(key)
			items.append(item)
	return items


def _catalog_path() -> Path:
	configured_path = get_settings().exercise_catalog_path.strip()
	if configured_path:
		return Path(configured_path).expanduser()

	app_dir = Path(__file__).resolve().parents[1]
	return app_dir / "resources" / CATALOG_FILENAME


def _entry_from_row(row: dict[str, Any]) -> ExerciseCatalogEntry:
	return ExerciseCatalogEntry(
		exercise_id=_clean_text(row.get("exercise_id")),
		slug=_clean_text(row.get("slug")),
		title=_clean_text(row.get("title")),
		introduction=_clean_text(row.get("introduction")),
		structures_involved=_clean_list(row.get("structures_involved")),
		related_conditions=_clean_list(row.get("related_conditions")),
		source_url=_clean_text(row.get("source_url")),
		video_url=_clean_text(row.get("video_url")),
		video_provider=_clean_text(row.get("video_provider")),
		video_id=_clean_text(row.get("video_id")),
	)


@lru_cache(maxsize=1)
def load_exercise_catalog() -> tuple[ExerciseCatalogEntry, ...]:
	path = _catalog_path()
	if not path.exists():
		raise RuntimeError(
			f"Exercise catalog file not found at {path}. Set EXERCISE_CATALOG_PATH or generate/copy "
			"backend/app/resources/rehab_exercises.catalog.jsonl."
		)

	entries: list[ExerciseCatalogEntry] = []
	with path.open("r", encoding="utf-8") as handle:
		for line_number, line in enumerate(handle, start=1):
			if not line.strip():
				continue
			row = json.loads(line)
			entry = _entry_from_row(row)
			if not entry.exercise_id:
				raise ValueError(f"Exercise catalog line {line_number} is missing exercise_id")
			entries.append(entry)
	return tuple(entries)


def get_exercise(exercise_id: str) -> ExerciseCatalogEntry | None:
	for exercise in load_exercise_catalog():
		if exercise.exercise_id == exercise_id:
			return exercise
	return None


def _contains_casefold(values: list[str], expected: str) -> bool:
	expected_key = expected.casefold()
	return any(expected_key in value.casefold() for value in values)


def _matches_structure(exercise: ExerciseCatalogEntry, structure: str) -> bool:
	structure_key = structure.casefold()
	return _contains_casefold(exercise.structures_involved, structure) or structure_key in " ".join(
		[exercise.slug, exercise.title, exercise.introduction]
	).casefold()


def _matches_query(exercise: ExerciseCatalogEntry, query: str) -> bool:
	query_key = query.casefold()
	search_text = " ".join(
		[
			exercise.exercise_id,
			exercise.slug,
			exercise.title,
			exercise.introduction,
			" ".join(exercise.structures_involved),
			" ".join(exercise.related_conditions),
		]
	).casefold()
	return query_key in search_text


def search_exercises(
	*,
	query: str | None,
	structure: str | None,
	condition: str | None,
	limit: int,
	offset: int,
) -> dict[str, Any]:
	results = list(load_exercise_catalog())
	clean_query = _clean_text(query)
	clean_structure = _clean_text(structure)
	clean_condition = _clean_text(condition)

	if clean_query:
		results = [exercise for exercise in results if _matches_query(exercise, clean_query)]
	if clean_structure:
		results = [exercise for exercise in results if _matches_structure(exercise, clean_structure)]
	if clean_condition:
		results = [exercise for exercise in results if _contains_casefold(exercise.related_conditions, clean_condition)]

	total = len(results)
	return {
		"total": total,
		"limit": limit,
		"offset": offset,
		"exercises": [exercise.to_response() for exercise in results[offset : offset + limit]],
	}


def get_facets() -> dict[str, list[str]]:
	structures: dict[str, str] = {}
	conditions: dict[str, str] = {}
	for exercise in load_exercise_catalog():
		for structure in exercise.structures_involved:
			structures.setdefault(structure.casefold(), structure)
		for condition in exercise.related_conditions:
			conditions.setdefault(condition.casefold(), condition)
	return {
		"structures": sorted(structures.values(), key=str.casefold),
		"conditions": sorted(conditions.values(), key=str.casefold),
	}
