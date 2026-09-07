"""Repair internal sequence watermarks and record post-0005 migration truth."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0008"
down_revision: Union[str, Sequence[str], None] = "20260801_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS memory_migration_reports (
                report_key varchar(128) PRIMARY KEY,
                source_table varchar(128) NOT NULL,
                source_row_count integer NOT NULL,
                duplicate_item_type_group_count integer NOT NULL,
                source_table_dropped boolean NOT NULL,
                historical_duplicate_recovery_verified boolean NOT NULL,
                note text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
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
            VALUES (
                '20260801_0008',
                'memory_document_items',
                0,
                0,
                true,
                false,
                '0005 was already applied before this report table existed; '
                'memory_document_items is absent, so historical duplicate-field '
                'recovery cannot be independently verified from the source table.'
            )
            ON CONFLICT (report_key) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    internal_message_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY session_id
                        ORDER BY
                            CASE
                                WHEN COALESCE(metadata->>'sequence', '') ~ '^[0-9]+$'
                                THEN (metadata->>'sequence')::bigint
                                ELSE NULL
                            END NULLS LAST,
                            internal_message_id
                    ) AS repaired_sequence
                FROM ai_internal_messages
            )
            UPDATE ai_internal_messages AS messages
            SET metadata = jsonb_set(
                COALESCE(messages.metadata, '{}'::jsonb),
                '{sequence}',
                to_jsonb(ranked.repaired_sequence::bigint),
                true
            )
            FROM ranked
            WHERE messages.internal_message_id = ranked.internal_message_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_internal_messages_session_sequence
            ON ai_internal_messages (
                session_id,
                (((metadata->>'sequence')::bigint))
            )
            WHERE session_id IS NOT NULL
              AND COALESCE(metadata->>'sequence', '') ~ '^[0-9]+$'
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
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(COALESCE(NEW.session_id::text, '<none>'), 0)
                );
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
            "DROP INDEX IF EXISTS uq_ai_internal_messages_session_sequence"
        )
    )
