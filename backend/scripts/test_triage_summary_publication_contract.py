from __future__ import annotations

import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("REHAB_TRIAGE_SUMMARY_AGENT", "fallback")

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AIDailyRehabRecommendation, CareEpisode, CareEpisodeTriageSummary, CareEpisodeTriageSummaryDraft, Patient
from app.db.session import SessionLocal, verify_schema_revision
from app.schemas.care_episodes import TriageSummaryRequest
from app.services import triage_summary_publication


def _unique_name(prefix: str) -> str:
	return f"{prefix}_{time.time_ns()}"


def _create_patient(db: Session) -> Patient:
	patient = Patient(patient_name=_unique_name("publication_patient"), password_hash="test-hash", real_info={})
	db.add(patient)
	db.flush()
	return patient


def _create_episode(db: Session, patient: Patient) -> CareEpisode:
	episode = CareEpisode(
		patient_id=patient.patient_id,
		issue_title="Right shoulder stiffness",
		body_area="Right shoulder",
		goal="Reach overhead comfortably",
		short_description="Stiff after activity and wants a cautious rehab plan.",
		origin="manual",
		status="active",
		safety_gate_status="needs_triage",
	)
	db.add(episode)
	db.flush()
	return episode


def _candidate(episode: CareEpisode, version: int, additional_context: str = "") -> CareEpisodeTriageSummary:
	return CareEpisodeTriageSummary(
		care_episode_id=episode.care_episode_id,
		patient_id=episode.patient_id,
		version=version,
		concern=episode.issue_title,
		relevant_context=f"Episode context. Additional context: {additional_context or 'None provided.'}",
		safety_signals=["No explicit red-flag symptoms were captured in the available episode context."],
		limitations=["Generated from the available triage context."],
		recommendation="Use gentle, low-risk activity while monitoring symptoms.",
		missing_information=[],
		unresolved_questions=[],
		clinician_review_needed=False,
		additional_context=additional_context,
	)


def _summary_count(db: Session, episode: CareEpisode) -> int:
	return int(
		db.execute(
			select(func.count(CareEpisodeTriageSummary.triage_summary_id)).where(
				CareEpisodeTriageSummary.care_episode_id == episode.care_episode_id
			)
		).scalar_one()
	)


def _recommendation_count(db: Session, episode: CareEpisode) -> int:
	return int(
		db.execute(
			select(func.count(AIDailyRehabRecommendation.recommendation_id)).where(
				AIDailyRehabRecommendation.care_episode_id == episode.care_episode_id
			)
		).scalar_one()
	)


def _with_patched_builder(fake_builder: Callable) -> Callable[[], None]:
	original = triage_summary_publication.build_triage_summary
	triage_summary_publication.build_triage_summary = fake_builder

	def restore() -> None:
		triage_summary_publication.build_triage_summary = original

	return restore


def _with_patched_recommendation_writer(fake_writer: Callable) -> Callable[[], None]:
	original = triage_summary_publication.create_rehab_recommendation_for_summary
	triage_summary_publication.create_rehab_recommendation_for_summary = fake_writer

	def restore() -> None:
		triage_summary_publication.create_rehab_recommendation_for_summary = original

	return restore


def test_generate_triage_summary_draft_does_not_save_or_create_recommendation() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		db.commit()

		def fake_builder(episode: CareEpisode, additional_context: str, version: int, **kwargs) -> CareEpisodeTriageSummary:
			assert version == 0
			return _candidate(episode, version, additional_context)

		restore_builder = _with_patched_builder(fake_builder)
		try:
			result = triage_summary_publication.generate_triage_summary_draft(
				db,
				patient.patient_id,
				episode.care_episode_id,
				TriageSummaryRequest(additional_context="Pain is mild and no numbness."),
			)
		finally:
			restore_builder()

		assert result.draft is not None
		assert result.summary is None
		assert result.artifact["triage_summary_draft_id"] == str(result.draft.triage_summary_draft_id)
		assert result.artifact["saved"] is False
		assert _summary_count(db, episode) == 0
		assert _recommendation_count(db, episode) == 0
		db.refresh(episode)
		assert episode.safety_gate_status == "needs_triage"
		assert episode.latest_triage_summary is None


