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
Usage: ./start-dev-apptainer.sh [start|restart|shutdown]

Start the local RehabFlow development stack without Docker. PostgreSQL,
Redis, and Qdrant run through Apptainer; FastAPI and Product run from the
checked-out Python and Node dependencies.

Commands:
  start       Start the stack (the default)
  restart     Shut down the stack, then start it again
  shutdown    Stop the launcher and the RehabFlow services

Useful overrides:
  REHABFLOW_RUNTIME_DIR=/path/to/runtime
  REHABFLOW_BACKEND_PORT=8000
  REHABFLOW_PRODUCT_PORT=3000
  NODE_BIN=/path/to/node

The launcher stays in the foreground. Press Ctrl-C to stop processes that it
started. Processes already serving a healthy endpoint are reused by start.
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

postgres_port="${REHABFLOW_POSTGRES_PORT:-5432}"
redis_port="${REHABFLOW_REDIS_PORT:-6379}"
qdrant_port="${REHABFLOW_QDRANT_PORT:-6333}"
qdrant_grpc_port="${REHABFLOW_QDRANT_GRPC_PORT:-6334}"
backend_port="${REHABFLOW_BACKEND_PORT:-${BACKEND_PORT:-8001}}"
product_port="${REHABFLOW_PRODUCT_PORT:-${PRODUCT_FRONTEND_PORT:-3000}}"

pid_exists() {
	[[ "${1:-}" =~ ^[0-9]+$ ]] && kill -0 "$1" 2>/dev/null
}

pid_args() {
	ps -o args= -p "$1" 2>/dev/null | sed -e 's/^[[:space:]]*//'
}

pid_parent() {
	ps -o ppid= -p "$1" 2>/dev/null | awk '{print $1}'
}

listener_pids() {
	local port="$1"
	if command -v ss >/dev/null 2>&1; then
		ss -ltnp 2>/dev/null |
			awk -v port="$port" '$4 ~ (":" port "$") {print}' |
			grep -oE 'pid=[0-9]+' |
			cut -d= -f2 |
			sort -nu || true
	elif command -v lsof >/dev/null 2>&1; then
		lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -nu || true
	fi
}

service_matches_pid() {
	local service="$1"
	local pid="$2"
	local args
	pid_exists "$pid" || return 1
	args="$(pid_args "$pid")"
	case "$service" in
		postgres) [[ "$args" == *postgres* ]] ;;
		redis) [[ "$args" == *redis* ]] ;;
		qdrant) [[ "$args" == *qdrant* ]] ;;
		backend) [[ "$args" == *uvicorn* && "$args" == *"app.main:app"* ]] ;;
		product) [[ "$args" == *"dev-next.mjs"* || "$args" == *"next dev"* || "$args" == *"next-server"* ]] ;;
		*) return 1 ;;
	esac
}

service_root_pid() {
	local service="$1"
	local original_pid="$2"
	local current_pid="$2"
	local args
	local parent_pid

	for ((depth = 0; depth < 32; depth++)); do
		pid_exists "$current_pid" || break
		args="$(pid_args "$current_pid")"
		case "$service" in
			postgres)
				[[ "$args" == *"Apptainer runtime parent:"*postgres* ]] && {
					printf '%s\n' "$current_pid"
					return 0
				}
				;;
			redis)
				[[ "$args" == *"Apptainer runtime parent:"*redis* ]] && {
					printf '%s\n' "$current_pid"
					return 0
				}
				;;
			qdrant)
				[[ "$args" == *"Apptainer runtime parent:"*qdrant* ]] && {
					printf '%s\n' "$current_pid"
					return 0
				}
				;;
			backend)
				[[ "$args" == *uvicorn* && "$args" == *"app.main:app"* ]] && {
					printf '%s\n' "$current_pid"
					return 0
				}
				;;
			product)
				[[ "$args" == *"dev-next.mjs"* ]] && {
					printf '%s\n' "$current_pid"
					return 0
				}
				;;
		esac
		parent_pid="$(pid_parent "$current_pid")"
		[[ "$parent_pid" =~ ^[0-9]+$ && "$parent_pid" -gt 1 && "$parent_pid" != "$current_pid" ]] || break
		current_pid="$parent_pid"
	done

	printf '%s\n' "$original_pid"
}

service_pid() {
	local service="$1"
	local port="$2"
	local candidate

	if [[ -r "$pid_dir/$service.pid" ]]; then
		candidate="$(<"$pid_dir/$service.pid")"
		if service_matches_pid "$service" "$candidate"; then
			service_root_pid "$service" "$candidate"
			return 0
		fi
	fi

	while read -r candidate; do
		[[ -n "$candidate" ]] || continue
		if service_matches_pid "$service" "$candidate"; then
			service_root_pid "$service" "$candidate"
			return 0
		fi
	done < <(listener_pids "$port")
	return 1
}

collect_descendants() {
	local parent_pid="$1"
	local child_pid
	while read -r child_pid; do
		[[ "$child_pid" =~ ^[0-9]+$ ]] || continue
		printf '%s\n' "$child_pid"
		collect_descendants "$child_pid"
	done < <(ps -eo pid=,ppid= 2>/dev/null | awk -v parent="$parent_pid" '$2 == parent {print $1}')
}

