"""Patient-facing progress updates for a running chat turn.

A streaming endpoint installs a sink for the duration of one turn; graph nodes and the
Consultant report coarse stages through ``report_progress``. Stages carry only fixed
labels, never patient content, model output, or tool arguments. Without a sink installed
(the normal request/response path and tests) reporting is a no-op.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

ProgressSink = Callable[[str, str], None]

_SINK: ContextVar[ProgressSink | None] = ContextVar("rehab_turn_progress_sink", default=None)

STAGE_LABELS: dict[str, str] = {
    "reading": "Reading your message",
    "planning": "Working out what to look up",
    "searching_catalog": "Searching the exercise library",
    "searching_web": "Checking current clinical guidance",
    "searching_history": "Looking back at your earlier sessions",
    "drafting": "Writing a reply",
    "reviewing": "Checking the reply for safety and accuracy",
    "revising": "Tightening the reply",
}

_NODE_STAGES: dict[str, str] = {
    "context_integrity": "reading",
    "router": "reading",
    "consultant": "planning",
    "safety_reviewer": "reviewing",
    "grounding_reviewer": "reviewing",
    "consultant_repair": "revising",
    "repair_revalidate_context": "revising",
}

_TOOL_STAGES: dict[str, str] = {
    "rehab_exercise_kb_search": "searching_catalog",
    "web_search": "searching_web",
    "read_episode_memory_level_2": "searching_history",
    "search_session_conversation": "searching_history",
}


@contextmanager
def progress_sink(sink: ProgressSink) -> Iterator[None]:
    """Route progress stages reported in this context to ``sink``."""
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def report_progress(stage: str) -> None:
    """Report one known stage; unknown stages and sink failures are ignored."""
    sink = _SINK.get()
    label = STAGE_LABELS.get(stage)
    if sink is None or label is None:
        return
    try:
        sink(stage, label)
    except Exception:
        # Progress is best effort and must never fail the patient's turn.
        pass


def report_node_started(node_name: str) -> None:
    stage = _NODE_STAGES.get(node_name)
    if stage is not None:
        report_progress(stage)


def report_tools_started(tool_names: list[str]) -> None:
    seen: set[str] = set()
    for name in tool_names:
        stage = _TOOL_STAGES.get(name)
        if stage is not None and stage not in seen:
            seen.add(stage)
            report_progress(stage)
