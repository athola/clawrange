#!/usr/bin/env bash
# scripts/tests/research.sh — smoke test for the multi-source
# research orchestrator. Calls /healthz/research, /research, and
# /research/sessions and reports per-channel readiness.
#
# Usage: ./scripts/tests/research.sh ["topic"]
# Exits non-zero on any HTTP error so it can be wired into CI.

set -euo pipefail
cd "$(dirname "$0")/../.."

# Load .env first (ports, PROXY_AUTH_TOKEN, ZAI/Reddit/GitHub creds) so the
# URL below honours WORKFLOWS_PORT from .env.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

WORKFLOWS_PORT="${WORKFLOWS_PORT:-5678}"
HOST="${WORKFLOWS_HOST:-localhost}"
BASE="http://${HOST}:${WORKFLOWS_PORT}"
TOPIC="${1:-claude code plugins shipping this week}"
CHANNEL_TOTAL=5   # discourse, code, discourse_web, academic, triz

HAVE_JQ=false
if command -v jq >/dev/null 2>&1; then
    HAVE_JQ=true
fi

# /research and /research/sessions require the bearer token.
# shellcheck source=scripts/workflows_auth.sh
. ./scripts/workflows_auth.sh
workflows_auth_init || exit 1

echo "── /healthz/research ──"
HEALTH=$(curl -sf --connect-timeout 5 --max-time 15 "${BASE}/healthz/research" || echo "")
if [ -z "${HEALTH}" ]; then
    echo "FAIL: workflows service unreachable at ${BASE}"
    echo "Hint: run 'make start' or set WORKFLOWS_HOST/WORKFLOWS_PORT"
    exit 1
fi

if [ "$HAVE_JQ" = true ]; then
    echo "${HEALTH}" | jq .
    HEALTHY_COUNT=$(echo "${HEALTH}" | jq -r '.configured_count // 0')
else
    echo "${HEALTH}"
    HEALTHY_COUNT=$(echo "${HEALTH}" | grep -oE '"configured_count":[0-9]+' \
        | head -1 | grep -oE '[0-9]+' || echo "0")
fi

echo
echo "Configured channels: ${HEALTHY_COUNT}/${CHANNEL_TOTAL}"

if [ "${HEALTHY_COUNT}" = "0" ]; then
    echo "FAIL: no research channels configured"
    echo "Set at least one of: REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD,"
    echo "GITHUB_PAT, ZAI_API_KEY"
    exit 1
fi

# Build the request body with a JSON encoder, not string interpolation, so
# quotes or backslashes in the topic cannot break the payload.
BODY=$(python3 -c 'import json,sys; print(json.dumps({"topic": sys.argv[1], "limit": 5}))' "${TOPIC}")

echo
echo "── POST /research (topic: ${TOPIC}) ──"
RES=$(curl -sf --connect-timeout 30 --max-time 180 -X POST \
    -H @"$WF_AUTH_HEADER" \
    -H 'content-type: application/json' \
    -d "${BODY}" \
    "${BASE}/research" || echo "")

if [ -z "${RES}" ]; then
    echo "FAIL: /research returned empty/error"
    exit 1
fi

if [ "$HAVE_JQ" = true ]; then
    SESSION_ID=$(echo "${RES}" | jq -r '.session_id')
    TOTAL=$(echo "${RES}" | jq -r '.total')
    ERRORS=$(echo "${RES}" | jq -r '(.errors // {}) | keys | join(",")')
    echo "session_id: ${SESSION_ID}"
    echo "total findings: ${TOTAL}"
    echo "errors: ${ERRORS:-none}"
    echo
    echo "Top 3 findings:"
    echo "${RES}" | jq -r '(.findings // [])[:3][] |
        "  [\(.confidence)] \(.title)\n    -> \(.url)"'
else
    echo "${RES}" | head -c 500
    echo
fi

echo
echo "── GET /research/sessions ──"
SESSIONS=$(curl -sf --connect-timeout 5 --max-time 30 -H @"$WF_AUTH_HEADER" \
    "${BASE}/research/sessions?limit=3" || echo "")
if [ -z "${SESSIONS}" ]; then
    echo "FAIL: /research/sessions returned empty/error"
    exit 1
fi
if [ "$HAVE_JQ" = true ]; then
    echo "${SESSIONS}" | jq .
else
    echo "${SESSIONS}"
fi

echo
echo "All research smoke checks passed."
