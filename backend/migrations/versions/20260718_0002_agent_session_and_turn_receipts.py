"""Bind AI sessions to Care Episodes and persist idempotent turn receipts."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260718_0002"
down_revision: Union[str, Sequence[str], None] = "20260718_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_sessions",
        sa.Column("care_episode_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ai_sessions_care_episode_id",
        "ai_sessions",
        "care_episodes",
        ["care_episode_id"],
        ["care_episode_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ai_sessions_care_episode_id",
        "ai_sessions",
        ["care_episode_id"],
        unique=False,
    )

    op.create_table(
        "ai_chat_turn_receipts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("workflow_version", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'interrupted', 'completed', 'failed')",
            name="ck_ai_chat_turn_receipts_status",
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_ai_chat_turn_receipts_idempotency_key",
        ),
        sa.CheckConstraint(
            "length(btrim(request_hash)) > 0",
            name="ck_ai_chat_turn_receipts_request_hash",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["ai_sessions.session_id"],
            name="fk_ai_chat_turn_receipts_session_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.patient_id"],
            name="fk_ai_chat_turn_receipts_patient_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_chat_turn_receipts"),
        sa.UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_ai_chat_turn_receipts_session_idempotency_key",
        ),
    )
    op.create_index(
        "ix_ai_chat_turn_receipts_patient_id",
        "ai_chat_turn_receipts",
        ["patient_id"],
        unique=False,
    )


    op.create_index(
        "ix_ai_chat_turn_receipts_session_id",
        "ai_chat_turn_receipts",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_chat_turn_receipts_session_id",
        table_name="ai_chat_turn_receipts",
    )
    op.drop_index(
        "ix_ai_chat_turn_receipts_patient_id",
        table_name="ai_chat_turn_receipts",
    )
    op.drop_table("ai_chat_turn_receipts")

    op.drop_index("ix_ai_sessions_care_episode_id", table_name="ai_sessions")
    op.drop_constraint(
        "fk_ai_sessions_care_episode_id",
        "ai_sessions",
        type_="foreignkey",
    )
    op.drop_column("ai_sessions", "care_episode_id")
