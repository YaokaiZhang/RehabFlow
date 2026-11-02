#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

action="${1:-start}"

log() {
  printf '[rehabflow] %s\n' "$*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./start-dev.sh [start|restart|shutdown|stop]

Start the local RehabFlow development stack with Docker Compose. PostgreSQL,
Redis, Qdrant, FastAPI, and Product run as Compose services.

Commands:
  start       Start or reuse the stack (the default)
  restart     Stop and recreate the stack, preserving named volumes
  shutdown    Stop the stack while preserving containers and volumes
  stop        Alias for shutdown

Useful overrides:
  BACKEND_PORT=8000
  PRODUCT_FRONTEND_PORT=3000
  REHABFLOW_POSTGRES_PORT=5432
  REHABFLOW_REDIS_PORT=6379
  REHABFLOW_QDRANT_PORT=6333
  REHABFLOW_LANGFUSE=1      Also start self-hosted Langfuse and send LLM traces to it
  LANGFUSE_PORT=3100        Langfuse UI port (with REHABFLOW_LANGFUSE=1)
EOF
}

if [[ "$action" == "--help" || "$action" == "-h" ]]; then
  usage
  exit 0
fi

[[ $# -le 1 ]] || die "expected at most one command (use --help for usage)"
case "$action" in
  start|restart|shutdown|stop) ;;
  *) die "unknown command: $action (use --help for usage)" ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v docker >/dev/null 2>&1 || die "Docker is required. Install Docker Engine/Desktop or enable rootless Docker."

docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then
  command -v sudo >/dev/null 2>&1 || die "Docker is unavailable and sudo is not installed. Enable a rootless Docker context."
  sudo -v
  docker_cmd=(sudo docker)
fi

compose() {
  "${docker_cmd[@]}" compose "$@"
}

compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' command)."

dotenv_value() {
  local name="$1"
  awk -v name="$name" '
    /^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/ {
      line = $0
      sub(/^[[:space:]]*(export[[:space:]]+)?/, "", line)
      key = line
      sub(/[[:space:]]*=.*/, "", key)
      gsub(/[[:space:]]/, "", key)
      if (key != name) next
      sub(/^[^=]*=/, "", line)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
      if (line ~ /^\".*\"$/) line = substr(line, 2, length(line) - 2)
      value = line
    }
    END { if (value != "") print value }
  ' .env 2>/dev/null || true
}

resolve_env_value() {
  local name="$1"
  local default_value="$2"
  local process_value="${!name:-}"
  local file_value=""
  if [[ -f .env ]]; then
    file_value="$(dotenv_value "$name")"
  fi
  printf '%s\n' "${process_value:-${file_value:-$default_value}}"
}

backend_port=""
product_port=""
postgres_port=""
redis_port=""
qdrant_port=""
qdrant_grpc_port=""
if [[ "$action" == "start" || "$action" == "restart" ]]; then
  [[ -f .env ]] || die "missing .env; copy .env.example to .env and configure provider credentials"
  backend_port="${REHABFLOW_BACKEND_PORT:-$(resolve_env_value BACKEND_PORT 8000)}"
  product_port="${REHABFLOW_PRODUCT_PORT:-$(resolve_env_value PRODUCT_FRONTEND_PORT 3000)}"
  postgres_port="${REHABFLOW_POSTGRES_PORT:-$(resolve_env_value REHABFLOW_POSTGRES_PORT 5432)}"
  redis_port="${REHABFLOW_REDIS_PORT:-$(resolve_env_value REHABFLOW_REDIS_PORT 6379)}"
  qdrant_port="${REHABFLOW_QDRANT_PORT:-$(resolve_env_value REHABFLOW_QDRANT_PORT 6333)}"
  qdrant_grpc_port="${REHABFLOW_QDRANT_GRPC_PORT:-$(resolve_env_value REHABFLOW_QDRANT_GRPC_PORT 6334)}"
  export BACKEND_PORT="$backend_port"
  export PRODUCT_FRONTEND_PORT="$product_port"
  export REHABFLOW_POSTGRES_PORT="$postgres_port"
  export REHABFLOW_REDIS_PORT="$redis_port"
  export REHABFLOW_QDRANT_PORT="$qdrant_port"
  export REHABFLOW_QDRANT_GRPC_PORT="$qdrant_grpc_port"
fi

compose_args=(--project-directory "$repo_root" -f "$repo_root/docker-compose.yml")

langfuse_enabled=0
langfuse_port=""
case "$(resolve_env_value REHABFLOW_LANGFUSE 0 | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) langfuse_enabled=1 ;;
esac
if [[ "$action" == "shutdown" || "$action" == "stop" ]]; then
  # Always include the overlay so Langfuse containers stop too if they ran.
  compose_args+=(-f "$repo_root/docker-compose.langfuse.yml")
elif (( langfuse_enabled )); then
  compose_args+=(-f "$repo_root/docker-compose.langfuse.yml")
  langfuse_port="$(resolve_env_value LANGFUSE_PORT 3100)"
  export LANGFUSE_PORT="$langfuse_port"
fi

validate_port() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 65535 )) || die "$name must be a TCP port between 1 and 65535"
}

