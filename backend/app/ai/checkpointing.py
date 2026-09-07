"""LangGraph checkpoint saver lifecycle helpers.

Opening a saver is intentionally side-effect free for database schema. Deployment
provisions LangGraph vendor tables through ``setup_langgraph_checkpoints.py``.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from langgraph.checkpoint.postgres import PostgresSaver

from app.core.config import Settings
from app.ai.checkpoint_privacy import MetadataOnlyCheckpointSaver, ensure_metadata_only_checkpointer


@dataclass(frozen=True)
class CheckpointRetentionPolicy:
    """Explicit completed-thread retention decision for the checkpoint boundary."""

    retain_completed: bool = False

    def cleanup_completed(self, saver: object, thread_id: str) -> bool:
        if self.retain_completed:
            return False
        deleter = getattr(saver, "delete_thread", None)
        if not callable(deleter):
            return False
        deleter(str(thread_id))
        return True


def cleanup_completed_checkpoint(
    saver: object,
    thread_id: str,
    *,
    policy: CheckpointRetentionPolicy | None = None,
) -> bool:
    return (policy or CheckpointRetentionPolicy()).cleanup_completed(saver, thread_id)


@contextmanager
def open_checkpoint_saver(settings: Settings) -> Iterator[PostgresSaver]:
    """Open a Postgres checkpoint saver without provisioning vendor tables."""
    with PostgresSaver.from_conn_string(
        settings.effective_checkpoint_database_url
    ) as saver:
        yield saver


def assert_checkpoint_tables_ready(saver: PostgresSaver | MetadataOnlyCheckpointSaver) -> None:
    """Read-only startup check; never calls setup or creates vendor tables."""
    try:
        saver.get_tuple({"configurable": {"thread_id": "__rehabflow_startup_check__", "checkpoint_ns": ""}})
    except Exception as exc:
        raise RuntimeError(
            "LangGraph checkpoint vendor tables are unavailable. Run "
            "python scripts/setup_langgraph_checkpoints.py before starting the API."
        ) from exc
