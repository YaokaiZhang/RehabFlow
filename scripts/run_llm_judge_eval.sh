#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/run_llm_judge_eval.sh [runner options]

Runs the combined 44-case service-backed real-model and longitudinal v2 diagnostics.
OPENAI_JUDGE_API_KEY is used only for judging and defaults to OPENAI_API_KEY.
OPENAI_API_KEY is used for backend generation.
Separate isolated application, checkpoint, Qdrant, catalog,
embedding, generation, and judge configuration is required.

Example using every user-facing argument:
  scripts/run_llm_judge_eval.sh \
    --reuse-generation-key \
    --cases evals/real_model/cases/v2.json \
    --artifact-dir evals/artifacts/combined-v2 \
    --baseline evals/artifacts/combined-v2/baseline.json \
    --include-conversation \
    --max-parallel-cases 2 \
    --max-parallel-judge-calls 1 \
    --judge-model gpt-5.6-luna \
    --judge-thinking-level high

REHAB_EVAL_MAX_PARALLEL_CASES and REHAB_EVAL_MAX_PARALLEL_JUDGE_CALLS
set the same concurrency limits.
--judge-model and --judge-thinking-level are forwarded to the judge provider; the
judge model defaults to REAL_MODEL_JUDGE_MODEL and thinking level to REAL_MODEL_JUDGE_THINKING_LEVEL.
The default artifact directory is backend/evals/artifacts/combined-v2.
Successful evaluator runs retain their test database/resources by default.
Set REHAB_EVAL_KEEP_TEST_DATA=0 to clean even a successful run.
USAGE
}

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
backend_dir="$repo_dir/backend"
python_bin="${REHAB_BACKEND_PYTHON:-$backend_dir/.venv/bin/python}"
reuse_generation_key=1
runner_args=(--include-conversation)

while (($#)); do
  case "$1" in
    --reuse-generation-key)
      reuse_generation_key=1
      shift
      ;;
    --judge-model)
      [[ $# -ge 2 ]] || { echo "--judge-model requires a value" >&2; exit 2; }
      runner_args+=(--judge-model "$2")
      shift 2
      ;;
    --judge-model=*)
      runner_args+=(--judge-model "${1#*=}")
      shift
      ;;
    --judge-thinking-level)
      [[ $# -ge 2 ]] || { echo "--judge-thinking-level requires a value" >&2; exit 2; }
      runner_args+=(--judge-thinking-level "$2")
      shift 2
      ;;
    --judge-thinking-level=*)
      runner_args+=(--judge-thinking-level "${1#*=}")
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      runner_args+=("$1")
      shift
      ;;
  esac
done

unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV REHAB_EVAL_WORKFLOW_VERSION
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL CONDA_PYTHON_EXE

# The evaluator owns fresh isolated databases, including the destructive memory migration.
export ENVIRONMENT=development
export REHAB_ALLOW_DESTRUCTIVE_MEMORY_MIGRATION=1

load_dotenv_value() {
  local key="$1"
  local env_file value
  for env_file in "$repo_dir/.env" "$backend_dir/.env"; do
    [[ -n "${!key:-}" ]] && return
    [[ -f "$env_file" ]] || continue
    value=$(sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*(.*)[[:space:]]*$/\2/p" "$env_file" | head -n 1)
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:-1}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:-1}"
    fi
    if [[ -n "$value" ]]; then
      printf -v "$key" '%s' "$value"
      export "$key"
      return
    fi
  done
}

