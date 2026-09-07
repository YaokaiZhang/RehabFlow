"""Read-only PostgreSQL prerequisite and deterministic runtime verification.

This command never runs Alembic, calls LangGraph setup(), writes checkpoint
rows, or changes application data. Deployment provisioning remains explicit:

    alembic -c ../alembic.ini upgrade head
    python scripts/setup_langgraph_checkpoints.py

By default the PostgreSQL target is TEST_DATABASE_URL. When that variable is
absent, the command reports a clear skip and exits successfully. Use
--smoke-only to exercise the deterministic in-memory runtime without a
database.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

APPLICATION_TABLES = frozenset(
    {
        "alembic_version",
        "patients",
        "care_episodes",
        "ai_sessions",
        "ai_messages",
        "ai_internal_messages",
        "ai_chat_turn_receipts",
    }
)
CHECKPOINT_TABLES = frozenset(
    {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    }
)


class VerificationFailure(RuntimeError):
    """A prerequisite is missing or does not match the checked-in contract."""


def _postgresql_url(raw_url: str) -> str:
    try:
        parsed = make_url(raw_url)
    except Exception as exc:
        raise VerificationFailure(
            f"database URL could not be parsed ({type(exc).__name__})"
        ) from exc

    driver = parsed.drivername.split("+", 1)[0]
    if driver not in {"postgres", "postgresql"}:
        raise VerificationFailure("database URL must use PostgreSQL")
    if driver == "postgres":
        parsed = parsed.set(drivername="postgresql")
    return parsed.render_as_string(hide_password=False)


def _read_database_metadata(raw_url: str) -> tuple[str, str, set[str]]:
    """Read connection identity and current-schema table names only."""
    url = _postgresql_url(raw_url)
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            database_name, schema_name = connection.execute(
                text("SELECT current_database(), current_schema()")
            ).one()
            table_names = set(
                connection.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = current_schema()
                          AND table_type = 'BASE TABLE'
                        """
                    )
                ).scalars()
            )
            return str(database_name), str(schema_name), table_names
    finally:
        engine.dispose()


def _alembic_heads() -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_DIR / "alembic.ini"))
    return set(ScriptDirectory.from_config(config).get_heads())


def _read_alembic_versions(raw_url: str) -> set[str]:
    url = _postgresql_url(raw_url)
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return set(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            )
    finally:
        engine.dispose()


def verify_application_prerequisites(raw_url: str) -> None:
    database_name, schema_name, tables = _read_database_metadata(raw_url)
    missing_tables = sorted(APPLICATION_TABLES - tables)
    if missing_tables:
        raise VerificationFailure(
            "application schema is missing tables: " + ", ".join(missing_tables)
        )

    current_versions = _read_alembic_versions(raw_url)
    expected_heads = _alembic_heads()
    if current_versions != expected_heads:
        raise VerificationFailure(
            "Alembic revision mismatch: "
            f"database={sorted(current_versions)!r}, "
            f"repository={sorted(expected_heads)!r}"
        )

    revisions = ", ".join(sorted(current_versions)) or "<none>"
    print(
        "PASS: application PostgreSQL prerequisites "
        f"({database_name}/{schema_name}, Alembic head {revisions})"
    )


def verify_checkpoint_prerequisites(raw_url: str) -> None:
    database_name, schema_name, tables = _read_database_metadata(raw_url)
    missing_tables = sorted(CHECKPOINT_TABLES - tables)
    if missing_tables:
        raise VerificationFailure(
            "LangGraph checkpoint schema is missing tables: "
            + ", ".join(missing_tables)
        )
    print(
        "PASS: LangGraph checkpoint prerequisites "
        f"({database_name}/{schema_name}; vendor tables present)"
    )


class _DeterministicRouter:
    """Minimal structured model used only by the no-I/O runtime smoke."""

    def __init__(self) -> None:
        self.calls = 0
        self._schema: Any | None = None

    def with_structured_output(self, schema: Any) -> "_DeterministicRouter":
        self._schema = schema
        return self

    def invoke(self, _messages: list[Any]) -> Any:
        if self._schema is None:
            raise AssertionError("runtime smoke model was not given an output schema")
        self.calls += 1
        return self._schema(
            routing_decision="urgent_end",
            if_emergency=True,
            emergency_reason="deterministic verifier route",
            needs_more_info=False,
            follow_up_question="",
            triage_notes="deterministic runtime smoke",
        )


