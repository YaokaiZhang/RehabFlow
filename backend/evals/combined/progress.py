"""Durable progress state for the combined evaluator."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
import os
import re
import tempfile
from threading import RLock
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from evals.combined.resource_manifest import RunManifest

LEDGER_VERSION = 1
_DANGEROUS_KEYS = (
    "api_key",
    "authorization",
    "content",
    "conversation",
    "credential",
    "generation_key",
    "judge_key",
    "message",
    "password",
    "prompt",
    "raw_output",
    "response",
    "secret",
    "url",
    "token",
    "trace",
    "transcript",
)


class LedgerRejected(RuntimeError):
    """A durable run cannot be trusted for automatic recovery."""


def _key_name(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def _contains_dangerous_key(key: object) -> bool:
    normalized = _key_name(key)
    return any(marker in normalized for marker in _DANGEROUS_KEYS)


def _fingerprint_value(value: object, *, key: str = "") -> object:
    if isinstance(value, Mapping):
        return {
            str(item_key): _fingerprint_value(item_value, key=_key_name(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_fingerprint_value(item, key=key) for item in value]
    if _contains_dangerous_key(key):
        material = str(value).encode("utf-8")
        return {"present": value is not None and str(value) != "", "sha256": hashlib.sha256(material).hexdigest()}
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def configuration_fingerprint(configuration: Mapping[str, Any]) -> str:
    """Return an immutable fingerprint without persisting any configuration value."""
    canonical = json.dumps(
        _fingerprint_value(configuration),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Replace one JSON file atomically and durably within its parent directory."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _safe_reason(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    if re.search(r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])", raw):
        return "untrusted"
    head = raw.split(":", 1)[0]
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", head)
    return normalized[:120] or "unknown"


def _safe_tree(value: object, *, allowed_keys: set[str] | None = None) -> object:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized = _key_name(key)
            if _contains_dangerous_key(normalized):
                continue
            if allowed_keys is not None and normalized not in allowed_keys:
                continue
            child = _safe_tree(raw_value, allowed_keys=None)
            if child is not None:
                output[key] = child
        return output
    if isinstance(value, (list, tuple)):
        return [
            item
            for raw_item in value
            if (item := _safe_tree(raw_item, allowed_keys=None)) is not None
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _relative_case_path(case_dir: Path, case_key: str) -> Path:
    digest = hashlib.sha256(case_key.encode("utf-8")).hexdigest()[:32]
    return case_dir / f"case-{digest}.json"


def _catalog_runtime_parent(catalog_dir: str | Path) -> Path | None:
    resolved = Path(catalog_dir).resolve()
    if resolved.name != "catalog" or not resolved.parent.name.startswith("run."):
        return None
    return resolved.parent.parent


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def validate_manifest_ownership(
    manifest: RunManifest,
    *,
    catalog_dir: str | Path | None = None,
) -> None:
    """Reject manifests whose handles are not exact evaluator-owned resources."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", manifest.run_id.strip()):
        raise LedgerRejected("manifest run id is missing")
    seen: set[str] = set()
    catalog_root = Path(catalog_dir).resolve() if catalog_dir is not None else None
    for case in manifest.cases.values():
        collection = case.qdrant_collection_name
        if collection is not None and (
            not collection.startswith("rehab_eval_")
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", collection)
        ):
            raise LedgerRejected("manifest contains a non-evaluator Qdrant collection")
        if case.qdrant_path is not None:
            path = Path(case.qdrant_path)
            root = manifest.qdrant_path
            if not path.is_absolute() or root is None or not _is_relative_to(path, root):
                raise LedgerRejected("manifest Qdrant path is outside the exact evaluator Qdrant directory")
        if case.catalog_path is not None:
            path = Path(case.catalog_path)
            if not path.is_absolute() or (catalog_root is not None and not _is_relative_to(path, catalog_root)):
                raise LedgerRejected("manifest catalog path is outside the exact evaluator catalog directory")
        for handle in (
            *manifest.exact_patient_ids(),
            *manifest.exact_care_episode_ids(),
            *manifest.exact_ai_session_ids(),
            *manifest.exact_checkpoint_thread_ids(),
            *manifest.exact_memory_document_ids(),
            *manifest.exact_memory_item_ids(),
            *manifest.exact_triage_summary_ids(),
        ):
            normalized = str(handle).strip()
            if not normalized:
                raise LedgerRejected("manifest contains an empty cleanup handle")
            if normalized in seen:
                continue
            seen.add(normalized)
    qdrant_registered = any(
        case.qdrant_collection_name is not None
        for case in manifest.cases.values()
    )
    binding = manifest.qdrant_endpoint_binding
    if qdrant_registered:
        if not isinstance(binding, Mapping):
            raise LedgerRejected("Qdrant endpoint ownership proof is missing")
        if set(binding) != {"version", "endpoint_hash", "ownership_hash", "ownership_proof"}:
            raise LedgerRejected("Qdrant endpoint ownership proof contains unsafe fields")
        if binding.get("version") != 1:
            raise LedgerRejected("Qdrant endpoint ownership proof is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("endpoint_hash") or "")):
            raise LedgerRejected("Qdrant endpoint binding is invalid")
        ownership_hash = binding.get("ownership_hash")
        if ownership_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", str(ownership_hash)):
            raise LedgerRejected("Qdrant ownership binding is invalid")
        if binding.get("ownership_proof") not in {"owner_identity", "endpoint_identity"}:
            raise LedgerRejected("Qdrant ownership proof type is invalid")
    database = manifest.database_bootstrap
    if database is not None and not isinstance(database, Mapping):
        raise LedgerRejected("database manifest is not an object")
    if isinstance(database, Mapping):
        def has_secret_key(value: object) -> bool:
            if isinstance(value, Mapping):
                return any(
                    _contains_dangerous_key(key) or has_secret_key(item)
                    for key, item in value.items()
                )
            if isinstance(value, (list, tuple)):
                return any(has_secret_key(item) for item in value)
            return False

        if has_secret_key(database):
            raise LedgerRejected("database manifest contains a secret field")
        for role in ("application", "checkpoint"):
            record = database.get(role)
            if not isinstance(record, Mapping):
                raise LedgerRejected("database manifest is incomplete")
            name = str(record.get("name") or "")
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", name):
                raise LedgerRejected("database manifest contains an unsafe database name")
            if not isinstance(record.get("created_by_run"), bool):
                raise LedgerRejected("database ownership state is unverifiable")
            if not isinstance(record.get("live_verified"), bool):
                raise LedgerRejected("database live ownership proof is unverifiable")
            if not str(record.get("owner_role") or "").strip():
                raise LedgerRejected("database owner role is unverifiable")
            if not str(record.get("ownership_marker") or "").startswith("rehabflow-evaluator:"):
                raise LedgerRejected("database ownership marker is unverifiable")


def _manifest_to_internal_dict(manifest: RunManifest) -> dict[str, Any]:
    return {
        "run_id": manifest.run_id,
        "database_bootstrap": _safe_tree(manifest.database_bootstrap),
        "qdrant_endpoint_binding": dict(manifest.qdrant_endpoint_binding) if manifest.qdrant_endpoint_binding else None,
        "qdrant_path": str(manifest.qdrant_path) if manifest.qdrant_path is not None else None,
        "cases": {
            key: {
                "case_id": case.case_id,
                "case_hash": case.case_hash,
                "patient_id": case.patient_id,
                "care_episode_ids": dict(case.care_episode_ids),
                "ai_session_ids": dict(case.ai_session_ids),
                "checkpoint_thread_ids": sorted(case.checkpoint_thread_ids),
                "memory_document_ids": sorted(case.memory_document_ids),
                "memory_item_ids": dict(case.memory_item_ids),
                "triage_summary_ids": dict(case.triage_summary_ids),
                "qdrant_collection_name": case.qdrant_collection_name,
                "qdrant_path": str(case.qdrant_path) if case.qdrant_path is not None else None,
                "catalog_path": str(case.catalog_path) if case.catalog_path is not None else None,
                "current_time": case.current_time,
            }
            for key, case in sorted(manifest.cases.items())
        },
    }


def _manifest_from_internal_dict(payload: Mapping[str, Any]) -> RunManifest:
    run_id = str(payload.get("run_id") or "").strip()
    raw_cases = payload.get("cases")
    if not run_id or not isinstance(raw_cases, Mapping):
        raise ValueError("manifest shape is invalid")
    manifest = RunManifest(run_id=run_id)
    database = payload.get("database_bootstrap")
    if database is not None:
        if not isinstance(database, Mapping):
            raise ValueError("database manifest shape is invalid")
        manifest.database_bootstrap = dict(database)
    qdrant_binding = payload.get("qdrant_endpoint_binding")
    if qdrant_binding is not None:
        manifest.qdrant_endpoint_binding = dict(qdrant_binding) if isinstance(qdrant_binding, Mapping) else None
    raw_qdrant_path = payload.get("qdrant_path")
    if raw_qdrant_path is not None:
        qdrant_path = Path(str(raw_qdrant_path))
        if not qdrant_path.is_absolute():
            raise ValueError("qdrant path must be absolute")
        manifest.qdrant_path = qdrant_path.resolve()
    for raw_key, raw_case in raw_cases.items():
        if not isinstance(raw_case, Mapping):
            raise ValueError("case manifest shape is invalid")
        case_id = str(raw_case.get("case_id") or raw_key).strip()
        if case_id != str(raw_key):
            raise ValueError("case manifest key does not match case id")
        case = manifest.begin_case(
            case_id,
            patient_id=str(raw_case.get("patient_id") or ""),
            qdrant_collection_name=(
                str(raw_case["qdrant_collection_name"])
                if raw_case.get("qdrant_collection_name") is not None
                else None
            ),
            qdrant_path=(
                Path(str(raw_case["qdrant_path"]))
                if raw_case.get("qdrant_path") is not None
                else None
            ),
            catalog_path=(
                Path(str(raw_case["catalog_path"]))
                if raw_case.get("catalog_path") is not None
                else None
            ),
            current_time=(
                str(raw_case["current_time"])
                if raw_case.get("current_time") is not None
                else None
            ),
        )
        case.case_hash = str(raw_case.get("case_hash") or "")
        case.care_episode_ids = {
            str(key): str(value)
            for key, value in dict(raw_case.get("care_episode_ids") or {}).items()
        }
        case.ai_session_ids = {
            str(key): str(value)
            for key, value in dict(raw_case.get("ai_session_ids") or {}).items()
        }
        case.checkpoint_thread_ids = {str(value) for value in raw_case.get("checkpoint_thread_ids") or ()}
        case.memory_document_ids = {str(value) for value in raw_case.get("memory_document_ids") or ()}
        case.memory_item_ids = {
            str(key): str(value)
            for key, value in dict(raw_case.get("memory_item_ids") or {}).items()
        }
        case.triage_summary_ids = {
            str(key): str(value)
            for key, value in dict(raw_case.get("triage_summary_ids") or {}).items()
        }
    if manifest.qdrant_path is None and manifest.qdrant_endpoint_binding is not None:
        catalog_roots = {
            Path(case.catalog_path).resolve().parent
            for case in manifest.cases.values()
            if case.catalog_path is not None
        }
        if len(catalog_roots) == 1:
            catalog_root = next(iter(catalog_roots))
            runtime_root = catalog_root.parent
            if catalog_root.name == "catalog" and runtime_root.name.startswith("run."):
                manifest.qdrant_path = runtime_root / "qdrant"
    return manifest


def _read_ledger_state(path: Path) -> Mapping[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LedgerRejected(f"corrupt evaluator ledger: {path.name}") from exc
    if not isinstance(state, Mapping):
        raise LedgerRejected("evaluator ledger root must be an object")
    if state.get("version") != LEDGER_VERSION:
        raise LedgerRejected("unsupported evaluator ledger version")
    run_id = str(state.get("run_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id):
        raise LedgerRejected("evaluator ledger run id is unsafe")
    if path.name != f"evaluator-run-{run_id}.ledger.json":
        raise LedgerRejected("evaluator ledger filename does not match its run id")
    fingerprint = str(state.get("fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise LedgerRejected("evaluator ledger fingerprint is invalid")
    raw_expected = state.get("expected_case_ids")
    if not isinstance(raw_expected, list) or not raw_expected:
        raise LedgerRejected("evaluator ledger case corpus is invalid")
    expected = tuple(str(item) for item in raw_expected)
    if any(not item or item != str(item).strip() for item in expected) or len(set(expected)) != len(expected):
        raise LedgerRejected("evaluator ledger case corpus is invalid")
    if not isinstance(state.get("case_checkpoints"), Mapping):
        raise LedgerRejected("evaluator ledger checkpoint index is invalid")
    return state


class ProgressLedger:
    """Durable state and terminal per-case checkpoints for one evaluator run."""

    def __init__(
        self,
        artifact_dir: str | Path,
        *,
        run_id: str,
        fingerprint: str,
        expected_case_ids: tuple[str, ...],
        ledger_path: Path | None = None,
        state: Mapping[str, Any] | None = None,
    ) -> None:
        self.artifact_dir = Path(artifact_dir).resolve()
        self.run_id = str(run_id).strip()
        self.fingerprint = str(fingerprint).strip()
        self.expected_case_ids = tuple(dict.fromkeys(str(item) for item in expected_case_ids))
        if not self.run_id or not self.fingerprint:
            raise LedgerRejected("run id and fingerprint are required")
        self.ledger_path = ledger_path or (self.artifact_dir / f"evaluator-run-{self.run_id}.ledger.json")
        self.case_dir = self.artifact_dir / f"evaluator-run-{self.run_id}.cases"
        self.diagnostic_path = self.artifact_dir / f"evaluator-run-{self.run_id}.interrupted.json"
        self.phase = "created"
        self.terminal = False
        self.interrupted = False
        self.cleanup: dict[str, Any] | None = None
        self.recovery_cleanup: dict[str, Any] | None = None
        self._manifest: RunManifest | None = None
        self._checkpoint_paths: dict[str, str] = {}
        self._lock = RLock()
        self.case_dir.mkdir(parents=True, exist_ok=True)
        if state is not None:
            self.phase = str(state.get("phase") or "created")
            self.terminal = bool(state.get("terminal"))
            self.interrupted = bool(state.get("interrupted"))
            self.cleanup = dict(state["cleanup"]) if isinstance(state.get("cleanup"), Mapping) else None
            self.recovery_cleanup = dict(state["recovery_cleanup"]) if isinstance(state.get("recovery_cleanup"), Mapping) else None
            raw_paths = state.get("case_checkpoints")
            if isinstance(raw_paths, Mapping):
                self._checkpoint_paths = {str(key): str(value) for key, value in raw_paths.items()}

    @classmethod
    def create(
        cls,
        artifact_dir: str | Path,
        *,
        fingerprint: str,
        expected_case_ids: tuple[str, ...] | list[str],
        run_id: str | None = None,
    ) -> "ProgressLedger":
        ledger = cls(
            artifact_dir,
            run_id=run_id or str(uuid.uuid4()),
            fingerprint=fingerprint,
            expected_case_ids=tuple(expected_case_ids),
        )
        ledger._persist()
        return ledger

    @classmethod
    def discover(
        cls,
        artifact_dir: str | Path,
        *,
        fingerprint: str,
        expected_case_ids: tuple[str, ...] | list[str] | None = None,
        catalog_dir: str | Path | None = None,
    ) -> "ProgressLedger":
        root = Path(artifact_dir).resolve()
        candidates = sorted(
            root.glob("evaluator-run-*.ledger.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in candidates:
            state = _read_ledger_state(path)
            if bool(state.get("terminal")) or str(state.get("phase") or "") == "terminal":
                continue
            expected = tuple(str(item) for item in (state.get("expected_case_ids") or ()))
            if (
                str(state.get("fingerprint") or "") != str(fingerprint)
                or (expected_case_ids is not None and expected != tuple(expected_case_ids))
            ):
                continue
            manifest_payload = state.get("manifest")
            if not isinstance(manifest_payload, Mapping):
                raise LedgerRejected("evaluator ledger has no verifiable cleanup manifest")
            try:
                manifest = _manifest_from_internal_dict(manifest_payload)
                catalog_roots = {
                    Path(case.catalog_path).resolve().parent
                    for case in manifest.cases.values()
                    if case.catalog_path is not None
                }
                if len(catalog_roots) > 1:
                    raise LedgerRejected("evaluator ledger has multiple catalog roots")
                validation_catalog_dir = catalog_dir
                if catalog_roots:
                    prior_catalog_dir = next(iter(catalog_roots))
                    if catalog_dir is not None:
                        current_catalog_dir = Path(catalog_dir).resolve()
                        if prior_catalog_dir != current_catalog_dir and (
                            _catalog_runtime_parent(prior_catalog_dir) is None
                            or _catalog_runtime_parent(current_catalog_dir) is None
                            or _catalog_runtime_parent(prior_catalog_dir)
                            != _catalog_runtime_parent(current_catalog_dir)
                        ):
                            raise LedgerRejected(
                                "evaluator ledger catalog is outside the current evaluator runtime"
                            )
                    validation_catalog_dir = prior_catalog_dir
                validate_manifest_ownership(manifest, catalog_dir=validation_catalog_dir)
            except LedgerRejected:
                raise
            except Exception as exc:
                raise LedgerRejected("evaluator ledger manifest is corrupt or unverifiable") from exc
            ledger = cls(
                root,
                run_id=str(state.get("run_id") or ""),
                fingerprint=str(state.get("fingerprint") or ""),
                expected_case_ids=expected,
                ledger_path=path,
                state=state,
            )
            ledger._manifest = manifest
            ledger._bind_manifest()
            ledger._refresh_checkpoints()
            return ledger
        raise LedgerRejected("no matching incomplete evaluator ledger found")

    @classmethod
    def load_unmatched(
        cls,
        ledger_path: str | Path,
        *,
        catalog_dir: str | Path | None = None,
    ) -> "ProgressLedger":
        path = Path(ledger_path).resolve()
        state = _read_ledger_state(path)
        if bool(state.get("terminal")) or str(state.get("phase") or "") == "terminal":
            raise LedgerRejected("evaluator ledger is not an incomplete object")
        manifest_payload = state.get("manifest")
        if not isinstance(manifest_payload, Mapping):
            raise LedgerRejected("evaluator ledger has no verifiable cleanup manifest")
        try:
            manifest = _manifest_from_internal_dict(manifest_payload)
            validate_manifest_ownership(manifest, catalog_dir=catalog_dir)
        except LedgerRejected:
            raise
        except Exception as exc:
            raise LedgerRejected("evaluator ledger manifest is corrupt or unverifiable") from exc
        expected = tuple(str(item) for item in (state.get("expected_case_ids") or ()))
        ledger = cls(
            path.parent,
            run_id=str(state.get("run_id") or ""),
            fingerprint=str(state.get("fingerprint") or ""),
            expected_case_ids=expected,
            ledger_path=path,
            state=state,
        )
        ledger._manifest = manifest
        ledger._bind_manifest()
        ledger._refresh_checkpoints()
        return ledger
    @property
    def manifest(self) -> RunManifest | None:
        return self._manifest

    @property
    def completed_case_ids(self) -> set[str]:
        self._refresh_checkpoints()
        return set(self._checkpoint_paths)

    @property
    def missing_case_ids(self) -> tuple[str, ...]:
        completed = self.completed_case_ids
        return tuple(case_id for case_id in self.expected_case_ids if case_id not in completed)

    def attach_manifest(self, manifest: RunManifest) -> None:
        if manifest.run_id != self.run_id:
            raise LedgerRejected("manifest run id does not match ledger")
        self._manifest = manifest
        self._bind_manifest()
        self._persist()

    def _bind_manifest(self) -> None:
        if self._manifest is None:
            return
        binder = getattr(self._manifest, "bind_change_hook", None)
        if callable(binder):
            binder(self.persist_manifest)

    def persist_manifest(self) -> None:
        self._persist()

    def _state(self) -> dict[str, Any]:
        return {
            "version": LEDGER_VERSION,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "phase": self.phase,
            "terminal": self.terminal,
            "interrupted": self.interrupted,
            "expected_case_ids": list(self.expected_case_ids),
            "case_checkpoints": dict(sorted(self._checkpoint_paths.items())),
            "manifest": _manifest_to_internal_dict(self._manifest) if self._manifest is not None else None,
            "cleanup": self.cleanup,
            "recovery_cleanup": self.recovery_cleanup,
        }

    def _persist(self) -> None:
        with self._lock:
            atomic_write_json(self.ledger_path, self._state())

    def mark_phase(self, phase: str) -> None:
        self.phase = str(phase)
        self._persist()

    def checkpoint_case(self, case_key: str, result: Mapping[str, Any]) -> None:
        key = str(case_key)
        if key not in self.expected_case_ids:
            raise LedgerRejected(f"case is outside the requested evaluator corpus: {key}")
        full_result = dict(result)
        path = _relative_case_path(self.case_dir, key)
        with self._lock:
            atomic_write_json(
                path,
                {
                    "version": LEDGER_VERSION,
                    "run_id": self.run_id,
                    "fingerprint": self.fingerprint,
                    "case_key": key,
                    "terminal": True,
                    "result": full_result,
                },
            )
            self._checkpoint_paths[key] = str(path.relative_to(self.artifact_dir))
            self.phase = "case_execution"
            self._persist()

    def _refresh_checkpoints(self) -> None:
        paths: dict[str, str] = {}
        for path in self.case_dir.glob("case-*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise LedgerRejected("corrupt evaluator case checkpoint") from exc
            if (
                payload.get("run_id") != self.run_id
                or payload.get("fingerprint") != self.fingerprint
                or payload.get("terminal") is not True
            ):
                raise LedgerRejected("unverifiable evaluator case checkpoint")
            key = str(payload.get("case_key") or "")
            expected_path = _relative_case_path(self.case_dir, key)
            if (
                key not in self.expected_case_ids
                or path.resolve() != expected_path.resolve()
                or not isinstance(payload.get("result"), Mapping)
            ):
                raise LedgerRejected("unverifiable evaluator case checkpoint")
            result = dict(payload["result"])
            paths[key] = str(path.relative_to(self.artifact_dir))
        self._checkpoint_paths = paths

    def load_case_results(self) -> list[dict[str, Any]]:
        self._refresh_checkpoints()
        results: list[dict[str, Any]] = []
        for case_key in self.expected_case_ids:
            relative = self._checkpoint_paths.get(case_key)
            if not relative:
                continue
            path = (self.artifact_dir / relative).resolve()
            if path.parent != self.case_dir.resolve():
                raise LedgerRejected("case checkpoint path escapes the evaluator run directory")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("run_id") != self.run_id
                or payload.get("fingerprint") != self.fingerprint
                or payload.get("terminal") is not True
                or payload.get("case_key") != case_key
            ):
                raise LedgerRejected("unverifiable evaluator case checkpoint")
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise LedgerRejected("case checkpoint result is invalid")
            results.append(dict(result))
        return results

    def mark_cleanup_pending(self) -> None:
        self.phase = "destructive_cleanup"
        self.cleanup = {"status": "pending", "failures": []}
        self._persist()

    def record_cleanup(self, cleanup: Mapping[str, Any]) -> None:
        self.cleanup = dict(_safe_tree(cleanup))
        self.phase = "cleanup_complete"
        self._persist()

    def record_recovery_cleanup(self, cleanup: Mapping[str, Any]) -> None:
        self.recovery_cleanup = dict(_safe_tree(cleanup))
        self.phase = "recovery_cleanup"
        self._persist()

    def write_interrupted_diagnostic(self, *, reason: str, exit_code: int | None = None) -> None:
        payload = {
            "version": LEDGER_VERSION,
            "run_id_hash": hashlib.sha256(self.run_id.encode("utf-8")).hexdigest()[:20],
            "phase": self.phase,
            "reason": _safe_reason(reason),
            "exit_code": int(exit_code) if exit_code is not None else None,
            "completed_case_count": len(self.completed_case_ids),
        }
        atomic_write_json(self.diagnostic_path, payload)
        self.interrupted = True
        self.phase = "interrupted"
        self._persist()

    def mark_terminal(self) -> None:
        self.phase = "terminal"
        self.terminal = True
        self.interrupted = False
        self.diagnostic_path.unlink(missing_ok=True)
        self._persist()


def run_checkpointed_cases(
    progress: ProgressLedger,
    cases: Sequence[tuple[str, Callable[[], Mapping[str, Any]]]],
    *,
    max_parallel_cases: int = 1,
) -> int:
    """Execute missing cases with bounded concurrency and durable checkpoints."""
    if not isinstance(max_parallel_cases, int) or isinstance(max_parallel_cases, bool):
        raise ValueError("max_parallel_cases must be a positive integer")
    if max_parallel_cases < 1:
        raise ValueError("max_parallel_cases must be a positive integer")
    completed = progress.completed_case_ids
    pending = [
        (str(case_key), execute)
        for case_key, execute in cases
        if str(case_key) not in completed
    ]
    if not pending:
        return 0
    if max_parallel_cases == 1:
        for case_key, execute in pending:
            progress.checkpoint_case(case_key, execute())
        return len(pending)
    executed_count = 0
    with ThreadPoolExecutor(max_workers=max_parallel_cases, thread_name_prefix="rehab-eval") as executor:
        futures = {
            executor.submit(execute): case_key
            for case_key, execute in pending
        }
        for future in as_completed(futures):
            case_key = futures[future]
            progress.checkpoint_case(case_key, future.result())
            executed_count += 1
    return executed_count


def run_checkpointed_staged_cases(
    progress: ProgressLedger,
    cases: Sequence[tuple[str, Callable[[], Mapping[str, Any]]]],
    *,
    postprocess: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    max_parallel_cases: int = 1,
    max_parallel_postprocess: int = 1,
    execution_gate: Any | None = None,
) -> int:
    """Run service work and postprocessing in separate bounded stages.

    A completed service case releases its service worker before its judge or
    other postprocessing begins. The optional gate limits simultaneous work
    across both stages when they share an upstream API budget.
    """
    for name, value in (
        ("max_parallel_cases", max_parallel_cases),
        ("max_parallel_postprocess", max_parallel_postprocess),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    completed = progress.completed_case_ids
    pending = [
        (str(case_key), execute)
        for case_key, execute in cases
        if str(case_key) not in completed
    ]
    if not pending:
        return 0

    def gated(call: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
        if execution_gate is None:
            return call()
        execution_gate.acquire()
        try:
            return call()
        finally:
            execution_gate.release()

    executed_count = 0
    with (
        ThreadPoolExecutor(
            max_workers=max_parallel_cases,
            thread_name_prefix="rehab-eval-service",
        ) as service_executor,
        ThreadPoolExecutor(
            max_workers=max_parallel_postprocess,
            thread_name_prefix="rehab-eval-postprocess",
        ) as postprocess_executor,
    ):
        service_futures = {
            service_executor.submit(gated, execute): case_key
            for case_key, execute in pending
        }
        postprocess_futures: dict[Any, str] = {}
        while service_futures or postprocess_futures:
            watched = tuple(service_futures) + tuple(postprocess_futures)
            done, _pending = wait(watched, return_when=FIRST_COMPLETED)
            for future in done:
                if future in service_futures:
                    case_key = service_futures.pop(future)
                    service_result = future.result()
                    postprocess_future = postprocess_executor.submit(
                        gated,
                        lambda case_key=case_key, service_result=service_result: postprocess(
                            case_key,
                            service_result,
                        ),
                    )
                    postprocess_futures[postprocess_future] = case_key
                else:
                    case_key = postprocess_futures.pop(future)
                    result = future.result()
                    progress.checkpoint_case(case_key, result)
                    executed_count += 1
    return executed_count


RunLedger = ProgressLedger
