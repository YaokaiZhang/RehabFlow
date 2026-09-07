"""Exact ownership manifest and privacy-safe projection for one evaluator run."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

_SAFE_COLLECTION = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_QDRANT_BINDING_VERSION = 1
_QDRANT_BINDING_FIELDS = frozenset({
    "version",
    "endpoint_hash",
    "ownership_hash",
    "ownership_proof",
})
_QDRANT_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_QDRANT_OWNERSHIP_PROOFS = frozenset({"owner_identity", "endpoint_identity"})


def privacy_hash(value: object, *, salt: str = "") -> str:
    material = f"{salt}\x00{value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


class QdrantBindingError(RuntimeError):
    """The live Qdrant target cannot be proven to be the recorded target."""


def _qdrant_endpoint_identity(
    *,
    qdrant_url: str | None,
    qdrant_path: str | Path | None,
) -> str:
    if qdrant_path is not None:
        path = Path(qdrant_path).expanduser()
        if not path.is_absolute():
            raise ValueError("Qdrant path must be absolute")
        return f"path:{path.resolve()}"

    parsed = urlsplit(str(qdrant_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Qdrant URL endpoint is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Qdrant URL endpoint is invalid") from exc
    host = parsed.hostname.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path.rstrip("/")
    return "url:" + urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def qdrant_endpoint_binding(
    *,
    qdrant_url: str | None,
    qdrant_path: str | Path | None,
    owner_identity: str | None,
) -> dict[str, Any]:
    endpoint = _qdrant_endpoint_identity(qdrant_url=qdrant_url, qdrant_path=qdrant_path)
    owner = str(owner_identity or "").strip().lower()
    return {
        "version": 1,
        "endpoint_hash": hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
        "ownership_hash": (
            hashlib.sha256(owner.encode("utf-8")).hexdigest() if owner else None
        ),
        "ownership_proof": "owner_identity" if owner else "endpoint_identity",
    }
def _required(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > 512:
        raise ValueError(f"{label} is too long")
    return normalized


@dataclass
class CaseManifest:
    """Exact internal handles; use privacy_safe() for report data."""

    case_id: str
    case_hash: str
    patient_id: str
    care_episode_ids: dict[str, str] = field(default_factory=dict)
    ai_session_ids: dict[str, str] = field(default_factory=dict)
    checkpoint_thread_ids: set[str] = field(default_factory=set)
    memory_document_ids: set[str] = field(default_factory=set)
    memory_item_ids: dict[str, str] = field(default_factory=dict)
    triage_summary_ids: dict[str, str] = field(default_factory=dict)
    qdrant_collection_name: str | None = None
    qdrant_path: Path | None = None
    catalog_path: Path | None = None
    current_time: str | None = None
    _change_hook: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.case_id = _required(self.case_id, "case_id")
        self.patient_id = _required(self.patient_id, "patient_id")
        if self.qdrant_collection_name is not None:
            self.record_qdrant_collection(self.qdrant_collection_name)
        if self.qdrant_path is not None:
            self.record_qdrant_path(self.qdrant_path)
        if self.catalog_path is not None:
            self.catalog_path = Path(self.catalog_path)

    def _changed(self) -> None:
        if self._change_hook is not None:
            self._change_hook()

    def record_care_episode(self, logical_id: object, physical_id: object) -> None:
        self.care_episode_ids[_required(logical_id, "care episode logical id")] = _required(
            physical_id, "care episode id"
        )
        self._changed()

    def record_ai_session(self, logical_id: object, physical_id: object) -> None:
        self.ai_session_ids[_required(logical_id, "AI session logical id")] = _required(
            physical_id, "AI session id"
        )
        self.record_checkpoint_thread(physical_id)
        self._changed()

    def record_checkpoint_thread(self, thread_id: object) -> None:
        self.checkpoint_thread_ids.add(_required(thread_id, "checkpoint thread id"))
        self._changed()

    def record_memory_document(self, document_id: object) -> None:
        self.memory_document_ids.add(_required(document_id, "Memory Document id"))
        self._changed()

    def record_memory_item(self, logical_id: object, physical_id: object) -> None:
        self.memory_item_ids[_required(logical_id, "memory logical id")] = _required(
            physical_id, "memory item id"
        )
        self._changed()

    def record_triage_summary(self, logical_id: object, physical_id: object) -> None:
        self.triage_summary_ids[_required(logical_id, "triage logical id")] = _required(
            physical_id, "triage summary id"
        )
        self._changed()

    def record_qdrant_collection(self, collection_name: object) -> None:
        normalized = _required(collection_name, "Qdrant collection name")
        if (
            _SAFE_COLLECTION.fullmatch(normalized) is None
            or not normalized.startswith("rehab_eval_")
        ):
            raise ValueError("Qdrant collection name is not an exact evaluator handle")
        self.qdrant_collection_name = normalized
        self._changed()

    def record_qdrant_path(self, path: str | Path) -> None:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_absolute():
            raise ValueError("generated Qdrant path must be absolute")
        self.qdrant_path = candidate
        self._changed()

    def record_catalog_path(self, path: str | Path) -> None:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("generated catalog path must be absolute")
        self.catalog_path = candidate
        self._changed()

    def privacy_safe(self, *, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or self.case_hash
        return {
            "case_hash": self.case_hash,
            "resource_counts": {
                "patients": 1,
                "care_episodes": len(self.care_episode_ids),
                "ai_sessions": len(self.ai_session_ids),
                "checkpoint_threads": len(self.checkpoint_thread_ids),
                "memory_documents": len(self.memory_document_ids),
                "memory_items": len(self.memory_item_ids),
                "triage_summaries": len(self.triage_summary_ids),
                "qdrant_collections": int(self.qdrant_collection_name is not None),
                "qdrant_paths": int(self.qdrant_path is not None),
                "catalog_files": int(self.catalog_path is not None),
            },
            "resource_hashes": {
                "patient": privacy_hash(self.patient_id, salt=run_id),
                "care_episodes": [privacy_hash(v, salt=run_id) for v in sorted(self.care_episode_ids.values())],
                "sessions": [privacy_hash(v, salt=run_id) for v in sorted(self.ai_session_ids.values())],
                "checkpoint_threads": [privacy_hash(v, salt=run_id) for v in sorted(self.checkpoint_thread_ids)],
                "memory_items": [privacy_hash(v, salt=run_id) for v in sorted(self.memory_item_ids.values())],
                "triage_summaries": [privacy_hash(v, salt=run_id) for v in sorted(self.triage_summary_ids.values())],
                "qdrant_collection": privacy_hash(self.qdrant_collection_name, salt=run_id) if self.qdrant_collection_name else None,
                "qdrant_path": privacy_hash(str(self.qdrant_path), salt=run_id) if self.qdrant_path else None,
                "catalog_path": privacy_hash(str(self.catalog_path), salt=run_id) if self.catalog_path else None,
            },
            **({"current_time": self.current_time} if self.current_time else {}),
        }


@dataclass
class RunManifest:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cases: dict[str, CaseManifest] = field(default_factory=dict)
    database_bootstrap: dict[str, Any] | None = None
    qdrant_endpoint_binding: dict[str, Any] | None = None
    qdrant_path: Path | None = None
    _change_hook: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.run_id = _required(self.run_id, "run_id")

    def bind_change_hook(self, hook: Callable[[], None] | None) -> None:
        self._change_hook = hook
        for case in self.cases.values():
            case._change_hook = hook

    def _changed(self) -> None:
        if self._change_hook is not None:
            self._change_hook()

    @property
    def run_hash(self) -> str:
        return privacy_hash(self.run_id)

    def begin_case(self, case_id: object, *, patient_id: object, qdrant_collection_name: str | None = None, qdrant_path: str | Path | None = None, catalog_path: str | Path | None = None, current_time: str | None = None) -> CaseManifest:
        key = _required(case_id, "case_id")
        if key in self.cases:
            raise ValueError(f"case already exists in run manifest: {key}")
        case = CaseManifest(
            case_id=key,
            case_hash=privacy_hash(key, salt=self.run_id),
            patient_id=_required(patient_id, "patient_id"),
            qdrant_collection_name=qdrant_collection_name,
            qdrant_path=Path(qdrant_path) if qdrant_path is not None else None,
            catalog_path=Path(catalog_path) if catalog_path is not None else None,
            current_time=current_time,
        )
        case._change_hook = self._change_hook
        self.cases[key] = case
        self._changed()
        return case

    def case(self, case_id: object) -> CaseManifest:
        key = _required(case_id, "case_id")
        if key not in self.cases:
            raise KeyError(f"case is not registered in run manifest: {key}")
        return self.cases[key]

    def record_database_bootstrap(self, record: Mapping[str, Any]) -> None:
        """Record exact non-secret database ownership handles for this run."""
        sensitive_keys = {"url", "password", "secret", "token", "api_key", "credential"}
        if any(str(key).lower() in sensitive_keys for key in record):
            raise ValueError("database manifest cannot contain credentials")
        self.database_bootstrap = scrub_manifest_value(dict(record))  # type: ignore[assignment]
        self._changed()

    def record_qdrant_endpoint_binding(
        self,
        *,
        qdrant_url: str | None,
        qdrant_path: str | Path | None = None,
        owner_identity: str | None = None,
    ) -> None:
        self.qdrant_endpoint_binding = qdrant_endpoint_binding(
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            owner_identity=owner_identity,
        )
        self.qdrant_path = (
            Path(qdrant_path).expanduser().resolve() if qdrant_path is not None else None
        )
        self._changed()
    def privacy_safe(self) -> dict[str, Any]:
        return {
            "run_hash": self.run_hash,
            "case_count": len(self.cases),
            "cases": [self.cases[key].privacy_safe(run_id=self.run_id) for key in sorted(self.cases)],
            "resource_counts": self.resource_counts(),
            "database_bootstrap": _privacy_safe_database_record(
                self.database_bootstrap,
                salt=self.run_id,
            ),
            "qdrant_endpoint_binding": dict(self.qdrant_endpoint_binding) if self.qdrant_endpoint_binding else None,
        }

    def resource_counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for case in self.cases.values():
            for key, value in case.privacy_safe(run_id=self.run_id)["resource_counts"].items():
                totals[key] = totals.get(key, 0) + int(value)
        return totals

    def exact_patient_ids(self) -> tuple[str, ...]:
        return tuple(case.patient_id for case in self.cases.values())

    def exact_checkpoint_thread_ids(self) -> tuple[str, ...]:
        return tuple(sorted(thread for case in self.cases.values() for thread in case.checkpoint_thread_ids))

    def exact_qdrant_collection_names(self) -> tuple[str, ...]:
        return tuple(sorted(case.qdrant_collection_name for case in self.cases.values() if case.qdrant_collection_name))

    def exact_qdrant_paths(self) -> tuple[Path, ...]:
        return tuple(sorted(case.qdrant_path for case in self.cases.values() if case.qdrant_path))

    def exact_catalog_paths(self) -> tuple[Path, ...]:
        return tuple(sorted(case.catalog_path for case in self.cases.values() if case.catalog_path))

    def exact_care_episode_ids(self) -> tuple[str, ...]:
        return tuple(sorted(value for case in self.cases.values() for value in case.care_episode_ids.values()))

    def exact_ai_session_ids(self) -> tuple[str, ...]:
        return tuple(sorted(value for case in self.cases.values() for value in case.ai_session_ids.values()))

    def exact_memory_document_ids(self) -> tuple[str, ...]:
        return tuple(sorted(_physical_handle(value) for case in self.cases.values() for value in case.memory_document_ids))

    def exact_memory_item_ids(self) -> tuple[str, ...]:
        return tuple(sorted(case.memory_item_ids[value] for case in self.cases.values() for value in sorted(case.memory_item_ids)))

    def exact_triage_summary_ids(self) -> tuple[str, ...]:
        return tuple(sorted(case.triage_summary_ids[value] for case in self.cases.values() for value in sorted(case.triage_summary_ids)))


def _validate_qdrant_endpoint_binding(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise QdrantBindingError(f"{label} is missing or not a mapping")
    if set(value) != _QDRANT_BINDING_FIELDS:
        raise QdrantBindingError(f"{label} has an unsupported shape")

    version = value["version"]
    if type(version) is not int or version != _QDRANT_BINDING_VERSION:
        raise QdrantBindingError(f"{label} version is unsupported")

    endpoint_hash = value["endpoint_hash"]
    if not isinstance(endpoint_hash, str) or _QDRANT_SHA256.fullmatch(endpoint_hash) is None:
        raise QdrantBindingError(f"{label} endpoint hash is malformed")

    ownership_hash = value["ownership_hash"]
    if ownership_hash is not None and (
        not isinstance(ownership_hash, str)
        or _QDRANT_SHA256.fullmatch(ownership_hash) is None
    ):
        raise QdrantBindingError(f"{label} ownership hash is malformed")

    ownership_proof = value["ownership_proof"]
    if not isinstance(ownership_proof, str) or ownership_proof not in _QDRANT_OWNERSHIP_PROOFS:
        raise QdrantBindingError(f"{label} ownership proof type is unsupported")
    if ownership_proof == "owner_identity" and ownership_hash is None:
        raise QdrantBindingError(f"{label} ownership hash is required")
    if ownership_proof == "endpoint_identity" and ownership_hash is not None:
        raise QdrantBindingError(f"{label} ownership hash is not allowed")

    return {
        "version": version,
        "endpoint_hash": endpoint_hash,
        "ownership_hash": ownership_hash,
        "ownership_proof": ownership_proof,
    }


def verify_qdrant_endpoint_binding(
    manifest: RunManifest,
    *,
    qdrant_url: str | None,
    qdrant_path: str | Path | None,
    owner_identity: str | None,
) -> None:
    if not manifest.exact_qdrant_collection_names():
        return
    expected = _validate_qdrant_endpoint_binding(
        manifest.qdrant_endpoint_binding,
        label="qdrant endpoint proof",
    )
    try:
        actual = qdrant_endpoint_binding(
            qdrant_url=qdrant_url,
            qdrant_path=qdrant_path,
            owner_identity=owner_identity,
        )
        actual = _validate_qdrant_endpoint_binding(
            actual,
            label="computed qdrant endpoint proof",
        )
    except QdrantBindingError:
        raise
    except Exception as exc:
        raise QdrantBindingError("qdrant endpoint proof is unavailable") from exc
    if expected["endpoint_hash"] != actual["endpoint_hash"]:
        raise QdrantBindingError("qdrant endpoint proof mismatch")
    if expected["ownership_hash"] != actual["ownership_hash"]:
        raise QdrantBindingError("qdrant ownership proof mismatch")
    if expected["ownership_proof"] != actual["ownership_proof"]:
        raise QdrantBindingError("qdrant ownership proof type mismatch")
def _physical_handle(value: object) -> str:
    return str(value).rsplit(":", 1)[-1]


def _privacy_safe_database_record(
    record: Mapping[str, Any] | None,
    *,
    salt: str,
) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None

    def resource(value: object) -> dict[str, Any]:
        item = value if isinstance(value, Mapping) else {}
        name = str(item.get("name") or "")
        owner = str(item.get("owner_role") or "")
        return {
            "database_hash": privacy_hash(name, salt=salt),
            "owner_role_hash": privacy_hash(owner, salt=salt),
            "ownership_state": str(item.get("ownership_state") or "unproven"),
            "created_by_run": bool(item.get("created_by_run")),
            "schema_revision": item.get("schema_revision"),
            "checkpoint_ready": bool(item.get("checkpoint_ready")),
        }

    namespace = str(record.get("namespace") or "")
    server = f"{record.get('server_host') or ''}:{record.get('server_port') or ''}"
    return {
        "namespace_hash": privacy_hash(namespace, salt=salt),
        "server_hash": privacy_hash(server, salt=salt),
        "application": resource(record.get("application")),
        "checkpoint": resource(record.get("checkpoint")),
    }


def scrub_manifest_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): scrub_manifest_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_manifest_value(item) for item in value]
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