def test_publish_triage_summary_directly_saves_summary_and_creates_recommendation() -> None:
	recommendation_calls: list[UUID] = []
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		db.commit()

		def fake_builder(episode: CareEpisode, additional_context: str, version: int, **kwargs) -> CareEpisodeTriageSummary:
			return _candidate(episode, version, additional_context)

		def fake_writer(db: Session, *, episode: CareEpisode, summary: CareEpisodeTriageSummary) -> None:
			recommendation_calls.append(summary.triage_summary_id)

		restore_builder = _with_patched_builder(fake_builder)
		restore_writer = _with_patched_recommendation_writer(fake_writer)
		try:
			result = triage_summary_publication.publish_triage_summary_directly(
				db,
				patient.patient_id,
				episode.care_episode_id,
				TriageSummaryRequest(additional_context="Pain is mild, no numbness, no bruising."),
			)
		finally:
			restore_writer()
			restore_builder()

		assert result.summary is not None
		assert result.draft is None
		assert not hasattr(result, "is_selected")
		assert result.summary.version == 1
		assert _summary_count(db, episode) == 1
		assert recommendation_calls == [result.summary.triage_summary_id]
		db.refresh(episode)
		latest_saved = triage_summary_publication.latest_saved_triage_summary(db, episode.care_episode_id)
		assert latest_saved is not None
		assert latest_saved.triage_summary_id == result.summary.triage_summary_id
		assert episode.safety_gate_status == "triage_complete"
		assert episode.latest_triage_summary["triage_summary_id"] == str(result.summary.triage_summary_id)
		assert episode.latest_triage_summary["version"] == result.summary.version


def test_select_triage_summary_draft_replaces_selected_summary() -> None:
	recommendation_calls: list[UUID] = []
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		existing = _candidate(episode, 1, "Existing saved summary.")
		db.add(existing)
		db.flush()
		episode.latest_triage_summary = triage_summary_publication.triage_summary_artifact(existing)
		episode.safety_gate_status = "triage_complete"
		db.commit()

		def fake_builder(episode: CareEpisode, additional_context: str, version: int, **kwargs) -> CareEpisodeTriageSummary:
			return _candidate(episode, version, additional_context)

		def fake_writer(db: Session, *, episode: CareEpisode, summary: CareEpisodeTriageSummary) -> None:
			recommendation_calls.append(summary.triage_summary_id)

		restore_builder = _with_patched_builder(fake_builder)
		restore_writer = _with_patched_recommendation_writer(fake_writer)
		try:
			draft_result = triage_summary_publication.generate_triage_summary_draft(
				db,
				patient.patient_id,
				episode.care_episode_id,
				TriageSummaryRequest(additional_context="New selected draft context."),
			)
			assert draft_result.draft is not None
			selected_result = triage_summary_publication.select_triage_summary_draft(
				db,
				patient.patient_id,
				episode.care_episode_id,
				draft_result.draft.triage_summary_draft_id,
			)
		finally:
			restore_writer()
			restore_builder()

		assert selected_result.summary is not None
		assert not hasattr(selected_result, "is_selected")
		assert selected_result.summary.version == 2
		assert selected_result.summary.triage_summary_id != existing.triage_summary_id
		db.refresh(episode)
		latest_saved = triage_summary_publication.latest_saved_triage_summary(db, episode.care_episode_id)
		assert latest_saved is not None
		assert latest_saved.triage_summary_id == selected_result.summary.triage_summary_id
		assert episode.latest_triage_summary["triage_summary_id"] == str(selected_result.summary.triage_summary_id)
		assert episode.latest_triage_summary["triage_summary_id"] != str(existing.triage_summary_id)
		assert episode.latest_triage_summary["version"] == selected_result.summary.version
		assert recommendation_calls == [selected_result.summary.triage_summary_id]



def test_publication_episode_lock_query_uses_for_update() -> None:
	patient_id = uuid.uuid4()
	episode_id = uuid.uuid4()
	episode = CareEpisode(
		care_episode_id=episode_id,
		patient_id=patient_id,
		issue_title="Lock check",
		body_area="General rehab concern",
		goal="Check locking",
		short_description="Synthetic episode for lock query inspection.",
		origin="manual",
		status="active",
		safety_gate_status="needs_triage",
	)

	class FakeResult:
		def scalars(self) -> "FakeResult":
			return self

		def one_or_none(self) -> CareEpisode:
			return episode

	class FakeDb:
		def execute(self, statement):
			assert getattr(statement, "_for_update_arg", None) is not None
			return FakeResult()

	assert triage_summary_publication._get_locked_patient_episode_for_publication(FakeDb(), patient_id, episode_id) is episode


