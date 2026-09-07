"""Evaluator-owned non-production backend service harness and provenance contracts."""
from __future__ import annotations

import re
from collections import deque
from threading import Thread
import json
import os
import signal
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

_EVAL_ENVS = frozenset({"evaluation", "test", "dev", "development"})
_STARTUP_SECRET_RE = re.compile(r"(?i)((?:password|api[_-]?key|token|secret)\s*[=:]\s*)[^\s,;]+")
_DATABASE_CREDENTIAL_RE = re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+@")
_DIRECT_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_HEALTH_DIAGNOSTIC_HEADERS = frozenset({
    "connection",
    "content-length",
    "content-type",
    "date",
    "location",
    "server",
    "transfer-encoding",
    "via",
    "x-cache",
    "x-powered-by",
})

def _sanitize_stderr_line(line: str) -> str:
    value = _STARTUP_SECRET_RE.sub(r"\1<redacted>", line)
    return _DATABASE_CREDENTIAL_RE.sub(r"\1<redacted>@", value)


def _allocate_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        return int(listener.getsockname()[1])


def _resolved_python_path(backend_dir: Path, python_bin: Path) -> Path:
    if python_bin.is_absolute():
        return python_bin
    candidates = (
        Path.cwd() / python_bin,
        backend_dir / python_bin,
        backend_dir.parent / python_bin,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return (Path.cwd() / python_bin).resolve()


def _signal_process_group(
    process: subprocess.Popen[bytes],
    process_group_id: int | None,
    signal_number: int,
) -> None:
    if os.name == "posix" and process_group_id is not None:
        try:
            os.killpg(process_group_id, signal_number)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        if signal_number == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except OSError:
        return


def _set_parent_death_signal() -> None:
    """Terminate an evaluator child when its parent is externally killed."""
    if os.name != "posix":
        return
    try:
        import ctypes

        parent_pid = os.getppid()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, int(signal.SIGTERM)) != 0:
            return
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)
    except (AttributeError, OSError, TypeError):
        return


_REQUIRED_CAPABILITIES = ("evaluation_telemetry", "evaluation_fixture_control")
FAULT_PROFILES = frozenset({
    "planner_failure",
    "provider_failure",
    "tool_failure",
    "evidence_unavailable",
    "reviewer_failure",
    "timeout",
    "service_unavailable",
})


def _env_value(value: object) -> str:
    return str(value).strip().lower()


