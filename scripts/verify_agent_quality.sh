#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/verify_agent_quality.sh [--database-url URL] [--redis-url URL]

Runs the local, provider-free agent quality checks against already-running
local test services. It never starts or mutates production services.
EOF
}

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
backend_dir="$repo_dir/backend"
# Keep the local fallback aligned with database/setup.sh, which provisions the
# rehab role and rehab_agent_quality database. CI and callers can still override this
# with AGENT_QUALITY_DATABASE_URL, DATABASE_URL, or --database-url.
database_url="${AGENT_QUALITY_DATABASE_URL:-${DATABASE_URL:-postgresql+psycopg2://rehab:rehab@localhost:5432/rehab_agent_quality}}"
redis_url="${AGENT_QUALITY_REDIS_URL:-${REDIS_URL:-redis://localhost:6379/0}}"

while (($#)); do
  case "$1" in
    --database-url)
      (($# >= 2)) || { usage >&2; exit 2; }
      database_url=$2
      shift 2
      ;;
    --redis-url)
      (($# >= 2)) || { usage >&2; exit 2; }
      redis_url=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

checkpoint_url="${database_url/postgresql+psycopg2/postgresql}"
checkpoint_url="${checkpoint_url/postgresql+psycopg/postgresql}"

export ENVIRONMENT=test
export DATABASE_URL="$database_url"
export POSTGRES_URL="$database_url"
export CHECKPOINT_DATABASE_URL="${AGENT_QUALITY_CHECKPOINT_DATABASE_URL:-$checkpoint_url}"
export TEST_DATABASE_URL="${AGENT_QUALITY_TEST_DATABASE_URL:-$CHECKPOINT_DATABASE_URL}"
export REDIS_URL="$redis_url"
export JWT_SECRET_KEY="agent-quality-local-jwt-secret"
export REHAB_TRACE_HMAC_KEY="agent-quality-local-trace-key"
unset QWEN_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY || true

run() {
  printf '+ %s\n' "$*"
  "$@"
}

cd "$backend_dir"
python_bin="${AGENT_QUALITY_PYTHON:-.venv/bin/python}"
[[ -x "$python_bin" ]] || { echo "Missing backend interpreter: $python_bin" >&2; exit 1; }

run "$python_bin" -m alembic -c ../alembic.ini upgrade head
run "$python_bin" scripts/setup_langgraph_checkpoints.py
run env \
  -u CHECKPOINT_DATABASE_URL \
  -u TEST_DATABASE_URL \
  -u REDIS_URL \
  -u JWT_SECRET_KEY \
  -u REHAB_TRACE_HMAC_KEY \
  -u TRACE_HMAC_KEY \
  -u TRACE_HMAC_KEY_VERSION \
  -u QWEN_API_KEY \
  -u QWEN_API_BASE \
  -u QDRANT_URL \
  POSTGRES_URL= \
  CHECKPOINT_DATABASE_URL= \
  TEST_DATABASE_URL= \
  "$python_bin" -m pytest \
  tests/ai tests/api tests/contracts tests/core tests/models \
  tests/observability tests/services -q
run "$python_bin" -m pytest tests/agent_eval -q
run "$python_bin" scripts/audit_agent_log_privacy.py
run "$python_bin" scripts/audit_config_secrets.py .

cd "$repo_dir"
run npm ci
run npm run verify:frontend
run npm run test:console-ai-chat-contract
run npm run test:console-ai-chat-proxy-contract
run npm run test:console-ai-chat-retry-contract
run npm run test:product-patient-stream-ticket-contract
run npm run test:product-live-monitor-contract
run npm run test:console-stream-transport-contract
run npm run build:product
run npm run build:console

printf 'Agent quality checks passed.\n'
