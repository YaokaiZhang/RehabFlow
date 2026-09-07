from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.vector.qdrant_store import KnowledgeDocument

EXERCISE_NAMESPACE = uuid.UUID("f203bd2e-d5fb-4c68-8a0b-9b8510383914")


def _clean_text(value: Any) -> str:
	text = "" if value is None else str(value)
	return " ".join(text.split())


def _clean_list(values: Any) -> list[str]:
	if not isinstance(values, list):
		return []

	seen: set[str] = set()
	cleaned: list[str] = []
	for value in values:
		item = _clean_text(value)
		key = item.casefold()
		if item and key not in seen:
			seen.add(key)
			cleaned.append(item)
	return cleaned


def _slug_from_url_or_title(url: str, title: str) -> str:
	path = urlparse(url).path.rstrip("/")
	if path:
		slug = path.split("/")[-1]
	else:
		slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
	return slug or str(uuid.uuid5(EXERCISE_NAMESPACE, title))


def _youtube_id(video_url: str) -> str:
	parsed = urlparse(video_url)
	if parsed.netloc.endswith("youtu.be"):
		return parsed.path.strip("/")
	if "youtube.com" in parsed.netloc:
		if parsed.path.startswith("/embed/"):
			return parsed.path.split("/embed/", 1)[1].split("/", 1)[0]
		query_id = parse_qs(parsed.query).get("v", [""])[0]
		if query_id:
			return query_id
	return ""


@dataclass(frozen=True)
class RehabExercise:
	exercise_id: str
	slug: str
	title: str
	source_url: str
	video_url: str
	video_provider: str
	video_id: str
	introduction: str
	structures_involved: list[str]
	related_conditions: list[str]
	source_row_count: int

	@property
	def embedding_text(self) -> str:
		sections = [
			f"Exercise: {self.title}",
			f"Introduction: {self.introduction}",
			"Structures involved: " + ", ".join(self.structures_involved),
			"Related conditions: " + ", ".join(self.related_conditions),
		]
		return "\n".join(section for section in sections if not section.endswith(": "))

	def to_payload(self) -> dict[str, Any]:
		return {
			"document_type": "rehab_exercise",
			"exercise_id": self.exercise_id,
			"slug": self.slug,
			"title": self.title,
			"source_url": self.source_url,
			"video_url": self.video_url,
			"video_provider": self.video_provider,
			"video_id": self.video_id,
			"local_video_path": None,
			"video_download_status": "not_downloaded",
			"introduction": self.introduction,
			"structures_involved": self.structures_involved,
			"related_conditions": self.related_conditions,
			"source_row_count": self.source_row_count,
		}

	def to_document(self) -> KnowledgeDocument:
		return KnowledgeDocument(
			document_id=self.exercise_id,
			text=self.embedding_text,
			metadata=self.to_payload(),
		)


def load_rehab_exercises(jsonl_path: Path) -> list[RehabExercise]:
	grouped: dict[str, dict[str, Any]] = {}
	row_counts: dict[str, int] = {}

	with jsonl_path.open("r", encoding="utf-8") as handle:
		for line_number, line in enumerate(handle, start=1):
			if not line.strip():
				continue
			raw = json.loads(line)
			source_url = _clean_text(raw.get("url"))
			title = _clean_text(raw.get("title"))
			if not source_url and not title:
				raise ValueError(f"Line {line_number} is missing both url and title")

			key = source_url or title.casefold()
			existing = grouped.setdefault(
				key,
				{
					"url": source_url,
					"title": title,
					"video_url": _clean_text(raw.get("video_url")),
					"introduction": _clean_text(raw.get("introduction")),
					"structures_involved": [],
					"related_conditions": [],
				},
			)
			row_counts[key] = row_counts.get(key, 0) + 1

			for field in ("url", "title", "video_url", "introduction"):
				if not existing.get(field):
					existing[field] = _clean_text(raw.get(field))
			existing["structures_involved"] = _clean_list(
				[*existing["structures_involved"], *_clean_list(raw.get("structures_involved"))]
			)
			existing["related_conditions"] = _clean_list(
				[*existing["related_conditions"], *_clean_list(raw.get("related_conditions"))]
			)

	exercises: list[RehabExercise] = []
	for key, raw in grouped.items():
		source_url = raw["url"]
		title = raw["title"]
		slug = _slug_from_url_or_title(source_url, title)
		video_url = raw["video_url"]
		video_id = _youtube_id(video_url)
		exercise_id = str(uuid.uuid5(EXERCISE_NAMESPACE, source_url or title.casefold()))
		exercises.append(
			RehabExercise(
				exercise_id=exercise_id,
				slug=slug,
				title=title,
				source_url=source_url,
				video_url=video_url,
				video_provider="youtube" if video_id else "",
				video_id=video_id,
				introduction=raw["introduction"],
				structures_involved=raw["structures_involved"],
				related_conditions=raw["related_conditions"],
				source_row_count=row_counts[key],
			)
		)

	return sorted(exercises, key=lambda item: (item.title.casefold(), item.slug))


def load_rehab_exercise_documents(jsonl_path: Path) -> list[KnowledgeDocument]:
	return [exercise.to_document() for exercise in load_rehab_exercises(jsonl_path)]

#TODO: change hardcoded rehab exercise loading
