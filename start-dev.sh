#!/usr/bin/env bash
set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Add provider credentials, then rerun ./start-dev.sh." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Engine/Desktop or enable rootless Docker." >&2
  exit 127
fi

# Prefer the user's Docker/rootless context. If the daemon socket is readable
# only by root, use the user's configured sudo policy for this invocation.
docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then
  command -v sudo >/dev/null 2>&1 || { echo "Docker is unavailable and sudo is not installed. Enable a rootless Docker context." >&2; exit 1; }
  sudo -v
  docker_cmd=(sudo docker)
fi
"${docker_cmd[@]}" compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required (the 'docker compose' command)." >&2; exit 1; }

if [[ -z "${IMAGE_REGISTRY:-}" ]] && ! curl -sS --max-time 5 https://registry-1.docker.io/v2/ >/dev/null 2>&1; then
  export IMAGE_REGISTRY="docker.m.daocloud.io"
  echo "Docker Hub is unreachable; using $IMAGE_REGISTRY." >&2
fi

exec "${docker_cmd[@]}" compose up --build "$@"
