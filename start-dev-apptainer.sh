#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

log() {
	printf '[rehabflow] %s\n' "$*" >&2
}

die() {
	log "ERROR: $*"
	exit 1
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
	cat <<'EOF'
Usage: ./start-dev-apptainer.sh

Start the local RehabFlow development stack without Docker. PostgreSQL,
Redis, and Qdrant run through Apptainer; FastAPI and Product run from the
checked-out Python and Node dependencies.

Useful overrides:
  REHABFLOW_RUNTIME_DIR=/path/to/runtime
  REHABFLOW_BACKEND_PORT=8000
  REHABFLOW_PRODUCT_PORT=3000
  NODE_BIN=/path/to/node

The launcher stays in the foreground. Press Ctrl-C to stop processes that it
started. Processes already serving a healthy endpoint are reused.
EOF
	exit 0
fi

[[ $# -eq 0 ]] || die "unknown argument: $1 (use --help for usage)"
[[ -f "$repo_root/.env" ]] || die "missing .env; copy .env.example to .env and configure provider credentials"
command -v apptainer >/dev/null 2>&1 || die "Apptainer is required"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v nc >/dev/null 2>&1 || die "netcat (nc) is required"

backend_dir="$repo_root/backend"
backend_python="${BACKEND_PYTHON:-$backend_dir/.venv/bin/python}"
[[ -x "$backend_python" ]] || die "backend Python not found at $backend_python"
[[ -x "$repo_root/node_modules/.bin/next" ]] || die "frontend dependencies not found; run npm ci from the repository root"

if [[ -n "${NODE_BIN:-}" ]]; then
	node_bin="$NODE_BIN"
elif command -v node >/dev/null 2>&1; then
	node_bin="$(command -v node)"
else
	node_bin=""
	for node_root in \
		"${HOME:-}/.vscode-server/cli/servers" \
		"${HOME:-}/.vscode-server/bin" \
		"${HOME:-}/.cursor-server"; do
		[[ -d "$node_root" ]] || continue
		node_bin="$(find "$node_root" -type f -path '*/server/node' -perm -111 -print -quit 2>/dev/null || true)"
		[[ -n "$node_bin" ]] && break
	done
fi
[[ -x "$node_bin" ]] || die "Node.js 20+ was not found; set NODE_BIN=/path/to/node"

if [[ -n "${REHABFLOW_RUNTIME_DIR:-}" ]]; then
	runtime_dir="$REHABFLOW_RUNTIME_DIR"
else
	runtime_dir="$repo_root/.rehabflow-runtime"
fi

image_dir="$runtime_dir/images"
cache_dir="$runtime_dir/cache"
log_dir="$runtime_dir/logs"
data_dir="$runtime_dir/data"
pid_dir="$runtime_dir/pids"
mkdir -p "$image_dir" "$cache_dir" "$log_dir" "$pid_dir" \
	"$data_dir/postgres" "$data_dir/postgres-run" "$data_dir/redis" \
	"$data_dir/qdrant" "$data_dir/qdrant-snapshots"

postgres_port="${REHABFLOW_POSTGRES_PORT:-5432}"
redis_port="${REHABFLOW_REDIS_PORT:-6379}"
qdrant_port="${REHABFLOW_QDRANT_PORT:-6333}"
qdrant_grpc_port="${REHABFLOW_QDRANT_GRPC_PORT:-6334}"
backend_port="${REHABFLOW_BACKEND_PORT:-${BACKEND_PORT:-8001}}"
product_port="${REHABFLOW_PRODUCT_PORT:-${PRODUCT_FRONTEND_PORT:-3000}}"

db_user="${REHABFLOW_POSTGRES_USER:-rehab}"
db_password="${REHABFLOW_POSTGRES_PASSWORD:-rehab}"
db_name="${REHABFLOW_POSTGRES_DB:-rehab_agent}"
environment="${REHABFLOW_ENVIRONMENT:-development}"
frontend_origin="${REHABFLOW_FRONTEND_ORIGIN:-http://localhost:${product_port}}"
allow_destructive="${REHAB_ALLOW_DESTRUCTIVE_MEMORY_MIGRATION:-1}"

[[ "$db_user" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "REHABFLOW_POSTGRES_USER must be a simple SQL identifier"
[[ "$db_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "REHABFLOW_POSTGRES_DB must be a simple SQL identifier"

postgres_image="$image_dir/postgres-16.sif"
redis_image="$image_dir/redis-7-alpine.sif"
qdrant_image="$image_dir/qdrant-latest.sif"

pull_image() {
	local destination="$1"
	local source="$2"
	if [[ -f "$destination" ]]; then
		return
	fi
	log "Pulling $source into $destination"
	APPTAINER_CACHEDIR="$cache_dir" apptainer pull \
		--dir "$image_dir" "$(basename "$destination")" "$source"
}

pull_image "$postgres_image" "${REHABFLOW_POSTGRES_IMAGE:-docker://library/postgres:16}"
pull_image "$redis_image" "${REHABFLOW_REDIS_IMAGE:-docker://library/redis:7-alpine}"
pull_image "$qdrant_image" "${REHABFLOW_QDRANT_IMAGE:-docker://qdrant/qdrant:latest}"

port_open() {
	nc -z -w 2 127.0.0.1 "$1" >/dev/null 2>&1
}

wait_for_port() {
	local port="$1"
	local description="$2"
	local timeout_seconds="${3:-60}"
	for ((second = 0; second < timeout_seconds; second++)); do
		port_open "$port" && return 0
		sleep 1
	done
	die "$description did not open port $port; inspect $log_dir"
}

wait_for_http() {
	local url="$1"
	local description="$2"
	local timeout_seconds="${3:-60}"
	for ((second = 0; second < timeout_seconds; second++)); do
		if curl --noproxy '*' --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
			return 0
		fi
		sleep 1
	done
	die "$description did not become healthy at $url; inspect $log_dir"
}

started_pids=()

cleanup() {
	local status=$?
	trap - EXIT INT TERM
	if ((${#started_pids[@]} > 0)); then
		log "Stopping processes started by this launcher"
		for pid in "${started_pids[@]}"; do
			kill "$pid" 2>/dev/null || true
		done
	fi
	exit "$status"
}

trap cleanup EXIT INT TERM

start_process() {
	local name="$1"
	shift
	local log_file="$log_dir/$name.log"
	log "Starting $name; log: $log_file"
	nohup "$@" >>"$log_file" 2>&1 < /dev/null &
	local pid=$!
	started_pids+=("$pid")
	printf '%s\n' "$pid" > "$pid_dir/$name.pid"
}

postgres_exec() {
	apptainer exec --env "PGPASSWORD=$db_password" "$postgres_image" "$@"
}

if postgres_exec pg_isready -h 127.0.0.1 -p "$postgres_port" -U "$db_user" >/dev/null 2>&1; then
	log "Reusing healthy PostgreSQL on $postgres_port"
elif port_open "$postgres_port"; then
	die "port $postgres_port is occupied but is not a usable PostgreSQL service"
else
	start_process postgres apptainer exec \
		--bind "$data_dir/postgres:/var/lib/postgresql/data" \
		--bind "$data_dir/postgres-run:/var/run/postgresql" \
		--env "POSTGRES_USER=$db_user" \
		--env "POSTGRES_PASSWORD=$db_password" \
		--env "POSTGRES_DB=$db_name" \
		"$postgres_image" docker-entrypoint.sh postgres \
		-c "listen_addresses=127.0.0.1" -p "$postgres_port"
	wait_for_port "$postgres_port" PostgreSQL
fi

if ! postgres_exec psql -h 127.0.0.1 -p "$postgres_port" -U "$db_user" -d postgres \
		-Atqc "SELECT 1 FROM pg_database WHERE datname = '$db_name'" | grep -qx '1'; then
	log "Creating PostgreSQL database $db_name"
	postgres_exec createdb -h 127.0.0.1 -p "$postgres_port" -U "$db_user" -O "$db_user" "$db_name"
fi

if redis_response="$(apptainer exec "$redis_image" redis-cli -h 127.0.0.1 -p "$redis_port" ping 2>/dev/null || true)" && [[ "$redis_response" == PONG ]]; then
	log "Reusing healthy Redis on $redis_port"
elif port_open "$redis_port"; then
	die "port $redis_port is occupied but is not a usable Redis service"
else
	start_process redis apptainer exec \
		--bind "$data_dir/redis:/data" \
		"$redis_image" docker-entrypoint.sh redis-server \
		--appendonly yes --dir /data --bind 127.0.0.1 --port "$redis_port"
	wait_for_port "$redis_port" Redis
fi

qdrant_url="http://127.0.0.1:${qdrant_port}"
if curl --noproxy '*' --fail --silent --max-time 5 "$qdrant_url/healthz" >/dev/null 2>&1; then
	log "Reusing healthy Qdrant on $qdrant_port"
elif port_open "$qdrant_port"; then
	die "port $qdrant_port is occupied but is not a usable Qdrant service"
else
	start_process qdrant apptainer exec \
		--pwd /qdrant \
		--bind "$data_dir/qdrant:/qdrant/storage" \
		--bind "$data_dir/qdrant-snapshots:/qdrant/snapshots" \
		--env "QDRANT__SERVICE__HOST=127.0.0.1" \
		--env "QDRANT__SERVICE__HTTP_PORT=$qdrant_port" \
		--env "QDRANT__SERVICE__GRPC_PORT=$qdrant_grpc_port" \
		--env "QDRANT__STORAGE__SNAPSHOTS_PATH=/qdrant/snapshots" \
		"$qdrant_image" /qdrant/entrypoint.sh
	wait_for_http "$qdrant_url/healthz" Qdrant
fi

database_url="postgresql+psycopg2://${db_user}:${db_password}@127.0.0.1:${postgres_port}/${db_name}"
checkpoint_database_url="postgresql://${db_user}:${db_password}@127.0.0.1:${postgres_port}/${db_name}"
backend_env=(
	"ENVIRONMENT=$environment"
	"BACKEND_PORT=$backend_port"
	"FRONTEND_ORIGIN=$frontend_origin"
	"DATABASE_URL=$database_url"
	"POSTGRES_URL=$database_url"
	"CHECKPOINT_DATABASE_URL=$checkpoint_database_url"
	"REDIS_URL=redis://127.0.0.1:${redis_port}/0"
	"QDRANT_URL=$qdrant_url"
	"QDRANT_API_KEY="
	"REHAB_ALLOW_DESTRUCTIVE_MEMORY_MIGRATION=$allow_destructive"
)

log "Applying database migrations"
env "${backend_env[@]}" "$backend_dir/.venv/bin/alembic" -c "$repo_root/alembic.ini" upgrade head
log "Initializing LangGraph checkpoint tables"
(cd "$backend_dir" && env "${backend_env[@]}" "$backend_python" scripts/setup_langgraph_checkpoints.py)

backend_url="http://127.0.0.1:${backend_port}"
if curl --noproxy '*' --fail --silent --max-time 5 "$backend_url/health" >/dev/null 2>&1; then
	log "Reusing healthy FastAPI on $backend_port"
elif port_open "$backend_port"; then
	die "port $backend_port is occupied but is not the RehabFlow API"
else
	start_process backend env "${backend_env[@]}" "PYTHONPATH=$backend_dir" \
		"$backend_python" -m uvicorn app.main:app --host 127.0.0.1 --port "$backend_port"
	wait_for_http "$backend_url/health" FastAPI
fi

node_path="$(dirname "$node_bin"):$PATH"
product_env=(
	"PATH=$node_path"
	"BACKEND_PORT=$backend_port"
	"NEXT_PUBLIC_BACKEND_PORT=$backend_port"
	"REHAB_BACKEND_BASE=$backend_url"
	"NEXT_PUBLIC_API_BASE=$backend_url"
	"FRONTEND_ORIGIN=$frontend_origin"
)
product_url="http://127.0.0.1:${product_port}"
if curl --noproxy '*' --fail --silent --max-time 5 "$product_url/" >/dev/null 2>&1; then
	log "Reusing healthy Product on $product_port"
elif port_open "$product_port"; then
	die "port $product_port is occupied but is not the RehabFlow Product app"
else
	start_process product env "${product_env[@]}" DEV_NEXT_REPO_ROOT="$repo_root" \
		bash -c 'cd "$1" && shift && exec "$@"' bash "$repo_root/apps/product" \
		"$node_bin" "$repo_root/scripts/dev-next.mjs" "$product_port"
	wait_for_http "$product_url/" Product
fi

log "RehabFlow is running"
log "Product: $product_url"
log "API: $backend_url/health"
log "Runtime data and logs: $runtime_dir"
while :; do
	sleep 3600
done
