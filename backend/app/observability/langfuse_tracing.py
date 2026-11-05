"""Optional LLM tracing to a self-hosted Langfuse instance.

This is a developer debugging aid, separate from the privacy-safe operational
trace in ``agent_trace``. Langfuse receives full prompts and model outputs, so:

* it is off unless ``LANGFUSE_ENABLED`` is true and both keys are configured;
* it is refused when ``ENVIRONMENT=production`` unless
  ``LANGFUSE_ALLOW_PRODUCTION`` is also true;
* every exported string passes through the same secret redaction used for
  internal graph logs;
* any failure (missing package, bad config, unreachable server) disables
  tracing instead of affecting a turn.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from threading import Lock
from typing import Any

logger = logging.getLogger("app.langfuse_tracing")

_lock = Lock()
_initialized = False
_client: Any | None = None
_public_key: str | None = None


def parse_trace_tags(raw: str | None) -> list[str]:
    """Split a comma-separated tag list, dropping blanks and duplicates."""
    tags: list[str] = []
    for part in str(raw or "").split(","):
        tag = part.strip()[:200]
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _configured_tags() -> list[str]:
    from app.core.config import get_settings

    return parse_trace_tags(getattr(get_settings(), "langfuse_trace_tags", None))


def _redact(data: Any) -> Any:
    """Recursively redact secret-looking substrings before export."""
    from app.services.ai_turn_persistence import redact_internal_text

    if isinstance(data, str):
        return redact_internal_text(data)
    if isinstance(data, Mapping):
        return {key: _redact(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_redact(value) for value in data]
    return data


def _mask(*, data: Any, **_kwargs: Any) -> Any:
    try:
        return _redact(data)
    except Exception:
        return "[REDACTION_FAILED]"


def _init_client() -> Any | None:
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.langfuse_enabled:
        return None
    if settings.environment == "production" and not settings.langfuse_allow_production:
        logger.warning(
            "langfuse_disabled: LANGFUSE_ENABLED is set in production without LANGFUSE_ALLOW_PRODUCTION"
        )
        return None
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.warning("langfuse_disabled: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning("langfuse_disabled: the langfuse package is not installed")
        return None

    global _public_key
    _public_key = settings.langfuse_public_key
    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        environment=(getattr(settings, "langfuse_tracing_environment", None) or "").strip()
        or settings.environment,
        release=settings.build_revision or None,
        mask=_mask,
    )
    logger.info("langfuse_enabled base_url=%s", settings.langfuse_base_url)
    return client


def get_langfuse_client() -> Any | None:
    """Return the shared Langfuse client, or ``None`` when tracing is off."""
    global _initialized, _client
    if _initialized:
        return _client
    with _lock:
        if not _initialized:
            try:
                _client = _init_client()
            except Exception:
                logger.warning("langfuse_disabled: client initialization failed", exc_info=True)
                _client = None
            _initialized = True
    return _client


def langfuse_enabled() -> bool:
    return get_langfuse_client() is not None


def langfuse_callbacks() -> list[Any]:
    """LangChain callback handlers to attach to a run; empty when tracing is off."""
    if get_langfuse_client() is None:
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler(public_key=_public_key)]
    except Exception:
        logger.warning("langfuse_callback_unavailable", exc_info=True)
        return []


def with_langfuse_callbacks(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a copy of a LangChain run config with Langfuse callbacks added."""
    merged: dict[str, Any] = dict(config or {})
    callbacks = langfuse_callbacks()
    if callbacks:
        existing = merged.get("callbacks")
        if isinstance(existing, list):
            merged["callbacks"] = [*existing, *callbacks]
        elif existing is None:
            merged["callbacks"] = callbacks
        else:
            # A callback manager is already attached; leave it untouched.
            return merged
    return merged


