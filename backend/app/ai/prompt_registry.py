"""Resolve graph prompts from Langfuse prompt management, with in-code fallbacks.

Every prompt has a default in ``app.ai.prompt_defaults``. When
``LANGFUSE_PROMPTS_ENABLED`` is true and the Langfuse client is available (see
``app.observability.langfuse_tracing``), the registry fetches the version
carrying ``LANGFUSE_PROMPT_LABEL`` (default ``production``) and caches it for
``LANGFUSE_PROMPT_CACHE_TTL_SECONDS``. Otherwise, and whenever a fetch fails,
the prompt is missing, or the Langfuse text is unsafe to compile, the in-code
default is used, so a turn never depends on Langfuse being reachable.

A Langfuse version must use exactly the default's ``{{variables}}``: a version
that drops a variable (which would silently lose request data) or adds an
unknown one (which would reach the model as literal braces) is rejected in
favour of the default.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal

from app.ai.prompt_defaults import DEFAULT_PROMPTS

logger = logging.getLogger("app.prompt_registry")

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_DEFAULT_LABEL = "production"
_DEFAULT_CACHE_TTL_SECONDS = 60
# Missing or invalid prompts are not refetched on every model call.
_unavailable_until: dict[tuple[str, str], float] = {}
_unavailable_lock = Lock()


@dataclass(frozen=True)
class ResolvedPrompt:
    name: str
    text: str
    source: Literal["langfuse", "default"]
    version: int | None = None
    label: str | None = None
    langfuse_prompt: Any | None = None

    def run_config(self, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return a LangChain run config that links the generation to this version.

        The Langfuse callback links a generation to the ``langfuse_prompt`` found in
        the metadata of a parent chain run; default prompts add nothing.
        """
        merged: dict[str, Any] = dict(config or {})
        if self.langfuse_prompt is not None:
            metadata = dict(merged.get("metadata") or {})
            metadata["langfuse_prompt"] = self.langfuse_prompt
            merged["metadata"] = metadata
        return merged


def template_variables(text: str) -> set[str]:
    return set(_VARIABLE.findall(text))


def compile_prompt(text: str, variables: Mapping[str, Any]) -> str:
    """Fill ``{{name}}`` placeholders in one pass; values are never re-parsed."""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            return match.group(0)
        value = variables[key]
        return "" if value is None else str(value)

    return _VARIABLE.sub(_replace, text)


def _settings_value(name: str, default: Any) -> Any:
    try:
        from app.core.config import get_settings

        value = getattr(get_settings(), name, default)
    except Exception:
        return default
    return default if value is None else value


def prompts_from_langfuse_enabled() -> bool:
    return bool(_settings_value("langfuse_prompts_enabled", False))


def _label() -> str:
    return str(_settings_value("langfuse_prompt_label", _DEFAULT_LABEL) or _DEFAULT_LABEL).strip() or _DEFAULT_LABEL


def _cache_ttl_seconds() -> int:
    try:
        return max(0, int(_settings_value("langfuse_prompt_cache_ttl_seconds", _DEFAULT_CACHE_TTL_SECONDS)))
    except (TypeError, ValueError):
        return _DEFAULT_CACHE_TTL_SECONDS


def _mark_unavailable(key: tuple[str, str], ttl: int, reason: str) -> None:
    logger.warning("langfuse_prompt_fallback name=%s label=%s reason=%s", key[0], key[1], reason)
    with _unavailable_lock:
        _unavailable_until[key] = time.monotonic() + max(ttl, 1)


def _recently_unavailable(key: tuple[str, str]) -> bool:
    with _unavailable_lock:
        until = _unavailable_until.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            del _unavailable_until[key]
            return False
        return True


def _fetch_langfuse_prompt(name: str, default_text: str) -> tuple[str, Any] | None:
    if not prompts_from_langfuse_enabled():
        return None
    from app.observability.langfuse_tracing import get_langfuse_client

    client = get_langfuse_client()
    if client is None:
        return None
    label = _label()
    key = (name, label)
    if _recently_unavailable(key):
        return None
    ttl = _cache_ttl_seconds()
    try:
        prompt = client.get_prompt(
            name,
            label=label,
            type="text",
            cache_ttl_seconds=ttl,
            fallback=default_text,
            max_retries=1,
            fetch_timeout_seconds=5,
        )
    except Exception as exc:
        _mark_unavailable(key, ttl, f"fetch_failed:{type(exc).__name__}")
        return None
    if getattr(prompt, "is_fallback", False):
        _mark_unavailable(key, ttl, "not_found_or_unreachable")
        return None
    text = getattr(prompt, "prompt", None)
    if not isinstance(text, str) or not text.strip():
        _mark_unavailable(key, ttl, "not_a_text_prompt")
        return None
    expected = template_variables(default_text)
    actual = template_variables(text)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        _mark_unavailable(key, ttl, f"variable_mismatch missing={missing} unknown={unknown}")
        return None
    return text, prompt


def get_prompt(name: str, /, **variables: Any) -> ResolvedPrompt:
    """Return the compiled prompt ``name``, from Langfuse when enabled."""
    default = DEFAULT_PROMPTS[name]
    expected = template_variables(default.text)
    missing = expected - set(variables)
    if missing:
        raise KeyError(f"prompt {name!r} requires variables {sorted(missing)}")
    fetched = _fetch_langfuse_prompt(name, default.text)
    if fetched is None:
        return ResolvedPrompt(name=name, text=compile_prompt(default.text, variables), source="default")
    text, prompt = fetched
    return ResolvedPrompt(
        name=name,
        text=compile_prompt(text, variables),
        source="langfuse",
        version=getattr(prompt, "version", None),
        label=_label(),
        langfuse_prompt=prompt,
    )


def prompt_text(name: str, /, **variables: Any) -> str:
    return get_prompt(name, **variables).text


def link_prompt(runnable: Any, prompt: ResolvedPrompt) -> Any:
    """Return ``runnable`` set up so a Langfuse trace links its generation to ``prompt``.

    The Langfuse callback reads ``langfuse_prompt`` from a chain run's metadata and
    registers it on that run's *parent*, where it would also link later, unrelated
    generations made by the same graph node. The metadata-carrying sequence is
    therefore nested inside a private outer sequence: the prompt registers there
    and is dropped when that run ends. Default prompts, and objects that are not
    LangChain runnables (such as test doubles), are returned unchanged.
    """
    if prompt.langfuse_prompt is None:
        return runnable
    try:
        from langchain_core.runnables import Runnable, RunnablePassthrough
    except ImportError:
        return runnable
    if not isinstance(runnable, Runnable):
        return runnable
    linked = (RunnablePassthrough() | runnable).with_config(prompt.run_config())
    return RunnablePassthrough() | linked


def _reset_for_tests() -> None:
    with _unavailable_lock:
        _unavailable_until.clear()
