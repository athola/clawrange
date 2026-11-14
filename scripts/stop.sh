#!/usr/bin/env bash
# Stop the AI MSP Testbed stack.
# Usage: ./scripts/stop.sh
# Data is bind-mounted under data/, so there are no named volumes to remove.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Stopping core stack (OpenClaw + workflows)..."
docker compose down 2>/dev/null || true

# Stop DeerFlow if running
if [ -d deer-flow ] && [ -f deer-flow/docker/docker-compose.yaml ]; then
  echo "Stopping DeerFlow..."
  (cd deer-flow && COMPOSE_FILE=docker/docker-compose.yaml docker compose down 2>/dev/null || true)
fi

echo "All services stopped."
