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

usage() {
	cat <<'EOF'
Usage: ./start-dev-apptainer.sh [start|restart|shutdown] [port options]

Start the local RehabFlow development stack without Docker. PostgreSQL,
Redis, and Qdrant run through Apptainer; FastAPI and Product run from the
checked-out Python and Node dependencies.

Commands:
  start       Start the stack (the default)
  restart     Shut down the stack, then start it again
  shutdown    Stop the launcher and the RehabFlow services (alias: stop)

Port options (ports are configured here only; .env port values are ignored):
  --product-port N      Product web app            (default 3000)
  --backend-port N      FastAPI                    (default 8001)
  --postgres-port N     PostgreSQL                 (default 5432)
  --redis-port N        Redis                      (default 6379)
  --qdrant-port N       Qdrant HTTP                (default 6333)
  --qdrant-grpc-port N  Qdrant gRPC                (default 6334)

shutdown and restart reuse the ports of the last start unless a port option
is given.

Other overrides (environment):
  REHABFLOW_RUNTIME_DIR=/path/to/runtime
  NODE_BIN=/path/to/node
  BACKEND_PYTHON=/path/to/python

The launcher stays in the foreground. Press Ctrl-C to stop processes that it
started. A service already running on its chosen port is reused by start.

Examples:
  ./start-dev-apptainer.sh --product-port 3005 --backend-port 8005
  ./start-dev-apptainer.sh restart --product-port 3006
  ./start-dev-apptainer.sh shutdown
EOF
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

services=(postgres redis qdrant backend product)
declare -A default_port=(
	[postgres]=5432
	[redis]=6379
	[qdrant]=6333
	[qdrant_grpc]=6334
	[backend]=8001
	[product]=3000
)
declare -A port_flag=(
	[--postgres-port]=postgres
	[--redis-port]=redis
	[--qdrant-port]=qdrant
	[--qdrant-grpc-port]=qdrant_grpc
	[--backend-port]=backend
	[--product-port]=product
)
declare -A cli_port=()
action=""

while (($# > 0)); do
	arg="$1"
	shift
	case "$arg" in
		-h|--help)
			usage
			exit 0
			;;
		--*=*)
			flag="${arg%%=*}"
			[[ -n "${port_flag[$flag]:-}" ]] || die "unknown option: $flag (use --help for usage)"
			cli_port["${port_flag[$flag]}"]="${arg#*=}"
			;;
		--*)
			[[ -n "${port_flag[$arg]:-}" ]] || die "unknown option: $arg (use --help for usage)"
			(($# > 0)) || die "$arg needs a port number"
			cli_port["${port_flag[$arg]}"]="$1"
			shift
			;;
		start|restart|shutdown|stop)
			[[ -z "$action" ]] || die "expected at most one command (use --help for usage)"
			action="$arg"
			;;
		*)
			die "unknown command: $arg (use --help for usage)"
			;;
	esac
done
action="${action:-start}"
[[ "$action" == "stop" ]] && action="shutdown"

runtime_dir="${REHABFLOW_RUNTIME_DIR:-$repo_root/.rehabflow-runtime}"
[[ -n "$runtime_dir" ]] || die "REHABFLOW_RUNTIME_DIR must not be empty"
image_dir="$runtime_dir/images"
cache_dir="$runtime_dir/cache"
log_dir="$runtime_dir/logs"
data_dir="$runtime_dir/data"
pid_dir="$runtime_dir/pids"
ports_file="$pid_dir/ports"

# Ports come from the command-line options, then (for shutdown and restart)
# from the last start, then from the defaults. Nothing is read from .env.
declare -A saved_port=()
if [[ "$action" != "start" && -r "$ports_file" ]]; then
	while IFS='=' read -r name value; do
		[[ -n "${default_port[$name]:-}" && "$value" =~ ^[0-9]+$ ]] && saved_port["$name"]="$value"
	done < "$ports_file"
fi

declare -A port=()
for name in "${!default_port[@]}"; do
	value="${cli_port[$name]:-${saved_port[$name]:-${default_port[$name]}}}"
	[[ "$value" =~ ^[0-9]+$ ]] && ((value >= 1 && value <= 65535)) ||
		die "--${name//_/-}-port must be a TCP port between 1 and 65535 (got '$value')"
	port["$name"]="$value"
done

declare -A port_owner=()
for name in "${!port[@]}"; do
	value="${port[$name]}"
	[[ -z "${port_owner[$value]:-}" ]] ||
		die "port $value is assigned to both ${port_owner[$value]} and $name"
	port_owner["$value"]="$name"
done

# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

pid_exists() {
	[[ "${1:-}" =~ ^[0-9]+$ ]] && kill -0 "$1" 2>/dev/null
}

pid_args() {
	ps -o args= -p "$1" 2>/dev/null | sed -e 's/^[[:space:]]*//'
}

pid_parent() {
	ps -o ppid= -p "$1" 2>/dev/null | awk '{print $1}'
}

can_list_listeners() {
	command -v ss >/dev/null 2>&1 || command -v lsof >/dev/null 2>&1
}

listener_pids() {
	local listen_port="$1"
	if command -v ss >/dev/null 2>&1; then
		ss -ltnp 2>/dev/null |
			awk -v port="$listen_port" '$4 ~ (":" port "$") {print}' |
			grep -oE 'pid=[0-9]+' |
			cut -d= -f2 |
			sort -nu || true
	elif command -v lsof >/dev/null 2>&1; then
		lsof -nP -t -iTCP:"$listen_port" -sTCP:LISTEN 2>/dev/null | sort -nu || true
	fi
}

# Does this process belong to the given service?
service_matches_pid() {
	local service="$1"
	local args
	pid_exists "$2" || return 1
	args="$(pid_args "$2")"
	case "$service" in
		postgres) [[ "$args" == *postgres* ]] ;;
		redis) [[ "$args" == *redis* ]] ;;
		qdrant) [[ "$args" == *qdrant* ]] ;;
		backend) [[ "$args" == *uvicorn* && "$args" == *"app.main:app"* ]] ;;
		product) [[ "$args" == *"dev-next.mjs"* || "$args" == *"next dev"* || "$args" == *"next-server"* ]] ;;
		*) return 1 ;;
	esac
}

# Is this process the top of the service's process tree?
service_is_root_pid() {
	local service="$1"
	local args
	args="$(pid_args "$2")"
	case "$service" in
		postgres|redis|qdrant) [[ "$args" == *"Apptainer runtime parent:"*"$service"* ]] ;;
		backend) [[ "$args" == *uvicorn* && "$args" == *"app.main:app"* ]] ;;
		product) [[ "$args" == *"dev-next.mjs"* ]] ;;
		*) return 1 ;;
	esac
}

service_root_pid() {
	local service="$1"
	local current_pid="$2"
	local parent_pid depth
	for ((depth = 0; depth < 32; depth++)); do
		pid_exists "$current_pid" || break
		if service_is_root_pid "$service" "$current_pid"; then
			printf '%s\n' "$current_pid"
			return 0
		fi
		parent_pid="$(pid_parent "$current_pid")"
		[[ "$parent_pid" =~ ^[0-9]+$ && "$parent_pid" -gt 1 && "$parent_pid" != "$current_pid" ]] || break
		current_pid="$parent_pid"
	done
	printf '%s\n' "$2"
}

service_pid() {
	local service="$1"
	local listen_port="$2"
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
	done < <(listener_pids "$listen_port")
	return 1
}

collect_descendants() {
	local child_pid
	while read -r child_pid; do
		[[ "$child_pid" =~ ^[0-9]+$ ]] || continue
		printf '%s\n' "$child_pid"
		collect_descendants "$child_pid"
	done < <(ps -eo pid=,ppid= 2>/dev/null | awk -v parent="$1" '$2 == parent {print $1}')
}

