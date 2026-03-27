#!/usr/bin/env bash
# Start the AI MSP Testbed stack.
# Usage: ./scripts/start.sh [--minimal] [--with-deerflow]
set -euo pipefail
cd "$(dirname "$0")/.."

MINIMAL=false
WITH_DEERFLOW=false

for arg in "$@"; do
  case "$arg" in
    --minimal)      MINIMAL=true ;;
    --with-deerflow) WITH_DEERFLOW=true ;;
    -h|--help)
      echo "Usage: ./scripts/start.sh [--minimal] [--with-deerflow]"
      echo "  --minimal       Start OpenClaw + n8n only"
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

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running. Start Docker Desktop or dockerd."
  exit 1
fi

if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example to .env and fill in your keys."
  echo "  cp .env.example .env"
  exit 1
fi

# Check for required API key
if grep -q '^OPENROUTER_API_KEY=$' .env 2>/dev/null; then
  echo "WARNING: OPENROUTER_API_KEY is empty in .env — LLM calls will fail."
  echo "  Get a key at https://openrouter.ai/settings/keys"
fi

# Generate n8n encryption key if missing
if grep -q '^N8N_ENCRYPTION_KEY=$' .env 2>/dev/null; then
  KEY=$(openssl rand -hex 32)
  sed -i "s/^N8N_ENCRYPTION_KEY=$/N8N_ENCRYPTION_KEY=${KEY}/" .env
  echo "Generated N8N_ENCRYPTION_KEY automatically."
fi

# ─── Start OpenClaw + n8n ──────────────────────────────────────────
if [ "$MINIMAL" = true ]; then
  echo "Starting minimal stack (OpenClaw + n8n)..."
  docker compose -f docker-compose.minimal.yml up -d
else
  echo "Starting core stack (OpenClaw + n8n)..."
  docker compose up -d
fi

# ─── Optionally start DeerFlow ─────────────────────────────────────
if [ "$WITH_DEERFLOW" = true ]; then
  if [ ! -d deer-flow ]; then
    echo "Cloning DeerFlow repository..."
    git clone --depth 1 https://github.com/bytedance/deer-flow.git deer-flow
  fi

  # Copy our OpenRouter config into DeerFlow
  cp deerflow/config.yaml deer-flow/config.yaml

  # Create DeerFlow .env from our .env
  OPENROUTER_KEY=$(grep '^OPENROUTER_API_KEY=' .env | cut -d= -f2)
  cat > deer-flow/.env <<EOF
OPENAI_API_KEY=${OPENROUTER_KEY}
OPENAI_API_BASE=https://openrouter.ai/api/v1
PORT=${DEERFLOW_PORT:-2026}
EOF

  echo "Starting DeerFlow..."
  cd deer-flow
  # Connect DeerFlow to our shared network
  COMPOSE_FILE=docker/docker-compose.yaml docker compose up -d
  cd ..

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
  OPENCLAW_OK=$(curl -sf http://localhost:${OPENCLAW_PORT:-3000}/healthz 2>/dev/null && echo "1" || echo "0")
  N8N_OK=$(curl -sf http://localhost:${N8N_PORT:-5678}/healthz 2>/dev/null && echo "1" || echo "0")

  if [ "$OPENCLAW_OK" = "1" ] && [ "$N8N_OK" = "1" ]; then
    echo ""
    echo "Stack is healthy!"
    echo "  OpenClaw: http://localhost:${OPENCLAW_PORT:-3000}"
    echo "  n8n:      http://localhost:${N8N_PORT:-5678}"
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
echo "WARNING: Services did not become healthy within 60 seconds."
echo "Check logs with: docker compose logs"
docker compose ps