def _mapping_value(value: object, key: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _validate_isolated_database_url(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg2", "postgresql+asyncpg"}:
        raise ValueError(f"{label} must use an isolated PostgreSQL URL")
    identity = f"{parsed.hostname or ''}:{parsed.port or ''}/{parsed.path or ''}".lower()
    if any(marker in identity for marker in ("prod", "production", "staging", "live")):
        raise ValueError(f"{label} appears to target a production database")
    if not any(marker in identity for marker in ("eval", "evaluator", "test", "ci", "dev")):
        raise ValueError(f"{label} is not recognizably evaluator-isolated")


def _database_target(value: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(value)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 5432, (parsed.path or "").rstrip("/").lower())

@dataclass(frozen=True)
class ServiceHarnessConfig:
    backend_dir: Path
    python_bin: Path
    host: str
    port: int
    environment: str
    expected_revision: str | None = None
    backend_launcher: Path | None = None
    node_bin: Path | None = None
    extra_environment: Mapping[str, str] = field(default_factory=dict)
    database_url: str | None = None
    checkpoint_database_url: str | None = None
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_path: Path | None = None
    catalog_dir: Path | None = None
    embedding_provider: str | None = None
    backend_generation_provider: str = "openai"
    backend_generation_key: str | None = None
    # Backend imports, schema checks, and evaluator startup can exceed 30s on a
    # loaded host. Keep the wait bounded while allowing normal cold starts.
    startup_timeout_seconds: float = 90.0

    def __post_init__(self) -> None:
        if not self.backend_dir.is_dir():
            raise ValueError("backend_dir must be an existing directory")
        if not _resolved_python_path(self.backend_dir, self.python_bin).is_file():
            raise ValueError("python_bin must be an existing executable")
        if not self.host.strip():
            raise ValueError("host is required")
        if not 0 <= int(self.port) <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if _env_value(self.environment) not in _EVAL_ENVS:
            raise ValueError("service harness only permits evaluation/test/development environments")
        if self.backend_launcher is not None:
            if not self.backend_launcher.is_absolute():
                raise ValueError("backend_launcher must be absolute")
            if not self.backend_launcher.is_file():
                raise ValueError("backend_launcher must be an existing file")
        if self.node_bin is not None:
            if not self.node_bin.is_absolute() or not self.node_bin.is_file():
                raise ValueError("node_bin must be an existing absolute file")
        if self.database_url:
            _validate_isolated_database_url(self.database_url, "application database")
        if self.checkpoint_database_url:
            _validate_isolated_database_url(self.checkpoint_database_url, "checkpoint database")
        if self.database_url and self.checkpoint_database_url and _database_target(self.database_url) == _database_target(self.checkpoint_database_url):
            raise ValueError("application and checkpoint databases must be isolated")
        if self.catalog_dir is not None and not self.catalog_dir.is_absolute():
            raise ValueError("catalog_dir must be absolute")
        if self.qdrant_url is not None and not urlsplit(self.qdrant_url).netloc:
            raise ValueError("qdrant_url must include a host")
        if self.qdrant_path is not None and not self.qdrant_path.is_absolute():
            raise ValueError("qdrant_path must be absolute")
        if self.startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{int(self.port)}"


@dataclass(frozen=True)
class BackendHealth:
    status: str
    service: str
    environment: str
    backend_revision: str
    workflow_version: str
    capabilities: Mapping[str, bool]
    identity: str
    dirty_state: str = "unreported"

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BackendHealth":
        capabilities = payload.get("capabilities", {})
        return cls(
            status=str(payload.get("status") or ""),
            service=str(payload.get("service") or ""),
            environment=str(payload.get("environment") or ""),
            backend_revision=str(payload.get("backend_revision") or ""),
            workflow_version=str(payload.get("workflow_version") or ""),
            capabilities={str(key): bool(value) for key, value in capabilities.items()} if isinstance(capabilities, Mapping) else {},
            identity=str(payload.get("identity") or ""),
            dirty_state=str(payload.get("dirty_state") or "unreported"),
        )

    def sanitized(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "service": self.service,
            "environment": self.environment,
            "backend_revision": self.backend_revision,
            "workflow_version": self.workflow_version,
            "capabilities": dict(self.capabilities),
            "identity": self.identity,
            "dirty_state": self.dirty_state,
        }


def validate_backend_health(health: BackendHealth, *, expected_revision: str | None = None, expected_workflow_version: str | None = None, require_restart: bool = False) -> BackendHealth:
    if health.identity != "evaluation":
        raise RuntimeError("backend health identity is not the evaluator identity")
    if _env_value(health.environment) not in _EVAL_ENVS:
        raise RuntimeError("backend health environment is not evaluation-safe")
    if expected_revision and health.backend_revision != expected_revision:
        raise RuntimeError("backend health revision does not match the evaluator revision")
    if not health.workflow_version:
        raise RuntimeError("backend health workflow version is missing")
    if expected_workflow_version is not None and health.workflow_version != expected_workflow_version:
        raise RuntimeError("backend health workflow version does not match the evaluator expectation")
    missing = [name for name in _REQUIRED_CAPABILITIES if health.capabilities.get(name) is not True]
    if require_restart and health.capabilities.get("restart_control") is not True:
        missing.append("restart_control")
    if missing:
        raise RuntimeError("backend health is missing evaluator capabilities: " + ", ".join(missing))
    return health


def _node_binary(config: ServiceHarnessConfig) -> Path:
    candidates: list[Path] = []
    if config.node_bin is not None:
        candidates.append(config.node_bin)
    configured = shutil.which("node")
    if configured:
        candidates.append(Path(configured))
    candidates.extend((Path.home() / "miniconda3" / "bin" / "node", Path("/opt/conda/bin/node"), Path("/usr/local/bin/node"), Path("/usr/bin/node")))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("Node.js is required to launch scripts/dev-backend.mjs")


def build_backend_command(config: ServiceHarnessConfig, *, port: int | None = None) -> list[str]:
    actual_port = config.port if port is None else int(port)
    launcher = config.backend_launcher or (config.backend_dir.parent / "scripts" / "dev-backend.mjs")
    python_bin = _resolved_python_path(config.backend_dir, config.python_bin)
    if launcher.is_file():
        return [str(_node_binary(config)), str(launcher), str(actual_port)]
    return [str(python_bin), "-m", "uvicorn", "app.main:app", "--host", config.host, "--port", str(actual_port)]


def compare_budget_usage(policy: Mapping[str, Any] | object, usage: Mapping[str, Any] | object, *, now: float | None = None) -> dict[str, Any]:
    def value(obj: object, key: str) -> object:
        return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)

    observed = {key: value(usage, key) for key in ("model_call_attempts", "tool_call_attempts", "total_tokens", "repair_attempts", "elapsed_ms")}
    reasons: list[str] = []
    unavailable = observed["total_tokens"] is None
    compliant = True
    for observed_key, limit_key, label in (
        ("model_call_attempts", "max_model_calls", "model_calls"),
        ("tool_call_attempts", "max_tool_calls", "tool_calls"),
        ("repair_attempts", "max_repairs", "repairs"),
    ):
        limit = value(policy, limit_key)
        current = observed[observed_key]
        if limit is None:
            unavailable = True
            reasons.append(f"{limit_key}_unavailable")
        elif current is None:
            unavailable = True
            reasons.append(f"{observed_key}_unavailable")
        elif int(current) > int(limit):
            compliant = False
            reasons.append(f"{observed_key}_exceeded")
    token_limit = value(policy, "max_total_tokens")
    if token_limit is None:
        unavailable = True
        reasons.append("max_total_tokens_unavailable")
    elif observed["total_tokens"] is None:
        unavailable = True
        reasons.append("token_usage_unavailable")
    elif int(observed["total_tokens"]) > int(token_limit):
        compliant = False
        reasons.append("total_tokens_exceeded")
    deadline = value(policy, "deadline_seconds")
    if deadline is not None:
        if observed["elapsed_ms"] is None:
            unavailable = True
            reasons.append("elapsed_ms_unavailable")
        elif float(observed["elapsed_ms"]) > float(deadline) * 1000:
            compliant = False
            reasons.append("deadline_exceeded")
    if now is not None and value(usage, "started_at") is not None and deadline is not None:
        if now - float(value(usage, "started_at")) > float(deadline):
            compliant = False
            reasons.append("deadline_exceeded")
    if unavailable:
        compliant = False
    return {"compliant": compliant, "unavailable": unavailable, "reason_codes": list(dict.fromkeys(reasons)), "observed": observed}


class ServiceHarness:
    def __init__(self, config: ServiceHarnessConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None
        self._fault_profile: str | None = None
        self._active_resource_environment: dict[str, str] = {}
        self._active_port: int | None = None
        self._last_process_returncode: int | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread: Thread | None = None
        self._last_response_status: int | None = None
        self._last_response_headers: dict[str, str] = {}
        self._last_response_body = ""

    @property
    def base_url(self) -> str:
        port = self._active_port if self._active_port is not None else self.config.port
        return f"http://{self.config.host}:{int(port)}"

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update({str(key): str(value) for key, value in self.config.extra_environment.items()})
        backend_provider = self.config.backend_generation_provider.strip().lower()
        backend_key_name = "OPENAI_API_KEY" if backend_provider == "openai" else "QWEN_API_KEY"
        backend_base_name = "OPENAI_API_BASE" if backend_provider == "openai" else "QWEN_API_BASE"
        backend_key = self.config.backend_generation_key or environment.get(backend_key_name, "")
        backend_base = environment.get(backend_base_name, "")
        for key in (
            "ENVIRONMENT",
            "REHAB_EVAL_MODE",
            "REHAB_EVAL_IDENTITY",
            "REHAB_EVAL_RESTART_CONTROL",
            "BACKEND_ENV_FILE",
            "BACKEND_PORT",
            "REHAB_BUILD_REVISION",
            "REHAB_EVAL_BACKEND_DIRTY_STATE",
            "DATABASE_URL",
            "POSTGRES_URL",
            "CHECKPOINT_DATABASE_URL",
            "QDRANT_URL",
            "QDRANT_API_KEY",
            "QDRANT_PATH",
            "QDRANT_COLLECTION_NAME",
            "EXERCISE_CATALOG_PATH",
            "REHAB_EVAL_CATALOG_DIR",
            "REHAB_EVAL_FAULT_PROFILE",
            "QWEN_JUDGE_API_KEY",
            "QWEN_JUDGE_API_BASE",
            "QWEN_API_KEY",
            "QWEN_API_BASE",
            "OPENAI_JUDGE_API_KEY",
            "OPENAI_JUDGE_API_BASE",
            "OPENAI_API_KEY",
            "OPENAI_API_BASE",
        ):
            environment.pop(key, None)
        environment.update({
            "ENVIRONMENT": "development" if _env_value(self.config.environment) == "evaluation" else self.config.environment,
            "DEBUG": "true",
            "REHAB_EVAL_MODE": "true",
            "REHAB_EVAL_RESTART_CONTROL": "true",
            "BACKEND_PYTHON": str(_resolved_python_path(self.config.backend_dir, self.config.python_bin)),
            "REHAB_EVAL_IDENTITY": "evaluation",
            "REHAB_EVAL_OPENAI_TIMEOUT_SECONDS": os.getenv("REHAB_EVAL_OPENAI_TIMEOUT_SECONDS", "120"),
            "REHAB_EVAL_OPENAI_MAX_RETRIES": os.getenv("REHAB_EVAL_OPENAI_MAX_RETRIES", "2"),
            "REHAB_EVAL_REVIEW_PROVIDER_CONCURRENCY": os.getenv(
                "REHAB_EVAL_REVIEW_PROVIDER_CONCURRENCY",
                "1",
            ),
        })
        if self.config.expected_revision:
            environment["REHAB_BUILD_REVISION"] = self.config.expected_revision
        if self.config.database_url is not None:
            environment["DATABASE_URL"] = self.config.database_url
            environment["POSTGRES_URL"] = self.config.database_url
        if self.config.checkpoint_database_url is not None:
            environment["CHECKPOINT_DATABASE_URL"] = self.config.checkpoint_database_url
        if self.config.qdrant_url is not None:
            environment["QDRANT_URL"] = self.config.qdrant_url
        if self.config.qdrant_path is not None:
            environment["QDRANT_PATH"] = str(self.config.qdrant_path)
        if self.config.qdrant_api_key is not None:
            environment["QDRANT_API_KEY"] = self.config.qdrant_api_key
        if self.config.embedding_provider is not None:
            environment["REHAB_EMBEDDING_PROVIDER"] = self.config.embedding_provider
        if self.config.catalog_dir is not None:
            environment["REHAB_EVAL_CATALOG_DIR"] = str(self.config.catalog_dir)
        environment.update(self._active_resource_environment)
        if self._fault_profile:
            environment["REHAB_EVAL_FAULT_PROFILE"] = self._fault_profile
        else:
            environment.pop("REHAB_EVAL_FAULT_PROFILE", None)
        if backend_key:
            environment[backend_key_name] = str(backend_key)
        if backend_base:
            environment[backend_base_name] = str(backend_base)
        return environment

    def _record_response(self, status: int, headers: Any, raw: str) -> None:
        self._last_response_status = int(status)
        self._last_response_headers = {
            str(key): str(value)
            for key, value in (headers.items() if headers is not None else ())
        }
        self._last_response_body = raw[:1000]

    def request_json(self, path: str, *, method: str = "GET", payload: Mapping[str, Any] | None = None, token: str | None = None, timeout_seconds: float = 30.0) -> tuple[int, dict[str, Any]]:
        body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {}), **({"Authorization": f"Bearer {token}"} if token else {})})
        self._last_response_status = None
        self._last_response_headers = {}
        self._last_response_body = ""
        try:
            with _DIRECT_HTTP_OPENER.open(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                self._record_response(response.status, response.headers, raw)
                parsed = json.loads(raw) if raw else {}
                return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            self._record_response(exc.code, exc.headers, raw)
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"detail": raw[:500]}
            return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}
        except urllib.error.URLError as exc:
            raise RuntimeError(f"backend service request failed: {type(exc.reason).__name__}") from exc

    def get_health(self) -> BackendHealth:
        status, payload = self.request_json("/ai/chat/health", timeout_seconds=5.0)
        if status != 200:
            detail_parts: list[str] = []
            if payload:
                try:
                    rendered = json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                except (TypeError, ValueError):
                    rendered = repr(payload)
                detail_parts.append(f"response={_sanitize_stderr_line(rendered[:500])}")
            safe_headers = {
                str(key).lower(): _sanitize_stderr_line(str(value))
                for key, value in self._last_response_headers.items()
                if str(key).lower() in _HEALTH_DIAGNOSTIC_HEADERS
            }
            if safe_headers:
                detail_parts.append(
                    "headers="
                    + json.dumps(safe_headers, ensure_ascii=False, sort_keys=True)
                )
            raw_response = _sanitize_stderr_line(
                self._last_response_body.replace("\r", " ").replace("\n", " ").strip()[:500]
            )
            if raw_response:
                detail_parts.append(f"raw_response={raw_response}")
            detail = "".join(f"; {part}" for part in detail_parts)
            process_id = (
                getattr(self._process, "pid", None)
                if self._process is not None
                else None
            )
            process_detail = f"; backend_pid={process_id}" if process_id else ""
            raise RuntimeError(
                f"backend health returned HTTP {status} at "
                f"{self.base_url}/ai/chat/health{process_detail}{detail}"
            )
        return BackendHealth.from_payload(payload)

    def _capture_stderr(self, stream: Any) -> None:
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_tail.append(_sanitize_stderr_line(line))
        except (OSError, ValueError):
            return

    def _stderr_diagnostic(self) -> str:
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)
        if not self._stderr_tail:
            return ""
        return "; backend stderr: " + " | ".join(list(self._stderr_tail)[-8:])

    def _service_exit_error(self) -> RuntimeError:
        detail = self._stderr_diagnostic()
        return RuntimeError(
            f"backend service exited with code {self._process.returncode}{detail}"
        )

    def _startup_timeout_error(self, last_error: Exception | None) -> RuntimeError:
        detail = ""
        if last_error is not None:
            detail = (
                "; last readiness error: "
                f"{type(last_error).__name__}: "
                f"{_sanitize_stderr_line(str(last_error))}"
            )
        return RuntimeError(
            "backend service did not become ready before the evaluator timeout"
            f"{detail}{self._stderr_diagnostic()}"
        )

    def wait_until_ready(self) -> BackendHealth:
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise self._service_exit_error() from last_error
            try:
                return self.get_health()
            except Exception as exc:
                last_error = exc
                if self._process is not None and self._process.poll() is not None:
                    raise self._service_exit_error() from exc
                time.sleep(0.25)
        if self._process is not None and self._process.poll() is not None:
            raise self._service_exit_error() from last_error
        raise self._startup_timeout_error(last_error) from last_error


    def start(self) -> BackendHealth:
        if not self.config.database_url or not self.config.checkpoint_database_url:
            raise RuntimeError("isolated application and checkpoint database URLs are required")
        if self._process is not None and self._process.poll() is None:
            return self.wait_until_ready()
        port = self.config.port or _allocate_free_port(self.config.host)
        self._active_port = int(port)
        self._stderr_tail.clear()
        self._stderr_thread = None
        self._process = subprocess.Popen(
            build_backend_command(self.config, port=port),
            cwd=str(self.config.backend_dir),
            env=self._environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_set_parent_death_signal,
        )
        process_pid = getattr(self._process, "pid", None)
        self._process_group_id = (
            int(process_pid)
            if os.name == "posix" and isinstance(process_pid, int) and process_pid > 0
            else None
        )
        stderr_stream = getattr(self._process, "stderr", None)
        if stderr_stream is not None:
            self._stderr_thread = Thread(
                target=self._capture_stderr,
                args=(stderr_stream,),
                daemon=True,
            )
            self._stderr_thread.start()
        try:
            return self.wait_until_ready()
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        process_group_id = self._process_group_id
        if process_group_id is not None:
            _signal_process_group(process, process_group_id, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _signal_process_group(process, process_group_id, signal.SIGKILL)
            process.wait(timeout=5)
        self._last_process_returncode = process.returncode
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)
        stderr_stream = getattr(process, "stderr", None)
        if stderr_stream is not None:
            stderr_stream.close()
        self._stderr_thread = None
        self._process = None
        self._process_group_id = None
        self._active_port = None

    @property
    def process_returncode(self) -> int | None:
        if self._process is not None:
            return self._process.poll()
        return self._last_process_returncode

    def activate_case_resources(self, resources: Any) -> BackendHealth | None:
        activation = resources.activation_environment()
        if any(not str(value).strip() for value in activation.values()):
            raise ValueError("case resource activation contains an empty handle")
        if not str(activation.get("QDRANT_COLLECTION_NAME", "")).startswith("rehab_eval_"):
            raise ValueError("case resource collection is outside the evaluator namespace")
        catalog_path = Path(str(activation.get("EXERCISE_CATALOG_PATH", "")))
        if not catalog_path.is_absolute():
            raise ValueError("case catalog activation must use an absolute path")
        if activation == self._active_resource_environment:
            return self.get_health() if self._process is not None and self._process.poll() is None else None
        was_running = self._process is not None and self._process.poll() is None
        self.stop()
        self._active_resource_environment = {str(key): str(value) for key, value in activation.items()}
        return self.start() if was_running else None

    def set_fault_profile(self, profile: str | None) -> BackendHealth | None:
        normalized = str(profile or "").strip().lower() or None
        if normalized is not None and normalized not in FAULT_PROFILES:
            raise ValueError("unsupported evaluator fault profile")
        if normalized == self._fault_profile:
            return self.get_health() if self._process is not None and self._process.poll() is None else None
        was_running = self._process is not None and self._process.poll() is None
        self.stop()
        self._fault_profile = normalized
        return self.start() if was_running else None

    def restart(self) -> BackendHealth:
        self.stop()
        return self.start()
