"""Artifact-first exact cleanup for evaluator-owned resources."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from evals.combined.resource_manifest import RunManifest, verify_qdrant_endpoint_binding


@dataclass
class CleanupResult:
    status: str
    failures: list[str] = field(default_factory=list)
    deleted: dict[str, int] = field(default_factory=dict)
    verified: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "passed" and not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "failures": list(self.failures),
            "deleted_counts": dict(self.deleted),
            "verification_counts": dict(self.verified),
        }


def preserve_test_data(_manifest: RunManifest) -> CleanupResult:
    """Record retained data without deleting any evaluator-owned resource."""
    counts = {"application": 0, "checkpoint": 0, "qdrant": 0, "catalog": 0}
    return CleanupResult("preserved", [], dict(counts), dict(counts))


def _failure(failures: list[str], phase: str, exc: Exception) -> None:
    failures.append(f"{phase}_{type(exc).__name__}")


def _cleanup_application(manifest: RunManifest, database_url: str) -> tuple[int, int]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db.models import (
        AISession,
        CareEpisode,
        CareEpisodeTriageSummary,
        CareEpisodeTriageSummaryDraft,
        MemoryDocument,
        Patient,
    )

    engine = create_engine(database_url)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        deleted = 0
        for raw_patient_id in manifest.exact_patient_ids():
            patient = session.get(Patient, UUID(raw_patient_id))
            if patient is not None:
                session.delete(patient)
                deleted += 1
        session.commit()
        remaining = 0
        for raw_patient_id in manifest.exact_patient_ids():
            if session.get(Patient, UUID(raw_patient_id)) is not None:
                remaining += 1
        for raw_id in manifest.exact_care_episode_ids():
            if session.get(CareEpisode, UUID(raw_id)) is not None:
                remaining += 1
        for raw_id in manifest.exact_ai_session_ids():
            if session.get(AISession, UUID(raw_id)) is not None:
                remaining += 1
        for raw_id in manifest.exact_memory_document_ids():
            if session.get(MemoryDocument, UUID(raw_id)) is not None:
                remaining += 1
        for raw_id in manifest.exact_triage_summary_ids():
            if (
                session.get(CareEpisodeTriageSummary, UUID(raw_id)) is not None
                or session.get(CareEpisodeTriageSummaryDraft, UUID(raw_id)) is not None
            ):
                remaining += 1
        return deleted, remaining
    finally:
        session.close()
        engine.dispose()


def _cleanup_checkpoints(manifest: RunManifest, database_url: str) -> tuple[int, int]:
    from langgraph.checkpoint.postgres import PostgresSaver
    from app.ai.checkpointing import cleanup_completed_checkpoint

    deleted = 0
    remaining = 0
    with PostgresSaver.from_conn_string(database_url) as saver:
        for thread_id in manifest.exact_checkpoint_thread_ids():
            if cleanup_completed_checkpoint(saver, thread_id):
                deleted += 1
        for thread_id in manifest.exact_checkpoint_thread_ids():
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            if saver.get_tuple(config) is not None:
                remaining += 1
                continue
            if next(saver.list({"configurable": {"thread_id": thread_id}}), None) is not None:
                remaining += 1
    return deleted, remaining


def _cleanup_qdrant(
    manifest: RunManifest,
    qdrant_url: str,
    qdrant_api_key: str | None,
    qdrant_path: str | None = None,
    qdrant_owner_identity: str | None = None,
) -> tuple[int, int]:
    if not manifest.exact_qdrant_collection_names():
        return 0, 0
    if qdrant_path is not None and not Path(qdrant_path).exists():
        return 0, 0
    verify_qdrant_endpoint_binding(
        manifest,
        qdrant_url=qdrant_url,
        qdrant_path=qdrant_path,
        owner_identity=qdrant_owner_identity,
    )
    from qdrant_client import QdrantClient

    case_paths = [
        (case.qdrant_path, case.qdrant_collection_name)
        for case in manifest.cases.values()
        if case.qdrant_path is not None and case.qdrant_collection_name
    ]
    if case_paths:
        base = Path(qdrant_path).expanduser().resolve() if qdrant_path else None
        if base is None:
            raise ValueError("per-case Qdrant cleanup requires the bound local root")
        deleted = 0
        remaining = 0
        for raw_path, name in case_paths:
            path = Path(raw_path).expanduser().resolve()
            if path != base and base not in path.parents:
                raise ValueError("per-case Qdrant path is outside the bound local root")
            if not path.exists():
                continue
            client = QdrantClient(path=str(path), force_disable_check_same_thread=True)
            try:
                existing = {str(item.name) for item in client.get_collections().collections}
                if name in existing:
                    client.delete_collection(collection_name=name)
                    deleted += 1
            finally:
                client.close()
            shutil.rmtree(path)
            if path.exists():
                remaining += 1
        return deleted, remaining

    client = (
        QdrantClient(path=qdrant_path, force_disable_check_same_thread=True)
        if qdrant_path
        else QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
    )
    try:
        existing = {str(item.name) for item in client.get_collections().collections}
        deleted = 0
        for name in manifest.exact_qdrant_collection_names():
            if name in existing:
                client.delete_collection(collection_name=name)
                deleted += 1
        remaining = sum(
            1
            for name in manifest.exact_qdrant_collection_names()
            if name in {str(item.name) for item in client.get_collections().collections}
        )
        return deleted, remaining
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _cleanup_catalogs(manifest: RunManifest) -> tuple[int, int]:
    deleted = 0
    remaining = 0
    for path in manifest.exact_catalog_paths():
        if path.exists():
            path.unlink()
            deleted += 1
        if path.exists():
            remaining += 1
    return deleted, remaining


def cleanup_exact_manifest(
    manifest: RunManifest,
    *,
    application_database_url: str,
    checkpoint_database_url: str,
    qdrant_url: str,
    qdrant_api_key: str | None = None,
    qdrant_path: str | None = None,
    qdrant_owner_identity: str | None = None,
    database_bootstrap: Any | None = None,
    database_cleanup: Callable[[Any], tuple[int, int]] | None = None,
    application_cleanup: Callable[[RunManifest], tuple[int, int]] | None = None,
    checkpoint_cleanup: Callable[[RunManifest], tuple[int, int]] | None = None,
    qdrant_cleanup: Callable[[RunManifest], tuple[int, int]] | None = None,
    catalog_cleanup: Callable[[RunManifest], tuple[int, int]] | None = None,
) -> CleanupResult:
    """Dispose only exact handles recorded in the manifest.

    Each phase is attempted independently so one failure remains visible and
    does not hide cleanup evidence from the other owned resource types.
    """

    failures: list[str] = []
    deleted = {"application": 0, "checkpoint": 0, "qdrant": 0, "catalog": 0}
    verified = {"application": 0, "checkpoint": 0, "qdrant": 0, "catalog": 0}
    def qdrant_phase(value: RunManifest) -> tuple[int, int]:
        if qdrant_path is not None and not Path(qdrant_path).exists():
            return 0, 0
        verify_qdrant_endpoint_binding(
            value,
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            owner_identity=qdrant_owner_identity,
        )
        if qdrant_cleanup is not None:
            return qdrant_cleanup(value)
        return _cleanup_qdrant(
            value,
            qdrant_url,
            qdrant_api_key,
            qdrant_path,
            qdrant_owner_identity,
        )
    phases = [
        ("application", application_cleanup or (lambda value: _cleanup_application(value, application_database_url))),
        ("checkpoint", checkpoint_cleanup or (lambda value: _cleanup_checkpoints(value, checkpoint_database_url))),
        ("qdrant", qdrant_phase),
        ("catalog", catalog_cleanup or _cleanup_catalogs),
    ]
    if database_bootstrap is not None:
        from evals.combined.database_bootstrap import drop_evaluator_databases

        deleted["database"] = 0
        verified["database"] = 0
        phases.append(
            (
                "database",
                lambda _manifest: (
                    database_cleanup(database_bootstrap)
                    if database_cleanup is not None
                    else drop_evaluator_databases(database_bootstrap)
                ),
            )
        )
    for name, action in phases:
        try:
            removed, remaining = action(manifest)
            deleted[name] = int(removed)
            verified[name] = int(remaining)
            if remaining:
                failures.append(f"{name}_verification_remaining")
        except Exception as exc:
            _failure(failures, name, exc)
    return CleanupResult("passed" if not failures else "failed", failures, deleted, verified)
