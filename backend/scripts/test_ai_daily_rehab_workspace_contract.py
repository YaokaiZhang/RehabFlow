
from __future__ import annotations

import sys
import time
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import (
    AIDailyRehabList,
    AIDailyRehabRecommendation,
    CareEpisode,
    CareEpisodeTriageSummary,
    EpisodeRehabSession,
    Patient,
)
from app.db.session import SessionLocal, init_db
from app.services.ai_daily_rehab_workspace import (
    AiDailyRehabListNotFound,
    AiDailyRehabSafetyGateBlocked,
    get_ai_daily_rehab_recommendation,
    load_ai_daily_rehab_workspace,
    start_ai_daily_rehab_session,
    summarize_ai_daily_rehab_session,
    update_ai_daily_rehab_list,
    update_ai_daily_rehab_session_checklist,
)
from app.services.exercise_catalog import load_exercise_catalog
from app.services.rehab_recommendations import InvalidRehabExerciseIds


def _patient(db, suffix: str) -> Patient:
    patient = Patient(patient_name=f"workspace_patient_{suffix}", password_hash="test")
    db.add(patient)
    db.flush()
    return patient


def _episode(db, patient: Patient, *, safety_gate_status: str = "triage_complete") -> CareEpisode:
    episode = CareEpisode(
        patient_id=patient.patient_id,
        issue_title="Ankle sprain",
        body_area="Ankle",
        goal="Walk normally",
        short_description="Rolled ankle last week.",
        safety_gate_status=safety_gate_status,
    )
    db.add(episode)
    db.flush()
    return episode


def _summary(db, patient: Patient, episode: CareEpisode) -> CareEpisodeTriageSummary:
    summary = CareEpisodeTriageSummary(
        care_episode_id=episode.care_episode_id,
        patient_id=patient.patient_id,
        version=1,
        concern="Mild ankle pain after a sprain.",
        relevant_context="Can bear weight and wants gentle daily rehab.",
        safety_signals=[],
        limitations=["Avoid sharp pain."],
        recommendation="Start with low intensity mobility and strength work.",
        missing_information=[],
        unresolved_questions=[],
        clinician_review_needed=False,
        additional_context="",
    )
    db.add(summary)
    db.flush()
    episode.latest_triage_summary = {
        "triage_summary_id": str(summary.triage_summary_id),
        "clinician_review_needed": False,
    }
    db.flush()
    return summary


def _recommendation(db, patient: Patient, episode: CareEpisode, summary: CareEpisodeTriageSummary, exercise_ids: list[str]) -> AIDailyRehabRecommendation:
    items = [
        {
            "exercise_id": exercise_id,
            "title": f"Exercise {index + 1}",
            "reason": "Matched saved Triage Summary context.",
            "dosage": "Start gently.",
            "structures_involved": [],
            "related_conditions": [],
            "video_url": "",
            "video_provider": "",
            "source_url": "",
            "source": "recommendation",
            "score": 0.9,
        }
        for index, exercise_id in enumerate(exercise_ids)
    ]
    recommendation = AIDailyRehabRecommendation(
        care_episode_id=episode.care_episode_id,
        patient_id=patient.patient_id,
        source_triage_summary_id=summary.triage_summary_id,
        source_triage_summary_version=summary.version,
        status="ready",
        empty_reason="",
        query_text="ankle rehab",
        items=items,
        active=True,
    )
    db.add(recommendation)
    db.flush()
    return recommendation


def _list(db, patient: Patient, episode: CareEpisode, recommendation: AIDailyRehabRecommendation, exercise_ids: list[str]) -> AIDailyRehabList:
    catalog_by_id = {entry.exercise_id: entry for entry in load_exercise_catalog()}
    rehab_list = AIDailyRehabList(
        care_episode_id=episode.care_episode_id,
        patient_id=patient.patient_id,
        source_recommendation_id=recommendation.recommendation_id,
        items=[
            {
                "exercise_id": exercise_id,
                "label": catalog_by_id[exercise_id].title,
                "snapshot": catalog_by_id[exercise_id].to_response(),
            }
            for exercise_id in exercise_ids
        ],
    )
    db.add(rehab_list)
    db.flush()
    return rehab_list


def _workspace_rows(db, suffix: str, *, recommendation_ids: list[str], list_ids: list[str] | None = None):
    patient = _patient(db, suffix)
    episode = _episode(db, patient)
    summary = _summary(db, patient, episode)
    recommendation = _recommendation(db, patient, episode, summary, recommendation_ids)
    rehab_list = None
    if list_ids is not None:
        rehab_list = _list(db, patient, episode, recommendation, list_ids)
    db.commit()
    return patient, episode, summary, recommendation, rehab_list


