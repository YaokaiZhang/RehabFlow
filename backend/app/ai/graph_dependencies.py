"""Dependency assembly for the constrained RehabFlow graph."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.ai.context_types import ContextPlan
from app.ai.evidence_registry import (
    EvidenceRegistry,
    build_production_evidence_registry,
)
from app.ai.graph_prompt_context import _source_order_context_plan
from app.ai.runtime_dependencies import (
    AgentRuntimeDependencies,
    ReviewerRuntimeConfig,
)
from app.ai.workflow_policy import WorkflowPolicy
from app.observability.agent_trace import TraceContextRegistry


def default_rehab_exercise_kb_search_factory(
    vector_store: Any,
) -> Callable[[str, int], list[dict[str, Any]]]:
    def _rehab_exercise_kb_search(
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return vector_store.search(query=query, limit=limit)

    return _rehab_exercise_kb_search


@dataclass
class RehabGraphDeps:
    router_model: Any = None
    consultant_model: Any = None
    reviewer_model: Any = None
    safety_reviewer_model: Any = None
    grounding_reviewer_model: Any = None
    safety_reviewer_runtime: ReviewerRuntimeConfig = ReviewerRuntimeConfig()
    grounding_reviewer_runtime: ReviewerRuntimeConfig = ReviewerRuntimeConfig()
    trace_registry: TraceContextRegistry = field(default_factory=TraceContextRegistry)
    legacy_reviewer_compatibility: bool = False
    rehab_exercise_kb_search: Callable[[str, int], list[dict[str, Any]]] | None = None
    web_search: Callable[[str], Any] | None = None
    tool_timeout_seconds: float = 20.0
    catalog_suggestions: Callable[[Any, int], list[dict[str, Any]]] | None = None
    policy: WorkflowPolicy | None = None
    trace_sink: Any = None
    vector_store: Any = None
    use_tool_calling: bool | None = None
    debug: bool | None = None
    evidence_registry: EvidenceRegistry | None = None
    clock: Any | None = None
    review_join_router: Callable[..., str] | None = None
    session_compaction_model: Any | None = None
    episode_memory_model: Any | None = None
    patient_memory_model: Any | None = None
    session_factory: Callable[[], Any] | None = None
    session_compaction_timeout_seconds: float = 120.0
    memory_agent_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        self.policy = self.policy or WorkflowPolicy(
            use_tool_calling=(
                True if self.use_tool_calling is None else self.use_tool_calling
            ),
            debug=False if self.debug is None else self.debug,
        )
        if self.use_tool_calling is None:
            self.use_tool_calling = self.policy.use_tool_calling
        if self.debug is None:
            self.debug = self.policy.debug
        try:
            self.tool_timeout_seconds = max(
                0.1, min(float(self.tool_timeout_seconds), 60.0)
            )
        except (TypeError, ValueError):
            self.tool_timeout_seconds = 20.0
        if self.rehab_exercise_kb_search is None and self.vector_store is not None:
            self.rehab_exercise_kb_search = (
                default_rehab_exercise_kb_search_factory(self.vector_store)
            )
        legacy_reviewer = (
            self.reviewer_model is not None
            and self.safety_reviewer_model is None
            and self.grounding_reviewer_model is None
        )
        if legacy_reviewer:
            self.legacy_reviewer_compatibility = True
        if self.safety_reviewer_model is None:
            self.safety_reviewer_model = self.reviewer_model
        if self.grounding_reviewer_model is None:
            self.grounding_reviewer_model = self.reviewer_model


def adapt_runtime_dependencies(deps: AgentRuntimeDependencies) -> RehabGraphDeps:
    retrieval = deps.retrieval
    rehab_exercise_kb_search = getattr(
        retrieval, "rehab_exercise_kb_search", None
    )
    if not callable(rehab_exercise_kb_search):
        raise TypeError(
            "AgentRuntime retrieval must supply rehab_exercise_kb_search(query, limit)"
        )
    legacy_reviewer_compatibility = (
        getattr(deps, "reviewer_model", None) is not None
        and getattr(deps, "safety_reviewer_model", None) is None
        and getattr(deps, "grounding_reviewer_model", None) is None
    )
    runtime_policy = getattr(deps, "policy", None)
    policy = (
        runtime_policy
        if isinstance(runtime_policy, WorkflowPolicy)
        else WorkflowPolicy(
            use_tool_calling=bool(
                getattr(runtime_policy, "use_tool_calling", True)
            ),
            debug=bool(getattr(runtime_policy, "debug", False)),
        )
    )
    evidence_registry = getattr(deps, "evidence_registry", None)
    runtime_session_factory = getattr(deps, "session_factory", None)
    if not callable(runtime_session_factory):
        from app.db.session import SessionLocal

        runtime_session_factory = SessionLocal
    if evidence_registry is None:
        evidence_registry = build_production_evidence_registry(
            session_factory=runtime_session_factory,
            retrieval=retrieval,
        )
    return RehabGraphDeps(
        router_model=deps.router_model,
        consultant_model=deps.consultant_model,
        reviewer_model=deps.reviewer_model,
        safety_reviewer_model=(
            getattr(deps, "safety_reviewer_model", None) or deps.reviewer_model
        ),
        grounding_reviewer_model=(
            getattr(deps, "grounding_reviewer_model", None) or deps.reviewer_model
        ),
        safety_reviewer_runtime=getattr(
            deps, "safety_reviewer_runtime", ReviewerRuntimeConfig()
        ),
        grounding_reviewer_runtime=getattr(
            deps, "grounding_reviewer_runtime", ReviewerRuntimeConfig()
        ),
        trace_registry=TraceContextRegistry(),
        legacy_reviewer_compatibility=legacy_reviewer_compatibility,
        rehab_exercise_kb_search=rehab_exercise_kb_search,
        web_search=getattr(deps, "web_search", None),
        tool_timeout_seconds=getattr(deps, "tool_timeout_seconds", 20.0),
        policy=policy,
        trace_sink=deps.trace_sink,
        evidence_registry=evidence_registry,
        clock=getattr(deps, "clock", None),
        session_compaction_model=getattr(deps, "session_compaction_model", None),
        episode_memory_model=getattr(deps, "episode_memory_model", None),
        patient_memory_model=getattr(deps, "patient_memory_model", None),
        session_factory=runtime_session_factory,
        session_compaction_timeout_seconds=getattr(
            deps, "session_compaction_timeout_seconds", 120.0
        ),
        memory_agent_timeout_seconds=getattr(
            deps, "memory_agent_timeout_seconds", 120.0
        ),
    )


def context_planner_for(_deps: RehabGraphDeps) -> Callable[..., ContextPlan]:
    return _source_order_context_plan


def QdrantKnowledgeStore(*args: Any, **kwargs: Any) -> Any:
    from app.vector import QdrantKnowledgeStore as Store

    return Store(*args, **kwargs)


def _qwen_model(model: str, temperature: float):
    from app.ai.runtime_dependencies import OpenAIProviderFactory
    from app.core.config import get_settings

    key = "consultant" if model == "qwen3.5-plus" else "router"
    return OpenAIProviderFactory().model(
        key, get_settings().openai_model, temperature
    )


_qwen_flash = None
QWEN_70B = None
SessionLocal = None


def _qwen_70b():
    global _qwen_flash, QWEN_70B
    if QWEN_70B is not None:
        return QWEN_70B
    if _qwen_flash is None:
        _qwen_flash = _qwen_model("qwen-flash", 0.1)
    return _qwen_flash
