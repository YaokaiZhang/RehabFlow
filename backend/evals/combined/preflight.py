"""Strict, secret-safe evaluator resource preflight."""
from __future__ import annotations

import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from evals.combined.resource_manifest import privacy_hash


_FORBIDDEN_RESOURCE_MARKER = re.compile(
    r"(?<![a-z0-9])(?:prod|production|staging|live|clinical|shared)(?![a-z0-9])",
    re.IGNORECASE,
)


def _contains_forbidden_resource_marker(value: str) -> bool:
    """Reject explicit resource-name components without matching words inside them."""
    return _FORBIDDEN_RESOURCE_MARKER.search(str(value)) is not None


class PreflightError(RuntimeError):
    """An evaluator prerequisite failed before any case was started."""


def _database_target(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg2", "postgresql+asyncpg"} or not parsed.hostname or not parsed.path.strip("/"):
        raise PreflightError("database URL is invalid for evaluator use")
    identity = f"{parsed.hostname}:{parsed.port or 5432}/{parsed.path.strip('/')}".lower()
    if _contains_forbidden_resource_marker(identity):
        raise PreflightError("database URL appears to target a shared or production resource")
    if not any(marker in identity for marker in ("eval", "evaluator", "test", "ci", "dev")):
        raise PreflightError("database URL is not recognizably evaluator-isolated")
    return parsed.hostname.lower(), parsed.port or 5432, parsed.path.strip("/").lower()


@dataclass(frozen=True)
class PreflightConfig:
    application_database_url: str
    checkpoint_database_url: str
    qdrant_url: str
    catalog_dir: Path
    backend_revision: str
    application_schema_revision: str
    workflow_version: str
    backend_generation_provider: str
    backend_generation_key: str | None
    judge_key: str | None
    environment: str
    evaluator_mode: bool
    embedding_provider: str = "hash"
    qdrant_owner_identity: str | None = None
    qdrant_allowlisted_hosts: tuple[str, ...] = ()
    qdrant_path: Path | None = None


@dataclass(frozen=True)
class PreflightResult:
    status: str
    checks: Mapping[str, str]
    safe_identity: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": dict(self.checks), "identity": dict(self.safe_identity)}


def validate_preflight_config(config: PreflightConfig) -> PreflightResult:
    if not config.evaluator_mode:
        raise PreflightError("evaluator-only mode is required")
    environment = str(config.environment).strip().lower()
    if environment not in {"evaluation", "test", "development", "dev"}:
        raise PreflightError("service environment is not evaluator-safe")
    if not config.backend_revision.strip() or config.backend_revision.strip().lower() == "unknown":
        raise PreflightError("backend revision is required")
    if not str(config.application_schema_revision or "").strip():
        raise PreflightError("application Alembic revision is required")
    if not config.workflow_version.strip():
        raise PreflightError("workflow version is required")
    application_target = _database_target(config.application_database_url)
    checkpoint_target = _database_target(config.checkpoint_database_url)
    if application_target == checkpoint_target:
        raise PreflightError("application and checkpoint database targets must be distinct")
    qdrant_path = Path(config.qdrant_path).expanduser() if config.qdrant_path else None
    if qdrant_path is not None:
        if not qdrant_path.is_absolute():
            raise PreflightError("evaluator Qdrant path must be absolute")
        qdrant_identity = f"path:{qdrant_path}"
        if _contains_forbidden_resource_marker(str(qdrant_path)):
            raise PreflightError("Qdrant path appears to target a shared or production resource")
        qdrant_host = ""
    else:
        qdrant = urlsplit(config.qdrant_url)
        if qdrant.scheme not in {"http", "https"} or not qdrant.netloc:
            raise PreflightError("evaluator Qdrant URL is invalid")
        qdrant_host = str(qdrant.hostname or "").strip().lower()
        if _contains_forbidden_resource_marker(qdrant_host):
            raise PreflightError("Qdrant URL appears to target a shared or production resource")
        qdrant_identity = f"{qdrant.scheme}://{qdrant.hostname}:{qdrant.port or ''}"
    allowlisted_hosts = {
        str(host).strip().lower()
        for host in config.qdrant_allowlisted_hosts
        if str(host).strip()
    }
    owner_identity = str(config.qdrant_owner_identity or "").strip().lower()
    if not owner_identity and qdrant_host not in allowlisted_hosts and qdrant_path is None:
        raise PreflightError("Qdrant evaluator ownership or an explicit host allowlist is required")
    if owner_identity in {"prod", "production", "staging", "live", "clinical", "shared"}:
        raise PreflightError("Qdrant owner identity is not evaluator-safe")
    root = Path(config.catalog_dir).expanduser()
    if not root.is_absolute():
        raise PreflightError("generated catalog directory must be absolute")
    if not root.exists() or not root.is_dir() or not os.access(root, os.W_OK):
        raise PreflightError("generated catalog directory is not writable")
    provider = config.backend_generation_provider.strip().lower()
    if provider not in {"openai", "qwen", "dashscope", "hash"}:
        raise PreflightError("backend generation provider is unsupported")
    if provider in {"openai", "qwen", "dashscope"} and not str(config.backend_generation_key or "").strip():
        raise PreflightError("backend generation credential is required")
    embedding_provider = config.embedding_provider.strip().lower()
    if embedding_provider == "dashscope":
        raise PreflightError("embedding provider alias dashscope is unsupported; use qwen")
    if embedding_provider not in {"hash", "openai", "qwen"}:
        raise PreflightError("embedding provider is unsupported")
    if embedding_provider in {"openai", "qwen", "dashscope"} and not str(config.backend_generation_key or "").strip():
        raise PreflightError("embedding credential is required")
    if not str(config.judge_key or "").strip():
        raise PreflightError("judge credential is required")
    return PreflightResult(
        status="passed",
        checks={
            "environment": "passed",
            "database_isolation": "passed",
            "catalog_directory": "passed",
            "qdrant_ownership": "passed",
            "backend_configuration": "passed",
            "judge_configuration": "passed",
        },
        safe_identity={
            "application_database": privacy_hash(application_target),
            "checkpoint_database": privacy_hash(checkpoint_target),
            "qdrant": privacy_hash(qdrant_identity),
            "catalog_directory": privacy_hash(str(root)),
        },
    )


def verify_application_schema(database_url: str, expected_revision: str | None = None) -> str:
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            actual = str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none() or "")
        if not actual:
            raise PreflightError("application Alembic revision is unavailable")
        if expected_revision and actual != expected_revision:
            raise PreflightError("application Alembic revision does not match the evaluator expectation")
        return actual
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError(f"application schema preflight failed: {type(exc).__name__}") from exc
    finally:
        engine.dispose()


