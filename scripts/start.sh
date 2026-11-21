#!/usr/bin/env bash
# Start the AI MSP Testbed stack.
# Usage: ./scripts/start.sh [--with-deerflow] [--skip-core]
set -euo pipefail
cd "$(dirname "$0")/.."

# DeerFlow is cloned at a pinned commit (tags are mutable). v2.2.0.
# Override with DEERFLOW_REF=<sha|branch|tag> to test another revision.
DEERFLOW_REF="${DEERFLOW_REF:-58fc5bd5b8e6ea46538e9481ba06ca2a46e14e2a}"
DEERFLOW_REPO="https://github.com/bytedance/deer-flow.git"

usage() {
  echo "Usage: ./scripts/start.sh [--with-deerflow] [--skip-core]"
  echo "  --with-deerflow Also start DeerFlow research layer"
  echo "  --skip-core     Do not (re)start OpenClaw + workflows; use when the"
  echo "                  core stack was already started (e.g. make start-prod-full)"
  echo "  --ensure-soul   Only run the pre-flight checks and render a missing"
  echo "                  openclaw/soul.md, then exit (make start-prod runs this"
  echo "                  before its own compose up)"
}

WITH_DEERFLOW=false
SKIP_CORE=false
ENSURE_SOUL_ONLY=false

for arg in "$@"; do
  case "$arg" in
    --with-deerflow) WITH_DEERFLOW=true ;;
    --skip-core) SKIP_CORE=true ;;
    --ensure-soul) ENSURE_SOUL_ONLY=true ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      usage >&2
      exit 2
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

# Load .env so ports (OPENCLAW_PORT, WORKFLOWS_PORT, DEERFLOW_PORT) and keys
# used below match what docker compose sees.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# Check for required API key (catch empty and placeholder values)
OPENROUTER_VAL="${OPENROUTER_API_KEY:-}"
if [ -z "$OPENROUTER_VAL" ] || echo "$OPENROUTER_VAL" | grep -qi "your-key\|example\|placeholder"; then
  echo "WARNING: OPENROUTER_API_KEY is empty or still a placeholder in .env — LLM calls will fail."
  echo "  Get a key at https://openrouter.ai/settings/keys"
fi

# The workflows API fails closed: without a real token every protected
# route, and the OpenClaw-to-proxy chat path, answers 401. The API also
# rejects the .env.example placeholder (workflows/auth.py).
if [ -z "${PROXY_AUTH_TOKEN:-}" ] || [ "${PROXY_AUTH_TOKEN}" = "generate-a-token-here" ]; then
  echo "ERROR: PROXY_AUTH_TOKEN is empty or the .env.example placeholder."
  echo "  Generate one and put it in .env, e.g.:"
  echo "  echo \"PROXY_AUTH_TOKEN=\$(openssl rand -hex 32)\" >> .env"
  exit 1
fi

# ─── Persona (openclaw/soul.md) ────────────────────────────────────
# soul.md is rendered per operator and gitignored, so a fresh clone has
# none. Docker would then create the bind-mount source as an empty
# root-owned directory, and OpenClaw would start with no persona.
ensure_soul() {
  local soul="openclaw/soul.md"
  local profile="${CLAWRANGE_PROFILE:-starter}"
  if [ -d "$soul" ]; then
    echo "ERROR: $soul is a directory, not a file. Docker created it when the"
    echo "  stack started before the persona was rendered. Remove it and retry:"
    echo "  sudo rmdir $soul && make persona PROFILE=$profile"
    exit 1
  fi
  [ -f "$soul" ] && return 0
  if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: $soul is missing and uv is not installed to render it."
    echo "  Install uv, then run: make persona PROFILE=$profile"
    exit 1
  fi
  echo "Rendering $soul from profile '$profile' (first run)..."
  if ! make persona PROFILE="$profile" || [ ! -f "$soul" ]; then
    echo "ERROR: could not render $soul from profile '$profile'."
    echo "  Fix the profile (or CLAWRANGE_PROFILE in .env), then run:"
    echo "  make persona PROFILE=$profile"
    exit 1
  fi
}

if [ "$ENSURE_SOUL_ONLY" = true ]; then
  ensure_soul
  exit 0
fi

# ─── Start OpenClaw + workflows ────────────────────────────────────
if [ "$SKIP_CORE" = true ]; then
  echo "Skipping core stack bring-up (--skip-core)."
else
  ensure_soul
  echo "Starting core stack (OpenClaw + workflows)..."
  docker compose up -d
fi

# ─── Optionally start DeerFlow ─────────────────────────────────────
if [ "$WITH_DEERFLOW" = true ]; then
  if [ ! -f deer-flow/docker/docker-compose.yaml ]; then
    # Clone into a temp dir and move into place only on success, so a failed
    # fetch never leaves a half-initialised deer-flow/ that later runs skip.
    echo "Cloning DeerFlow at ${DEERFLOW_REF}..."
    rm -rf deer-flow.tmp
    git init -q deer-flow.tmp
    git -C deer-flow.tmp remote add origin "$DEERFLOW_REPO"
    git -C deer-flow.tmp fetch -q --depth 1 origin "$DEERFLOW_REF"
    git -C deer-flow.tmp checkout -q FETCH_HEAD
    rm -rf deer-flow
    mv deer-flow.tmp deer-flow
  fi

  # Copy our OpenRouter config into DeerFlow, substituting the API key
  # literally (python str.replace: safe for |, &, \ and other sed specials).
  # The key travels via the environment, never argv, so it stays out of ps.
  # Secrets files are created under umask 077 and forced to 600 on reruns.
  (
    umask 077
    OPENROUTER_API_KEY="$OPENROUTER_VAL" python3 - deerflow/config.yaml deer-flow/config.yaml <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    text = f.read()
with open(dst, "w") as f:
    f.write(text.replace("$OPENROUTER_API_KEY", os.environ["OPENROUTER_API_KEY"]))
PY
    # deploy.sh passes this file to compose as --env-file. BIND_HOST stays
    # loopback: never 0.0.0.0 (LAN-reachable under WSL mirrored networking).
    cat > deer-flow/.env <<EOF
OPENAI_API_KEY=${OPENROUTER_VAL}
OPENAI_API_BASE=https://openrouter.ai/api/v1
PORT=${DEERFLOW_PORT:-2026}
BIND_HOST=127.0.0.1
EOF
    chmod 600 deer-flow/config.yaml deer-flow/.env
  )

  echo "Starting DeerFlow (upstream scripts/deploy.sh: build + start)..."
  # Upstream's deploy script sets DEER_FLOW_HOME, the config paths and the
  # generated auth secrets its compose file needs; raw `docker compose up`
  # fails with "invalid spec: :/app/backend/config.yaml:ro".
  (cd deer-flow && ./scripts/deploy.sh)

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
  OPENCLAW_OK=$(curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${OPENCLAW_PORT:-3000}/healthz" >/dev/null 2>&1 && echo "1" || echo "0")
  WORKFLOWS_OK=$(curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${WORKFLOWS_PORT:-5678}/healthz" >/dev/null 2>&1 && echo "1" || echo "0")

  DEERFLOW_OK="1"
  if [ "$WITH_DEERFLOW" = true ]; then
    DEERFLOW_OK=$(curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${DEERFLOW_PORT:-2026}/health" >/dev/null 2>&1 && echo "1" || echo "0")
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
    echo "Run ./scripts/tests/all.sh to validate the stack."
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
