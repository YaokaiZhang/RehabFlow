"""Metadata-only LangGraph checkpoint boundary.

Checkpoint values are control-plane data. Natural-language payloads are sealed
into durable opaque references before they reach the configured saver. Runtime
invocation enters a rehydration scope; diagnostics and ordinary checkpoint reads
do not. The sealed payload travels with the checkpoint, so closing and reopening
the saver does not lose protected runtime state.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import pickle
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


_REHYDRATE_CHECKPOINTS: ContextVar[bool] = ContextVar(
    "rehabflow_rehydrate_checkpoints",
    default=False,
)
_OPAQUE_MARKER = "__rehabflow_opaque_ref__"
_AUTH_BINDING = "__rehabflow_auth_binding__"


class CheckpointAccessDenied(PermissionError):
    """Checkpoint belongs to a different authenticated owner or session."""

# These are the only graph channels allowed to remain visible as control
# metadata. All other channel values are opaque, including IDs and source lists
# that might otherwise expose patient or episode identity.
_SAFE_CONTROL_CHANNELS = frozenset(
    {
        "workflow_version",
        "route",
        "repair_count",
        "repair_action_count",
        "review_attempt",
        "model_calls",
        "tool_calls",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "policy_gate_severity",
        "policy_gate_reason_codes",
        "clarification_answered",
        "context_rehydrated",
        "principal_authorized",
        "session_authorized",
        "care_episode_authorized",
        "rehab_knowledge_requested",
        "rehab_knowledge_scope",
        "tool_calling_used",
        "repair_requires_revalidation",
        "force_revalidate_context",
        "discard_previous_evidence",
        "idempotency_key_present",
        "request_hash_present",
        "prior_request_hash_present",
        "budget",
        "source_allowances_metadata",
    }
)
_LANGGRAPH_CONTROL_PREFIXES = ("branch:", "__pregel_")


class CheckpointPrivacyStore:
    """Durable opaque payload codec for saver-visible checkpoint references.

    The reference contains an authenticated encrypted payload rather than the
    original value. This avoids a process-local lookup table: a Postgres or
    MemorySaver checkpoint can be reopened by a new runtime and still be
    rehydrated without putting patient or prompt content in control channels.
    """

    def __init__(self) -> None:
        self._fernet = Fernet(_fernet_key())
        self._values: dict[str, tuple[str, object]] = {}
        self._by_thread: dict[str, set[str]] = {}
        self._lock = RLock()

    def put(self, thread_id: str, value: object) -> dict[str, object]:
        try:
            payload = pickle.dumps(value, protocol=5)
            token = self._fernet.encrypt(payload).decode("ascii")
            reference = f"opaque:v2:{token}"
        except Exception:
            # A checkpoint must never commit a process-local or unsealed
            # reference. The caller receives a fixed safe failure instead.
            raise RuntimeError("checkpoint payload sealing failed") from None
        thread = str(thread_id)
        with self._lock:
            self._values[reference] = (thread, value)
            self._by_thread.setdefault(thread, set()).add(reference)
        return {_OPAQUE_MARKER: reference}

    def get(self, reference: str) -> object | None:
        if reference.startswith("opaque:v2:"):
            try:
                payload = self._fernet.decrypt(reference.removeprefix("opaque:v2:").encode("ascii"))
                return pickle.loads(payload)
            except (InvalidToken, ValueError, TypeError, pickle.PickleError, EOFError):
                return None
        with self._lock:
            item = self._values.get(reference)
        return item[1] if item is not None else None

    def delete_thread(self, thread_id: str) -> None:
        thread = str(thread_id)
        with self._lock:
            keys = [key for key in self._by_thread if key == thread or key.startswith(f"{thread}:")]
            references: set[str] = set()
            for key in keys:
                references.update(self._by_thread.pop(key, set()))
            for reference in references:
                self._values.pop(reference, None)


def _thread_id(config: Mapping[str, object] | None) -> str:
    configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
    if isinstance(configurable, Mapping):
        thread = str(configurable.get("thread_id") or "anonymous")
        owner = str(
            configurable.get("authenticated_owner")
            or configurable.get("principal_id")
            or configurable.get("patient_id")
            or "anonymous"
        )
        return f"{thread}:{owner}"
    return "anonymous"


def _fernet_key() -> bytes:
    """Derive a stable codec key from server configuration, never request data."""
    try:
        from app.core.config import get_settings

        settings = get_settings()
    except Exception:
        environment = os.getenv("ENVIRONMENT", "").strip().lower()
        if environment not in {"test", "development", "dev"}:
            raise RuntimeError("checkpoint payload key is unavailable") from None
        secret = "rehabflow-test-checkpoint-codec-v2"
    else:
        secret = settings.trace_hmac_key or getattr(settings, "checkpoint_payload_key", None)
        if not secret:
            if settings.environment == "production":
                raise RuntimeError("checkpoint payload key is required in production")
            secret = "rehabflow-development-checkpoint-codec-v2"
    return base64.urlsafe_b64encode(hashlib.sha256(str(secret).encode("utf-8")).digest())


def _authorization_binding(config: Mapping[str, object] | None) -> str:
    configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
    if not isinstance(configurable, Mapping):
        configurable = {}
    thread = str(configurable.get("thread_id") or "anonymous")
    owner = str(
        configurable.get("authenticated_owner")
        or configurable.get("principal_id")
        or configurable.get("patient_id")
        or "anonymous"
    )
    material = f"{thread}\x00{owner}".encode("utf-8")
    return hmac.new(_fernet_key(), material, hashlib.sha256).hexdigest()


def _authorize_checkpoint(config: Mapping[str, object], item: Any) -> None:
    metadata = getattr(item, "metadata", {})
    if not isinstance(metadata, Mapping) or not hmac.compare_digest(
        str(metadata.get(_AUTH_BINDING) or ""),
        _authorization_binding(config),
    ):
        raise CheckpointAccessDenied("checkpoint owner/session access denied")


def _preserve_auth_config(config: Mapping[str, object], item: Any) -> Any:
    """Carry authenticated identity through LangGraph's saver-owned config."""
    source_configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
    item_config = getattr(item, "config", {})
    if not isinstance(source_configurable, Mapping) or not isinstance(item_config, Mapping):
        return item
    configurable = dict(item_config.get("configurable", {}))
    for key in ("authenticated_owner", "principal_id", "patient_id"):
        if key in source_configurable:
            configurable[key] = source_configurable[key]
    merged_config = dict(item_config)
    merged_config["configurable"] = configurable
    return item._replace(config=merged_config)


