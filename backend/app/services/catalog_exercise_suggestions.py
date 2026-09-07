"""Catalog-backed exercise suggestions from Exercise Catalog rows only.

Exercise Catalog rows are the only source of suggested exercises. Vector search
results may rank candidates, but payload exercise names or metadata are never
trusted as suggested exercise content.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.services.exercise_catalog import ExerciseCatalogEntry, get_exercise

CatalogExerciseSearcher = Callable[[str, int], list[Any]]

EMPTY_REASON_NO_MATCHES = "No matching catalog exercises were found."


@dataclass(frozen=True)
class CatalogExerciseSearchDocument:
    exercise_id: str
    document_type: str
    score: float | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class CatalogExerciseSuggestion:
    exercise_id: str
    title: str
    reason: str
    introduction: str
    structures_involved: list[str]
    related_conditions: list[str]
    video_url: str
    video_provider: str
    source_url: str
    score: float | None

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "exercise_id": self.exercise_id,
            "title": self.title,
            "score": self.score,
            "structures_involved": self.structures_involved,
            "related_conditions": self.related_conditions,
            "introduction": self.introduction,
        }


@dataclass(frozen=True)
class CatalogExerciseSuggestionResult:
    suggestions: list[CatalogExerciseSuggestion]
    empty_reason: str | None


class CatalogExerciseSuggestionError(RuntimeError):
    pass


class CatalogExerciseSearchUnavailable(CatalogExerciseSuggestionError):
    pass


def _clean_text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _optional_score(raw_score: Any) -> tuple[bool, float | None]:
    if raw_score is None:
        return True, None
    try:
        return True, float(raw_score)
    except (TypeError, ValueError):
        return False, None


def _payload_from_raw(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        payload = raw.get("payload") or {}
    else:
        payload = getattr(raw, "payload", {}) or {}
    return payload if isinstance(payload, dict) else None


def _raw_value(raw: object, key: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)


def _payload_exercise_id(payload: dict[str, Any], raw: object) -> str:
    return _clean_text(payload.get("exercise_id") or payload.get("id") or _raw_value(raw, "id"))


def parse_search_document(raw: object) -> CatalogExerciseSearchDocument | None:
    payload = _payload_from_raw(raw)
    if payload is None:
        return None

    score_valid, score = _optional_score(_raw_value(raw, "score"))
    if not score_valid:
        return None

    return CatalogExerciseSearchDocument(
        exercise_id=_payload_exercise_id(payload, raw),
        document_type=_clean_text(payload.get("document_type")),
        score=score,
        payload=payload,
    )


def _search_with_qdrant(query_text: str, limit: int) -> list[Any]:
    from app.vector import QdrantKnowledgeStore

    store = QdrantKnowledgeStore()
    try:
        return store.search(query_text, limit=limit)
    finally:
        store.close()


def _suggestion_from_entry(entry: ExerciseCatalogEntry, *, score: float | None) -> CatalogExerciseSuggestion:
    return CatalogExerciseSuggestion(
        exercise_id=entry.exercise_id,
        title=entry.title,
        reason=f"Matched the saved clinical context for {entry.title}.",
        introduction=entry.introduction,
        structures_involved=entry.structures_involved,
        related_conditions=entry.related_conditions,
        video_url=entry.video_url,
        video_provider=entry.video_provider,
        source_url=entry.source_url,
        score=score,
    )


def _empty_result() -> CatalogExerciseSuggestionResult:
    return CatalogExerciseSuggestionResult(suggestions=[], empty_reason=EMPTY_REASON_NO_MATCHES)


def suggest_catalog_exercises(
    db: object,
    query_text: str,
    limit: int = 5,
    searcher: CatalogExerciseSearcher | None = None,
) -> CatalogExerciseSuggestionResult:
    """Return rank-ordered suggestions backed by existing Exercise Catalog rows.

    ``db`` is accepted to keep graph and recommendation call sites aligned with
    the future DB-backed catalog boundary. The current catalog implementation
    validates through ``exercise_catalog.get_exercise``.
    """
    _ = db
    if limit <= 0:
        return _empty_result()

    active_searcher = searcher or _search_with_qdrant
    try:
        raw_documents = active_searcher(query_text, limit)
    except Exception as exc:
        raise CatalogExerciseSearchUnavailable(
            f"Catalog exercise search was unavailable: {exc}"
        ) from exc

    seen: set[str] = set()
    suggestions: list[CatalogExerciseSuggestion] = []
    for raw_document in raw_documents if isinstance(raw_documents, list) else []:
        document = parse_search_document(raw_document)
        if document is None:
            continue
        if document.document_type != "rehab_exercise":
            continue
        if not document.exercise_id or document.exercise_id in seen:
            continue

        try:
            entry = get_exercise(document.exercise_id)
        except Exception:
            return _empty_result()
        if entry is None:
            continue

        seen.add(entry.exercise_id)
        suggestions.append(_suggestion_from_entry(entry, score=document.score))
        if len(suggestions) >= limit:
            break

    if not suggestions:
        return _empty_result()
    return CatalogExerciseSuggestionResult(suggestions=suggestions, empty_reason=None)
