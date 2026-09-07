from collections.abc import Generator
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

settings = get_settings()

engine = create_engine(settings.postgres_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class SchemaRevisionError(RuntimeError):
	"""Raised when the database does not match the Alembic migration head."""


def get_db() -> Generator[Session, None, None]:
	db = SessionLocal()
	try:
		yield db
	finally:
		db.close()


def verify_schema_revision() -> None:
	"""Fail API startup unless the existing schema matches Alembic's current heads."""
	expected_revisions = set(ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_heads())

	with engine.connect() as connection:
		if not inspect(connection).has_table("alembic_version"):
			raise SchemaRevisionError(
				"Database schema is not initialized: alembic_version is missing. "
				"Run cd backend && alembic -c ../alembic.ini upgrade head before starting the API."
			)

		actual_revisions = set(
			connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
		)

	if actual_revisions != expected_revisions:
		raise SchemaRevisionError(
			"Database schema revision mismatch: "
			f"expected {sorted(expected_revisions)}, found {sorted(actual_revisions)}. "
			"Run cd backend && alembic -c ../alembic.ini upgrade head before starting the API."
		)