stop_process_tree() {
	local root_pid="$1"
	local -a descendants=()
	local pid
	local index
	local running

	pid_exists "$root_pid" || return 0
	mapfile -t descendants < <(collect_descendants "$root_pid")
	for ((index = ${#descendants[@]} - 1; index >= 0; index--)); do
		pid="${descendants[$index]}"
		kill -TERM "$pid" 2>/dev/null || true
	done
	kill -TERM "$root_pid" 2>/dev/null || true

	for ((second = 0; second < 40; second++)); do
		running=0
		for pid in "${descendants[@]}" "$root_pid"; do
			if pid_exists "$pid"; then
				running=1
				break
			fi
		done
		if ((running == 0)); then
			return 0
		fi
		sleep 0.25
	done

	log "Graceful stop timed out for PID $root_pid; sending SIGKILL to its recorded process tree"
	for pid in "${descendants[@]}" "$root_pid"; do
		kill -KILL "$pid" 2>/dev/null || true
	done
}

shutdown_stack() {
	if ! command -v ss >/dev/null 2>&1 && ! command -v lsof >/dev/null 2>&1; then
		die "shutdown requires ss or lsof to discover reused services"
	fi

	declare -A target_names=()
	declare -A targets=()
	local -a services=(postgres redis qdrant backend product)
	local -a ports=("$postgres_port" "$redis_port" "$qdrant_port" "$backend_port" "$product_port")
	local service
	local pid
	local index
	local launcher_pid=""

	for index in "${!services[@]}"; do
		service="${services[$index]}"
		pid=""
		if pid="$(service_pid "$service" "${ports[$index]}")"; then
			targets["$pid"]=1
			target_names["$pid"]="${target_names[$pid]-}$service "
		fi
	done

	if [[ -r "$pid_dir/launcher.pid" ]]; then
		launcher_pid="$(<"$pid_dir/launcher.pid")"
		if [[ "$launcher_pid" =~ ^[0-9]+$ ]] && pid_exists "$launcher_pid"; then
			local launcher_args
			launcher_args="$(pid_args "$launcher_pid")"
			if [[ "$launcher_args" == *"start-dev-apptainer.sh"* ]]; then
				targets["$launcher_pid"]=1
				target_names["$launcher_pid"]="launcher"
			else
				launcher_pid=""
			fi
		else
			launcher_pid=""
		fi
	fi

	if ((${#targets[@]} == 0)); then
		log "No running RehabFlow processes found"
		return 0
	fi

	for pid in "${!targets[@]}"; do
		[[ "$pid" == "$launcher_pid" ]] && continue
		log "Stopping ${target_names[$pid]} (PID $pid)"
		stop_process_tree "$pid"
	done
	if [[ -n "$launcher_pid" ]]; then
		log "Stopping launcher (PID $launcher_pid)"
		stop_process_tree "$launcher_pid"
	fi

	for service in launcher "${services[@]}"; do
		rm -f "$pid_dir/$service.pid"
	done
}

if [[ "$action" == "shutdown" || "$action" == "stop" ]]; then
	shutdown_stack
	exit 0
fi

if [[ "$action" == "restart" ]]; then
	"$0" shutdown
	exec "$0" start
fi

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

mkdir -p "$image_dir" "$cache_dir" "$log_dir" "$pid_dir" \
	"$data_dir/postgres" "$data_dir/postgres-run" "$data_dir/redis" \
	"$data_dir/qdrant" "$data_dir/qdrant-snapshots"

claim_launcher_pid() {
	local existing_pid=""
	local existing_args=""
	if [[ -r "$pid_dir/launcher.pid" ]]; then
		existing_pid="$(<"$pid_dir/launcher.pid")"
		if [[ "$existing_pid" =~ ^[0-9]+$ ]] && [[ "$existing_pid" != "$$" ]] && pid_exists "$existing_pid"; then
			existing_args="$(pid_args "$existing_pid")"
			[[ "$existing_args" == *"start-dev-apptainer.sh"* ]] &&
				die "launcher already running as PID $existing_pid; use './start-dev-apptainer.sh restart'"
		fi
	fi
	printf '%s\n' "$$" > "$pid_dir/launcher.pid"
}

claim_launcher_pid

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

wait_for_postgres() {
	local port="$1"
	local description="$2"
	local timeout_seconds="${3:-120}"
	for ((second = 0; second < timeout_seconds; second++)); do
		if postgres_exec pg_isready -h 127.0.0.1 -p "$port" -U "$db_user" >/dev/null 2>&1; then
			return 0
		fi
		sleep 1
	done
	die "$description did not become ready on port $port; inspect $log_dir/postgres.log"
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
declare -A started_pid_by_name=()

cleanup() {
	local status=$?
	trap - EXIT INT TERM
	if ((${#started_pids[@]} > 0)); then
		log "Stopping processes started by this launcher"
		for pid in "${started_pids[@]}"; do
			stop_process_tree "$pid"
		done
	fi
	for name in postgres redis qdrant backend product; do
		rm -f "$pid_dir/$name.pid"
	done
	rm -f "$pid_dir/launcher.pid"
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
	started_pid_by_name["$name"]="$pid"
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
	wait_for_postgres "$postgres_port" PostgreSQL 120
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
