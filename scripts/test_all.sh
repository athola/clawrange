#!/usr/bin/env bash
# Run all validation tests and produce a pass/fail report.
# Usage: ./scripts/test_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Source .env for port configuration
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

OPENCLAW_PORT="${OPENCLAW_PORT:-3000}"
N8N_PORT="${N8N_PORT:-5678}"
DEERFLOW_PORT="${DEERFLOW_PORT:-2026}"

# POSIX-compatible results tracking (no arrays)
RESULTS=""
PASSED=0
TOTAL=0

pass() { RESULTS="${RESULTS}PASS "; PASSED=$((PASSED + 1)); TOTAL=$((TOTAL + 1)); echo "  PASS"; }
fail() { RESULTS="${RESULTS}FAIL "; TOTAL=$((TOTAL + 1)); echo "  FAIL"; }
skip() { RESULTS="${RESULTS}SKIP "; TOTAL=$((TOTAL + 1)); echo "  SKIP"; }

echo "============================================="
echo " AI MSP TESTBED — STACK VALIDATION"
echo "============================================="
echo ""

# ─── Test 1: Stack Health ───────────────────────────────────────────
echo "Test 1 — Stack Health"

T1_OK=true

printf "  OpenClaw (localhost:${OPENCLAW_PORT}): "
if curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${OPENCLAW_PORT}/healthz" >/dev/null 2>&1; then
  echo "UP"
else
  echo "DOWN"
  T1_OK=false
fi

printf "  n8n (localhost:${N8N_PORT}): "
if curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${N8N_PORT}/healthz" >/dev/null 2>&1; then
  echo "UP"
else
  echo "DOWN"
  T1_OK=false
fi

printf "  DeerFlow (localhost:${DEERFLOW_PORT}): "
if curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${DEERFLOW_PORT}/api/health" >/dev/null 2>&1; then
  echo "UP"
else
  echo "DOWN (optional)"
fi

if [ "$T1_OK" = true ]; then pass; else fail; fi
echo ""

# ─── Test 2: OpenClaw Response ──────────────────────────────────────
echo "Test 2 — OpenClaw Connectivity"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 30 -X POST "http://localhost:${OPENCLAW_PORT}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${OPENCLAW_GATEWAY_TOKEN:-testbed-token-change-me}" \
  -d '{"model": "openclaw:main", "messages": [{"role": "user", "content": "What financing options does Longview Home Center offer?"}]}' 2>/dev/null) || RESPONSE=""

echo "  Response: $(echo "$RESPONSE" | head -c 200)"
if echo "$RESPONSE" | grep -qiE "FHA|VA|conventional|in-house|financ"; then
  pass
else
  fail
  echo "  Expected mention of FHA, VA, conventional, or in-house financing."
  echo "  Check OPENROUTER_API_KEY in .env"
fi
echo ""

# ─── Test 3: n8n Webhook Roundtrip ─────────────────────────────────
echo "Test 3 — n8n Webhook Roundtrip"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 -X POST "http://localhost:${N8N_PORT}/webhook-test/test" \
  -H "Content-Type: application/json" \
  -d '{"message": "ping", "source": "openclaw-test"}' 2>/dev/null) || RESPONSE=""

echo "  Response: $(echo "$RESPONSE" | head -c 200)"
if echo "$RESPONSE" | grep -qi "received"; then
  pass
else
  fail
  echo "  Ensure the 'Test Webhook' workflow is activated in n8n UI."
fi
echo ""

# ─── Test 4: Lead Status Lookup ─────────────────────────────────────
echo "Test 4 — Lead Status Lookup"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 -X POST "http://localhost:${N8N_PORT}/webhook-test/lead-status" \
  -H "Content-Type: application/json" \
  -d '{"name": "John Smith", "phone": "903-555-0100"}' 2>/dev/null) || RESPONSE=""

echo "  Response: $(echo "$RESPONSE" | head -c 200)"
if echo "$RESPONSE" | grep -qi "John Smith"; then
  pass
else
  fail
  echo "  Ensure the 'Lead Status Lookup' workflow is activated in n8n UI."
fi
echo ""

# ─── Test 5: Morning Briefing ──────────────────────────────────────
echo "Test 5 — Morning Briefing (workflow existence check)"

# n8n API requires auth; use API key if available, otherwise note limitation
RESPONSE=""
if [ -n "${N8N_API_KEY:-}" ]; then
  RESPONSE=$(curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${N8N_PORT}/api/v1/workflows" \
    -H "Accept: application/json" \
    -H "X-N8N-API-KEY: ${N8N_API_KEY}" 2>/dev/null) || RESPONSE=""
fi

if echo "$RESPONSE" | grep -qi "Morning Briefing"; then
  echo "  Morning Briefing workflow found in n8n."
  pass
else
  if [ -z "${N8N_API_KEY:-}" ]; then
    echo "  N8N_API_KEY not set — cannot query n8n API. Checking workflow file exists locally."
    if [ -f n8n/workflows/morning_briefing.json ]; then
      echo "  morning_briefing.json exists locally."
      pass
    else
      fail
      echo "  morning_briefing.json not found."
    fi
  else
    fail
    echo "  Morning Briefing workflow not found. Import workflows from n8n/workflows/."
  fi
fi
echo ""

# ─── Test 6: DeerFlow Research ──────────────────────────────────────
echo "Test 6 — DeerFlow Research (optional)"
if ! curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${DEERFLOW_PORT}/api/health" >/dev/null 2>&1; then
  echo "  DeerFlow is not running. Skipping."
  skip
else
  RESPONSE=$(curl -sf --connect-timeout 5 --max-time 120 -X POST "http://localhost:${DEERFLOW_PORT}/api/langgraph/runs" \
    -H "Content-Type: application/json" \
    -d '{
      "input": {
        "messages": [{"role": "user", "content": "What are the top 3 manufactured home lenders in Texas for FHA loans?"}]
      },
      "config": {}
    }' 2>&1) || RESPONSE=""

  echo "  Response: $(echo "$RESPONSE" | head -c 200)"
  if echo "$RESPONSE" | grep -qiE "lender|FHA|Texas|mortgage|manufactured"; then
    pass
  else
    fail
    echo "  Check OPENROUTER_API_KEY and DeerFlow config."
  fi
fi
echo ""

# ─── Summary ────────────────────────────────────────────────────────
LABELS="Stack Health|OpenClaw Response|n8n Roundtrip|Lead Lookup|Morning Briefing|DeerFlow Research"

echo "============================================="
echo " STACK VALIDATION REPORT"
echo "============================================="

i=0
for STATUS in $RESULTS; do
  i=$((i + 1))
  LABEL=$(echo "$LABELS" | cut -d'|' -f"$i")
  printf "  Test %d — %-20s [%s]\n" "$i" "$LABEL" "$STATUS"
done

echo "============================================="
echo "  OVERALL: ${PASSED}/${TOTAL} tests passed"
echo "============================================="

if [ "$PASSED" -ge 5 ]; then
  echo "  Stack is ready for testing!"
elif [ "$PASSED" -ge 3 ]; then
  echo "  Core services working. Check failed tests above."
else
  echo "  Multiple failures. Review docker compose logs."
fi

if [ "$PASSED" -lt "$TOTAL" ]; then
  exit 1
fi