def test_direct_publish_builds_candidate_before_lock_and_recommends_after_publication_commit() -> None:
	events: list[str] = []
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		db.commit()

		def fake_builder(episode: CareEpisode, additional_context: str, version: int, **kwargs) -> CareEpisodeTriageSummary:
			events.append("build")
			return _candidate(episode, version, additional_context)

		def fake_writer(db: Session, *, episode: CareEpisode, summary: CareEpisodeTriageSummary) -> None:
			events.append("recommend")

		original_locker = triage_summary_publication._get_locked_patient_episode_for_publication
		original_next_version = triage_summary_publication._next_summary_version
		original_commit = db.commit

		def fake_locker(db: Session, patient_id: UUID, episode_id: UUID) -> CareEpisode:
			events.append("lock")
			return original_locker(db, patient_id, episode_id)

		def fake_next_version(db: Session, episode_id: UUID) -> int:
			events.append("version")
			return original_next_version(db, episode_id)

		def tracking_commit() -> None:
			events.append("commit")
			original_commit()

		restore_builder = _with_patched_builder(fake_builder)
		restore_writer = _with_patched_recommendation_writer(fake_writer)
		triage_summary_publication._get_locked_patient_episode_for_publication = fake_locker
		triage_summary_publication._next_summary_version = fake_next_version
		db.commit = tracking_commit
		try:
			triage_summary_publication.publish_triage_summary_directly(
				db,
				patient.patient_id,
				episode.care_episode_id,
				TriageSummaryRequest(additional_context="Slow build and recommendation should be outside lock."),
			)
		finally:
			db.commit = original_commit
			triage_summary_publication._next_summary_version = original_next_version
			triage_summary_publication._get_locked_patient_episode_for_publication = original_locker
			restore_writer()
			restore_builder()

	assert events[:4] == ["build", "lock", "version", "commit"], events
	assert events[-2:] == ["recommend", "commit"], events
	assert events.index("recommend") > events.index("commit"), events


def test_publish_and_select_allocate_versions_after_locking_episode() -> None:
	events: list[str] = []
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		db.commit()

		def fake_builder(episode: CareEpisode, additional_context: str, version: int, **kwargs) -> CareEpisodeTriageSummary:
			return _candidate(episode, version, additional_context)

		def fake_writer(db: Session, *, episode: CareEpisode, summary: CareEpisodeTriageSummary) -> None:
			return None

		original_locker = getattr(triage_summary_publication, "_get_locked_patient_episode_for_publication", None)
		if original_locker is None:
			raise AssertionError("Publication service must lock the episode before allocating summary versions")
		original_next_version = triage_summary_publication._next_summary_version

		def fake_locker(db: Session, patient_id: UUID, episode_id: UUID) -> CareEpisode:
			events.append("lock")
			return original_locker(db, patient_id, episode_id)

		def fake_next_version(db: Session, episode_id: UUID) -> int:
			events.append("version")
			return original_next_version(db, episode_id)

		restore_builder = _with_patched_builder(fake_builder)
		restore_writer = _with_patched_recommendation_writer(fake_writer)
		triage_summary_publication._get_locked_patient_episode_for_publication = fake_locker
		triage_summary_publication._next_summary_version = fake_next_version
		try:
			triage_summary_publication.publish_triage_summary_directly(
				db,
				patient.patient_id,
				episode.care_episode_id,
				TriageSummaryRequest(additional_context="Direct save should lock before version."),
			)
			draft_result = triage_summary_publication.generate_triage_summary_draft(
				db,
				patient.patient_id,
				episode.care_episode_id,
				TriageSummaryRequest(additional_context="Draft to select after lock."),
			)
			assert draft_result.draft is not None
			triage_summary_publication.select_triage_summary_draft(
				db,
				patient.patient_id,
				episode.care_episode_id,
				draft_result.draft.triage_summary_draft_id,
			)
		finally:
			triage_summary_publication._next_summary_version = original_next_version
			triage_summary_publication._get_locked_patient_episode_for_publication = original_locker
			restore_writer()
			restore_builder()

	assert events == ["lock", "version", "lock", "version"]


def test_generate_triage_summary_draft_rolls_back_on_commit_failure() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		db.commit()

		def fake_builder(episode: CareEpisode, additional_context: str, version: int, **kwargs) -> CareEpisodeTriageSummary:
			return _candidate(episode, version, additional_context)

		original_commit = db.commit

		def failing_commit() -> None:
			raise RuntimeError("draft commit failed")

		restore_builder = _with_patched_builder(fake_builder)
		db.commit = failing_commit
		try:
			try:
				triage_summary_publication.generate_triage_summary_draft(
					db,
					patient.patient_id,
					episode.care_episode_id,
					TriageSummaryRequest(additional_context="Commit failure should rollback."),
				)
			except RuntimeError as exc:
				assert str(exc) == "draft commit failed"
			else:
				raise AssertionError("Draft commit failure should propagate")
		finally:
			db.commit = original_commit
			restore_builder()

		assert not db.new
		assert db.execute(
			select(func.count(CareEpisodeTriageSummaryDraft.triage_summary_draft_id)).where(
				CareEpisodeTriageSummaryDraft.care_episode_id == episode.care_episode_id
			)
		).scalar_one() == 0