def test_load_workspace_requires_saved_triage_summary() -> None:
    db = SessionLocal()
    try:
        patient = _patient(db, f"blocked_{time.time_ns()}")
        episode = _episode(db, patient, safety_gate_status="triage_complete")
        db.commit()
        try:
            load_ai_daily_rehab_workspace(db, patient.patient_id, episode.care_episode_id)
            raise AssertionError("expected saved-summary safety gate to block workspace loading")
        except AiDailyRehabSafetyGateBlocked:
            pass
    finally:
        db.close()



def test_recommendation_read_requires_saved_triage_summary() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient = _patient(db, f"rec_gate_{time.time_ns()}")
        episode = _episode(db, patient, safety_gate_status="triage_complete")
        recommendation = AIDailyRehabRecommendation(
            care_episode_id=episode.care_episode_id,
            patient_id=patient.patient_id,
            source_triage_summary_id=None,
            source_triage_summary_version=0,
            status="ready",
            empty_reason="",
            query_text="stale recommendation",
            items=[{"exercise_id": catalog[0].exercise_id, "title": catalog[0].title}],
            active=True,
        )
        db.add(recommendation)
        db.commit()
        try:
            get_ai_daily_rehab_recommendation(db, patient.patient_id, episode.care_episode_id)
            raise AssertionError("expected recommendation read to require saved Triage Summary context")
        except AiDailyRehabSafetyGateBlocked:
            pass
    finally:
        db.close()


def test_invalid_update_does_not_create_empty_daily_list() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient, episode, _, _, _ = _workspace_rows(
            db,
            f"no_list_{time.time_ns()}",
            recommendation_ids=[catalog[0].exercise_id],
            list_ids=None,
        )
        try:
            update_ai_daily_rehab_list(db, patient.patient_id, episode.care_episode_id, [catalog[1].exercise_id])
            raise AssertionError("expected non-recommended exercise id to be rejected")
        except InvalidRehabExerciseIds:
            pass
        rehab_list = db.execute(select(AIDailyRehabList).where(AIDailyRehabList.care_episode_id == episode.care_episode_id)).scalars().one_or_none()
        assert rehab_list is None
    finally:
        db.close()


def test_invalid_update_does_not_mutate_existing_daily_list() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient, episode, _, _, rehab_list = _workspace_rows(
            db,
            f"keep_list_{time.time_ns()}",
            recommendation_ids=[catalog[0].exercise_id],
            list_ids=[catalog[0].exercise_id],
        )
        try:
            update_ai_daily_rehab_list(db, patient.patient_id, episode.care_episode_id, [catalog[1].exercise_id])
            raise AssertionError("expected non-recommended exercise id to be rejected")
        except InvalidRehabExerciseIds:
            pass
        db.refresh(rehab_list)
        assert [item["exercise_id"] for item in rehab_list.items] == [catalog[0].exercise_id]
    finally:
        db.close()


def test_start_session_rejects_saved_list_items_missing_from_current_recommendation() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient, episode, summary, old_recommendation, rehab_list = _workspace_rows(
            db,
            f"stale_list_{time.time_ns()}",
            recommendation_ids=[catalog[0].exercise_id],
            list_ids=[catalog[0].exercise_id],
        )
        old_recommendation.active = False
        _recommendation(db, patient, episode, summary, [catalog[1].exercise_id])
        db.commit()
        try:
            start_ai_daily_rehab_session(db, patient.patient_id, episode.care_episode_id, rehab_list.list_id)
            raise AssertionError("expected stale saved list exercise to block session start")
        except AiDailyRehabListNotFound:
            pass
    finally:
        db.close()

def test_load_workspace_returns_recommendation_and_daily_list_for_saved_summary() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient, episode, _, recommendation, rehab_list = _workspace_rows(
            db,
            f"load_{time.time_ns()}",
            recommendation_ids=[catalog[0].exercise_id, catalog[1].exercise_id],
            list_ids=[catalog[0].exercise_id],
        )
        workspace = load_ai_daily_rehab_workspace(db, patient.patient_id, episode.care_episode_id)
        assert workspace.recommendation.recommendation_id == recommendation.recommendation_id
        assert workspace.rehab_list.list_id == rehab_list.list_id
        assert [item["exercise_id"] for item in workspace.rehab_list.items] == [catalog[0].exercise_id]
    finally:
        db.close()


