#!/usr/bin/env bash
# scripts/test_research.sh — smoke test for the multi-source
# research orchestrator. Calls /healthz/research, /research, and
# /research/sessions and reports per-channel readiness.
#
# Exits non-zero on any HTTP error so it can be wired into CI.

set -euo pipefail

WORKFLOWS_PORT="${WORKFLOWS_PORT:-5678}"
HOST="${WORKFLOWS_HOST:-localhost}"
BASE="http://${HOST}:${WORKFLOWS_PORT}"
TOPIC="${1:-claude code plugins shipping this week}"

# Load .env if present (for ZAI/Reddit/GitHub creds)
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

echo "── /healthz/research ──"
HEALTH=$(curl -sf --connect-timeout 5 "${BASE}/healthz/research" || echo "")
if [ -z "${HEALTH}" ]; then
    echo "FAIL: workflows service unreachable at ${BASE}"
    echo "Hint: run 'make start' or set WORKFLOWS_HOST/WORKFLOWS_PORT"
    exit 1
fi

if command -v jq >/dev/null 2>&1; then
    echo "${HEALTH}" | jq .
    HEALTHY_COUNT=$(echo "${HEALTH}" | jq -r '.configured_count')
else
    echo "${HEALTH}"
    HEALTHY_COUNT=$(echo "${HEALTH}" | grep -oE '"configured_count":[0-9]+' \
        | head -1 | grep -oE '[0-9]+' || echo "0")
fi

echo
echo "Configured channels: ${HEALTHY_COUNT}/3"

if [ "${HEALTHY_COUNT}" = "0" ]; then
    echo "FAIL: no research channels configured"
    echo "Set at least one of: REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD,"
    echo "GITHUB_PAT, ZAI_API_KEY"
    exit 1
fi

echo
echo "── POST /research (topic: ${TOPIC}) ──"
RES=$(curl -sf --connect-timeout 30 -X POST \
    -H 'content-type: application/json' \
    -d "{\"topic\": \"${TOPIC}\", \"limit\": 5}" \
    "${BASE}/research" || echo "")

if [ -z "${RES}" ]; then
    echo "FAIL: /research returned empty/error"
    exit 1
fi

if command -v jq >/dev/null 2>&1; then
    SESSION_ID=$(echo "${RES}" | jq -r '.session_id')
    TOTAL=$(echo "${RES}" | jq -r '.total')
    ERRORS=$(echo "${RES}" | jq -r '.errors | keys | join(",")')
    echo "session_id: ${SESSION_ID}"
    echo "total findings: ${TOTAL}"
    echo "errors: ${ERRORS:-none}"
    echo
    echo "Top 3 findings:"
    echo "${RES}" | jq -r '.findings[:3][] |
        "  [\(.confidence)] \(.title)\n    -> \(.url)"'
else
    echo "${RES}" | head -c 500
    echo
fi

echo
echo "── GET /research/sessions ──"
curl -sf --connect-timeout 5 "${BASE}/research/sessions?limit=3" \
    | { command -v jq >/dev/null 2>&1 && jq . || cat; }

echo
echo "All research smoke checks passed."
