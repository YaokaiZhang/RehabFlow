import hashlib
import os
from collections.abc import MutableMapping
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[2]


def backend_source_revision(backend_dir: Path | None = None) -> str:
    """Return a deterministic revision for the runnable backend source."""
    root = (backend_dir or BACKEND_DIR).resolve()
    digest = hashlib.sha256()
    source_root = root / "app"
    source_paths = sorted(
        path
        for path in source_root.rglob("*.py")
        if path.is_file()
    )
    launcher = root.parent / "scripts" / "dev-backend.mjs"
    if launcher.is_file():
        source_paths.append(launcher)
    for path in source_paths:
        digest.update(path.relative_to(root.parent).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def backend_env_paths(override: str | Path | None = None) -> tuple[Path, ...]:
    explicit = override or os.getenv("BACKEND_ENV_FILE")
    if explicit:
        return (Path(explicit),)
    return (BACKEND_DIR / ".env", BACKEND_DIR.parent / ".env")


def _read_dotenv_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip(chr(34) + "'")
        if normalized_key and normalized_value:
            values[normalized_key] = normalized_value
    return values


def load_backend_env(
    *,
    paths: tuple[Path, ...] | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Load legacy dotenv values, then root values, without overriding process env."""
    target = os.environ if environ is None else environ
    merged: dict[str, str] = {}
    for env_file in paths or backend_env_paths():
        merged.update(_read_dotenv_file(env_file))
    for key, value in merged.items():
        target.setdefault(key, value)


_DEFAULT_ENV_FILES = tuple(str(path) for path in backend_env_paths())
DEFAULT_JWT_SECRET_KEY = "CHANGE_ME_FOR_PRODUCTION"
_INSECURE_PRODUCTION_VALUES = frozenset(
	{
		"",
		"change_me",
		"change_me_for_production",
		"changeme",
		"default",
		"example",
		"example-secret",
		"replace-me",
		"replace_me",
		"todo",
		"generate-a-random-secret",
		"generate-a-unique-jwt-secret",
		"generate-a-dedicated-trace-hmac-key",
		"generate-random-secret",
	}
)
_PLACEHOLDER_SECRET_PATTERN = re.compile(
	r"(?i)(?:^|[-_. ])(?:"
	r"change[-_ ]?me(?:[-_ ]?(?:later|for[-_ ]?production))?|"
	r"replace[-_ ]?(?:me|with)(?:[-_ ]?(?:a|an))?(?:[-_ ]?(?:secret|key|token|value))?|"
	r"generate(?:d)?[-_ ]?(?:a|an)?[-_ ]?(?:random|unique|dedicated)?[-_ ]?(?:secret|key|token)|"
	r"(?:default|example|placeholder|dummy|sample|todo|test)[-_ ]?(?:secret|key|token|value)?"
	r")(?:$|[-_. ])"
)
_SECRET_NAMES = (
	"authorization",
	"api[_-]?key",
	"access[_-]?token",
	"refresh[_-]?token",
	"token",
	"secret",
	"password",
	"ticket",
	"prompt",
	"message",
	"credential",
)
_SENSITIVE_VALUE_PATTERN = re.compile(
	rf"(?i)(?P<quote>['\"]?)(?P<name>[A-Za-z0-9_.-]*(?:{'|'.join(_SECRET_NAMES)})[A-Za-z0-9_.-]*)(?P=quote)\s*(?:[:=])\s*(?:Bearer\s+)?(?:'[^']*'|\"[^\"]*\"|[^\s,;}}]+)"
)


def redact_sensitive_text(value: object) -> str:
	"""Return a log-safe representation without credential or user-content values."""
	text = str(value)
	return _SENSITIVE_VALUE_PATTERN.sub(
		lambda match: f"{match.group('name')}=[REDACTED]", text
	)


def redact_exception(exc: BaseException) -> str:
	return redact_sensitive_text(exc)


def redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
	return {str(key): redact_sensitive_text(item) for key, item in value.items()}


def _is_insecure_production_value(value: object) -> bool:
	"""Recognize operator placeholders without rejecting normal random secrets."""
	normalized = str(value or "").strip().lower()
	if normalized in _INSECURE_PRODUCTION_VALUES:
		return True
	return bool(_PLACEHOLDER_SECRET_PATTERN.search(normalized))

def evaluator_mode_enabled() -> bool:
	"""Return true only for an explicitly evaluator-owned non-production process."""
	return (
		os.getenv("REHAB_EVAL_MODE", "").strip().lower() in {"1", "true", "yes"}
		and os.getenv("REHAB_EVAL_IDENTITY", "").strip().lower() == "evaluation"
		and os.getenv("ENVIRONMENT", "development").strip().lower()
		not in {"prod", "production", "staging", "live"}
	)


def evaluator_fault_profile(profile: str) -> bool:
	"""Check a fault profile without allowing production configuration to activate it."""
	return evaluator_mode_enabled() and os.getenv("REHAB_EVAL_FAULT_PROFILE", "").strip().lower() == profile


def _make_url(database_url: str):
	from sqlalchemy.engine import make_url

	return make_url(database_url)


def _database_target_identity(database_url: str) -> tuple[str | None, int | None, str | None]:
	url = _make_url(database_url)
	return url.host, url.port, url.database


def _runtime_sqlalchemy_url(database_url: str) -> str:
	url = _make_url(database_url)
	if url.drivername == "postgresql":
		url = url.set(drivername="postgresql+psycopg2")
	return url.render_as_string(hide_password=False)


class Settings(BaseSettings):
	app_name: str = "rehab-agent-api"
	environment: str = "dev"
	frontend_origin: str = "http://localhost:3002"
	# These are server-owned controls. Requests cannot override either control.
	debug: bool = False
	tracing_enabled: bool = True
	build_revision: str | None = Field(
		default_factory=backend_source_revision,
		validation_alias=AliasChoices("REHAB_BUILD_REVISION", "BUILD_REVISION", "build_revision"),
	)
	evaluation_identity: str = Field(
		default="rehabflow",
		validation_alias=AliasChoices("REHAB_EVAL_IDENTITY", "evaluation_identity"),
	)

	# DATABASE_URL is the canonical application and Alembic target.
	database_url: str
	# Legacy SQLAlchemy callers may provide POSTGRES_URL explicitly. When omitted,
	# derive the runtime URL from DATABASE_URL.
	postgres_url: str | None = None
	# Keep psycopg checkpointer URLs independent of SQLAlchemy driver normalization.
	checkpoint_database_url: str | None = None
	redis_url: str = "redis://localhost:6379/0"
	stream_ticket_ttl_seconds: int = 60

	qdrant_url: str = "http://localhost:6333"
	qdrant_api_key: str | None = None
	qdrant_path: str | None = None
	qdrant_collection_name: str = "rehab_knowledge"
	qdrant_vector_size: int = 1024

	exercise_catalog_path: str = ""

	# Production embeddings use the OpenAI embeddings API.
	embedding_provider: str = Field(
		default="openai",
		validation_alias=AliasChoices(
			"REHAB_EMBEDDING_PROVIDER",
			"EMBEDDING_PROVIDER",
			"embedding_provider",
		),
	)
	openai_api_key: str | None = None
	openai_api_base: str = "https://api.openai.com/v1"
	openai_model: str = "gpt-5.4-mini"
	openai_embedding_model: str = "text-embedding-3-small"
	embedding_api_key: str | None = Field(
		default=None,
		validation_alias=AliasChoices("EMBEDDING_API_KEY", "embedding_api_key"),
	)
	embedding_api_base: str | None = Field(
		default=None,
		validation_alias=AliasChoices("EMBEDDING_API_BASE", "embedding_api_base"),
	)
	embedding_model: str | None = Field(
		default=None,
		validation_alias=AliasChoices("EMBEDDING_MODEL", "embedding_model"),
	)
	qwen_api_key: str | None = None
	qwen_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
	qwen_embedding_model: str = "text-embedding-v4"
	embedding_timeout_seconds: float = 60.0
	embedding_max_retries: int = 3
	embedding_batch_size: int = 10
	reviewer_timeout_seconds: float = Field(
		default=10.0,
		validation_alias=AliasChoices(
			"REHAB_REVIEWER_TIMEOUT_SECONDS",
			"reviewer_timeout_seconds",
		),
	)
	session_compaction_timeout_seconds: float = Field(
		default=120.0,
		validation_alias=AliasChoices(
			"REHAB_SESSION_COMPACTION_TIMEOUT_SECONDS",
			"session_compaction_timeout_seconds",
		),
	)
	memory_agent_timeout_seconds: float = Field(
		default=120.0,
		validation_alias=AliasChoices(
			"REHAB_MEMORY_AGENT_TIMEOUT_SECONDS",
			"memory_agent_timeout_seconds",
		),
	)
	memory_maintenance_poll_interval_seconds: float = Field(
		default=2.0,
		validation_alias=AliasChoices(
			"REHAB_MEMORY_MAINTENANCE_POLL_INTERVAL_SECONDS",
			"memory_maintenance_poll_interval_seconds",
		),
	)
	memory_maintenance_batch_size: int = Field(
		default=10,
		validation_alias=AliasChoices(
			"REHAB_MEMORY_MAINTENANCE_BATCH_SIZE",
			"memory_maintenance_batch_size",
		),
	)

	jwt_secret_key: str = DEFAULT_JWT_SECRET_KEY
	jwt_algorithm: str = "HS256"
	jwt_access_token_expire_minutes: int = 60 * 24
	trace_hmac_key: str | None = Field(
		default=None,
		validation_alias=AliasChoices("REHAB_TRACE_HMAC_KEY", "TRACE_HMAC_KEY", "trace_hmac_key"),
	)
	trace_hmac_key_version: str = Field(
		default="v1",
		validation_alias=AliasChoices(
			"REHAB_TRACE_HMAC_KEY_VERSION",
			"TRACE_HMAC_KEY_VERSION",
			"trace_hmac_key_version",
		),
	)

	model_config = SettingsConfigDict(
		env_file=_DEFAULT_ENV_FILES,
		env_file_encoding="utf-8",
		env_ignore_empty=True,
		extra="ignore",
		populate_by_name=True,
	)

	def __init__(self, **values: Any) -> None:
		if "_env_file" not in values:
			override = os.getenv("BACKEND_ENV_FILE")
			if override:
				values["_env_file"] = override
		super().__init__(**values)

	@property
	def effective_checkpoint_database_url(self) -> str:
		return self.checkpoint_database_url or self.database_url

	@model_validator(mode="after")
	def resolve_application_database_targets(self) -> "Settings":
		if self.postgres_url is None:
			self.postgres_url = _runtime_sqlalchemy_url(self.database_url)
		elif _database_target_identity(self.postgres_url) != _database_target_identity(self.database_url):
			raise ValueError(
				"postgres_url and database_url must identify the same database target"
			)
		return self

	@model_validator(mode="after")
	def validate_runtime_secrets_and_controls(self) -> "Settings":
		if self.environment == "production":
			secret_values = {
				"jwt_secret_key": self.jwt_secret_key,
				"trace_hmac_key": self.trace_hmac_key,
				"openai_api_key": self.openai_api_key,
				"embedding_api_key": self.embedding_api_key,
				"qwen_api_key": self.qwen_api_key,
			}
			insecure = {
				name
				for name, value in secret_values.items()
				if _is_insecure_production_value(value)
			}
			if self.jwt_secret_key == DEFAULT_JWT_SECRET_KEY or "jwt_secret_key" in insecure:
				raise ValueError("jwt_secret_key must be explicitly configured in production")
			if "trace_hmac_key" in insecure:
				raise ValueError("trace_hmac_key must be explicitly configured in production")
			provider = self.embedding_provider.strip().lower()
			if provider == "openai":
				if self.embedding_api_key and "embedding_api_key" in insecure:
					raise ValueError("embedding_api_key must be explicitly configured in production")
				if not self.embedding_api_key and "openai_api_key" in insecure:
					raise ValueError("openai_api_key must be explicitly configured in production")
			if provider in {"qwen", "dashscope"}:
				if self.embedding_api_key and "embedding_api_key" in insecure:
					raise ValueError("embedding_api_key must be explicitly configured in production")
				if not self.embedding_api_key and "qwen_api_key" in insecure:
					raise ValueError("qwen_api_key must be explicitly configured in production")
			non_empty = [value for value in secret_values.values() if value]
			if len(non_empty) != len(set(non_empty)):
				raise ValueError("production secrets must be distinct")
			self.debug = False
		return self

	@model_validator(mode="after")
	def validate_service_url_schemes(self) -> "Settings":
		redis_scheme = str(self.redis_url).split(":", 1)[0].lower()
		if redis_scheme not in {"redis", "rediss"}:
			raise ValueError("redis_url must use redis:// or rediss://")
		for field_name, value in (
			("qdrant_url", self.qdrant_url),
			("openai_api_base", self.openai_api_base),
			("qwen_api_base", self.qwen_api_base),
			("embedding_api_base", self.embedding_api_base),
		):
			if value is None:
				continue
			scheme = str(value).split(":", 1)[0].lower()
			if scheme not in {"http", "https"}:
				raise ValueError(f"{field_name} must use http:// or https://")
			if self.environment == "production" and scheme != "https":
				raise ValueError(f"{field_name} must use https:// in production")
		return self

	@field_validator("environment", mode="before")
	@classmethod
	def normalize_environment(cls, value: str) -> str:
		normalized = str(value).strip().lower()
		aliases = {"dev": "development", "development": "development", "test": "test", "prod": "production", "production": "production"}
		if normalized not in aliases:
			raise ValueError("environment must be test, development, or production")
		return aliases[normalized]

	@field_validator("postgres_url", "checkpoint_database_url", mode="before")
	@classmethod
	def empty_optional_database_url_to_none(cls, value: str | None) -> str | None:
		if value is None or (isinstance(value, str) and not value.strip()):
			return None
		return value

	@field_validator("database_url", "postgres_url", "checkpoint_database_url")
	@classmethod
	def validate_database_url(cls, value: str | None, info: Any) -> str | None:
		if value is None:
			return value
		try:
			url = _make_url(value)
		except Exception as exc:
			raise ValueError(f"{info.field_name} must be a valid database URL") from exc
		if not url.drivername:
			raise ValueError(f"{info.field_name} must include a database scheme")
		return value

	@field_validator("stream_ticket_ttl_seconds")
	@classmethod
	def bounded_stream_ticket_ttl(cls, value: int) -> int:
		if not 1 <= value <= 60:
			raise ValueError("stream_ticket_ttl_seconds must be between 1 and 60 seconds")
		return value

	@field_validator("reviewer_timeout_seconds")
	@classmethod
	def bounded_reviewer_timeout(cls, value: float) -> float:
		if not 0.1 <= value <= 300:
			raise ValueError("reviewer_timeout_seconds must be between 0.1 and 300 seconds")
		return value

	@field_validator("session_compaction_timeout_seconds", "memory_agent_timeout_seconds")
	@classmethod
	def bounded_context_agent_timeout(cls, value: float) -> float:
		if not 0.1 <= value <= 900:
			raise ValueError("context agent timeout must be between 0.1 and 900 seconds")
		return value

	@field_validator("memory_maintenance_poll_interval_seconds")
	@classmethod
	def bounded_memory_maintenance_poll_interval(cls, value: float) -> float:
		if not 0.1 <= value <= 300:
			raise ValueError("memory maintenance poll interval must be between 0.1 and 300 seconds")
		return value

	@field_validator("memory_maintenance_batch_size")
	@classmethod
	def bounded_memory_maintenance_batch_size(cls, value: int) -> int:
		if not 1 <= value <= 100:
			raise ValueError("memory maintenance batch size must be between 1 and 100")
		return value

	@field_validator("qdrant_api_key", "qdrant_path", mode="before")
	@classmethod
	def empty_string_to_none(cls, value: str | None) -> str | None:
		if value == "":
			return None
		return value

	def validate_runtime_configuration(self) -> None:
		"""Validate startup prerequisites without logging any credential values."""
		for field_name, value in (
			("database_url", self.database_url),
			("checkpoint_database_url", self.effective_checkpoint_database_url),
			("redis_url", self.redis_url),
			("qdrant_url", self.qdrant_url),
			("openai_api_base", self.openai_api_base),
			("qwen_api_base", self.qwen_api_base),
			("embedding_api_base", self.embedding_api_base),
		):
			if value is None:
				continue
			try:
				url = _make_url(value) if field_name.endswith("database_url") else value
			except Exception as exc:
				raise ValueError(f"{field_name} is invalid") from exc
			if field_name.endswith("database_url") and not url.drivername:
				raise ValueError(f"{field_name} is invalid")
			if field_name == "redis_url" and not str(value).startswith(("redis://", "rediss://")):
				raise ValueError(f"{field_name} is invalid")
			if field_name in {"qdrant_url", "openai_api_base", "qwen_api_base", "embedding_api_base"} and not str(value).startswith(("http://", "https://")):
				raise ValueError(f"{field_name} is invalid")
		if self.environment == "production":
			# Re-run the same server-owned checks for callers that mutate settings.
			self.validate_runtime_secrets_and_controls()


@lru_cache
def get_settings() -> Settings:
	try:
		return Settings()
	except ValidationError as exc:
		# Keep field names and error types useful for operators, but never render
		# Pydantic's input_value payload (which can contain credentials).
		details = "; ".join(
			f"{'.'.join(str(part) for part in error.get('loc', ())) or 'settings'}: {error.get('type', 'invalid')}"
			for error in exc.errors()
		)
		raise RuntimeError(f"application configuration is invalid ({details})") from None
	except Exception as exc:
		# Pydantic's default error rendering includes input values. Keep startup
		# diagnostics actionable without echoing credentials or user content.
		raise RuntimeError("application configuration is invalid") from None