def test_update_daily_list_rejects_exercise_ids_not_in_recommendation() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient, episode, _, _, _ = _workspace_rows(
            db,
            f"reject_{time.time_ns()}",
            recommendation_ids=[catalog[0].exercise_id],
            list_ids=[],
        )
        try:
            update_ai_daily_rehab_list(db, patient.patient_id, episode.care_episode_id, [catalog[1].exercise_id])
            raise AssertionError("expected non-recommended exercise id to be rejected")
        except InvalidRehabExerciseIds as exc:
            assert exc.exercise_ids == [catalog[1].exercise_id]
    finally:
        db.close()


def test_start_session_from_daily_list_copies_selected_catalog_exercises() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient, episode, _, _, rehab_list = _workspace_rows(
            db,
            f"session_{time.time_ns()}",
            recommendation_ids=[catalog[0].exercise_id, catalog[1].exercise_id],
            list_ids=[catalog[0].exercise_id, catalog[1].exercise_id],
        )
        session = start_ai_daily_rehab_session(db, patient.patient_id, episode.care_episode_id, rehab_list.list_id)
        assert [item["exercise_id"] for item in session.recommended_exercises] == [catalog[0].exercise_id, catalog[1].exercise_id]
        assert [item["exercise_id"] for item in session.checklist] == [catalog[0].exercise_id, catalog[1].exercise_id]
        assert session.checklist[0]["snapshot"]["exercise_id"] == catalog[0].exercise_id
        assert session.completion_status == "in_progress"
    finally:
        db.close()


def test_update_session_checklist_preserves_existing_session_exercise_order() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient, episode, _, _, rehab_list = _workspace_rows(
            db,
            f"checklist_{time.time_ns()}",
            recommendation_ids=[catalog[0].exercise_id, catalog[1].exercise_id],
            list_ids=[catalog[0].exercise_id, catalog[1].exercise_id],
        )
        session = start_ai_daily_rehab_session(db, patient.patient_id, episode.care_episode_id, rehab_list.list_id)
        updated = update_ai_daily_rehab_session_checklist(
            db,
            patient.patient_id,
            episode.care_episode_id,
            session.session_id,
            completed_items=[catalog[1].exercise_id],
            patient_notes="Second item felt smooth.",
        )
        assert [item["exercise_id"] for item in updated.checklist] == [catalog[0].exercise_id, catalog[1].exercise_id]
        assert [item["completed"] for item in updated.checklist] == [False, True]
        assert updated.patient_notes == "Second item felt smooth."
    finally:
        db.close()


def test_summarize_session_regenerates_after_notes_and_checklist_change() -> None:
    db = SessionLocal()
    try:
        catalog = list(load_exercise_catalog())
        patient, episode, _, _, rehab_list = _workspace_rows(
            db,
            f"summary_{time.time_ns()}",
            recommendation_ids=[catalog[0].exercise_id, catalog[1].exercise_id],
            list_ids=[catalog[0].exercise_id, catalog[1].exercise_id],
        )
        session = start_ai_daily_rehab_session(db, patient.patient_id, episode.care_episode_id, rehab_list.list_id)
        first = summarize_ai_daily_rehab_session(db, patient.patient_id, episode.care_episode_id, session.session_id)
        first_summary = first.session_summary

        update_ai_daily_rehab_session_checklist(
            db,
            patient_id=patient.patient_id,
            episode_id=episode.care_episode_id,
            session_id=session.session_id,
            completed_items=[catalog[0].exercise_id],
            patient_notes="The first movement felt comfortable.",
        )
        second = summarize_ai_daily_rehab_session(db, patient.patient_id, episode.care_episode_id, session.session_id)

        assert second.session_summary != first_summary
        assert f"Completed: {catalog[0].title}." in second.session_summary
        assert f"Remaining: {catalog[1].title}." in second.session_summary
        assert "Patient notes: The first movement felt comfortable." in second.session_summary
        assert second.completion_status == "summarized"
    finally:
        db.close()


def main() -> None:
    init_db()
    test_load_workspace_requires_saved_triage_summary()
    test_recommendation_read_requires_saved_triage_summary()
    test_load_workspace_returns_recommendation_and_daily_list_for_saved_summary()
    test_update_daily_list_rejects_exercise_ids_not_in_recommendation()
    test_invalid_update_does_not_create_empty_daily_list()
    test_invalid_update_does_not_mutate_existing_daily_list()
    test_start_session_from_daily_list_copies_selected_catalog_exercises()
    test_start_session_rejects_saved_list_items_missing_from_current_recommendation()
    test_update_session_checklist_preserves_existing_session_exercise_order()
    test_summarize_session_regenerates_after_notes_and_checklist_change()
    print("ai daily rehab workspace contract ok")


if __name__ == "__main__":
    main()