@contextmanager
def langfuse_trace_attributes(
    *,
    trace_name: str,
    session_id: str | None = None,
    tags: list[str] | None = None,
) -> Iterator[None]:
    """Group spans created inside the block under one named trace/session.

    The session id is the RehabFlow chat session UUID, so a Langfuse session
    lines up with the ``ai_internal_messages`` rows for that session. No user
    or patient identifier is sent. ``LANGFUSE_TRACE_TAGS`` is added to the
    given tags.
    """
    if get_langfuse_client() is None:
        yield
        return
    merged_tags = _configured_tags()
    merged_tags += [tag for tag in tags or () if tag not in merged_tags]
    try:
        from langfuse import propagate_attributes

        manager = propagate_attributes(
            trace_name=trace_name,
            session_id=str(session_id) if session_id else None,
            tags=merged_tags or None,
        )
    except Exception:
        logger.debug("langfuse_propagate_attributes_unavailable", exc_info=True)
        manager = nullcontext()
    with manager:
        yield


def _current_langchain_observation() -> Any | None:
    """The Langfuse span of the LangChain run (e.g. graph node) we are inside.

    The LangChain handler parents its spans explicitly rather than making them
    the active OpenTelemetry span, so a direct SDK call inside a node would
    otherwise start a separate trace. LangChain exposes the current run's
    callback manager through its run-config context variable; it carries the
    parent run id, which the handler maps to its span.
    """
    try:
        from langchain_core.runnables.config import var_child_runnable_config
        from langfuse.langchain import CallbackHandler
    except Exception:
        return None
    config = var_child_runnable_config.get()
    if not config:
        return None
    manager = config.get("callbacks")
    parent_run_id = getattr(manager, "parent_run_id", None)
    handlers = getattr(manager, "handlers", None)
    if parent_run_id is None or not handlers:
        return None
    for handler in handlers:
        if isinstance(handler, CallbackHandler):
            runs = getattr(handler, "_runs", None)
            if isinstance(runs, Mapping) and parent_run_id in runs:
                return runs[parent_run_id]
    return None


class _NoopObservation:
    def update(self, **_kwargs: Any) -> "_NoopObservation":
        return self


@contextmanager
def langfuse_generation(
    *,
    name: str,
    model: str | None = None,
    input: Any = None,
    model_parameters: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """Record a direct (non-LangChain) model call as a Langfuse generation.

    Nests under the current span, so a call made inside a graph node appears
    inside that turn's trace. Yields an object with ``update(...)`` for the
    output and usage; it is a no-op when tracing is off or unavailable.
    """
    client = get_langfuse_client()
    if client is None:
        yield _NoopObservation()
        return
    try:
        parent = _current_langchain_observation() or client
        manager = parent.start_as_current_observation(
            name=name,
            as_type="generation",
            model=model,
            input=input,
            model_parameters=dict(model_parameters) if model_parameters else None,
            metadata=dict(metadata) if metadata else None,
        )
        observation = manager.__enter__()
    except Exception:
        logger.debug("langfuse_generation_unavailable", exc_info=True)
        yield _NoopObservation()
        return
    try:
        yield observation
    except BaseException as exc:
        try:
            observation.update(level="ERROR", status_message=type(exc).__name__)
        except Exception:
            pass
        manager.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            logger.debug("langfuse_generation_end_failed", exc_info=True)


def anthropic_usage_details(response: Any) -> dict[str, int]:
    """Map an Anthropic ``usage`` object to Langfuse usage details."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    details: dict[str, int] = {}
    for source, target in (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_input_tokens", "cache_read_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
    ):
        value = getattr(usage, source, None)
        if isinstance(value, int):
            details[target] = value
    server_tools = getattr(usage, "server_tool_use", None)
    searches = getattr(server_tools, "web_search_requests", None)
    if isinstance(searches, int):
        details["web_search_requests"] = searches
    return details


def flush_langfuse() -> None:
    client = _client if _initialized else None
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        logger.debug("langfuse_flush_failed", exc_info=True)


def shutdown_langfuse() -> None:
    client = _client if _initialized else None
    if client is None:
        return
    try:
        client.shutdown()
    except Exception:
        logger.debug("langfuse_shutdown_failed", exc_info=True)


def _reset_for_tests() -> None:
    global _initialized, _client, _public_key
    with _lock:
        _initialized = False
        _client = None
        _public_key = None
