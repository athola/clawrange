#!/usr/bin/env bash
# Stop the AI MSP Testbed stack.
# Usage: ./scripts/stop.sh [--all]
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Stopping core stack (OpenClaw + n8n)..."
docker compose down 2>/dev/null || true

# Stop DeerFlow if running
if [ -d deer-flow ] && [ -f deer-flow/docker/docker-compose.yaml ]; then
  echo "Stopping DeerFlow..."
  (cd deer-flow && COMPOSE_FILE=docker/docker-compose.yaml docker compose down 2>/dev/null || true)
fi

echo "All services stopped."

if [ "${1:-}" = "--all" ]; then
  echo "Removing named volumes..."
  docker volume rm msp-n8n-data 2>/dev/null || true
  echo "Volumes removed."
fi
