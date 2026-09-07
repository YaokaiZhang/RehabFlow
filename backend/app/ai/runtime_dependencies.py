from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from app.ai.workflow_policy import WorkflowPolicy
from app.ai.evidence_registry import EvidenceRegistry, build_production_evidence_registry
from app.core.config import Settings, evaluator_fault_profile, get_settings
from app.observability.agent_trace import NoOpTraceSink, StructuredLogTraceSink, TraceSink


def evaluator_openai_options() -> dict[str, Any]:
    """Bound evaluator provider waits without changing production client behavior."""
    if os.getenv("REHAB_EVAL_MODE", "").strip().lower() != "true":
        return {}
    raw_timeout = os.getenv("REHAB_EVAL_OPENAI_TIMEOUT_SECONDS", "90")
    raw_retries = os.getenv("REHAB_EVAL_OPENAI_MAX_RETRIES", "2")
    try:
        timeout = float(raw_timeout)
        max_retries = int(raw_retries)
    except ValueError:
        return {}
    if timeout <= 0 or max_retries < 0:
        return {}
    return {"timeout": timeout, "max_retries": max_retries}


def openai_client_options(
    *,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """Return the bounded timeout/retry policy shared by production model clients."""
    options: dict[str, Any] = {"timeout": 120.0, "max_retries": 2}
    try:
        timeout = float(
            os.getenv(
                "REHAB_OPENAI_TIMEOUT_SECONDS",
                str(options["timeout"]),
            )
        )
        if timeout > 0:
            options["timeout"] = timeout
    except ValueError:
        pass
    try:
        max_retries = int(
            os.getenv(
                "REHAB_OPENAI_MAX_RETRIES",
                str(options["max_retries"]),
            )
        )
        if max_retries >= 0:
            options["max_retries"] = max_retries
    except ValueError:
        pass
    if os.getenv("REHAB_EVAL_MODE", "").strip().lower() == "true":
        options.update(evaluator_openai_options())
    if timeout_seconds is not None:
        options["timeout"] = timeout_seconds
    if max_retries is not None:
        options["max_retries"] = max_retries
    return options


def with_responses_provider_retry(runnable: Any) -> Any:
    """Retry an immediate Responses provider lookup failure once."""
    retry = getattr(runnable, "with_retry", None)
    if not callable(retry):
        return runnable
    try:
        from openai import NotFoundError
    except ImportError:
        return runnable
    try:
        return retry(
            retry_if_exception_type=(NotFoundError,),
            wait_exponential_jitter=False,
            stop_after_attempt=2,
        )
    except TypeError:
        return runnable


def structured_output_model(model: Any, schema: type[Any]) -> Any:
    """Bind structured output through the active model transport."""
    factory = getattr(model, "with_structured_output", None)
    if not callable(factory):
        raise TypeError("model does not support structured output")
    if getattr(model, "use_responses_api", False):
        return with_responses_provider_retry(factory(schema, method="json_schema"))
    return factory(schema)


class RetrievalAdapter(Protocol):
    def rehab_exercise_kb_search(self, query: str, limit: int) -> list[dict[str, Any]]: ...


class ExerciseCatalogAdapter(Protocol):
    def suggest(self, docs: object, *, limit: int) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ReviewerRuntimeConfig:
    # Per-reviewer timeout and call budget; reviewers never share this budget.
    timeout_seconds: float = 10.0
    max_calls: int = 2

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("reviewer timeout_seconds must be positive")
        if self.max_calls < 0:
            raise ValueError("reviewer max_calls must be non-negative")


@dataclass(frozen=True)
class AgentRuntimeDependencies:
    router_model: Any
    consultant_model: Any
    reviewer_model: Any
    retrieval: RetrievalAdapter | Any
    catalog: ExerciseCatalogAdapter | Any
    policy: WorkflowPolicy
    checkpointer: Any
    trace_sink: TraceSink | Any = field(default_factory=NoOpTraceSink)
    web_search: Callable[[str], Any] | None = None
    tool_timeout_seconds: float = 20.0
    safety_reviewer_model: Any | None = None
    grounding_reviewer_model: Any | None = None
    safety_reviewer_runtime: ReviewerRuntimeConfig = ReviewerRuntimeConfig()
    grounding_reviewer_runtime: ReviewerRuntimeConfig = ReviewerRuntimeConfig()
    evidence_registry: EvidenceRegistry | None = None
    clock: Any | None = None
    session_compaction_model: Any | None = None
    episode_memory_model: Any | None = None
    patient_memory_model: Any | None = None
    session_factory: Callable[[], Any] | None = None
    session_compaction_timeout_seconds: float = 120.0
    memory_agent_timeout_seconds: float = 120.0


def configured_trace_sink(settings: Settings | None = None) -> TraceSink:
    """Build the server-owned trace sink; requests never participate in this choice."""

    configuration = settings or get_settings()
    if not configuration.tracing_enabled:
        return NoOpTraceSink()
    return StructuredLogTraceSink()


class LazyChatModel:
    """Provider proxy that defers construction until a graph node invokes it."""

    def __init__(self, loader: Callable[[], Any]) -> None:
        self._loader = loader
        self._model: Any | None = None
        self._lock = threading.Lock()

    def _loaded(self) -> Any:
        model = self._model
        if model is None:
            with self._lock:
                model = self._model
                if model is None:
                    model = self._loader()
                    self._model = model
        return model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loaded(), name)


