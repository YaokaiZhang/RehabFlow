"""Persist compaction attempt budgets and mark pre-0006 hashes explicitly."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0007"
down_revision: Union[str, Sequence[str], None] = "20260801_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_session_compaction_states",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.alter_column("ai_session_compaction_states", "attempts", server_default=None)
    op.execute(
        sa.text(
            """
            UPDATE memory_maintenance_runs
            SET request_hash = 'legacy-md5:' || request_hash
            WHERE request_hash ~ '^[0-9a-fA-F]{32}$'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE memory_maintenance_runs
            SET request_hash = substring(request_hash from 12)
            WHERE request_hash LIKE 'legacy-md5:%'
            """
        )
    )
    op.drop_column("ai_session_compaction_states", "attempts")
