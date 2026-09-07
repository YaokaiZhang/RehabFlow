"""Deterministic, privacy-safe operational traces for agent turns.

Trace events are deliberately narrower than product conversation records and
checkpoint state. This module contains only control metadata needed to explain
which workflow path ran, how it ended, and whether a bounded tool/reviewer
step succeeded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from threading import RLock
from time import perf_counter
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


EventType = Literal[
    "turn_started",
    "node_started",
    "node_finished",
    "tool_finished",
    "checkpointed",
    "review_finished",
    "repair_started",
    "turn_finished",
    "turn_failed",
]

TRACE_HMAC_KEY_ENV = "REHAB_TRACE_HMAC_KEY"
TRACE_HMAC_KEY_VERSION_ENV = "REHAB_TRACE_HMAC_KEY_VERSION"
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_SESSION_HASH_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}:[0-9a-f]{64}$")
_DEVELOPMENT_FALLBACK_KEY = b"rehabflow-trace-dev-key-v1"
_DEVELOPMENT_ENVIRONMENTS = frozenset({"dev", "development", "test"})
_PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
_SAFE_SOURCE_ID_RE = re.compile(
    r"^(?:rehab_knowledge|rehab_activity|exercise|evidence):[A-Za-z0-9_.-]{1,224}$"
)
_IDENTITY_LIKE_RE = re.compile(
    r"(?:^|[_:./-])(patient|session|episode|care_episode|stream|ticket)"
    r"(?:$|[_:./-])",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+|authorization\s*[:=]|password\s*[:=]|secret\s*[:=]|"
    r"api[_-]?key\s*[:=]|token\s*[:=]|-----BEGIN|sk-[A-Za-z0-9])",
    re.IGNORECASE,
)
_ALLOWED_TRACE_TOOL_NAMES = frozenset({"rehab_exercise_kb_search", "web_search", "evidence_adapter"})
_ALLOWED_TRACE_NODES = frozenset(
	{
		"context_integrity",
		"router",
		"consultant",
		"consultant_repair",
		"safety_reviewer",
		"grounding_reviewer",
		"evidence_planner",
		"gather_evidence",
		"evidence_join",
		"context_triage",
		"policy_gate",
		"review_join",
		"clarification",
		"repair_revalidate_context",
		"unsafe_fallback",
	}
)
_ALLOWED_TRACE_ROUTES = frozenset({"urgent", "clarify", "consultant", "unsupported"})
_ALLOWED_TRACE_OUTCOMES = frozenset(
	{
		"started",
		"ok",
		"completed",
		"released",
		"held",
		"awaiting_input",
		"checkpointed",
		"failed",
		"approve",
		"reject",
		"error",
		"timeout",
		"blocked",
		"pass",
		"repairable",
		"hard_stop",
		"unsafe_fallback",
	}
)
_ALLOWED_TRACE_FAILURES = frozenset(
	{
		"provider_error",
		"tool_error",
		"tool_timeout",
		"evidence_error",
		"evidence_timeout",
		"forbidden",
		"checkpoint_unavailable",
		"policy_rejection",
		"interrupted",
		"runtime_error",
	}
)

_ALLOWED_METADATA_KEYS = frozenset(
    {
        "node",
        "route",
        "elapsed_ms",
        "input_tokens",
        "output_tokens",
        "source_ids",
        "tool_name",
        "outcome",
        "failure_category",
        "attempt",
    }
)
_FORBIDDEN_METADATA_KEY_RE = re.compile(
    r"(?:message|prompt|response|content|text|memory|evidence|credential|"
    r"authorization|jwt|ticket|exception|secret|password|api[_-]?key|"
    r"patient[_-]?id|session[_-]?id|care[_-]?episode[_-]?id)",
    re.IGNORECASE,
)


class TracePrivacyError(ValueError):
    """Raised when a caller attempts to put content or identity into a trace."""


def _key_bytes(key: str | bytes | bytearray | None) -> bytes:
    if key is not None:
        value = key.encode("utf-8") if isinstance(key, str) else bytes(key)
        if not value:
            raise ValueError("trace HMAC key must not be empty")
        return value

    explicit_environment = next(
        (
            value.strip().lower()
            for name in ("ENVIRONMENT", "REHAB_ENVIRONMENT")
            if (value := os.getenv(name))
            and value.strip()
        ),
        None,
    )
    if explicit_environment is None:
        try:
            from app.core.config import backend_env_paths

            for env_file in reversed(backend_env_paths()):
                try:
                    lines = env_file.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError):
                    continue
                for line in lines:
                    normalized_line = line.strip()
                    if normalized_line.startswith("export "):
                        normalized_line = normalized_line[7:].lstrip()
                    name, separator, value = normalized_line.partition("=")
                    if not separator or name.strip() not in {
                        "ENVIRONMENT",
                        "REHAB_ENVIRONMENT",
                    }:
                        continue
                    candidate = value.strip()
                    if (
                        len(candidate) >= 2
                        and candidate[0] == candidate[-1]
                        and candidate[0] in {"'", '"'}
                    ):
                        candidate = candidate[1:-1].strip()
                    if candidate:
                        explicit_environment = candidate.lower()
                        break
                if explicit_environment is not None:
                    break
        except (OSError, UnicodeError):
            pass
    try:
        from app.core.config import get_settings

        settings = get_settings()
    except Exception:
        if explicit_environment in _DEVELOPMENT_ENVIRONMENTS:
            return _DEVELOPMENT_FALLBACK_KEY
        if explicit_environment in _PRODUCTION_ENVIRONMENTS:
            raise ValueError(
                "trace HMAC key must be explicitly configured in production"
            ) from None
        raise ValueError("trace HMAC configuration could not be validated") from None

    configured_environment = str(getattr(settings, "environment", "") or "").strip().lower()
    environment = explicit_environment or configured_environment
    configured = str(getattr(settings, "trace_hmac_key", "") or "").strip()
    if configured:
        return configured.encode("utf-8")
    if environment in _PRODUCTION_ENVIRONMENTS:
        raise ValueError("trace HMAC key must be explicitly configured in production")
    if environment in _DEVELOPMENT_ENVIRONMENTS:
        return _DEVELOPMENT_FALLBACK_KEY
    raise ValueError(
        "trace HMAC key requires an explicit test/development environment"
    )


def _key_version() -> str:
	try:
		from app.core.config import get_settings

		value = str(get_settings().trace_hmac_key_version or "v1").strip()
	except Exception:
		# _key_bytes performs the environment fail-closed check. This fallback is
		# only reached by explicit test/development callers with a supplied key.
		value = "v1"
	if not value or not _SAFE_VALUE_RE.fullmatch(value):
		return "v1"
	return value[:32]


def derive_session_hash(
    session_id: object,
    *,
    key: str | bytes | bytearray | None = None,
) -> str:
    """Derive a rotation-scoped opaque correlation value from a session ID."""

    normalized = str(session_id or "anonymous").strip() or "anonymous"
    digest = hmac.new(
        _key_bytes(key),
        b"rehabflow:agent-trace:session:" + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{_key_version()}:{digest}"


def _safe_scalar(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TracePrivacyError(f"{field_name} must be a safe metadata string")
    normalized = value.strip()
    if not normalized or not _SAFE_VALUE_RE.fullmatch(normalized):
        raise TracePrivacyError(f"{field_name} contains non-contract text")
    if _JWT_RE.fullmatch(normalized) or _SECRET_VALUE_RE.search(normalized):
        raise TracePrivacyError(f"{field_name} contains secret-like text")
    return normalized


def _safe_source_id(value: object) -> str:
    normalized = _safe_scalar(value, field_name="source_id")
    if not _SAFE_SOURCE_ID_RE.fullmatch(normalized) or _IDENTITY_LIKE_RE.search(normalized):
        raise TracePrivacyError("source_id is not a public allowlisted identifier")
    return normalized


def is_safe_trace_source_id(value: object) -> bool:
    try:
        _safe_source_id(value)
    except TracePrivacyError:
        return False
    return True


def _safe_tool_name(value: object) -> str | None:
    if value is None:
        return None
    normalized = _safe_scalar(value, field_name="tool_name")
    if normalized not in _ALLOWED_TRACE_TOOL_NAMES:
        raise TracePrivacyError("tool_name is not an allowlisted tool")
    return normalized


def _safe_source_ids(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
        raise TracePrivacyError("source_ids must be a sequence of safe identifiers")
    normalized: list[str] = []
    seen: set[str] = set()
    for source_id in value:
        item = _safe_source_id(source_id)
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return normalized


def _safe_optional_scalar(value: object, *, field_name: str) -> str | None:
	if value is None:
		return None
	normalized = _safe_scalar(value, field_name=field_name)
	allowed = {
		"node": _ALLOWED_TRACE_NODES,
		"route": _ALLOWED_TRACE_ROUTES,
		"outcome": _ALLOWED_TRACE_OUTCOMES,
		"failure_category": _ALLOWED_TRACE_FAILURES,
	}.get(field_name)
	if allowed is not None and normalized not in allowed:
		raise TracePrivacyError(f"{field_name} is not an allowlisted trace category")
	return normalized


class AgentTraceEvent(BaseModel):
    """The complete allowlist for an operational agent trace event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: UUID
    session_hash: str
    turn_id: UUID
    workflow_version: str
    event_index: int = Field(ge=0)
    event_type: EventType
    node: str | None = None
    route: str | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    source_ids: list[str] = Field(default_factory=list)
    tool_name: str | None = None
    outcome: str | None = None
    failure_category: str | None = None
    attempt: int = Field(default=0, ge=0)

    @field_validator("session_hash")
    @classmethod
    def validate_session_hash(cls, value: str) -> str:
        if not _SESSION_HASH_RE.fullmatch(value):
            raise TracePrivacyError("session_hash must be a versioned HMAC digest")
        return value

    @field_validator("workflow_version")
    @classmethod
    def validate_workflow_version(cls, value: str) -> str:
        return _safe_scalar(value, field_name="workflow_version")

    @field_validator("node", "route", "tool_name", "outcome", "failure_category")
    @classmethod
    def validate_safe_optional_text(cls, value: str | None, info: Any) -> str | None:
        if info.field_name == "tool_name":
            return _safe_tool_name(value)
        return _safe_optional_scalar(value, field_name=info.field_name)

    @field_validator("source_ids")
    @classmethod
    def validate_safe_sources(cls, value: list[str]) -> list[str]:
        return _safe_source_ids(value)

    # Existing development sinks treated trace events as mappings. These tiny
    # compatibility methods do not widen the serialized contract.
    def get(self, key: str, default: object = None) -> object:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)