class OpenAIProviderFactory:
    """Lazy OpenAI provider factory used by the default runtime."""

    def __init__(self, *, overrides: dict[str, Any] | None = None) -> None:
        self._overrides = dict(overrides or {})
        self._models: dict[str, Any] = {}

    def model(
        self,
        key: str,
        model_name: str,
        temperature: float,
        *,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        if key not in self._models:
            self._models[key] = LazyChatModel(
                lambda: self._build_model(
                    model_name,
                    temperature,
                    timeout_seconds=timeout_seconds,
                    max_retries=max_retries,
                )
            )
        return self._models[key]

    def router_model(self) -> Any:
        return self.model("router", get_settings().openai_model, 0.1)

    def consultant_model(self) -> Any:
        return self.model("consultant", get_settings().openai_model, 0.4)

    def session_compaction_model(self) -> Any:
        settings = get_settings()
        return self.model(
            "session_compaction",
            settings.openai_model,
            0.2,
            timeout_seconds=settings.session_compaction_timeout_seconds,
            max_retries=0,
        )

    def episode_memory_model(self) -> Any:
        settings = get_settings()
        return self.model(
            "episode_memory",
            settings.openai_model,
            0.2,
            timeout_seconds=settings.memory_agent_timeout_seconds,
            max_retries=0,
        )

    def patient_memory_model(self) -> Any:
        settings = get_settings()
        return self.model(
            "patient_memory",
            settings.openai_model,
            0.2,
            timeout_seconds=settings.memory_agent_timeout_seconds,
            max_retries=0,
        )

    @staticmethod
    def _build_model(
        model_name: str,
        temperature: float,
        *,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> Any:
        from langchain_openai import ChatOpenAI

        from app.core.config import get_settings

        if evaluator_fault_profile("provider_failure"):
            raise RuntimeError("evaluator provider fault profile")
        settings = get_settings()
        openai_api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY")
        if not openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set. Please set the environment variable or backend/.env value."
            )
        return ChatOpenAI(
            model=model_name,
            base_url=settings.openai_api_base,
            api_key=openai_api_key,
            reasoning_effort="none",
            use_responses_api=True,
            **openai_client_options(
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            ),
        )

QwenProviderFactory = OpenAIProviderFactory

class _RuntimeRetrieval:
    def __init__(self, vector_store_factory: Callable[[], Any]) -> None:
        self._vector_store_factory = vector_store_factory
        self._vector_store: Any | None = None
        self._lock = threading.Lock()

    def _get_vector_store(self) -> Any:
        vector_store = self._vector_store
        if vector_store is None:
            with self._lock:
                vector_store = self._vector_store
                if vector_store is None:
                    vector_store = self._vector_store_factory()
                    self._vector_store = vector_store
        return vector_store

    def rehab_exercise_kb_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        return self._get_vector_store().search(query=query, limit=limit)

    def close(self) -> None:
        with self._lock:
            vector_store = self._vector_store
            if vector_store is None:
                return
            close = getattr(vector_store, "close", None)
            if callable(close):
                close()


def build_runtime_dependencies(
    checkpointer: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
    vector_store_factory: Callable[[], Any] | None = None,
    model_overrides: dict[str, Any] | None = None,
    policy: WorkflowPolicy | None = None,
    trace_sink: TraceSink | Any | None = None,
) -> AgentRuntimeDependencies:
    if vector_store_factory is None:
        from app.vector import QdrantKnowledgeStore
        vector_store_factory = QdrantKnowledgeStore
    retrieval = _RuntimeRetrieval(vector_store_factory)
    if session_factory is None:
        def session_factory() -> Any:
            from app.db.session import SessionLocal

            return SessionLocal()
    evidence_registry = build_production_evidence_registry(
        session_factory=session_factory,
        retrieval=retrieval,
    )
    settings = get_settings()
    reviewer_runtime = ReviewerRuntimeConfig(timeout_seconds=settings.reviewer_timeout_seconds)
    provider_factory = OpenAIProviderFactory(overrides=model_overrides)
    router_model = provider_factory.router_model()
    return AgentRuntimeDependencies(
        router_model=router_model,
        consultant_model=provider_factory.consultant_model(),
        reviewer_model=router_model,
        retrieval=retrieval,
        catalog=None,
        policy=policy or WorkflowPolicy(),
        evidence_registry=evidence_registry,
        checkpointer=checkpointer,
        trace_sink=trace_sink if trace_sink is not None else configured_trace_sink(),
        safety_reviewer_model=router_model,
        grounding_reviewer_model=router_model,
        safety_reviewer_runtime=reviewer_runtime,
        grounding_reviewer_runtime=reviewer_runtime,
        session_compaction_model=provider_factory.session_compaction_model(),
        episode_memory_model=provider_factory.episode_memory_model(),
        patient_memory_model=provider_factory.patient_memory_model(),
        session_factory=session_factory,
        session_compaction_timeout_seconds=settings.session_compaction_timeout_seconds,
        memory_agent_timeout_seconds=settings.memory_agent_timeout_seconds,
    )
