"""Repair durable maintenance claims and backfill internal event sequences."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260801_0006"
down_revision: Union[str, Sequence[str], None] = "20260731_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.add_column(
        "memory_maintenance_runs",
        sa.Column("request_hash", sa.String(length=128), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "memory_maintenance_runs",
        sa.Column("claim_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "memory_maintenance_runs",
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE memory_maintenance_runs
            SET request_hash = 'legacy-md5:' || md5(trigger_identity)
            WHERE request_hash = ''
            """
        )
    )
    op.alter_column("memory_maintenance_runs", "request_hash", server_default=None)
    op.create_index(
        "ix_memory_maintenance_runs_claim_expires_at",
        "memory_maintenance_runs",
        ["claim_expires_at"],
        unique=False,
    )

    # Preserve every legacy internal row. Numeric rows keep their sequence;
    # null or malformed rows are assigned deterministic numbers after the
    # existing per-session maximum, ordered by creation time and UUID.
    op.execute(
        sa.text(
            """
            WITH numeric_sequences AS (
                SELECT session_id,
                       COALESCE(MAX((metadata->>'sequence')::bigint), 0) AS max_sequence
                FROM ai_internal_messages
                WHERE COALESCE(metadata->>'sequence', '') ~ '^[0-9]+$'
                GROUP BY session_id
            ), legacy_rows AS (
                SELECT internal_message_id,
                       session_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY session_id
                           ORDER BY created_at, internal_message_id
                       ) AS legacy_sequence
                FROM ai_internal_messages
                WHERE NOT (COALESCE(metadata->>'sequence', '') ~ '^[0-9]+$')
            ), numbered_rows AS (
                SELECT legacy_rows.internal_message_id,
                       COALESCE(numeric_sequences.max_sequence, 0) + legacy_rows.legacy_sequence AS sequence
                FROM legacy_rows
                LEFT JOIN numeric_sequences USING (session_id)
            )
            UPDATE ai_internal_messages AS messages
            SET metadata = jsonb_set(
                COALESCE(messages.metadata, '{}'::jsonb),
                '{sequence}',
                to_jsonb(numbered_rows.sequence),
                true
            )
            FROM numbered_rows
            WHERE messages.internal_message_id = numbered_rows.internal_message_id
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION rehab_assign_ai_internal_sequence()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                next_sequence bigint;
            BEGIN
                NEW.metadata := COALESCE(NEW.metadata, '{}'::jsonb);
                IF COALESCE(NEW.metadata->>'sequence', '') ~ '^[0-9]+$' THEN
                    RETURN NEW;
                END IF;
                PERFORM pg_advisory_xact_lock(hashtext(COALESCE(NEW.session_id::text, '<none>')));
                SELECT COALESCE(MAX((metadata->>'sequence')::bigint), 0) + 1
                INTO next_sequence
                FROM ai_internal_messages
                WHERE session_id IS NOT DISTINCT FROM NEW.session_id
                  AND COALESCE(metadata->>'sequence', '') ~ '^[0-9]+$';
                NEW.metadata := jsonb_set(
                    NEW.metadata,
                    '{sequence}',
                    to_jsonb(next_sequence),
                    true
                );
                RETURN NEW;
            END;
            $$;
            DROP TRIGGER IF EXISTS trg_ai_internal_message_sequence ON ai_internal_messages;
            CREATE TRIGGER trg_ai_internal_message_sequence
            BEFORE INSERT ON ai_internal_messages
            FOR EACH ROW
            EXECUTE FUNCTION rehab_assign_ai_internal_sequence();
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS trg_ai_internal_message_sequence ON ai_internal_messages;
            DROP FUNCTION IF EXISTS rehab_assign_ai_internal_sequence();
            """
        )
    )
    op.drop_index(
        "ix_memory_maintenance_runs_claim_expires_at",
        table_name="memory_maintenance_runs",
    )
    op.drop_column("memory_maintenance_runs", "claim_expires_at")
    op.drop_column("memory_maintenance_runs", "claim_token")
    op.drop_column("memory_maintenance_runs", "request_hash")