if [[ "$action" == "shutdown" || "$action" == "stop" ]]; then
  compose "${compose_args[@]}" stop
  exit 0
fi

validate_port BACKEND_PORT "$backend_port"
validate_port PRODUCT_FRONTEND_PORT "$product_port"
validate_port REHABFLOW_POSTGRES_PORT "$postgres_port"
validate_port REHABFLOW_REDIS_PORT "$redis_port"
validate_port REHABFLOW_QDRANT_PORT "$qdrant_port"
validate_port REHABFLOW_QDRANT_GRPC_PORT "$qdrant_grpc_port"
if (( langfuse_enabled )); then
  validate_port LANGFUSE_PORT "$langfuse_port"
fi

if [[ -z "${IMAGE_REGISTRY:-}" ]] && ! curl -sS --max-time 5 https://registry-1.docker.io/v2/ >/dev/null 2>&1; then
  export IMAGE_REGISTRY="docker.m.daocloud.io"
  log "Docker Hub is unreachable; using $IMAGE_REGISTRY."
fi

compose "${compose_args[@]}" config >/dev/null || die "Docker Compose configuration is invalid"

if [[ "$action" == "restart" ]]; then
  "$0" shutdown
  exec "$0" start
fi

stopping=0
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if (( stopping == 0 )); then
    stopping=1
    log "Stopping Docker Compose services"
    compose "${compose_args[@]}" stop >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

wait_for_http() {
  local url="$1"
  local description="$2"
  local timeout_seconds="${3:-120}"
  local second
  for ((second = 0; second < timeout_seconds; second++)); do
    if curl --noproxy '*' --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  die "$description did not become healthy at $url; inspect Docker Compose logs"
}

compose "${compose_args[@]}" up -d postgres redis qdrant
wait_for_http "http://127.0.0.1:${qdrant_port}/healthz" Qdrant
if (( langfuse_enabled )); then
  log "Starting self-hosted Langfuse (first start pulls images and runs migrations)"
  compose "${compose_args[@]}" up -d langfuse-web langfuse-worker
  wait_for_http "http://127.0.0.1:${langfuse_port}/api/public/health" Langfuse 300
fi
compose "${compose_args[@]}" up --build -d backend product
wait_for_http "http://127.0.0.1:${backend_port}/health" FastAPI
wait_for_http "http://127.0.0.1:${product_port}/" Product

log "RehabFlow is running"
log "Product: http://127.0.0.1:${product_port}"
log "API: http://127.0.0.1:${backend_port}/health"
log "Qdrant: http://127.0.0.1:${qdrant_port}/healthz"
if (( langfuse_enabled )); then
  log "Langfuse: http://127.0.0.1:${langfuse_port} (dev login: dev@rehabflow.local / rehabflow-dev unless overridden)"
fi
log "Use './start-dev.sh shutdown' to stop containers while preserving volumes"

compose "${compose_args[@]}" logs --follow --tail=0