stop_process_tree() {
	local root_pid="$1"
	local -a descendants=()
	local pid index second running
	pid_exists "$root_pid" || return 0
	mapfile -t descendants < <(collect_descendants "$root_pid")
	for ((index = ${#descendants[@]} - 1; index >= 0; index--)); do
		kill -TERM "${descendants[$index]}" 2>/dev/null || true
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
		((running == 0)) && return 0
		sleep 0.25
	done

	log "Graceful stop timed out for PID $root_pid; sending SIGKILL to its process tree"
	for pid in "${descendants[@]}" "$root_pid"; do
		kill -KILL "$pid" 2>/dev/null || true
	done
}

running_launcher_pid() {
	local pid
	[[ -r "$pid_dir/launcher.pid" ]] || return 1
	pid="$(<"$pid_dir/launcher.pid")"
	[[ "$pid" =~ ^[0-9]+$ && "$pid" != "$$" ]] || return 1
	pid_exists "$pid" || return 1
	[[ "$(pid_args "$pid")" == *"start-dev-apptainer.sh"* ]] || return 1
	printf '%s\n' "$pid"
}

remove_pid_files() {
	local name
	for name in "$@"; do
		rm -f -- "${pid_dir:?}/${name:?}.pid"
	done
}

# ---------------------------------------------------------------------------
# shutdown / restart
# ---------------------------------------------------------------------------

shutdown_stack() {
	can_list_listeners || die "shutdown requires ss or lsof to discover running services"

	declare -A targets=()
	local service pid launcher_pid
	for service in "${services[@]}"; do
		if pid="$(service_pid "$service" "${port[$service]}")"; then
			targets["$pid"]="${targets[$pid]-}$service "
		fi
	done
	launcher_pid="$(running_launcher_pid || true)"

	if ((${#targets[@]} == 0)) && [[ -z "$launcher_pid" ]]; then
		log "No running RehabFlow processes found"
	fi
	for pid in "${!targets[@]}"; do
		[[ "$pid" == "$launcher_pid" ]] && continue
		log "Stopping ${targets[$pid]}(PID $pid)"
		stop_process_tree "$pid"
	done
	if [[ -n "$launcher_pid" ]]; then
		log "Stopping launcher (PID $launcher_pid)"
		stop_process_tree "$launcher_pid"
	fi
	remove_pid_files launcher "${services[@]}"
}

if [[ "$action" == "shutdown" ]]; then
	shutdown_stack
	exit 0
fi

if [[ "$action" == "restart" ]]; then
	shutdown_stack
	restart_args=()
	for name in "${!port[@]}"; do
		restart_args+=("--${name//_/-}-port" "${port[$name]}")
	done
	exec "$0" start "${restart_args[@]}"
fi

# ---------------------------------------------------------------------------
# start: prerequisites
# ---------------------------------------------------------------------------

[[ -f "$repo_root/.env" ]] || die "missing .env; copy .env.example to .env and configure provider credentials"
command -v apptainer >/dev/null 2>&1 || die "Apptainer is required"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v nc >/dev/null 2>&1 || die "netcat (nc) is required"

backend_dir="$repo_root/backend"
backend_python="${BACKEND_PYTHON:-$backend_dir/.venv/bin/python}"
[[ -x "$backend_python" ]] || die "backend Python not found at $backend_python"
[[ -x "$repo_root/node_modules/.bin/next" ]] || die "frontend dependencies not found; run npm ci from the repository root"

find_node() {
	local node_root candidate
	if [[ -n "${NODE_BIN:-}" ]]; then
		printf '%s\n' "$NODE_BIN"
		return
	fi
	if command -v node >/dev/null 2>&1; then
		command -v node
		return
	fi
	for node_root in \
		"${HOME:-}/.vscode-server/cli/servers" \
		"${HOME:-}/.vscode-server/bin" \
		"${HOME:-}/.cursor-server"; do
		[[ -d "$node_root" ]] || continue
		candidate="$(find "$node_root" -type f -path '*/server/node' -perm -111 -print -quit 2>/dev/null || true)"
		if [[ -n "$candidate" ]]; then
			printf '%s\n' "$candidate"
			return
		fi
	done
}
node_bin="$(find_node)"
[[ -n "$node_bin" && -x "$node_bin" ]] || die "Node.js 20+ was not found; set NODE_BIN=/path/to/node"

mkdir -p "$image_dir" "$cache_dir" "$log_dir" "$pid_dir" \
	"$data_dir/postgres" "$data_dir/postgres-run" "$data_dir/redis" \
	"$data_dir/qdrant" "$data_dir/qdrant-snapshots"

if existing_launcher="$(running_launcher_pid)"; then
	die "launcher already running as PID $existing_launcher; use './start-dev-apptainer.sh restart'"
fi
printf '%s\n' "$$" > "$pid_dir/launcher.pid"
for name in "${!port[@]}"; do
	printf '%s=%s\n' "$name" "${port[$name]}"
done | sort > "$ports_file"

db_user="${REHABFLOW_POSTGRES_USER:-rehab}"
db_password="${REHABFLOW_POSTGRES_PASSWORD:-rehab}"
db_name="${REHABFLOW_POSTGRES_DB:-rehab_agent}"
environment="${REHABFLOW_ENVIRONMENT:-development}"
allow_destructive="${REHAB_ALLOW_DESTRUCTIVE_MEMORY_MIGRATION:-1}"
[[ "$db_user" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "REHABFLOW_POSTGRES_USER must be a simple SQL identifier"
[[ "$db_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "REHABFLOW_POSTGRES_DB must be a simple SQL identifier"

postgres_image="$image_dir/postgres-16.sif"
redis_image="$image_dir/redis-7-alpine.sif"
qdrant_image="$image_dir/qdrant-latest.sif"

pull_image() {
	local destination="$1"
	local source="$2"
	[[ -f "$destination" ]] && return
	log "Pulling $source into $destination"
	APPTAINER_CACHEDIR="$cache_dir" apptainer pull \
		--dir "$image_dir" "$(basename "$destination")" "$source"
}

pull_image "$postgres_image" "${REHABFLOW_POSTGRES_IMAGE:-docker://library/postgres:16}"
pull_image "$redis_image" "${REHABFLOW_REDIS_IMAGE:-docker://library/redis:7-alpine}"
pull_image "$qdrant_image" "${REHABFLOW_QDRANT_IMAGE:-docker://qdrant/qdrant:latest}"

# ---------------------------------------------------------------------------
# start: service helpers
# ---------------------------------------------------------------------------

port_open() {
	nc -z -w 2 127.0.0.1 "$1" >/dev/null 2>&1
}

http_ok() {
	curl --noproxy '*' --fail --silent --max-time 5 "$1" >/dev/null 2>&1
}

wait_until() {
	local description="$1"
	local timeout_seconds="$2"
	local second
	shift 2
	for ((second = 0; second < timeout_seconds; second++)); do
		"$@" && return 0
		sleep 1
	done
	die "$description did not become ready in ${timeout_seconds}s; inspect $log_dir"
}

postgres_exec() {
	apptainer exec --env "PGPASSWORD=$db_password" "$postgres_image" "$@"
}

postgres_ready() {
	postgres_exec pg_isready -h 127.0.0.1 -p "${port[postgres]}" -U "$db_user" >/dev/null 2>&1
}

redis_ready() {
	[[ "$(apptainer exec "$redis_image" redis-cli -h 127.0.0.1 -p "${port[redis]}" ping 2>/dev/null || true)" == PONG ]]
}

started_pids=()

cleanup() {
	local status=$?
	local pid
	trap - EXIT INT TERM
	if ((${#started_pids[@]} > 0)); then
		log "Stopping processes started by this launcher"
		for pid in "${started_pids[@]}"; do
			stop_process_tree "$pid"
		done
	fi
	remove_pid_files launcher "${services[@]}"
	exit "$status"
}
trap cleanup EXIT INT TERM

start_process() {
	local name="$1"
	shift
	local log_file="$log_dir/$name.log"
	log "Starting $name on port ${port[$name]}; log: $log_file"
	nohup "$@" >>"$log_file" 2>&1 < /dev/null &
	started_pids+=("$!")
	printf '%s\n' "$!" > "$pid_dir/$name.pid"
}

# Returns 0 when a healthy instance of the service already owns its port and
# can be reused, 1 when the port is free, and exits when something else
# occupies it.
reuse_existing() {
	local service="$1"
	local label="$2"
	local healthy="$3"
	local listen_port="${port[$service]}"
	if ((healthy)); then
		if ! can_list_listeners || service_pid "$service" "$listen_port" >/dev/null; then
			log "Reusing healthy $label on $listen_port"
			return 0
		fi
		die "port $listen_port answers but is not RehabFlow's $label; pick another with --${service}-port"
	fi
	port_open "$listen_port" &&
		die "port $listen_port is occupied by something other than $label; pick another with --${service}-port"
	return 1
}

# ---------------------------------------------------------------------------
# start: services
# ---------------------------------------------------------------------------

healthy=0; postgres_ready && healthy=1
if ! reuse_existing postgres PostgreSQL "$healthy"; then
	start_process postgres apptainer exec \
		--bind "$data_dir/postgres:/var/lib/postgresql/data" \
		--bind "$data_dir/postgres-run:/var/run/postgresql" \
		--env "POSTGRES_USER=$db_user" \
		--env "POSTGRES_PASSWORD=$db_password" \
		--env "POSTGRES_DB=$db_name" \
		"$postgres_image" docker-entrypoint.sh postgres \
		-c "listen_addresses=127.0.0.1" -p "${port[postgres]}"
	wait_until PostgreSQL 120 postgres_ready
fi

if ! postgres_exec psql -h 127.0.0.1 -p "${port[postgres]}" -U "$db_user" -d postgres \
		-Atqc "SELECT 1 FROM pg_database WHERE datname = '$db_name'" | grep -qx '1'; then
	log "Creating PostgreSQL database $db_name"
	postgres_exec createdb -h 127.0.0.1 -p "${port[postgres]}" -U "$db_user" -O "$db_user" "$db_name"
fi

healthy=0; redis_ready && healthy=1
if ! reuse_existing redis Redis "$healthy"; then
	start_process redis apptainer exec \
		--bind "$data_dir/redis:/data" \
		"$redis_image" docker-entrypoint.sh redis-server \
		--appendonly yes --dir /data --bind 127.0.0.1 --port "${port[redis]}"
	wait_until Redis 60 redis_ready
fi

qdrant_url="http://127.0.0.1:${port[qdrant]}"
healthy=0; http_ok "$qdrant_url/healthz" && healthy=1
if ! reuse_existing qdrant Qdrant "$healthy"; then
	port_open "${port[qdrant_grpc]}" &&
		die "port ${port[qdrant_grpc]} is occupied; pick another with --qdrant-grpc-port"
	start_process qdrant apptainer exec \
		--pwd /qdrant \
		--bind "$data_dir/qdrant:/qdrant/storage" \
		--bind "$data_dir/qdrant-snapshots:/qdrant/snapshots" \
		--env "QDRANT__SERVICE__HOST=127.0.0.1" \
		--env "QDRANT__SERVICE__HTTP_PORT=${port[qdrant]}" \
		--env "QDRANT__SERVICE__GRPC_PORT=${port[qdrant_grpc]}" \
		--env "QDRANT__STORAGE__SNAPSHOTS_PATH=/qdrant/snapshots" \
		"$qdrant_image" /qdrant/entrypoint.sh
	wait_until Qdrant 60 http_ok "$qdrant_url/healthz"
fi

backend_url="http://127.0.0.1:${port[backend]}"
product_url="http://127.0.0.1:${port[product]}"
frontend_origin="http://localhost:${port[product]}"
database_url="postgresql+psycopg2://${db_user}:${db_password}@127.0.0.1:${port[postgres]}/${db_name}"
checkpoint_database_url="postgresql://${db_user}:${db_password}@127.0.0.1:${port[postgres]}/${db_name}"

# Process environment takes precedence over .env in both the backend settings
# and the Next.js port resolver, so every port and URL derived from the options
# above is handed to the children explicitly.
backend_env=(
	"ENVIRONMENT=$environment"
	"BACKEND_PORT=${port[backend]}"
	"FRONTEND_ORIGIN=$frontend_origin"
	"DATABASE_URL=$database_url"
	"POSTGRES_URL=$database_url"
	"CHECKPOINT_DATABASE_URL=$checkpoint_database_url"
	"REDIS_URL=redis://127.0.0.1:${port[redis]}/0"
	"QDRANT_URL=$qdrant_url"
	"QDRANT_API_KEY="
	"REHAB_ALLOW_DESTRUCTIVE_MEMORY_MIGRATION=$allow_destructive"
)

log "Applying database migrations"
env "${backend_env[@]}" "$backend_dir/.venv/bin/alembic" -c "$repo_root/alembic.ini" upgrade head
log "Initializing LangGraph checkpoint tables"
(cd "$backend_dir" && env "${backend_env[@]}" "$backend_python" scripts/setup_langgraph_checkpoints.py)

healthy=0; http_ok "$backend_url/health" && healthy=1
if ! reuse_existing backend FastAPI "$healthy"; then
	start_process backend env "${backend_env[@]}" "PYTHONPATH=$backend_dir" \
		"$backend_python" -m uvicorn app.main:app --host 127.0.0.1 --port "${port[backend]}"
	wait_until FastAPI 60 http_ok "$backend_url/health"
fi

product_env=(
	"PATH=$(dirname "$node_bin"):$PATH"
	"DEV_NEXT_REPO_ROOT=$repo_root"
	"FRONTEND_PORT=${port[product]}"
	"PRODUCT_FRONTEND_PORT=${port[product]}"
	"BACKEND_PORT=${port[backend]}"
	"NEXT_PUBLIC_BACKEND_PORT=${port[backend]}"
	"REHAB_BACKEND_BASE=$backend_url"
	"NEXT_PUBLIC_API_BASE=$backend_url"
	"FRONTEND_ORIGIN=$frontend_origin"
)
healthy=0; http_ok "$product_url/" && healthy=1
if ! reuse_existing product Product "$healthy"; then
	start_process product env "${product_env[@]}" \
		bash -c 'cd "$1" && shift && exec "$@"' bash "$repo_root/apps/product" \
		"$node_bin" "$repo_root/scripts/dev-next.mjs" "${port[product]}"
	wait_until Product 120 http_ok "$product_url/"
fi

log "RehabFlow is running"
log "Product: $product_url"
log "API: $backend_url/health"
log "Runtime data and logs: $runtime_dir"
log "Stop with Ctrl-C or './start-dev-apptainer.sh shutdown'"
while :; do
	sleep 3600
done
