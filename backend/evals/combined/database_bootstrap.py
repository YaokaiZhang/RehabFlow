"""Evaluator-owned PostgreSQL database bootstrap and exact lifecycle."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from evals.combined.resource_manifest import privacy_hash


class DatabaseBootstrapError(RuntimeError):
    """A database prerequisite could not be proven without exposing secrets."""

    def __init__(
        self,
        message: str,
        *,
        cleanup_result: Any | None = None,
        partial_result: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.cleanup_result = cleanup_result
        self.partial_result = partial_result


@dataclass(frozen=True)
class DatabaseCleanupResult:
    """Exact bootstrap rollback evidence, including failures."""

    status: str
    failures: list[str]
    attempted: list[str]
    deleted: list[str]
    remaining: list[str]

    @property
    def ok(self) -> bool:
        return self.status == "passed" and not self.failures and not self.remaining

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "failures": list(self.failures),
            "attempted_count": len(self.attempted),
            "deleted_count": len(self.deleted),
            "remaining_count": len(self.remaining),
        }


_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_NAMESPACE = re.compile(r"^(?:rehab_eval|evaluator|eval|test)_[a-z0-9_]{1,48}$")
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_EVALUATOR_HOST = re.compile(r"(^|[-_.])(?:eval|evaluator|ci|test)(?:[-_.]|$)")
_BLOCKED_MARKERS = (
    "prod",
    "production",
    "staging",
    "live",
    "shared",
    "clinical",
    "development",
    "dev",
    "local",
)
CHECKPOINT_VENDOR_TABLES = frozenset(
    {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    }
)
OWNERSHIP_PREFIX = "rehabflow-evaluator:r13"


def _blocked_target(value: str) -> bool:
    lowered = value.lower()
    return any(
        re.search(rf"(^|[-_.]){re.escape(marker)}($|[-_.])", lowered)
        for marker in _BLOCKED_MARKERS
    )


def _url_target(raw_url: str, label: str) -> tuple[str, int, str]:
    try:
        parsed = make_url(raw_url)
    except Exception as exc:
        raise DatabaseBootstrapError(f"{label} is not a valid PostgreSQL connection") from exc
    if not parsed.drivername.startswith("postgresql") or not parsed.host or not parsed.database:
        raise DatabaseBootstrapError(f"{label} must be a PostgreSQL server connection")
    host = str(parsed.host).lower()
    database = str(parsed.database).lower()
    if _blocked_target(host) or _blocked_target(database):
        raise DatabaseBootstrapError(f"{label} appears to target a product, development, or shared resource")
    return host, int(parsed.port or 5432), database


def validate_evaluator_sql_target(
    admin_url: str,
    *,
    allowlisted_hosts: tuple[str, ...] = (),
) -> tuple[str, int, str]:
    """Validate that an administrator URL identifies an evaluator SQL server."""
    target = _url_target(admin_url, "evaluator SQL administrator connection")
    host = target[0]
    allowlist = {str(value).strip().lower() for value in allowlisted_hosts if str(value).strip()}
    if host not in allowlist and _EVALUATOR_HOST.search(host) is None:
        raise DatabaseBootstrapError(
            "evaluator SQL administrator connection is not explicitly evaluator-isolated"
        )
    return target


def _validate_name(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if _DATABASE_NAME.fullmatch(normalized) is None or _blocked_target(normalized):
        raise DatabaseBootstrapError(f"{label} is not an exact evaluator database name")
    return normalized


def _quote_identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise DatabaseBootstrapError("evaluator database identifier is unsafe")
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class EvaluatorDatabaseConfig:
    """All evaluator SQL inputs; database URLs may be omitted for bootstrap-created DBs."""

    admin_url: str
    namespace: str
    application_database_name: str
    checkpoint_database_name: str
    expected_application_schema_revision: str
    repo_root: Path
    run_id: str = "bootstrap"
    application_url: str | None = None
    checkpoint_url: str | None = None
    owner_role: str | None = None
    allowlisted_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_evaluator_sql_target(self.admin_url, allowlisted_hosts=self.allowlisted_hosts)
        namespace = str(self.namespace or "").strip().lower()
        if _NAMESPACE.fullmatch(namespace) is None or _blocked_target(namespace):
            raise DatabaseBootstrapError("evaluator SQL namespace is not exact and evaluator-owned")
        application_name = _validate_name(self.application_database_name, "application")
        checkpoint_name = _validate_name(self.checkpoint_database_name, "checkpoint")
        if application_name != f"{namespace}_app":
            raise DatabaseBootstrapError("application database name must be the exact namespace app name")
        if checkpoint_name != f"{namespace}_checkpoint":
            raise DatabaseBootstrapError("checkpoint database name must be the exact namespace checkpoint name")
        if application_name == checkpoint_name:
            raise DatabaseBootstrapError("application and checkpoint database names must be distinct")
        if not str(self.expected_application_schema_revision or "").strip():
            raise DatabaseBootstrapError("expected application Alembic revision is required")
        if not Path(self.repo_root).is_dir():
            raise DatabaseBootstrapError("evaluator repository root is unavailable")
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", str(self.run_id).strip()) is None:
            raise DatabaseBootstrapError("evaluator run id is unsafe")
        if self.owner_role is not None and _IDENTIFIER.fullmatch(str(self.owner_role).strip().lower()) is None:
            raise DatabaseBootstrapError("evaluator database owner role is unsafe")
        admin_target = _url_target(self.admin_url, "evaluator SQL administrator connection")
        for value, label, name in (
            (self.application_url, "application database URL", application_name),
            (self.checkpoint_url, "checkpoint database URL", checkpoint_name),
        ):
            if value is None or not str(value).strip():
                continue
            target = _url_target(value, label)
            if target[:2] != admin_target[:2]:
                raise DatabaseBootstrapError(f"{label} is not on the evaluator SQL server")
            if target[2] != name:
                raise DatabaseBootstrapError(f"{label} does not identify the exact evaluator database")


@dataclass(frozen=True)
class DatabaseInfo:
    name: str
    owner: str
    comment: str


@dataclass(frozen=True)
class DatabaseResource:
    role: str
    name: str
    url: str
    owner_role: str
    ownership_marker: str
    ownership_state: str
    created_by_run: bool
    schema_revision: str | None = None
    checkpoint_ready: bool = False
    live_verified: bool = False

    def manifest_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "owner_role": self.owner_role,
            "ownership_marker": self.ownership_marker,
            "ownership_state": self.ownership_state,
            "created_by_run": self.created_by_run,
            "schema_revision": self.schema_revision,
            "checkpoint_ready": self.checkpoint_ready,
            "live_verified": self.live_verified,
        }


@dataclass(frozen=True)
class DatabaseBootstrapResult:
    namespace: str
    server_host: str
    server_port: int
    admin_url: str
    application: DatabaseResource
    checkpoint: DatabaseResource
    run_id: str = "bootstrap"

    def manifest_record(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "server_host": self.server_host,
            "server_port": self.server_port,
            "run_id": self.run_id,
            "application": self.application.manifest_record(),
            "checkpoint": self.checkpoint.manifest_record(),
        }

    def privacy_safe(self, *, salt: str = "") -> dict[str, Any]:
        record = self.manifest_record()
        return {
            "namespace_hash": privacy_hash(record["namespace"], salt=salt),
            "server_hash": privacy_hash(
                f"{record['server_host']}:{record['server_port']}", salt=salt
            ),
            "application": {
                "database_hash": privacy_hash(self.application.name, salt=salt),
                "owner_role_hash": privacy_hash(self.application.owner_role, salt=salt),
                "ownership_state": self.application.ownership_state,
                "created_by_run": self.application.created_by_run,
                "schema_revision": self.application.schema_revision,
                "checkpoint_ready": False,
            },
            "checkpoint": {
                "database_hash": privacy_hash(self.checkpoint.name, salt=salt),
                "owner_role_hash": privacy_hash(self.checkpoint.owner_role, salt=salt),
                "ownership_state": self.checkpoint.ownership_state,
                "created_by_run": self.checkpoint.created_by_run,
                "schema_revision": None,
                "checkpoint_ready": self.checkpoint.checkpoint_ready,
            },
        }


class PostgresDatabaseAdmin:
    """Small administrator boundary limited to exact evaluator database names."""

    def __init__(self, admin_url: str) -> None:
        self.admin_url = admin_url
        self._engine: Engine | None = None
        try:
            self._engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
            with self._engine.connect() as connection:
                self.current_user = str(connection.execute(text("SELECT current_user")).scalar_one())
                self.server_host = str(
                    connection.execute(text("SELECT COALESCE(inet_server_addr()::text, 'local')")).scalar_one()
                )
                self.server_port = int(connection.execute(text("SELECT inet_server_port()")).scalar_one() or 5432)
        except Exception as exc:
            self.close()
            raise DatabaseBootstrapError("evaluator SQL administrator connection failed") from exc

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def database_info(self, name: str) -> DatabaseInfo | None:
        if self._engine is None:
            raise DatabaseBootstrapError("evaluator SQL administrator connection is closed")
        safe_name = _validate_name(name, "database")
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT datname, pg_get_userbyid(datdba) AS owner, "
                        "COALESCE(shobj_description(oid, 'pg_database'), '') AS comment "
                        "FROM pg_database WHERE datname = :name"
                    ),
                    {"name": safe_name},
                ).mappings().first()
            if row is None:
                return None
            return DatabaseInfo(
                name=str(row["datname"]),
                owner=str(row["owner"]),
                comment=str(row["comment"] or ""),
            )
        except DatabaseBootstrapError:
            raise
        except Exception as exc:
            raise DatabaseBootstrapError("evaluator SQL database metadata could not be read") from exc

    def create_database(self, name: str, owner: str) -> None:
        if self._engine is None:
            raise DatabaseBootstrapError("evaluator SQL administrator connection is closed")
        safe_name = _validate_name(name, "database")
        safe_owner = str(owner).strip().lower()
        if _IDENTIFIER.fullmatch(safe_owner) is None:
            raise DatabaseBootstrapError("evaluator database owner role is unsafe")
        try:
            with self._engine.connect() as connection:
                connection.execute(
                    text(f"CREATE DATABASE {_quote_identifier(safe_name)} OWNER {_quote_identifier(safe_owner)}")
                )
        except Exception as exc:
            raise DatabaseBootstrapError("evaluator database creation failed") from exc

    def record_ownership(self, name: str, marker: str) -> None:
        if self._engine is None:
            raise DatabaseBootstrapError("evaluator SQL administrator connection is closed")
        safe_name = _validate_name(name, "database")
        try:
            with self._engine.connect() as connection:
                connection.execute(
                    text(
                        f"COMMENT ON DATABASE {_quote_identifier(safe_name)} "
                        f"IS {_quote_literal(marker)}"
                    )
                )
        except Exception as exc:
            raise DatabaseBootstrapError("evaluator database ownership record could not be written") from exc

    def drop_owned_database(self, resource: DatabaseResource) -> bool:
        if not resource.created_by_run or not resource.live_verified:
            return False
        info = self.database_info(resource.name)
        if info is None:
            return False
        if info.owner != resource.owner_role or info.comment != resource.ownership_marker:
            raise DatabaseBootstrapError("evaluator database ownership could not be re-proven for cleanup")
        safe_name = _validate_name(resource.name, "database")
        try:
            with self._engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": safe_name},
                )
                connection.execute(text(f"DROP DATABASE {_quote_identifier(safe_name)}"))
        except Exception as exc:
            raise DatabaseBootstrapError("evaluator database drop failed") from exc
        if self.database_info(safe_name) is not None:
            raise DatabaseBootstrapError("evaluator database removal could not be verified")
        return True


def _ownership_marker(
    namespace: str,
    role: str,
    owner_role: str,
    run_id: str = "bootstrap",
) -> str:
    return f"{OWNERSHIP_PREFIX}:{namespace}:{run_id}:{role}:{owner_role}"


def _database_url(raw_url: str, name: str) -> str:
    try:
        return make_url(raw_url).set(database=name).render_as_string(hide_password=False)
    except Exception as exc:
        raise DatabaseBootstrapError("evaluator database URL could not be constructed") from exc


@contextmanager
def _database_environment(database_url: str):
    previous = {key: os.environ.get(key) for key in ("DATABASE_URL", "POSTGRES_URL")}
    os.environ["DATABASE_URL"] = database_url
    os.environ["POSTGRES_URL"] = database_url
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _default_schema_runner(
    database_url: str,
    expected_revision: str,
    apply_migrations: bool,
    repo_root: Path,
) -> str:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from evals.combined.preflight import verify_application_schema
    from app.core.config import get_settings

    alembic_config = Config(str(Path(repo_root) / "alembic.ini"))
    actual_expected = expected_revision
    if expected_revision.strip().lower() == "head":
        actual_expected = ScriptDirectory.from_config(alembic_config).get_current_head()
    try:
        if apply_migrations:
            with _database_environment(database_url):
                get_settings.cache_clear()
                command.upgrade(alembic_config, actual_expected)
        with _database_environment(database_url):
            get_settings.cache_clear()
            return verify_application_schema(database_url, expected_revision=actual_expected)
    except DatabaseBootstrapError:
        raise
    except Exception as exc:
        raise DatabaseBootstrapError("evaluator application schema setup failed") from exc
    finally:
        get_settings.cache_clear()


def _checkpoint_sqlalchemy_url(database_url: str) -> str:
    parsed = make_url(database_url)
    return parsed.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)


def _default_checkpoint_runner(
    application_url: str,
    checkpoint_url: str,
    repo_root: Path,
) -> bool:
    del repo_root
    try:
        from app.core.config import Settings
        from scripts.setup_langgraph_checkpoints import run_deployment_smoke

        settings = Settings(
            _env_file=None,
            database_url=application_url,
            checkpoint_database_url=make_url(checkpoint_url).set(drivername="postgresql").render_as_string(
                hide_password=False
            ),
            environment="development",
        )
        run_deployment_smoke(settings)
        engine = create_engine(_checkpoint_sqlalchemy_url(checkpoint_url))
        try:
            available = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        missing = CHECKPOINT_VENDOR_TABLES - available
        if missing:
            raise DatabaseBootstrapError("LangGraph checkpoint vendor tables are incomplete")
        return True
    except DatabaseBootstrapError:
        raise
    except Exception as exc:
        raise DatabaseBootstrapError("LangGraph checkpoint setup or readiness verification failed") from exc


def _default_admin_factory(config: EvaluatorDatabaseConfig) -> PostgresDatabaseAdmin:
    return PostgresDatabaseAdmin(config.admin_url)


def _cleanup_created(admin: Any, resources: list[DatabaseResource]) -> DatabaseCleanupResult:
    attempted: list[str] = []
    deleted: list[str] = []
    remaining: list[str] = []
    failures: list[str] = []
    for resource in reversed(resources):
        attempted.append(resource.name)
        try:
            if admin.drop_owned_database(resource):
                deleted.append(resource.name)
            if admin.database_info(resource.name) is not None:
                remaining.append(resource.name)
        except Exception as exc:
            failures.append(f"{resource.role}_{type(exc).__name__}")
    return DatabaseCleanupResult(
        status="passed" if not failures and not remaining else "failed",
        failures=failures,
        attempted=attempted,
        deleted=deleted,
        remaining=remaining,
    )


def _partial_bootstrap_result(
    config: EvaluatorDatabaseConfig,
    *,
    server_host: str,
    server_port: int,
    owner_role: str,
    resources: dict[str, DatabaseResource],
) -> DatabaseBootstrapResult:
    partial: dict[str, DatabaseResource] = dict(resources)
    for role, name, explicit_url in (
        ("application", config.application_database_name, config.application_url),
        ("checkpoint", config.checkpoint_database_name, config.checkpoint_url),
    ):
        if role in partial:
            continue
        marker = _ownership_marker(config.namespace, role, owner_role, config.run_id)
        partial[role] = DatabaseResource(
            role=role,
            name=name,
            url=_database_url(explicit_url or config.admin_url, name),
            owner_role=owner_role,
            ownership_marker=marker,
            ownership_state="unproven",
            created_by_run=False,
            live_verified=False,
        )
    return DatabaseBootstrapResult(
        namespace=str(config.namespace).strip().lower(),
        server_host=server_host,
        server_port=server_port,
        admin_url=config.admin_url,
        application=partial["application"],
        checkpoint=partial["checkpoint"],
        run_id=config.run_id,
    )


def bootstrap_evaluator_databases(
    config: EvaluatorDatabaseConfig,
    *,
    admin_factory: Callable[[EvaluatorDatabaseConfig], Any] = _default_admin_factory,
    schema_runner: Callable[[str, str, bool, Path], str] = _default_schema_runner,
    checkpoint_runner: Callable[[str, str, Path], bool] = _default_checkpoint_runner,
    resource_hook: Callable[[dict[str, Any]], None] | None = None,
) -> DatabaseBootstrapResult:
    """Create or verify the exact evaluator databases before case setup."""
    admin: Any | None = None
    created_resources: list[DatabaseResource] = []
    known_resources: dict[str, DatabaseResource] = {}
    admin_target: tuple[str, int, str] | None = None
    owner_role = str(config.owner_role or "").strip().lower()
    try:
        admin = admin_factory(config)
        admin_target = _url_target(config.admin_url, "evaluator SQL administrator connection")
        owner_role = str(config.owner_role or getattr(admin, "current_user", "")).strip().lower()
        if _IDENTIFIER.fullmatch(owner_role) is None:
            raise DatabaseBootstrapError("evaluator database owner role could not be proven")
        resources: dict[str, DatabaseResource] = {}
        for role, name, explicit_url in (
            ("application", config.application_database_name, config.application_url),
            ("checkpoint", config.checkpoint_database_name, config.checkpoint_url),
        ):
            marker = _ownership_marker(config.namespace, role, owner_role, config.run_id)
            info = admin.database_info(name)
            created = info is None
            marker_was_present = bool(info.comment) if info is not None else False
            if created:
                admin.create_database(name, owner_role)
                created_resource = DatabaseResource(
                    role=role,
                    name=name,
                    url=_database_url(config.admin_url, name),
                    owner_role=owner_role,
                    ownership_marker=marker,
                    ownership_state="created_by_run",
                    created_by_run=True,
                    live_verified=False,
                )
                created_resources.append(created_resource)
                known_resources[role] = created_resource
                if resource_hook is not None:
                    resource_hook(
                        _partial_bootstrap_result(
                            config,
                            server_host=admin_target[0],
                            server_port=admin_target[1],
                            owner_role=owner_role,
                            resources=known_resources,
                        ).manifest_record()
                    )
                info = admin.database_info(name)
            if info is None or info.owner != owner_role:
                raise DatabaseBootstrapError(f"{role} database ownership could not be proven")
            if info.comment and info.comment != marker:
                raise DatabaseBootstrapError(f"{role} database has a different evaluator ownership record")
            if not info.comment:
                admin.record_ownership(name, marker)
            live_info = admin.database_info(name)
            if live_info is None or live_info.owner != owner_role or live_info.comment != marker:
                raise DatabaseBootstrapError(f"{role} database live ownership proof is incomplete")
            live_created_by_run = created or (marker_was_present and live_info.comment == marker)
            resources[role] = DatabaseResource(
                role=role,
                name=name,
                url=_database_url(explicit_url or config.admin_url, name),
                owner_role=owner_role,
                ownership_marker=marker,
                ownership_state="created_by_run" if live_created_by_run else "pre_existing_evaluator",
                created_by_run=live_created_by_run,
                live_verified=True,
            )
            known_resources[role] = resources[role]
            if created:
                created_resources[:] = [
                    item for item in created_resources if item.name != resources[role].name
                ]
                created_resources.append(resources[role])
            if resource_hook is not None:
                resource_hook(
                    _partial_bootstrap_result(
                        config,
                        server_host=admin_target[0],
                        server_port=admin_target[1],
                        owner_role=owner_role,
                        resources=known_resources,
                    ).manifest_record()
                )
        application = resources["application"]
        application_revision = schema_runner(
            application.url,
            config.expected_application_schema_revision,
            application.created_by_run,
            Path(config.repo_root),
        )
        if not str(application_revision or "").strip():
            raise DatabaseBootstrapError("application schema readiness could not be proven")
        checkpoint = resources["checkpoint"]
        checkpoint_ready = checkpoint_runner(
            application.url,
            checkpoint.url,
            Path(config.repo_root),
        )
        if checkpoint_ready is not True:
            raise DatabaseBootstrapError("LangGraph checkpoint readiness could not be proven")
        return DatabaseBootstrapResult(
            namespace=str(config.namespace).strip().lower(),
            server_host=admin_target[0],
            server_port=admin_target[1],
            admin_url=config.admin_url,
            application=DatabaseResource(
                **{**application.__dict__, "schema_revision": str(application_revision)}
            ),
            checkpoint=DatabaseResource(
                **{**checkpoint.__dict__, "checkpoint_ready": True}
            ),
            run_id=config.run_id,
        )
    except Exception as exc:
        cleanup_result = (
            _cleanup_created(admin, created_resources)
            if admin is not None
            else DatabaseCleanupResult(
                status="passed",
                failures=[],
                attempted=[],
                deleted=[],
                remaining=[],
            )
        )
        partial_result = None
        if known_resources and admin_target is not None:
            partial_result = _partial_bootstrap_result(
                config,
                server_host=admin_target[0],
                server_port=admin_target[1],
                owner_role=owner_role,
                resources=known_resources,
            )
        if not cleanup_result.ok:
            raise DatabaseBootstrapError(
                "evaluator database bootstrap failed; exact cleanup failed",
                cleanup_result=cleanup_result,
                partial_result=partial_result,
            ) from exc
        if isinstance(exc, DatabaseBootstrapError):
            raise DatabaseBootstrapError(str(exc), partial_result=partial_result) from exc
        raise DatabaseBootstrapError(
            "evaluator database bootstrap failed",
            partial_result=partial_result,
        ) from exc
    finally:
        if admin is not None:
            close = getattr(admin, "close", None)
            if callable(close):
                close()


def recover_evaluator_databases_from_manifest(
    record: Mapping[str, Any] | None,
    *,
    admin_url: str,
    run_id: str,
    allowlisted_hosts: tuple[str, ...] = (),
    admin_factory: Callable[[str], Any] | None = None,
) -> DatabaseBootstrapResult:
    """Reconstruct exact prior database handles from live SQL metadata.

    Ledger fields select exact names and the expected run marker only; they
    never establish ownership or creation. Every droppable resource is rebuilt
    after a fresh administrator-boundary read.
    """
    if not isinstance(record, Mapping):
        raise DatabaseBootstrapError("prior evaluator ledger has no database ownership manifest")
    target = validate_evaluator_sql_target(admin_url, allowlisted_hosts=allowlisted_hosts)
    namespace = str(record.get("namespace") or "").strip().lower()
    recorded_run_id = str(record.get("run_id") or "").strip()
    if not namespace or recorded_run_id != str(run_id).strip():
        raise DatabaseBootstrapError("prior evaluator ledger run identity is unverifiable")
    if str(record.get("server_host") or "").strip().lower() != target[0] or int(record.get("server_port") or 0) != target[1]:
        raise DatabaseBootstrapError("prior evaluator ledger SQL server identity does not match the supplied evaluator admin")
    admin = admin_factory(admin_url) if admin_factory is not None else PostgresDatabaseAdmin(admin_url)
    try:
        resources: dict[str, DatabaseResource] = {}
        for role in ("application", "checkpoint"):
            raw = record.get(role)
            if not isinstance(raw, Mapping):
                raise DatabaseBootstrapError("prior evaluator ledger database manifest is incomplete")
            name = _validate_name(str(raw.get("name") or ""), role)
            expected_name = f"{namespace}_{'app' if role == 'application' else 'checkpoint'}"
            if name != expected_name:
                raise DatabaseBootstrapError(
                    "prior evaluator ledger database name is not bound to its recorded namespace"
                )
            owner_role = str(raw.get("owner_role") or "").strip().lower()
            if _IDENTIFIER.fullmatch(owner_role) is None:
                raise DatabaseBootstrapError("prior evaluator ledger database owner is unverifiable")
            marker = _ownership_marker(namespace, role, owner_role, run_id)
            if str(raw.get("ownership_marker") or "") != marker:
                raise DatabaseBootstrapError("prior evaluator ledger database ownership marker is unverifiable")
            if not isinstance(raw.get("created_by_run"), bool):
                raise DatabaseBootstrapError("prior evaluator ledger database creation state is unverifiable")
            if not isinstance(raw.get("live_verified"), bool):
                raise DatabaseBootstrapError("prior evaluator ledger live ownership proof is unverifiable")
            if str(raw.get("ownership_state") or "") == "unproven" and raw.get("live_verified") is False:
                resources[role] = DatabaseResource(
                    role=role,
                    name=name,
                    url=_database_url(admin_url, name),
                    owner_role=owner_role,
                    ownership_marker=marker,
                    ownership_state="unproven",
                    created_by_run=False,
                    live_verified=False,
                )
                continue
            info = admin.database_info(name)
            if info is None:
                resources[role] = DatabaseResource(
                    role=role,
                    name=name,
                    url=_database_url(admin_url, name),
                    owner_role=owner_role,
                    ownership_marker=marker,
                    ownership_state="already_cleaned",
                    created_by_run=False,
                    live_verified=False,
                    schema_revision=raw.get("schema_revision") if isinstance(raw.get("schema_revision"), str) else None,
                    checkpoint_ready=raw.get("checkpoint_ready") is True,
                )
                continue
            if info.name != name or info.owner != owner_role or info.comment != marker:
                raise DatabaseBootstrapError("prior evaluator ledger live database ownership could not be proven")
            resources[role] = DatabaseResource(
                role=role,
                name=name,
                url=_database_url(admin_url, name),
                owner_role=owner_role,
                ownership_marker=marker,
                ownership_state=str(raw.get("ownership_state") or "pre_existing_evaluator"),
                created_by_run=bool(raw.get("created_by_run")),
                live_verified=True,
                schema_revision=raw.get("schema_revision") if isinstance(raw.get("schema_revision"), str) else None,
                checkpoint_ready=raw.get("checkpoint_ready") is True,
            )
        return DatabaseBootstrapResult(
            namespace=namespace,
            server_host=target[0],
            server_port=target[1],
            admin_url=admin_url,
            application=resources["application"],
            checkpoint=resources["checkpoint"],
            run_id=run_id,
        )
    finally:
        if admin_factory is None:
            admin.close()


def drop_evaluator_databases(
    result: DatabaseBootstrapResult,
    *,
    admin_adapter: Any | None = None,
) -> tuple[int, int]:
    """Drop and verify only databases marked as created by this run."""
    target = _url_target(result.admin_url, "evaluator SQL administrator connection")
    if target[:2] != (str(result.server_host).lower(), int(result.server_port)):
        raise DatabaseBootstrapError("evaluator SQL server identity could not be re-proven for cleanup")
    admin = admin_adapter or PostgresDatabaseAdmin(result.admin_url)
    if admin_adapter is not None:
        live_host = str(getattr(admin, "server_host", "") or "").lower()
        live_port = int(getattr(admin, "server_port", 0) or 0)
        if live_host and (live_host, live_port) != target[:2]:
            raise DatabaseBootstrapError("evaluator SQL admin adapter is on a different server")
    deleted = 0
    remaining = 0
    try:
        for resource in (result.application, result.checkpoint):
            if not resource.created_by_run:
                continue
            if not resource.live_verified:
                raise DatabaseBootstrapError(
                    "evaluator database creation ownership was not live-verified"
                )
            info = admin.database_info(resource.name)
            if info is None:
                continue
            if info.name != resource.name or info.owner != resource.owner_role or info.comment != resource.ownership_marker:
                raise DatabaseBootstrapError(
                    "evaluator database identity or ownership marker changed before cleanup"
                )
            if admin.drop_owned_database(resource):
                deleted += 1
            if admin.database_info(resource.name) is not None:
                remaining += 1
        return deleted, remaining
    finally:
        if admin_adapter is None:
            admin.close()
