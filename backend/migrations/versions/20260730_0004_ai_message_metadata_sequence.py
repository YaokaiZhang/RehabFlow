"""Persist deterministic sequence metadata for AI messages."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260730_0004"
down_revision: Union[str, Sequence[str], None] = "20260723_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_messages",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        sa.text(
            """
            WITH ordered AS (
                SELECT
                    message_id,
                    row_number() OVER (
                        PARTITION BY session_id
                        ORDER BY created_at ASC, message_id ASC
                    ) AS sequence
                FROM ai_messages
            )
            UPDATE ai_messages AS messages
            SET metadata = jsonb_build_object('sequence', ordered.sequence)
            FROM ordered
            WHERE messages.message_id = ordered.message_id
            """
        )
    )
    op.alter_column("ai_messages", "metadata", server_default=None)


def downgrade() -> None:
    op.drop_column("ai_messages", "metadata")
