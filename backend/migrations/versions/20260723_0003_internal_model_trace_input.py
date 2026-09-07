"""Persist full internal model inputs alongside AI output traces."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260723_0003"
down_revision: Union[str, Sequence[str], None] = "20260718_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_internal_messages",
        sa.Column("model_input", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.alter_column("ai_internal_messages", "model_input", server_default=None)


def downgrade() -> None:
    op.drop_column("ai_internal_messages", "model_input")
