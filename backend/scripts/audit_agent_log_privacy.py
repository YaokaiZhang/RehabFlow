"""Read-only aggregate audit for compatibility AI internal log rows.

The audit deliberately selects counts and event labels only. It never selects
content, metadata values, prompts, or identifiers for output.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings
from app.db.models import AIInternalMessage


def aggregate_internal_log_counts(db) -> dict[str, object]:
    """Return only aggregate counts/categories from the internal-log table."""
    total_rows = int(
        db.scalar(select(func.count()).select_from(AIInternalMessage)) or 0
    )
    non_empty_content_rows = int(
        db.scalar(
            select(func.count())
            .select_from(AIInternalMessage)
            .where(func.length(AIInternalMessage.content) > 0)
        )
        or 0
    )
    event_type_counts = db.execute(
        select(AIInternalMessage.event_type, func.count())
        .group_by(AIInternalMessage.event_type)
        .order_by(AIInternalMessage.event_type)
    ).all()
    return {
        "table": "ai_internal_messages",
        "total_rows": total_rows,
        "non_empty_content_rows": non_empty_content_rows,
        "event_type_counts": {
            str(event_type): int(count) for event_type, count in event_type_counts
        },
    }


def main() -> int:
    try:
        engine = create_engine(get_settings().postgres_url, pool_pre_ping=True)
        with engine.connect() as connection:
            report = aggregate_internal_log_counts(connection)
        engine.dispose()
    except Exception:
        print("Agent log privacy audit failed.", file=sys.stderr)
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