def verify_checkpoint_tables(saver: Any) -> None:
    try:
        if saver.get_tuple({"configurable": {"thread_id": "__rehabflow_evaluator_preflight__", "checkpoint_ns": ""}}) is not None:
            raise PreflightError("checkpoint preflight probe unexpectedly exists")
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError("LangGraph checkpoint vendor tables are unavailable") from exc


def verify_qdrant_capabilities(
    client: Any,
    *,
    probe_collection: str,
    ownership_verified: bool = False,
) -> None:
    if not ownership_verified:
        raise PreflightError("Qdrant capability mutation requires verified evaluator ownership")
    if not probe_collection or "/" in probe_collection or " " in probe_collection:
        raise PreflightError("Qdrant probe collection must be exact and safe")
    created = False
    try:
        from qdrant_client.models import Distance, PointStruct, VectorParams
        client.create_collection(
            collection_name=probe_collection,
            vectors_config=VectorParams(size=1, distance=Distance.COSINE),
        )
        created = True
        client.get_collection(collection_name=probe_collection)
        names = {str(item.name) for item in client.get_collections().collections}
        if probe_collection not in names:
            raise PreflightError("Qdrant probe collection was not created")
        probe_id = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
        client.upsert(
            collection_name=probe_collection,
            points=[PointStruct(id=probe_id, vector=[1.0], payload={"evaluator_probe": True})],
            wait=True,
        )
        if callable(getattr(client, "query_points", None)):
            points = client.query_points(
                collection_name=probe_collection,
                query=[1.0],
                limit=1,
                with_payload=False,
            ).points
        else:
            points = client.search(
                collection_name=probe_collection,
                query_vector=[1.0],
                limit=1,
                with_payload=False,
            )
        if not points:
            raise PreflightError("Qdrant probe query returned no point")
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError(f"Qdrant capability preflight failed: {type(exc).__name__}") from exc
    finally:
        if created:
            try:
                client.delete_collection(collection_name=probe_collection)
                names = {str(item.name) for item in client.get_collections().collections}
                if probe_collection in names:
                    raise PreflightError("Qdrant exact collection deletion could not be verified")
            except PreflightError:
                raise
            except Exception as exc:
                raise PreflightError(f"Qdrant probe cleanup failed: {type(exc).__name__}") from exc


def writable_probe(directory: str | Path) -> None:
    root = Path(directory)
    try:
        with tempfile.NamedTemporaryFile(prefix=".evaluator-preflight-", dir=root, delete=True):
            pass
    except Exception as exc:
        raise PreflightError("evaluator catalog directory failed a writable probe") from exc
