"""Bounded deterministic policy defaults for workflow control state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field

from app.ai.workflow_state import WORKFLOW_VERSION, WorkflowBudget


# These budgets apply to one graph invocation. They leave enough headroom for
# the multi-node safety workflow while a session carries roughly nine turns of
# conversation context across successive invocations.
DEFAULT_MAX_MODEL_CALLS = 32
DEFAULT_MAX_TOOL_CALLS = 64
DEFAULT_MAX_TOTAL_TOKENS = 100_000
DEFAULT_MAX_REPAIRS = 3
DEFAULT_DEADLINE_SECONDS = 600


class WorkflowPolicy(BaseModel):
    """Non-clinical limits and supported versions for deterministic workflow control."""

    model_config = ConfigDict(extra="forbid")

    use_tool_calling: bool = True
    debug: bool = False
    supported_workflow_versions: tuple[str, ...] = (WORKFLOW_VERSION,)
    max_model_calls: int = Field(DEFAULT_MAX_MODEL_CALLS, ge=1, le=64)
    max_tool_calls: int = Field(DEFAULT_MAX_TOOL_CALLS, ge=0, le=128)
    # Retained for compatibility; provider token usage is observational only.
    max_total_tokens: int = Field(DEFAULT_MAX_TOTAL_TOKENS, ge=1, le=100_000)
    max_repairs: int = Field(DEFAULT_MAX_REPAIRS, ge=0, le=3)
    deadline_seconds: int = Field(DEFAULT_DEADLINE_SECONDS, ge=1, le=600)

    def supports_workflow_version(self, value: object) -> bool:
        return isinstance(value, str) and value in self.supported_workflow_versions

    def repair_is_allowed(self, repair_count: object) -> bool:
        """Return whether the bounded automatic repair slot is still available."""
        return (
            isinstance(repair_count, int)
            and not isinstance(repair_count, bool)
            and 0 <= repair_count < self.max_repairs
        )

    def build_budget(self, *, now: datetime | None = None) -> WorkflowBudget:
        started_at = now or datetime.now(timezone.utc)
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return WorkflowBudget(
            deadline_at=started_at + timedelta(seconds=self.deadline_seconds),
            max_model_calls=self.max_model_calls,
            max_tool_calls=self.max_tool_calls,
            max_total_tokens=self.max_total_tokens,
            max_repairs=self.max_repairs,
        )
