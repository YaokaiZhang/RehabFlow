"""Graph composition and trace wrapper for the RehabFlow workflow."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from time import perf_counter
from typing import Any, Callable

from app.ai.clarification import clarification_node
from app.ai.consultant_invocation import consultant_agent
from app.ai.context_integrity import context_integrity
from app.ai.fallback import unsafe_fallback_agent
from app.ai.graph_construction import GraphNodes, GraphRoutes, assemble_rehab_graph
from app.ai.graph_dependencies import RehabGraphDeps
from app.ai.graph_routes import (
    route_after_context_integrity,
    route_after_context_revalidation,
    route_after_policy_gate,
    route_after_router,
)
from app.ai.graph_prompt_context import _source_order_context_plan
from app.ai.graph_tracing import (
    _trace_attempt, _trace_elapsed, _trace_input_tokens, _trace_node_finished,
    _trace_node_started, _trace_output_tokens, _trace_review_status,
    _trace_route, _trace_sources, _trace_turn_failed, _trace_turn_finished,
)
from app.ai.policy_gate import process_policy_gate
from app.ai.repair import repair_revalidate_context_node, route_after_repair, consultant_repair_node
from app.ai.review_nodes import (
    dispatch_reviewers, grounding_reviewer_node, review_join,
    route_after_review_join, safety_reviewer_node,
)
from app.ai.router import router_agent
from app.ai.workflow_state import RehabGraphState, WORKFLOW_VERSION
from app.observability.agent_trace import TraceContext

def build_rehab_graph(deps: RehabGraphDeps):

    def _trace_context(state: Mapping[str, Any]) -> TraceContext:
        return deps.trace_registry.for_state(
            state,
            workflow_version=str(state.get("workflow_version") or WORKFLOW_VERSION),
            sink=deps.trace_sink,
        )

    def _finish_node_trace(
        context: TraceContext,
        state: Mapping[str, Any],
        updates: Any,
        *,
        node_name: str,
        started: float,
    ) -> None:
        if isinstance(updates, dict):
            _trace_node_finished(
                context,
                state,
                updates,
                node_name=node_name,
                started=started,
            )
            _trace_turn_finished(context, state, updates, node_name=node_name)
        else:
            context.emit(
                "node_finished",
                node=node_name,
                route=_trace_route(state),
                elapsed_ms=_trace_elapsed(started),
                source_ids=_trace_sources(state),
                outcome="checkpointed" if node_name == "clarification" else "ok",
                attempt=_trace_attempt(state),
            )
            if node_name == "clarification":
                context.emit(
                    "checkpointed",
                    node=node_name,
                    route=_trace_route(state),
                    source_ids=_trace_sources(state),
                    outcome="awaiting_input",
                    attempt=_trace_attempt(state),
                )

    def traced(node_name: str, node: Callable[[RehabGraphState], Any]) -> Callable[[RehabGraphState], Any]:
        def invoke(state: RehabGraphState) -> Any:
            context = _trace_context(state)
            _trace_node_started(context, state, node_name)
            started = perf_counter()
            try:
                updates = node(state)
            except Exception as exc:
                _trace_turn_failed(context, state, node_name, exc)
                raise
            _finish_node_trace(
                context,
                state,
                updates,
                node_name=node_name,
                started=started,
            )
            return updates
        return invoke

    async def async_traced(
        node_name: str,
        node: Callable[[RehabGraphState], Any],
        state: RehabGraphState,
    ) -> Any:
        context = _trace_context(state)
        _trace_node_started(context, state, node_name)
        started = perf_counter()
        try:
            updates = await node(state)
        except Exception as exc:
            _trace_turn_failed(context, state, node_name, exc)
            raise
        _finish_node_trace(
            context,
            state,
            updates,
            node_name=node_name,
            started=started,
        )
        return updates

    async def traced_reviewer(
        node_name: str,
        node: Callable[..., Any],
        state: RehabGraphState,
    ) -> RehabGraphState:
        context = _trace_context(state)
        _trace_node_started(context, state, node_name)
        started = perf_counter()
        review_state = dict(state)
        prior_model_calls = state.get("model_calls")
        if not isinstance(prior_model_calls, int) or isinstance(prior_model_calls, bool):
            prior_model_calls = 0
        trace_sequence = max(
            len(state.get("debug_trace", []) or ()) + 1,
            prior_model_calls + 6,
        )
        review_state["trace_sequence"] = trace_sequence + (
            0 if node_name == "safety_reviewer" else 1
        )
        try:
            updates = await node(review_state)
        except Exception as exc:
            _trace_turn_failed(context, state, node_name, exc)
            raise
        if not isinstance(updates, dict):
            _finish_node_trace(
                context,
                state,
                updates,
                node_name=node_name,
                started=started,
            )
            return {}
        review_status = _trace_review_status(node_name, updates)
        context.emit(
            "review_finished",
            node=node_name,
            route=_trace_route(state, updates),
            elapsed_ms=_trace_elapsed(started),
            input_tokens=_trace_input_tokens(state, updates),
            output_tokens=_trace_output_tokens(state, updates),
            source_ids=_trace_sources(state, updates),
            outcome=review_status,
            attempt=_trace_attempt(state, updates),
        )
        _trace_node_finished(
            context,
            state,
            updates,
            node_name=node_name,
            started=started,
        )
        # Reviewer branches share one graph state. Keep only reducer-safe
        # decision/control fields here; the join reconstructs one metadata trace.
        updates.pop("debug_trace", None)
        updates.pop("review_status", None)
        updates.pop("review_passed", None)
        updates.pop("reviewer_feedback", None)
        updates.pop("reviewer_call_counts", None)
        return updates


    def safety_review_graph_node(state: RehabGraphState) -> RehabGraphState:
        return asyncio.run(
            traced_reviewer(
                "safety_reviewer",
                lambda value: safety_reviewer_node(
                    value,
                    model=deps.safety_reviewer_model,
                    runtime=deps.safety_reviewer_runtime,
                ),
                state,
            )
        )

    def grounding_review_graph_node(state: RehabGraphState) -> RehabGraphState:
        return asyncio.run(
            traced_reviewer(
                "grounding_reviewer",
                lambda value: grounding_reviewer_node(
                    value,
                    model=deps.grounding_reviewer_model,
                    runtime=deps.grounding_reviewer_runtime,
                    legacy_grounding_compatibility=deps.legacy_reviewer_compatibility,
                ),
                state,
            )
        )

    selected_review_join_route = deps.review_join_router or route_after_review_join

    def review_join_route(state: RehabGraphState) -> str:
        return selected_review_join_route(state, policy=deps.policy)

    review_join_route.__name__ = f"review_join_route_{id(deps)}"
    review_join_route.__qualname__ = review_join_route.__name__


    from app.ai.graph_construction import GraphNodes, GraphRoutes, assemble_rehab_graph

    return assemble_rehab_graph(
        nodes=GraphNodes(
            router=traced(
                "router",
                lambda state: router_agent(
                    state,
                    deps.router_model,
                    policy=deps.policy,
                    compaction_model=deps.session_compaction_model,
                    compaction_timeout_seconds=deps.session_compaction_timeout_seconds,
                    session_factory=deps.session_factory,
                ),
            ),
            consultant=traced("consultant", lambda state: consultant_agent(state, deps)),
            consultant_repair=traced(
                "consultant_repair", lambda state: consultant_repair_node(state, deps)
            ),
            safety_reviewer=safety_review_graph_node,
            grounding_reviewer=grounding_review_graph_node,
            review_join=traced("review_join", review_join),
            repair_revalidate_context=traced(
                "repair_revalidate_context", repair_revalidate_context_node
            ),
            unsafe_fallback=traced("unsafe_fallback", unsafe_fallback_agent),
            context_integrity=traced("context_integrity", context_integrity),
            policy_gate=traced(
                "policy_gate",
                lambda state: process_policy_gate(
                    state,
                    policy=deps.policy,
                    now=(
                        deps.clock.now_datetime()
                        if deps.clock is not None
                        and callable(getattr(deps.clock, "now_datetime", None))
                        else None
                    ),
                ),
            ),
            clarification=traced("clarification", clarification_node),
        ),
        routes=GraphRoutes(
            after_context_integrity=route_after_context_integrity,
            after_router=route_after_router,
            dispatch_reviewers=dispatch_reviewers,
            after_review_join=review_join_route,
            after_repair=route_after_repair,
            after_context_revalidation=route_after_context_revalidation,
        ),
    )

