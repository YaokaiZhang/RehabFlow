"""Persist context compaction state and structured memory maintenance."""
from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260731_0005"
down_revision: Union[str, Sequence[str], None] = "20260730_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _require_destructive_authority() -> None:
    if os.getenv("REHAB_ALLOW_DESTRUCTIVE_MEMORY_MIGRATION") != "1":
        raise RuntimeError(
            "0005 removes legacy memory tables; set "
            "REHAB_ALLOW_DESTRUCTIVE_MEMORY_MIGRATION=1 explicitly"
        )
    environment = os.getenv("ENVIRONMENT", "").strip().lower()
    if environment not in {"development", "test", "evaluation"}:
        raise RuntimeError(
            "0005 destructive memory migration is limited to development/test/evaluation"
        )


def upgrade() -> None:
    _require_destructive_authority()
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.add_column(
        "memory_documents",
        sa.Column("editable_fields", jsonb, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "memory_documents",
        sa.Column("summary_text", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "memory_documents",
        sa.Column("level1_keywords", jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "memory_documents",
        sa.Column("level1_description", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "memory_documents",
        sa.Column("level1_session_ids", jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "memory_documents",
        sa.Column("level2_summary", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "memory_documents",
        sa.Column("last_memory_error", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.create_table(
        "memory_migration_reports",
        sa.Column("report_key", sa.String(length=128), nullable=False),
        sa.Column("source_table", sa.String(length=128), nullable=False),
        sa.Column("source_row_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_item_type_group_count", sa.Integer(), nullable=False),
        sa.Column("source_table_dropped", sa.Boolean(), nullable=False),
        sa.Column("historical_duplicate_recovery_verified", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("report_key", name="pk_memory_migration_reports"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO memory_migration_reports (
                report_key,
                source_table,
                source_row_count,
                duplicate_item_type_group_count,
                source_table_dropped,
                historical_duplicate_recovery_verified,
                note
            )
            SELECT
                '20260731_0005',
                'memory_document_items',
                count(*)::integer,
                (
                    SELECT count(*)::integer
                    FROM (
                        SELECT document_id, item_type
                        FROM memory_document_items
                        WHERE status = 'active'
                          AND archived_at IS NULL
                          AND source_type = 'patient_edit'
                        GROUP BY document_id, item_type
                        HAVING count(*) > 1
                    ) AS duplicate_groups
                ),
                false,
                true,
                'Fresh 0005 run converted active patient-edit rows losslessly: '
                'single values remain scalars and duplicate item_type values remain '
                'ordered JSON arrays before the source table is dropped.'
            FROM memory_document_items
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE memory_documents AS documents
            SET editable_fields = COALESCE((
                SELECT jsonb_object_agg(grouped.item_type, grouped.value ORDER BY grouped.item_type)
                FROM (
                    SELECT
                        items.item_type,
                        CASE
                            WHEN count(*) = 1 THEN to_jsonb((array_agg(items.content ORDER BY items.created_at, items.memory_item_id))[1])
                            ELSE jsonb_agg(to_jsonb(items.content) ORDER BY items.created_at, items.memory_item_id)
                        END AS value
                    FROM memory_document_items AS items
                    WHERE items.document_id = documents.document_id
                      AND items.status = 'active'
                      AND items.archived_at IS NULL
                      AND items.source_type = 'patient_edit'
                    GROUP BY items.item_type
                ) AS grouped
            ), '{}'::jsonb)
            WHERE documents.scope = 'patient'
              AND documents.care_episode_id IS NULL
            """
        )
    )
    op.drop_table("memory_document_items")
    op.drop_table("memory_agent_runs")
    op.execute(
        sa.text(
            """
            UPDATE memory_migration_reports
            SET source_table_dropped = true
            WHERE report_key = '20260731_0005'
            """
        )
    )
    for column in (
        "editable_fields",
        "summary_text",
        "level1_keywords",
        "level1_description",
        "level1_session_ids",
        "level2_summary",
        "last_memory_error",
    ):
        op.alter_column("memory_documents", column, server_default=None)

    op.create_table(
        "ai_session_compaction_states",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rolling_block", sa.Text(), nullable=False),
        sa.Column("source_watermark", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["ai_sessions.session_id"],
            name="fk_ai_session_compaction_states_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name="pk_ai_session_compaction_states"),
    )

    op.create_table(
        "memory_maintenance_runs",
        sa.Column("maintenance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_identity", sa.String(length=255), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("care_episode_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("snapshot", jsonb, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("episode_status", sa.String(length=32), nullable=False),
        sa.Column("patient_status", sa.String(length=32), nullable=False),
        sa.Column("episode_attempts", sa.Integer(), nullable=False),
        sa.Column("patient_attempts", sa.Integer(), nullable=False),
        sa.Column("errors", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.patient_id"],
            name="fk_memory_maintenance_runs_patient_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["care_episode_id"],
            ["care_episodes.care_episode_id"],
            name="fk_memory_maintenance_runs_care_episode_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("maintenance_id", name="pk_memory_maintenance_runs"),
        sa.UniqueConstraint("trigger_identity", name="uq_memory_maintenance_trigger_identity"),
    )
    op.create_index(
        "ix_memory_maintenance_runs_patient_id",
        "memory_maintenance_runs",
        ["patient_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_maintenance_runs_care_episode_id",
        "memory_maintenance_runs",
        ["care_episode_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_ai_messages_content_english_fts ON ai_messages USING gin (to_tsvector('english', content))"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_ai_messages_content_english_fts"))
    op.drop_index("ix_memory_maintenance_runs_care_episode_id", table_name="memory_maintenance_runs")
    op.drop_index("ix_memory_maintenance_runs_patient_id", table_name="memory_maintenance_runs")
    op.drop_table("memory_maintenance_runs")
    op.drop_table("ai_session_compaction_states")
    for column in (
        "last_memory_error",
        "level2_summary",
        "level1_session_ids",
        "level1_description",
        "level1_keywords",
        "summary_text",
        "editable_fields",
    ):
        op.drop_column("memory_documents", column)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "memory_agent_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("care_episode_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("changes", jsonb, nullable=False),
        sa.Column("metadata", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.patient_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["care_episode_id"], ["care_episodes.care_episode_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_memory_agent_runs_care_episode_id",
        "memory_agent_runs",
        ["care_episode_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_agent_runs_patient_id",
        "memory_agent_runs",
        ["patient_id"],
        unique=False,
    )
    op.create_table(
        "memory_document_items",
        sa.Column("memory_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("visibility", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("evidence", jsonb, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["memory_documents.document_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("memory_item_id"),
    )
    op.create_index(
        "ix_memory_document_items_document_id",
        "memory_document_items",
        ["document_id"],
        unique=False,
    )
    op.drop_table("memory_migration_reports")
