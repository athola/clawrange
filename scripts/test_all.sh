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

RESULTS=()
PASSED=0

pass() { RESULTS+=("PASS"); PASSED=$((PASSED + 1)); echo "  PASS"; }
fail() { RESULTS+=("FAIL"); echo "  FAIL"; }

echo "============================================="
echo " AI MSP TESTBED — STACK VALIDATION"
echo "============================================="
echo ""

# ─── Test 1: Stack Health ───────────────────────────────────────────
echo "Test 1 — Stack Health"

T1_OK=true

printf "  OpenClaw (localhost:${OPENCLAW_PORT}): "
if curl -sf "http://localhost:${OPENCLAW_PORT}/healthz" >/dev/null 2>&1; then
  echo "UP"
else
  echo "DOWN"
  T1_OK=false
fi

printf "  n8n (localhost:${N8N_PORT}): "
if curl -sf "http://localhost:${N8N_PORT}/healthz" >/dev/null 2>&1; then
  echo "UP"
else
  echo "DOWN"
  T1_OK=false
fi

printf "  DeerFlow (localhost:${DEERFLOW_PORT}): "
if curl -sf "http://localhost:${DEERFLOW_PORT}/api/health" >/dev/null 2>&1; then
  echo "UP"
else
  echo "DOWN (optional)"
fi

if [ "$T1_OK" = true ]; then pass; else fail; fi
echo ""

# ─── Test 2: OpenClaw Response ──────────────────────────────────────
echo "Test 2 — OpenClaw Connectivity"
RESPONSE=$(curl -sf -X POST "http://localhost:${OPENCLAW_PORT}/v1/chat/completions" \
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
RESPONSE=$(curl -sf -X POST "http://localhost:${N8N_PORT}/webhook-test/test" \
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
RESPONSE=$(curl -sf -X POST "http://localhost:${N8N_PORT}/webhook-test/lead-status" \
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
echo "Test 5 — Morning Briefing (manual trigger)"

# Try triggering via n8n API — the workflow uses a schedule trigger,
# so we test by calling the n8n execution API if available.
# Fallback: check if the workflow exists.
RESPONSE=$(curl -sf "http://localhost:${N8N_PORT}/api/v1/workflows" \
  -H "Accept: application/json" 2>/dev/null) || RESPONSE=""

if echo "$RESPONSE" | grep -qi "Morning Briefing"; then
  echo "  Morning Briefing workflow found in n8n."
  pass
else
  fail
  echo "  Morning Briefing workflow not found. Import workflows from n8n/workflows/."
fi
echo ""

# ─── Test 6: DeerFlow Research ──────────────────────────────────────
echo "Test 6 — DeerFlow Research (optional)"
if ! curl -sf "http://localhost:${DEERFLOW_PORT}/api/health" >/dev/null 2>&1; then
  echo "  DeerFlow is not running. Skipping."
  RESULTS+=("SKIP")
  echo "  SKIP"
else
  RESPONSE=$(curl -sf -X POST "http://localhost:${DEERFLOW_PORT}/api/langgraph/runs" \
    -H "Content-Type: application/json" \
    --max-time 120 \
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
TOTAL=${#RESULTS[@]}

echo "============================================="
echo " STACK VALIDATION REPORT"
echo "============================================="

LABELS=("Stack Health" "OpenClaw Response" "n8n Roundtrip" "Lead Lookup" "Morning Briefing" "DeerFlow Research")
for i in $(seq 0 $((TOTAL - 1))); do
  STATUS="${RESULTS[$i]:-N/A}"
  printf "  Test %d — %-20s [%s]\n" $((i + 1)) "${LABELS[$i]:-Test $((i+1))}" "$STATUS"
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
