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

# Test 3: Send a test message (if API endpoint exists)
echo ""
echo "Test 3: Send test message"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 30 -X POST "${BASE}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${OPENCLAW_GATEWAY_TOKEN:-testbed-token-change-me}" \
  -d '{"model": "openclaw:main", "messages": [{"role": "user", "content": "What financing options does Longview Home Center offer?"}]}' 2>/dev/null) || RESPONSE=""

if echo "$RESPONSE" | grep -qiE "FHA|VA|conventional|in-house|financing"; then
  pass "Got relevant financing response"
  echo "  Response: $(echo "$RESPONSE" | head -c 200)"
else
  fail "Response did not mention financing options"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
  echo "  Note: This may fail if OPENROUTER_API_KEY is not set."
fi

echo ""
echo "OpenClaw Tests: ${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then exit 1; else exit 0; fi