load_dotenv_value OPENAI_API_KEY
load_dotenv_value OPENAI_JUDGE_API_KEY
load_dotenv_value OPENAI_API_BASE
load_dotenv_value OPENAI_JUDGE_API_BASE
load_dotenv_value REAL_MODEL_JUDGE_MODEL
load_dotenv_value REAL_MODEL_JUDGE_THINKING_LEVEL
load_dotenv_value REHAB_EVAL_SCHEMA_REVISION
load_dotenv_value REHAB_EVAL_BACKEND_REVISION
load_dotenv_value REHAB_EVAL_SQL_ADMIN_URL
load_dotenv_value REHAB_EVAL_SQL_NAMESPACE
load_dotenv_value REHAB_EVAL_APPLICATION_DATABASE_NAME
load_dotenv_value REHAB_EVAL_CHECKPOINT_DATABASE_NAME
load_dotenv_value REHAB_EVAL_SQL_SERVER_ALLOWLIST
load_dotenv_value REHAB_EVAL_SQL_OWNER_ROLE
load_dotenv_value REHAB_EVAL_EMBEDDING_PROVIDER
load_dotenv_value REHAB_EVAL_DATABASE_URL
load_dotenv_value REHAB_EVAL_CHECKPOINT_DATABASE_URL
load_dotenv_value REHAB_EVAL_QDRANT_URL
load_dotenv_value REHAB_EVAL_QDRANT_PATH
load_dotenv_value REHAB_EVAL_QDRANT_API_KEY
load_dotenv_value REHAB_EVAL_CATALOG_DIR
load_dotenv_value REHAB_EVAL_QDRANT_OWNER_IDENTITY
load_dotenv_value REHAB_EVAL_QDRANT_ALLOWLIST
load_dotenv_value REHAB_EVAL_BACKEND_GENERATION_PROVIDER
backend_generation_provider="${REHAB_EVAL_BACKEND_GENERATION_PROVIDER:-openai}"
if [[ "$backend_generation_provider" == "openai" ]] && [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo OPENAI_API_KEY-is-required-for-backend-generation >&2
  exit 2
fi
if [[ -z "${OPENAI_JUDGE_API_KEY:-}" ]]; then
  if [[ "$reuse_generation_key" -eq 1 && -n "${OPENAI_API_KEY:-}" ]]; then
    OPENAI_JUDGE_API_KEY="$OPENAI_API_KEY"
    export OPENAI_JUDGE_API_KEY
  else
    echo OPENAI_JUDGE_API_KEY-is-required-for-judging >&2
    exit 2
  fi
fi
REHAB_EVAL_EMBEDDING_PROVIDER="${REHAB_EVAL_EMBEDDING_PROVIDER:-hash}"
export REHAB_EVAL_EMBEDDING_PROVIDER
evaluator_runtime_parent="${REHAB_EVAL_RUNTIME_DIR:-$repo_dir/backend/evals/artifacts/.runtime}"
case "$evaluator_runtime_parent" in
  /tmp|/tmp/*|/var/tmp|/var/tmp/*|/private/tmp|/private/tmp/*|/private/var/folders|/private/var/folders/*)
    echo evaluator-runtime-path-must-not-be-system-temp >&2
    exit 2
    ;;
esac
local_runtime_dir=""
local_postgres_data_dir=""
local_postgres_bin_dir=""
local_postgres_started=0
local_postgres_port=55432
preserve_local_runtime=0
cleanup_local_resources() {
  local exit_status=$?
  local local_postgres_running=0
  if (( local_postgres_started == 1 )); then
    if (( preserve_local_runtime == 0 )); then
      if ! "$local_postgres_bin_dir/pg_ctl" -D "$local_postgres_data_dir" -t 15 -w stop -m fast >/dev/null 2>&1; then
        "$local_postgres_bin_dir/pg_ctl" -D "$local_postgres_data_dir" -t 15 -w stop -m immediate >/dev/null 2>&1 || true
      fi
      if "$local_postgres_bin_dir/pg_ctl" -D "$local_postgres_data_dir" status >/dev/null 2>&1; then
        local_postgres_running=1
        exit_status=1
      fi
    else
      echo "evaluator-test-data-preserved runtime_dir=$local_runtime_dir sql_admin=postgresql://rehab_eval_admin@127.0.0.1:$local_postgres_port/postgres" >&2
    fi
  fi
  if (( preserve_local_runtime == 0 && local_postgres_running == 0 )) && [[ -n "$local_runtime_dir" && -d "$local_runtime_dir" ]]; then
    rm -rf -- "$local_runtime_dir"
  fi
  exit "$exit_status"
}
trap cleanup_local_resources EXIT

ensure_local_runtime_dir() {
  if [[ -n "$local_runtime_dir" ]]; then
    return
  fi
  mkdir -p "$evaluator_runtime_parent"
  local_runtime_dir=$(mktemp -d "$evaluator_runtime_parent/run.XXXXXX")
}

sql_configured_count=0
[[ -n "${REHAB_EVAL_SQL_ADMIN_URL:-}" ]] && sql_configured_count=$((sql_configured_count + 1))
[[ -n "${REHAB_EVAL_SQL_NAMESPACE:-}" ]] && sql_configured_count=$((sql_configured_count + 1))
[[ -n "${REHAB_EVAL_APPLICATION_DATABASE_NAME:-}" ]] && sql_configured_count=$((sql_configured_count + 1))
[[ -n "${REHAB_EVAL_CHECKPOINT_DATABASE_NAME:-}" ]] && sql_configured_count=$((sql_configured_count + 1))
if (( sql_configured_count != 0 && sql_configured_count != 4 )); then
  echo separate-evaluator-SQL-configuration-is-incomplete >&2
  exit 2
fi
if (( sql_configured_count == 0 )); then
  ensure_local_runtime_dir
  local_postgres_data_dir="$local_runtime_dir/postgres"
  local_pg_candidate=${REHAB_EVAL_POSTGRES_BIN_DIR:-}
  if [[ -n "$local_pg_candidate" && -x "$local_pg_candidate/initdb" && -x "$local_pg_candidate/pg_ctl" ]]; then
    local_postgres_bin_dir="$local_pg_candidate"
  fi
  if [[ -z "$local_postgres_bin_dir" ]]; then
    local_pg_candidate=$(command -v initdb 2>/dev/null || true)
    if [[ -n "$local_pg_candidate" && -x "${local_pg_candidate%/*}/pg_ctl" ]]; then
      local_postgres_bin_dir="${local_pg_candidate%/*}"
    fi
  fi
  if [[ -z "$local_postgres_bin_dir" ]]; then
    for local_pg_exe in /proc/[0-9]*/exe; do
      local_pg_target=$(readlink -f "$local_pg_exe" 2>/dev/null || true)
      if [[ "${local_pg_target##*/}" == "postgres" && -x "${local_pg_target%/*}/initdb" && -x "${local_pg_target%/*}/pg_ctl" ]]; then
        local_postgres_bin_dir="${local_pg_target%/*}"
        break
      fi
    done
  fi
  if [[ -z "$local_postgres_bin_dir" ]]; then
    echo evaluator-local-PostgreSQL-initdb-and-pg_ctl-are-required >&2
    exit 2
  fi
  local_postgres_port=""
  for candidate_port in $(seq 55432 55482); do
    if command -v ss >/dev/null 2>&1; then
      if ss -ltn 2>/dev/null | grep -Eq "[:.]${candidate_port}[[:space:]]"; then
        continue
      fi
    elif [[ -x "$local_postgres_bin_dir/pg_isready" ]] && "$local_postgres_bin_dir/pg_isready" -h 127.0.0.1 -p "$candidate_port" >/dev/null 2>&1; then
      continue
    fi
    local_postgres_port="$candidate_port"
    break
  done
  if [[ -z "$local_postgres_port" ]]; then
    echo evaluator-local-PostgreSQL-port-range-exhausted >&2
    exit 2
  fi
  "$local_postgres_bin_dir/initdb" -D "$local_postgres_data_dir" -U rehab_eval_admin --auth=trust --no-locale --encoding=UTF8 >"$local_runtime_dir/postgres-init.log" 2>&1
  "$local_postgres_bin_dir/pg_ctl" -D "$local_postgres_data_dir" -o "-p $local_postgres_port -h 127.0.0.1" -l "$local_runtime_dir/postgres.log" -w start >/dev/null
  local_postgres_started=1
  REHAB_EVAL_MANAGED_LOCAL_RUNTIME_DIR="$local_runtime_dir"
  REHAB_EVAL_LOCAL_POSTGRES_MANAGED=1
  REHAB_EVAL_SQL_ADMIN_URL="postgresql://rehab_eval_admin@127.0.0.1:$local_postgres_port/postgres"
  REHAB_EVAL_SQL_NAMESPACE="rehab_eval_run"
  REHAB_EVAL_APPLICATION_DATABASE_NAME="rehab_eval_run_app"
  REHAB_EVAL_CHECKPOINT_DATABASE_NAME="rehab_eval_run_checkpoint"
  REHAB_EVAL_SQL_SERVER_ALLOWLIST="127.0.0.1"
  REHAB_EVAL_SQL_OWNER_ROLE="rehab_eval_admin"
  export REHAB_EVAL_MANAGED_LOCAL_RUNTIME_DIR REHAB_EVAL_LOCAL_POSTGRES_MANAGED REHAB_EVAL_SQL_ADMIN_URL REHAB_EVAL_SQL_NAMESPACE REHAB_EVAL_APPLICATION_DATABASE_NAME REHAB_EVAL_CHECKPOINT_DATABASE_NAME REHAB_EVAL_SQL_SERVER_ALLOWLIST REHAB_EVAL_SQL_OWNER_ROLE
fi

if [[ -n "${REHAB_EVAL_QDRANT_URL:-}" && -n "${REHAB_EVAL_QDRANT_PATH:-}" ]]; then
  echo evaluator-Qdrant-URL-and-path-are-mutually-exclusive >&2
  exit 2
fi
if [[ -z "${REHAB_EVAL_QDRANT_URL:-}" && -z "${REHAB_EVAL_QDRANT_PATH:-}" ]]; then
  ensure_local_runtime_dir
  REHAB_EVAL_QDRANT_PATH="$local_runtime_dir/qdrant"
  mkdir -p "$REHAB_EVAL_QDRANT_PATH"
  REHAB_EVAL_QDRANT_OWNER_IDENTITY="local-evaluator"
  export REHAB_EVAL_QDRANT_PATH REHAB_EVAL_QDRANT_OWNER_IDENTITY
fi
if [[ -z "${REHAB_EVAL_CATALOG_DIR:-}" ]]; then
  ensure_local_runtime_dir
  REHAB_EVAL_CATALOG_DIR="$local_runtime_dir/catalog"
  mkdir -p "$REHAB_EVAL_CATALOG_DIR"
  export REHAB_EVAL_CATALOG_DIR
elif [[ ! -d "$REHAB_EVAL_CATALOG_DIR" ]]; then
  mkdir -p "$REHAB_EVAL_CATALOG_DIR"
fi
if [[ -z "${REHAB_EVAL_QDRANT_OWNER_IDENTITY:-}" && -z "${REHAB_EVAL_QDRANT_ALLOWLIST:-}" ]]; then
  echo evaluator-Qdrant-ownership-or-explicit-allowlist-is-required >&2
  exit 2
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Missing backend interpreter: $python_bin" >&2
  exit 1
fi
if [[ -z "${REHAB_EVAL_SCHEMA_REVISION:-}" ]]; then
  mapfile -t schema_heads < <(
    "$python_bin" -m alembic -c "$repo_dir/alembic.ini" heads 2>/dev/null |
      awk '$2 == "(head)" { print $1 }'
  )
  if (( ${#schema_heads[@]} != 1 )); then
    echo evaluator-Alembic-revision-could-not-be-derived-unambiguously >&2
    exit 2
  fi
  REHAB_EVAL_SCHEMA_REVISION="${schema_heads[0]}"
  export REHAB_EVAL_SCHEMA_REVISION
fi
runner_args+=(--backend-revision "${REHAB_EVAL_BACKEND_REVISION:-}")
runner_args+=(--backend-dir "$backend_dir" --python-bin "$python_bin" --application-schema-revision "$REHAB_EVAL_SCHEMA_REVISION" --embedding-provider "${REHAB_EVAL_EMBEDDING_PROVIDER:-hash}" --backend-generation-provider "$backend_generation_provider" --qdrant-owner-identity "${REHAB_EVAL_QDRANT_OWNER_IDENTITY:-}" --qdrant-allowlist "${REHAB_EVAL_QDRANT_ALLOWLIST:-}")
if [[ -n "${REHAB_EVAL_QDRANT_PATH:-}" ]]; then
  runner_args+=(--qdrant-path "$REHAB_EVAL_QDRANT_PATH")
fi
export REHAB_REAL_MODEL_ENABLED="${REHAB_REAL_MODEL_ENABLED:-1}"
export REHAB_EVAL_OPENAI_TIMEOUT_SECONDS="${REHAB_EVAL_OPENAI_TIMEOUT_SECONDS:-120}"
export OPENAI_API_KEY OPENAI_JUDGE_API_KEY OPENAI_API_BASE OPENAI_JUDGE_API_BASE
cd "$backend_dir"
max_restarts="${REHAB_EVAL_MAX_RESTARTS:-200}"
if ! [[ "$max_restarts" =~ ^[0-9]+$ ]]; then
  echo evaluator-max-restarts-must-be-a-nonnegative-integer >&2
  exit 2
fi
restart_count=0
while :; do
  set +e
  (
    exec "$python_bin" -m evals.combined.run "${runner_args[@]}"
  )
  evaluator_exit_code=$?
  set -e
  if (( evaluator_exit_code == 137 && restart_count < max_restarts )); then
    restart_count=$((restart_count + 1))
    echo "evaluator-restarting-after-exit-137 attempt=${restart_count}/${max_restarts}" >&2
    sleep 1
    continue
  fi
  if (( evaluator_exit_code == 0 )) && [[ "${REHAB_EVAL_KEEP_TEST_DATA:-1}" == "1" || "${REHAB_EVAL_KEEP_TEST_DATA:-1}" == "true" || "${REHAB_EVAL_KEEP_TEST_DATA:-1}" == "yes" ]]; then
    preserve_local_runtime=1
  fi
  exit "$evaluator_exit_code"
done
