"""Fixed LangGraph topology assembly for the RehabFlow agent workflow.

This owner receives already-bound nodes and route selectors. It reads and writes no
patient content: LangGraph passes the existing ``RehabGraphState`` through unchanged.
Node names and route domains are deliberately literal so topology changes remain
reviewable and compatibility tests can compare the compiled graph.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.ai.workflow_state import RehabGraphState

GraphNode = Callable[[RehabGraphState], Any]
GraphRoute = Callable[[RehabGraphState], str]

GRAPH_ROUTE_DOMAINS = {
    "after_context_integrity": frozenset({"router", "unsafe_fallback"}),
    "after_router": frozenset(
        {"urgent_end", "clarification", "consultant", "unsafe_fallback"}
    ),
    "dispatch_reviewers": frozenset({"unsafe_fallback"}),
    "after_review_join": frozenset(
        {"safe_end", "consultant_repair", "repair_revalidate_context", "unsafe_fallback"}
    ),
    "after_repair": frozenset({"policy_gate", "repair_revalidate_context", "unsafe_fallback"}),
    "after_context_revalidation": frozenset({"consultant", "unsafe_fallback"}),
}


@dataclass(frozen=True)
class GraphNodes:
    """The fixed node implementations; each accepts typed state and returns state updates."""

    router: GraphNode
    consultant: GraphNode
    consultant_repair: GraphNode
    safety_reviewer: GraphNode
    grounding_reviewer: GraphNode
    review_join: GraphNode
    repair_revalidate_context: GraphNode
    unsafe_fallback: GraphNode
    context_integrity: GraphNode
    policy_gate: GraphNode
    clarification: GraphNode


@dataclass(frozen=True)
class GraphRoutes:
    """Metadata-only edge selectors with the frozen route-label domains below."""

    after_context_integrity: GraphRoute
    after_router: GraphRoute
    dispatch_reviewers: GraphRoute
    after_review_join: GraphRoute
    after_repair: GraphRoute
    after_context_revalidation: GraphRoute


def assemble_rehab_graph(*, nodes: GraphNodes, routes: GraphRoutes) -> StateGraph:
    """Build the frozen graph without changing state serialization or node identity."""
    graph = StateGraph(RehabGraphState)
    for name in (
        "router", "consultant", "consultant_repair", "safety_reviewer",
        "grounding_reviewer", "review_join", "repair_revalidate_context",
        "unsafe_fallback", "context_integrity", "policy_gate", "clarification",
    ):
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, "context_integrity")
    graph.add_conditional_edges(
        "context_integrity", routes.after_context_integrity,
        {"router": "router", "unsafe_fallback": "unsafe_fallback"},
    )
    graph.add_conditional_edges(
        "router", routes.after_router,
        {
            "urgent_end": END,
            "clarification": "clarification",
            "consultant": "consultant",
            "unsafe_fallback": "unsafe_fallback",
        },
    )
    graph.add_edge("consultant", "policy_gate")
    graph.add_conditional_edges(
        "policy_gate", routes.dispatch_reviewers,
        {"unsafe_fallback": "unsafe_fallback"},
    )
    graph.add_edge("safety_reviewer", "review_join")
    graph.add_edge("grounding_reviewer", "review_join")
    graph.add_conditional_edges(
        "review_join", routes.after_review_join,
        {
            "safe_end": END,
            "consultant_repair": "consultant_repair",
            "repair_revalidate_context": "repair_revalidate_context",
            "unsafe_fallback": "unsafe_fallback",
        },
    )
    graph.add_conditional_edges(
        "consultant_repair", routes.after_repair,
        {
            "policy_gate": "policy_gate",
            "repair_revalidate_context": "repair_revalidate_context",
            "unsafe_fallback": "unsafe_fallback",
        },
    )
    graph.add_conditional_edges(
        "repair_revalidate_context", routes.after_context_revalidation,
        {"consultant": "consultant", "unsafe_fallback": "unsafe_fallback"},
    )
    graph.add_edge("unsafe_fallback", END)
    return graph