def _is_opaque(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {_OPAQUE_MARKER} and isinstance(value.get(_OPAQUE_MARKER), str)


def _safe_control_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        # Only enum-like control values and versioned metadata remain visible.
        if len(value) <= 64 and all(char.isalnum() or char in "._:/-" for char in value):
            return value
        return None
    if isinstance(value, (list, tuple, set)):
        return [_safe_control_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _safe_control_value(item) for key, item in value.items()}
    return None


def _safe_langgraph_control_value(value: object) -> object:
    """Keep scheduler control values typed while rejecting arbitrary payloads."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= 128 and all(char.isalnum() or char in "._:/-" for char in value):
            return value
        return None
    if isinstance(value, tuple):
        return tuple(_safe_langgraph_control_value(item) for item in value)
    if isinstance(value, list):
        return [_safe_langgraph_control_value(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _safe_langgraph_control_value(item)
            for key, item in value.items()
            if isinstance(key, (str, int))
        }
    return None


def _protect_channel_value(
    channel: str,
    value: object,
    *,
    thread_id: str,
    store: CheckpointPrivacyStore,
) -> object:
    if channel.startswith(_LANGGRAPH_CONTROL_PREFIXES):
        return _safe_langgraph_control_value(value)
    if channel not in _SAFE_CONTROL_CHANNELS:
        return store.put(thread_id, value)
    if not isinstance(value, (type(None), bool, int, float, str, list, tuple, set, Mapping)):
        return store.put(thread_id, value)
    return _safe_control_value(value)


def _restore_value(value: object, store: CheckpointPrivacyStore) -> object:
    if _is_opaque(value):
        reference = str(value[_OPAQUE_MARKER])
        restored = store.get(reference)
        return restored if restored is not None else None
    if isinstance(value, list):
        return [_restore_value(item, store) for item in value]
    if isinstance(value, tuple):
        return tuple(_restore_value(item, store) for item in value)
    if isinstance(value, Mapping):
        return {key: _restore_value(item, store) for key, item in value.items()}
    return value


def _protect_checkpoint(
    checkpoint: Mapping[str, object],
    *,
    thread_id: str,
    store: CheckpointPrivacyStore,
) -> dict[str, object]:
    protected = dict(checkpoint)
    channels = checkpoint.get("channel_values")
    if isinstance(channels, Mapping):
        protected["channel_values"] = {
            str(channel): _protect_channel_value(
                str(channel), value, thread_id=thread_id, store=store
            )
            for channel, value in channels.items()
        }
    return protected


def _restore_checkpoint(checkpoint: Mapping[str, object], store: CheckpointPrivacyStore) -> dict[str, object]:
    restored = dict(checkpoint)
    channels = checkpoint.get("channel_values")
    if isinstance(channels, Mapping):
        restored["channel_values"] = {
            str(channel): _restore_value(value, store)
            for channel, value in channels.items()
        }
    return restored


_SAFE_METADATA_KEYS = frozenset({"source", "step", "parents"})


def _protect_metadata(
    metadata: Mapping[str, object],
    *,
    thread_id: str,
    store: CheckpointPrivacyStore,
) -> dict[str, object]:
    protected: dict[str, object] = {}
    for key, value in metadata.items():
        name = str(key)
        # LangGraph uses these fields to select/replay checkpoints. Preserve
        # their types; writes and all other metadata may contain raw content.
        if name in _SAFE_METADATA_KEYS:
            protected[name] = _safe_control_value(value)
        else:
            protected[name] = store.put(thread_id, value)
    return protected


def _restore_metadata(metadata: Mapping[str, object], store: CheckpointPrivacyStore) -> dict[str, object]:
    return {str(key): _restore_value(value, store) for key, value in metadata.items()}


def _restore_pending_writes(writes: object, store: CheckpointPrivacyStore) -> object:
    if not isinstance(writes, list):
        return writes
    restored: list[object] = []
    for write in writes:
        if isinstance(write, tuple) and len(write) >= 3:
            restored.append((*write[:2], _restore_value(write[2], store), *write[3:]))
        elif isinstance(write, list) and len(write) >= 3:
            restored.append([*write[:2], _restore_value(write[2], store), *write[3:]])
        else:
            restored.append(write)
    return restored


class MetadataOnlyCheckpointSaver:
    """Delegating saver that keeps all persisted channel values metadata-only."""

    def __init__(self, saver: Any, *, store: CheckpointPrivacyStore | None = None) -> None:
        self._saver = saver
        self._store = store or CheckpointPrivacyStore()

    @property
    def raw_saver(self) -> Any:
        return self._saver

    @contextmanager
    def rehydrate(self) -> Iterator[None]:
        token = _REHYDRATE_CHECKPOINTS.set(True)
        try:
            yield
        finally:
            _REHYDRATE_CHECKPOINTS.reset(token)

    def put(
        self,
        config: Mapping[str, object],
        checkpoint: Mapping[str, object],
        metadata: Mapping[str, object],
        new_versions: Mapping[str, object],
    ) -> Any:
        protected = _protect_checkpoint(
            checkpoint,
            thread_id=_thread_id(config),
            store=self._store,
        )
        protected_metadata = _protect_metadata(
            metadata,
            thread_id=_thread_id(config),
            store=self._store,
        )
        protected_metadata[_AUTH_BINDING] = _authorization_binding(config)
        result = self._saver.put(config, protected, protected_metadata, new_versions)
        if isinstance(result, Mapping):
            result = dict(result)
            configurable = dict(result.get("configurable", {}))
            source_configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
            if isinstance(source_configurable, Mapping):
                for key in ("authenticated_owner", "principal_id", "patient_id"):
                    if key in source_configurable:
                        configurable[key] = source_configurable[key]
            result["configurable"] = configurable
        return result

    def get_tuple(self, config: Mapping[str, object]) -> Any:
        item = self._saver.get_tuple(config)
        if item is None:
            return item
        _authorize_checkpoint(config, item)
        item = _preserve_auth_config(config, item)
        if not _REHYDRATE_CHECKPOINTS.get():
            return item
        checkpoint = _restore_checkpoint(item.checkpoint, self._store)
        return item._replace(
            checkpoint=checkpoint,
            metadata=_restore_metadata(item.metadata, self._store),
            pending_writes=_restore_pending_writes(item.pending_writes, self._store),
        )

    async def aget_tuple(self, config: Mapping[str, object]) -> Any:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: Mapping[str, object] | None,
        *,
        filter: Mapping[str, object] | None = None,
        before: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        items = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in items:
            yield item

    async def aput(
        self,
        config: Mapping[str, object],
        checkpoint: Mapping[str, object],
        metadata: Mapping[str, object],
        new_versions: Mapping[str, object],
    ) -> Any:
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: Mapping[str, object],
        writes: Iterable[tuple[str, object]],
        task_id: str,
        task_path: str = "",
    ) -> Any:
        return await asyncio.to_thread(
            self.put_writes, config, writes, task_id, task_path
        )

    async def adelete_thread(self, thread_id: str) -> Any:
        return await asyncio.to_thread(self.delete_thread, thread_id)

    def list(self, config: Mapping[str, object] | None = None, **kwargs: object) -> Iterable[Any]:
        items = self._saver.list(config, **kwargs)
        if not _REHYDRATE_CHECKPOINTS.get():
            return (self._authorize(config, item) for item in items)
        return (
            self._restore_item(config, item)
            for item in items
        )

    def _authorize(self, config: Mapping[str, object], item: Any) -> Any:
        _authorize_checkpoint(config, item)
        return _preserve_auth_config(config, item)

    def _restore_item(self, config: Mapping[str, object], item: Any) -> Any:
        _authorize_checkpoint(config, item)
        item = _preserve_auth_config(config, item)
        return item._replace(
                checkpoint=_restore_checkpoint(item.checkpoint, self._store),
                metadata=_restore_metadata(item.metadata, self._store),
                pending_writes=_restore_pending_writes(item.pending_writes, self._store),
        )

    def put_writes(
        self,
        config: Mapping[str, object],
        writes: Iterable[tuple[str, str, object]],
        task_id: str,
        task_path: str = "",
    ) -> Any:
        protected_writes = [
            (
                channel,
                _protect_channel_value(
                    str(channel), value, thread_id=_thread_id(config), store=self._store
                ),
            )
            for channel, value in writes
        ]
        return self._saver.put_writes(config, protected_writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> Any:
        self._store.delete_thread(thread_id)
        return self._saver.delete_thread(thread_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._saver, name)


def ensure_metadata_only_checkpointer(checkpointer: Any) -> MetadataOnlyCheckpointSaver:
    if isinstance(checkpointer, MetadataOnlyCheckpointSaver):
        return checkpointer
    return MetadataOnlyCheckpointSaver(checkpointer)
