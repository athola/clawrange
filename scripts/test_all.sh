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
WORKFLOWS_PORT="${WORKFLOWS_PORT:-5678}"
DEERFLOW_PORT="${DEERFLOW_PORT:-2026}"

# POSIX-compatible results tracking (no arrays)
RESULTS=""
PASSED=0
SKIPPED=0
TOTAL=0

pass() { RESULTS="${RESULTS}PASS "; PASSED=$((PASSED + 1)); TOTAL=$((TOTAL + 1)); echo "  PASS"; }
fail() { RESULTS="${RESULTS}FAIL "; TOTAL=$((TOTAL + 1)); echo "  FAIL"; }
skip() { RESULTS="${RESULTS}SKIP "; SKIPPED=$((SKIPPED + 1)); TOTAL=$((TOTAL + 1)); echo "  SKIP"; }

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

printf "  Workflows (localhost:${WORKFLOWS_PORT}): "
if curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${WORKFLOWS_PORT}/healthz" >/dev/null 2>&1; then
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

# ─── Test 2: OpenClaw Agent Response ───────────────────────────────
echo "Test 2 — OpenClaw Agent"
RESPONSE=$(docker exec msp-openclaw runuser -u node -- openclaw agent --agent max \
  -m "Respond with exactly one word: PONG" --json 2>/dev/null) || RESPONSE=""

AGENT_TEXT=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('payloads',[{}])[0].get('text',''))" 2>/dev/null) || AGENT_TEXT=""
echo "  Response: $(echo "$AGENT_TEXT" | head -c 200)"
if echo "$AGENT_TEXT" | grep -qi "PONG"; then
  pass
elif echo "$AGENT_TEXT" | grep -qi "billing\|credits\|balance\|insufficient"; then
  echo "  OpenRouter balance empty — agent works but LLM call failed."
  echo "  Top up at: https://openrouter.ai/settings/credits"
  skip
else
  fail
  echo "  Expected PONG or billing error."
  echo "  Check OPENROUTER_API_KEY in .env"
fi
echo ""

# ─── Test 3: Webhook Roundtrip ─────────────────────────────────────
echo "Test 3 — Webhook Roundtrip"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 -X POST "http://localhost:${WORKFLOWS_PORT}/webhook-test/test" \
  -H "Content-Type: application/json" \
  -d '{"message": "ping", "source": "openclaw-test"}' 2>/dev/null) || RESPONSE=""

echo "  Response: $(echo "$RESPONSE" | head -c 200)"
if echo "$RESPONSE" | grep -qi "received"; then
  pass
else
  fail
  echo "  Check workflows service: docker compose logs workflows"
fi
echo ""

# ─── Test 4: Task Queue Lifecycle ──────────────────────────────────
# Validates the core FastAPI surface that replaced n8n: create a task,
# read it back, then cancel it. Round-trips the brain DB.
echo "Test 4 — Task Queue Lifecycle"
TASK_DESC="stack-validation probe $(date +%s)"
CREATE_RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 -X POST "http://localhost:${WORKFLOWS_PORT}/task" \
  -H "Content-Type: application/json" \
  -d "{\"description\": \"${TASK_DESC}\", \"priority\": 5}" 2>/dev/null) || CREATE_RESPONSE=""

TASK_ID=$(echo "$CREATE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null) || TASK_ID=""
echo "  Created task id: ${TASK_ID:-<none>}"

if [ -z "$TASK_ID" ]; then
  fail
  echo "  POST /task did not return an id. Check: docker compose logs workflows"
else
  GET_RESPONSE=$(curl -sf --connect-timeout 5 --max-time 10 "http://localhost:${WORKFLOWS_PORT}/task/${TASK_ID}" 2>/dev/null) || GET_RESPONSE=""
  echo "  Read response: $(echo "$GET_RESPONSE" | head -c 160)"
  curl -sf --connect-timeout 5 --max-time 10 -X DELETE "http://localhost:${WORKFLOWS_PORT}/task/${TASK_ID}" >/dev/null 2>&1 || true
  if echo "$GET_RESPONSE" | grep -qF "$TASK_DESC"; then
    pass
  else
    fail
    echo "  GET /task/${TASK_ID} did not echo the description back."
  fi
fi
echo ""

# ─── Test 5: Tier and Balance Probe ────────────────────────────────
# Validates the LLM proxy is configured and can report tier status +
# OpenRouter balance. This is the closest equivalent of the old
# "morning briefing" canary on the new FastAPI surface.
echo "Test 5 — Tier and Balance Probe"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 "http://localhost:${WORKFLOWS_PORT}/tier" 2>/dev/null) || RESPONSE=""

echo "  Response: $(echo "$RESPONSE" | head -c 200)"
if echo "$RESPONSE" | grep -qiE '"tiers"|"balance"|"current_tier"'; then
  pass
else
  fail
  echo "  GET /tier did not return tier metadata. Check: docker compose logs workflows"
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
LABELS="Stack Health|OpenClaw Response|Webhook Roundtrip|Task Queue|Tier Probe|DeerFlow Research"

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

FAILED=$((TOTAL - PASSED - SKIPPED))
if [ "$FAILED" -gt 0 ]; then
  exit 1
fi