class TraceSink(Protocol):
    """Destination for already-sanitized operational events."""

    def record(self, event: AgentTraceEvent) -> None:
        ...


def sanitize_trace_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """Validate the explicit trace metadata allowlist before event creation."""

    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TracePrivacyError("trace metadata must be a mapping")
    sanitized: dict[str, object] = {}
    for raw_key, value in metadata.items():
        key = str(raw_key)
        if key not in _ALLOWED_METADATA_KEYS:
            if _FORBIDDEN_METADATA_KEY_RE.search(key):
                raise TracePrivacyError(f"forbidden trace metadata key: {key}")
            raise TracePrivacyError(f"trace metadata key is not allowlisted: {key}")
        if key == "source_ids":
            sanitized[key] = _safe_source_ids(value)
        elif key in {"elapsed_ms", "input_tokens", "output_tokens", "attempt"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TracePrivacyError(f"{key} must be a non-negative integer")
            sanitized[key] = value
        elif key == "tool_name":
            sanitized[key] = _safe_tool_name(value)
        elif key in {"node", "route", "outcome", "failure_category"}:
            sanitized[key] = _safe_optional_scalar(value, field_name=key)
    return sanitized


class NoOpTraceSink:
    """Default sink used when operational trace export is not configured."""

    def record(self, _event: AgentTraceEvent) -> None:
        return None


class InMemoryTraceSink:
    """Thread-safe sink intended for deterministic tests and local diagnostics."""

    def __init__(self) -> None:
        self._events: list[AgentTraceEvent] = []
        self._lock = RLock()

    @property
    def events(self) -> list[AgentTraceEvent]:
        with self._lock:
            return list(self._events)

    def record(self, event: AgentTraceEvent) -> None:
        with self._lock:
            self._events.append(event)


class StructuredLogTraceSink:
    """JSON structured-log sink; it never receives raw model or patient text."""

    def __init__(self, logger: logging.Logger | str | None = None) -> None:
        self.logger = logging.getLogger(logger) if isinstance(logger, str) else (logger or logging.getLogger("app.agent_trace"))

    def record(self, event: AgentTraceEvent) -> None:
        payload = event.model_dump(mode="json")
        self.logger.info("agent_trace %s", json.dumps(payload, sort_keys=True, separators=(",", ":")))


def record_trace_event(sink: TraceSink | Callable[[AgentTraceEvent], object] | None, event: AgentTraceEvent) -> None:
    """Best-effort export adapter, including the legacy callable sink seam."""

    try:
        record = getattr(sink, "record", None)
        if callable(record):
            record(event)
        elif callable(sink):
            sink(event)
    except Exception:
        logging.getLogger("app.agent_trace").debug("trace_sink_export_failed")


class TraceContext:
    """Owns correlation IDs and an atomic event index for one graph turn."""

    def __init__(
        self,
        *,
        session_id: object,
        workflow_version: str,
        sink: TraceSink | Callable[[AgentTraceEvent], object] | None = None,
        trace_id: UUID | None = None,
        turn_id: UUID | None = None,
        hmac_key: str | bytes | bytearray | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.trace_id = trace_id or uuid4()
        self.turn_id = turn_id or uuid4()
        self.session_hash = derive_session_hash(session_id, key=hmac_key)
        self.workflow_version = workflow_version
        self.sink = sink or NoOpTraceSink()
        self._clock = clock
        self._event_index = 0
        self._lock = RLock()
        self._started_at = clock()
        self._closed = False

    @property
    def event_index(self) -> int:
        with self._lock:
            return self._event_index

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def emit(
        self,
        event_type: EventType | str,
        *,
        metadata: Mapping[str, object] | None = None,
        node: str | None = None,
        route: str | None = None,
        elapsed_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
        tool_name: str | None = None,
        outcome: str | None = None,
        failure_category: str | None = None,
        attempt: int = 0,
    ) -> AgentTraceEvent:
        fields: dict[str, object] = {
            "node": node,
            "route": route,
            "elapsed_ms": elapsed_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "source_ids": source_ids,
            "tool_name": tool_name,
            "outcome": outcome,
            "failure_category": failure_category,
            "attempt": attempt,
        }
        fields.update(sanitize_trace_metadata(metadata))
        if source_ids is not None:
            fields["source_ids"] = _safe_source_ids(source_ids)
        if tool_name is not None:
            fields["tool_name"] = _safe_tool_name(tool_name)
        fields = {key: value for key, value in fields.items() if value is not None}
        with self._lock:
            event = AgentTraceEvent(
                trace_id=self.trace_id,
                session_hash=self.session_hash,
                turn_id=self.turn_id,
                workflow_version=self.workflow_version,
                event_index=self._event_index,
                event_type=event_type,
                **fields,
            )
            self._event_index += 1
            if event_type in {"turn_finished", "turn_failed"}:
                self._closed = True
            record_trace_event(self.sink, event)
        return event

    def elapsed_ms(self) -> int:
        return max(0, round((self._clock() - self._started_at) * 1000))


class TraceContextRegistry:
    """Shares one TraceContext across graph nodes and concurrent reviewer branches."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_alias: dict[tuple[str, str], TraceContext] = {}

    def for_state(
        self,
        state: Mapping[str, object],
        *,
        workflow_version: str,
        sink: TraceSink | Callable[[AgentTraceEvent], object] | None,
    ) -> TraceContext:
        session_key = str(state.get("session_id") or "anonymous")
        alias = str(
            state.get("trace_turn_id")
            or state.get("evidence_turn_id")
            or uuid4()
        )
        key = (session_key, alias)
        with self._lock:
            context = self._by_alias.get(key)
            if context is None or context.closed:
                context = TraceContext(
                    session_id=session_key,
                    workflow_version=workflow_version,
                    sink=sink,
                )
            self._by_alias[key] = context
            return context