def test_unexpected_triage_summary_builder_errors_propagate() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		db.commit()

		def fake_builder(episode: CareEpisode, additional_context: str, version: int, **kwargs) -> CareEpisodeTriageSummary:
			raise RuntimeError("builder exploded")

		restore_builder = _with_patched_builder(fake_builder)
		try:
			try:
				triage_summary_publication.generate_triage_summary_draft(
					db,
					patient.patient_id,
					episode.care_episode_id,
					TriageSummaryRequest(additional_context="Trigger unexpected builder failure."),
				)
			except RuntimeError as exc:
				assert str(exc) == "builder exploded"
			except triage_summary_publication.TriageSummaryPublicationError as exc:
				raise AssertionError("Unexpected builder failures must not be mapped to publication errors") from exc
			else:
				raise AssertionError("Unexpected builder failure should propagate")
		finally:
			restore_builder()


def test_delete_triage_summary_draft_deletes_draft_without_touching_saved_summaries() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		summary = _candidate(episode, 1, "Already saved.")
		db.add(summary)
		db.commit()

		def fake_builder(episode: CareEpisode, additional_context: str, version: int, **kwargs) -> CareEpisodeTriageSummary:
			return _candidate(episode, version, additional_context)

		restore_builder = _with_patched_builder(fake_builder)
		try:
			draft_result = triage_summary_publication.generate_triage_summary_draft(
				db,
				patient.patient_id,
				episode.care_episode_id,
				TriageSummaryRequest(additional_context="Draft to delete."),
			)
		finally:
			restore_builder()

		assert draft_result.draft is not None
		draft_id = draft_result.draft.triage_summary_draft_id
		triage_summary_publication.delete_triage_summary_draft(db, patient.patient_id, episode.care_episode_id, draft_id)

		assert db.get(CareEpisodeTriageSummaryDraft, draft_id) is None
		assert db.get(CareEpisodeTriageSummary, summary.triage_summary_id) is not None
		assert _summary_count(db, episode) == 1


def test_delete_triage_summary_draft_rolls_back_on_commit_failure() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		draft = CareEpisodeTriageSummaryDraft(
			care_episode_id=episode.care_episode_id,
			patient_id=patient.patient_id,
			concern="Draft rollback",
			relevant_context="Draft delete rollback context.",
			safety_signals=[],
			limitations=[],
			recommendation="Draft recommendation.",
			missing_information=[],
			unresolved_questions=[],
			clinician_review_needed=False,
			additional_context="",
		)
		db.add(draft)
		db.commit()
		draft_id = draft.triage_summary_draft_id
		original_commit = db.commit

		def failing_commit() -> None:
			raise RuntimeError("delete commit failed")

		db.commit = failing_commit
		try:
			try:
				triage_summary_publication.delete_triage_summary_draft(db, patient.patient_id, episode.care_episode_id, draft_id)
			except RuntimeError as exc:
				assert str(exc) == "delete commit failed"
			else:
				raise AssertionError("Draft delete commit failure should propagate")
		finally:
			db.commit = original_commit

		assert not db.deleted
		assert db.get(CareEpisodeTriageSummaryDraft, draft_id) is not None


def test_delete_triage_summary_draft_rejects_saved_summary_ids() -> None:
	with SessionLocal() as db:
		patient = _create_patient(db)
		episode = _create_episode(db, patient)
		summary = _candidate(episode, 1, "Already saved.")
		db.add(summary)
		db.commit()

		try:
			triage_summary_publication.delete_triage_summary_draft(
				db,
				patient.patient_id,
				episode.care_episode_id,
				summary.triage_summary_id,
			)
		except (triage_summary_publication.TriageSummaryNotFound, triage_summary_publication.TriageSummaryPublicationError):
			pass
		else:
			raise AssertionError("Deleting a saved Triage Summary through the draft path should fail")


def main() -> None:
	verify_schema_revision()
	test_generate_triage_summary_draft_does_not_save_or_create_recommendation()
	test_publish_triage_summary_directly_saves_summary_and_creates_recommendation()
	test_select_triage_summary_draft_replaces_selected_summary()
	test_publication_episode_lock_query_uses_for_update()
	test_direct_publish_builds_candidate_before_lock_and_recommends_after_publication_commit()
	test_publish_and_select_allocate_versions_after_locking_episode()
	test_generate_triage_summary_draft_rolls_back_on_commit_failure()
	test_unexpected_triage_summary_builder_errors_propagate()
	test_delete_triage_summary_draft_deletes_draft_without_touching_saved_summaries()
	test_delete_triage_summary_draft_rolls_back_on_commit_failure()
	test_delete_triage_summary_draft_rejects_saved_summary_ids()
	print("triage summary publication contract ok")


if __name__ == "__main__":
	main()
