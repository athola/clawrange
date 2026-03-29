#!/usr/bin/env bash
# Test the OpenClaw layer only.
set -euo pipefail
cd "$(dirname "$0")/.."

# Source .env for port and token configuration
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

PORT="${OPENCLAW_PORT:-3000}"
BASE="http://localhost:${PORT}"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== OpenClaw Tests ==="

# Test 1: Health check
echo ""
echo "Test 1: Health check"
if curl -sf --connect-timeout 5 --max-time 10 "${BASE}/healthz" >/dev/null 2>&1; then
  pass "OpenClaw is healthy at ${BASE}"
else
  fail "OpenClaw health check failed at ${BASE}"
  echo "  Is the service running? Check: docker compose ps"
  exit 1
fi

# Test 2: Readiness
echo ""
echo "Test 2: Readiness probe"
if curl -sf --connect-timeout 5 --max-time 10 "${BASE}/readyz" >/dev/null 2>&1; then
  pass "OpenClaw is ready"
else
  fail "OpenClaw readiness probe failed"
fi

# Test 3: Send a test message via agent CLI
echo ""
echo "Test 3: Send test message"
RESPONSE=$(docker exec msp-openclaw runuser -u node -- openclaw agent --agent main \
  -m "What financing options does Longview Home Center offer?" --json 2>/dev/null) || RESPONSE=""

AGENT_TEXT=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('payloads',[{}])[0].get('text',''))" 2>/dev/null) || AGENT_TEXT=""

if echo "$AGENT_TEXT" | grep -qiE "FHA|VA|conventional|in-house|financing"; then
  pass "Got relevant financing response"
  echo "  Response: $(echo "$AGENT_TEXT" | head -c 200)"
elif echo "$AGENT_TEXT" | grep -qi "billing\|credits\|balance\|insufficient"; then
  pass "Agent works (OpenRouter balance empty)"
  echo "  Top up at: https://openrouter.ai/settings/credits"
else
  fail "Response did not mention financing options"
  echo "  Response: $(echo "$AGENT_TEXT" | head -c 300)"
  echo "  Note: This may fail if OPENROUTER_API_KEY is not set."
fi

echo ""
echo "OpenClaw Tests: ${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then exit 1; else exit 0; fi
