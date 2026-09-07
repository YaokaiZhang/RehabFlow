from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AIDailyRehabList, AIDailyRehabRecommendation, CareEpisode, CareEpisodeTriageSummary
from app.services.catalog_exercise_suggestions import (
	CatalogExerciseSearchUnavailable,
	CatalogExerciseSuggestion,
	suggest_catalog_exercises,
)
from app.services.exercise_catalog import ExerciseCatalogEntry, get_exercise

RecommendationSearcher = Callable[[str, int], list[dict[str, Any]]]

DEFAULT_RECOMMENDATION_LIMIT = 8
_DEFAULT_EMPTY_REASON = "No database-backed exercise recommendation was available for the saved Triage Summary."
_BUILD_ERROR_REASON = "Recommendation retrieval results could not be processed, so no database-backed exercise matches were created."
_TEST_SEARCHER: RecommendationSearcher | None = None


def set_rehab_recommendation_searcher_for_tests(searcher: RecommendationSearcher | None) -> None:
	global _TEST_SEARCHER
	_TEST_SEARCHER = searcher


def build_recommendation_query(*, episode: CareEpisode, summary: CareEpisodeTriageSummary) -> str:
	parts = [
		("Concern", summary.concern),
		("Relevant context", summary.relevant_context),
		("Limitations", " ".join(str(item) for item in summary.limitations if item)),
		("Episode body area", episode.body_area),
		("Episode goal", episode.goal),
		("Episode description", episode.short_description),
		("Recommendation", summary.recommendation),
	]
	return "\n".join(f"{label}: {' '.join(str(value).split())}" for label, value in parts if str(value or "").strip())


def _dosage_for_suggestion(suggestion: CatalogExerciseSuggestion) -> str:
	structure_hint = suggestion.structures_involved[0] if suggestion.structures_involved else "the target area"
	return f"Start with 1-2 gentle sets focused on {structure_hint}."


def _item_from_suggestion(suggestion: CatalogExerciseSuggestion) -> dict[str, Any]:
	return {
		"exercise_id": suggestion.exercise_id,
		"title": suggestion.title,
		"reason": f"Matched the saved Triage Summary context for {suggestion.title}.",
		"dosage": _dosage_for_suggestion(suggestion),
		"structures_involved": suggestion.structures_involved,
		"related_conditions": suggestion.related_conditions,
		"video_url": suggestion.video_url,
		"video_provider": suggestion.video_provider,
		"source_url": suggestion.source_url,
		"source": "recommendation",
		"score": suggestion.score,
	}


def _catalog_backed_items(query_text: str, *, limit: int = DEFAULT_RECOMMENDATION_LIMIT) -> tuple[list[dict[str, Any]], str]:
	try:
		result = suggest_catalog_exercises(None, query_text, limit=limit, searcher=_TEST_SEARCHER)
	except CatalogExerciseSearchUnavailable as exc:
		return [], f"Vector retrieval was unavailable, so no database-backed exercise matches were created: {exc}"
	return [_item_from_suggestion(suggestion) for suggestion in result.suggestions], result.empty_reason or ""


def create_rehab_recommendation_for_summary(
	db: Session,
	*,
	episode: CareEpisode,
	summary: CareEpisodeTriageSummary,
) -> AIDailyRehabRecommendation:
	query_text = build_recommendation_query(episode=episode, summary=summary)
	try:
		items, retrieval_error = _catalog_backed_items(query_text)
	except Exception as exc:
		items = []
		retrieval_error = f"{_BUILD_ERROR_REASON} {exc}"
	status = "ready" if items else "empty"
	empty_reason = "" if items else retrieval_error or _DEFAULT_EMPTY_REASON

	locked_episode = db.execute(
		select(CareEpisode)
		.where(CareEpisode.care_episode_id == episode.care_episode_id)
		.with_for_update()
	).scalars().one_or_none()
	if locked_episode is not None:
		episode = locked_episode

	active_recommendations = db.execute(
		select(AIDailyRehabRecommendation).where(
			AIDailyRehabRecommendation.care_episode_id == episode.care_episode_id,
			AIDailyRehabRecommendation.active == True,  # noqa: E712
		)
	).scalars().all()
	for recommendation in active_recommendations:
		recommendation.active = False

	recommendation = AIDailyRehabRecommendation(
		care_episode_id=episode.care_episode_id,
		patient_id=episode.patient_id,
		source_triage_summary_id=summary.triage_summary_id,
		source_triage_summary_version=summary.version,
		status=status,
		empty_reason=empty_reason,
		query_text=query_text,
		items=items,
		active=True,
	)
	db.add(recommendation)
	db.flush()
	return recommendation

def latest_active_recommendation(db: Session, *, episode: CareEpisode) -> AIDailyRehabRecommendation | None:
    return db.execute(
        select(AIDailyRehabRecommendation)
        .where(
            AIDailyRehabRecommendation.care_episode_id == episode.care_episode_id,
            AIDailyRehabRecommendation.active == True,  # noqa: E712
        )
        .order_by(AIDailyRehabRecommendation.created_at.desc())
    ).scalars().first()


class InvalidRehabExerciseIds(ValueError):
    def __init__(self, exercise_ids: list[str]) -> None:
        self.exercise_ids = exercise_ids
        super().__init__(f"Invalid exercise IDs: {', '.join(exercise_ids)}")


def _rehab_list_item_from_entry(entry: ExerciseCatalogEntry) -> dict[str, Any]:
    return {
        "exercise_id": entry.exercise_id,
        "label": entry.title,
        "snapshot": entry.to_response(),
    }


def get_or_create_rehab_list(db: Session, *, episode: CareEpisode) -> AIDailyRehabList:
    locked_episode = db.execute(
        select(CareEpisode)
        .where(CareEpisode.care_episode_id == episode.care_episode_id)
        .with_for_update()
    ).scalars().one_or_none()
    if locked_episode is not None:
        episode = locked_episode

    rehab_list = db.execute(
        select(AIDailyRehabList).where(AIDailyRehabList.care_episode_id == episode.care_episode_id)
    ).scalars().one_or_none()
    if rehab_list is not None:
        return rehab_list

    recommendation = latest_active_recommendation(db, episode=episode)
    rehab_list = AIDailyRehabList(
        care_episode_id=episode.care_episode_id,
        patient_id=episode.patient_id,
        source_recommendation_id=recommendation.recommendation_id if recommendation is not None else None,
        items=[],
    )
    db.add(rehab_list)
    db.flush()
    return rehab_list


def update_rehab_list_items(
    db: Session,
    *,
    rehab_list: AIDailyRehabList,
    exercise_ids: list[str],
) -> AIDailyRehabList:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    invalid_exercise_ids: list[str] = []
    for raw_exercise_id in exercise_ids:
        exercise_id = str(raw_exercise_id or "").strip()
        if not exercise_id:
            invalid_exercise_ids.append(exercise_id)
            continue
        if exercise_id in seen:
            continue
        entry = get_exercise(exercise_id)
        if entry is None:
            invalid_exercise_ids.append(exercise_id)
            continue
        seen.add(entry.exercise_id)
        items.append(_rehab_list_item_from_entry(entry))
    if invalid_exercise_ids:
        raise InvalidRehabExerciseIds(invalid_exercise_ids)
    rehab_list.items = items
    db.flush()
    return rehab_list