class _NoopRetrieval:
    def rehab_exercise_kb_search(self, _query: str, _limit: int) -> list[dict[str, Any]]:
        return []


class _NoopCatalog:
    def suggest(self, _docs: object, *, limit: int) -> list[dict[str, Any]]:
        return []


def run_runtime_smoke() -> None:
    """Compile and invoke the graph without provider, database, or network I/O."""
    from langgraph.checkpoint.memory import MemorySaver

    from app.ai.runtime import AgentRuntime
    from app.ai.runtime_dependencies import AgentRuntimeDependencies, WorkflowPolicy
    from app.ai.workflow_state import WORKFLOW_VERSION, Route

    router = _DeterministicRouter()
    checkpoint = MemorySaver()
    trace: list[dict[str, Any]] = []
    policy = WorkflowPolicy(use_tool_calling=False, debug=False)
    runtime = AgentRuntime(
        AgentRuntimeDependencies(
            router_model=router,
            consultant_model=object(),
            reviewer_model=object(),
            retrieval=_NoopRetrieval(),
            catalog=_NoopCatalog(),
            policy=policy,
            checkpointer=checkpoint,
            trace_sink=trace.append,
        )
    )
    session_id = "__rehabflow_runtime_verifier_smoke__"
    config = {"configurable": {"thread_id": session_id}}
    try:
        result = runtime.invoke_turn(
            {
                "user_input": "deterministic runtime smoke",
                "session_id": session_id,
                "patient_id": "verifier-patient",
                "principal_id": "verifier-patient",
                "conversation_history": [],
                "context_packets": {},
                "debug_trace": [],
                "internal_log_events": [],
                "workflow_version": WORKFLOW_VERSION,
                "principal_authorized": True,
                "session_authorized": True,
                "care_episode_authorized": True,
                "requested_route": Route.CONSULTANT,
                "authorized_source_ids": ["conversation:" + session_id],
                "source_ids": [],
                "authorized_tool_names": ["rehab_exercise_kb_search"],
                "allowed_catalog_ids": [],
                "budget": policy.build_budget(),
                "model_calls": 0,
                "tool_calls": 0,
                "total_tokens": 0,
                "idempotency_key": None,
                "request_hash": "deterministic-runtime-smoke",
                "prior_request_hash": None,
                "repair_count": 0,
            },
            session_id=session_id,
        )
        if result.get("routing_decision") != "urgent_end":
            raise VerificationFailure("runtime smoke did not take the deterministic route")
        if not str(result.get("final_response") or "").startswith("EMERGENCY WARNING:"):
            raise VerificationFailure("runtime smoke did not produce a final response")
        if router.calls != 1:
            raise VerificationFailure(
                f"runtime smoke invoked the deterministic model {router.calls} times"
            )
        if not trace:
            raise VerificationFailure("runtime smoke emitted no runtime trace event")
        if checkpoint.get_tuple(config) is None:
            raise VerificationFailure("runtime smoke did not create an in-memory checkpoint")
    finally:
        runtime.close()

    print("PASS: deterministic runtime smoke (one model call, one in-memory checkpoint)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify durable runtime prerequisites without mutating PostgreSQL."
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("TEST_DATABASE_URL"),
        help="PostgreSQL URL; defaults to TEST_DATABASE_URL.",
    )
    parser.add_argument(
        "--checkpoint-database-url",
        default=os.getenv("CHECKPOINT_DATABASE_URL"),
        help="Checkpoint PostgreSQL URL; defaults to the application URL.",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="Run only the deterministic in-memory runtime smoke.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.smoke_only:
        try:
            run_runtime_smoke()
        except Exception as exc:
            print(
                f"FAIL: deterministic runtime smoke: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        return 0

    if not args.database_url:
        print(
            "SKIP: TEST_DATABASE_URL is not configured; "
            "PostgreSQL durability verification was not run."
        )
        return 0

    try:
        verify_application_prerequisites(args.database_url)
        verify_checkpoint_prerequisites(
            args.checkpoint_database_url or args.database_url
        )
        run_runtime_smoke()
    except Exception as exc:
        print(
            f"FAIL: durable runtime verification: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print("PASS: durable runtime verification completed without database writes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
