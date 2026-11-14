#!/usr/bin/env bash
# Start the AI MSP Testbed stack.
# Usage: ./scripts/start.sh [--with-deerflow]
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_DEERFLOW=false

for arg in "$@"; do
  case "$arg" in
    --with-deerflow) WITH_DEERFLOW=true ;;
    -h|--help)
      echo "Usage: ./scripts/start.sh [--with-deerflow]"
      echo "  --with-deerflow Also start DeerFlow research layer"
      exit 0
      ;;
  esac
done

# ─── Pre-flight checks ─────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed. Install Docker first."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: 'docker compose' plugin not found. Install Docker Compose V2."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running. Start Docker Desktop or dockerd."
  exit 1
fi

if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example to .env and fill in your keys."
  echo "  cp .env.example .env"
  exit 1
fi

# Check for required API key (catch empty and placeholder values)
OPENROUTER_VAL=$(grep '^OPENROUTER_API_KEY=' .env | cut -d= -f2- || true)
if [ -z "$OPENROUTER_VAL" ] || echo "$OPENROUTER_VAL" | grep -qi "your-key\|example\|placeholder"; then
  echo "WARNING: OPENROUTER_API_KEY is empty or still a placeholder in .env — LLM calls will fail."
  echo "  Get a key at https://openrouter.ai/settings/keys"
fi

# ─── Start OpenClaw + workflows ────────────────────────────────────
echo "Starting core stack (OpenClaw + workflows)..."
docker compose up -d

# ─── Optionally start DeerFlow ─────────────────────────────────────
if [ "$WITH_DEERFLOW" = true ]; then
  if [ ! -d deer-flow ]; then
    echo "Cloning DeerFlow repository..."
    git clone --depth 1 https://github.com/bytedance/deer-flow.git deer-flow
  fi

  # Copy our OpenRouter config into DeerFlow and replace API key placeholder
  OPENROUTER_KEY=$(grep '^OPENROUTER_API_KEY=' .env | cut -d= -f2- || true)
  cp deerflow/config.yaml deer-flow/config.yaml
  sed "s|\\\$OPENROUTER_API_KEY|${OPENROUTER_KEY}|g" deer-flow/config.yaml > deer-flow/config.yaml.tmp \
    && mv deer-flow/config.yaml.tmp deer-flow/config.yaml

  # Create DeerFlow .env from our .env
  cat > deer-flow/.env <<EOF
OPENAI_API_KEY=${OPENROUTER_KEY}
OPENAI_API_BASE=https://openrouter.ai/api/v1
PORT=${DEERFLOW_PORT:-2026}
EOF

  echo "Starting DeerFlow..."
  # Run DeerFlow compose in a subshell to avoid cd side effects
  (cd deer-flow && COMPOSE_FILE=docker/docker-compose.yaml docker compose up -d)

  # Connect DeerFlow containers to msp-network if not already
  for container in $(docker ps --filter "name=deer-flow" --format '{{.Names}}' 2>/dev/null); do
    docker network connect msp-network "$container" 2>/dev/null || true
  done
fi

# ─── Wait for health ───────────────────────────────────────────────
echo ""
echo "Waiting for services to become healthy..."
TRIES=0
MAX_TRIES=30

while [ $TRIES -lt $MAX_TRIES ]; do
  OPENCLAW_OK=$(curl -sf --connect-timeout 5 http://localhost:${OPENCLAW_PORT:-3000}/healthz 2>/dev/null && echo "1" || echo "0")
  WORKFLOWS_OK=$(curl -sf --connect-timeout 5 http://localhost:${WORKFLOWS_PORT:-5678}/healthz 2>/dev/null && echo "1" || echo "0")

  DEERFLOW_OK="1"
  if [ "$WITH_DEERFLOW" = true ]; then
    DEERFLOW_OK=$(curl -sf --connect-timeout 5 http://localhost:${DEERFLOW_PORT:-2026}/api/health 2>/dev/null && echo "1" || echo "0")
  fi

  if [ "$OPENCLAW_OK" = "1" ] && [ "$WORKFLOWS_OK" = "1" ] && [ "$DEERFLOW_OK" = "1" ]; then
    echo ""
    echo "Stack is healthy!"
    echo "  OpenClaw: http://localhost:${OPENCLAW_PORT:-3000}"
    echo "  Workflows: http://localhost:${WORKFLOWS_PORT:-5678}"
    if [ "$WITH_DEERFLOW" = true ]; then
      echo "  DeerFlow: http://localhost:${DEERFLOW_PORT:-2026}"
    fi
    echo ""
    echo "Run ./scripts/test_all.sh to validate the stack."
    exit 0
  fi

  TRIES=$((TRIES + 1))
  printf "."
  sleep 2
done

echo ""
echo "ERROR: Services did not become healthy within 60 seconds."
echo "Check logs with: docker compose logs"
docker compose ps
exit 1
