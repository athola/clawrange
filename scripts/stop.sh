#!/usr/bin/env bash
# Stop the AI MSP Testbed stack.
# Usage: ./scripts/stop.sh [--all]
#   --all  Also remove compose volumes and orphan containers
#          (docker compose down -v --remove-orphans) for core and DeerFlow.
# Brain/CRM data is bind-mounted under data/ and is never removed here.
set -euo pipefail
cd "$(dirname "$0")/.."

REMOVE_ALL=false
for arg in "$@"; do
  case "$arg" in
    --all) REMOVE_ALL=true ;;
    -h|--help)
      echo "Usage: ./scripts/stop.sh [--all]"
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      echo "Usage: ./scripts/stop.sh [--all]" >&2
      exit 2
      ;;
  esac
done

compose_down() {
  if [ "$REMOVE_ALL" = true ]; then
    docker compose down -v --remove-orphans 2>/dev/null || true
  else
    docker compose down 2>/dev/null || true
  fi
}

if [ "$REMOVE_ALL" = true ]; then
  echo "Stopping core stack (OpenClaw + workflows) and removing volumes..."
else
  echo "Stopping core stack (OpenClaw + workflows)..."
fi
compose_down

# Stop DeerFlow if running
if [ -d deer-flow ] && [ -f deer-flow/docker/docker-compose.yaml ]; then
  echo "Stopping DeerFlow..."
  # Upstream deploy.sh started it and exports the paths its compose file
  # interpolates; a bare `docker compose down` fails with "invalid spec".
  (cd deer-flow && ./scripts/deploy.sh down) || true
  if [ "$REMOVE_ALL" = true ]; then
    docker volume ls -q --filter label=com.docker.compose.project=deer-flow \
      | xargs -r docker volume rm >/dev/null 2>&1 || true
  fi
fi

echo "All services stopped."
