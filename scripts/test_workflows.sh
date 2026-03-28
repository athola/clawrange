#!/usr/bin/env bash
# Test workflow service endpoints.
set -euo pipefail
cd "$(dirname "$0")/.."

# Source .env for port configuration
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

PORT="${WORKFLOWS_PORT:-5678}"
BASE="http://localhost:${PORT}"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== Workflow Tests ==="

# Test 1: Health check
echo ""
echo "Test 1: Health check"
if curl -sf --connect-timeout 5 --max-time 10 "${BASE}/healthz" >/dev/null 2>&1; then
  pass "Workflows service is healthy at ${BASE}"
else
  fail "Health check failed at ${BASE}"
  echo "  Is the service running? Check: docker compose ps"
  exit 1
fi

# Test 2: Test webhook (connectivity canary)
echo ""
echo "Test 2: Test webhook roundtrip"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 -X POST "${BASE}/webhook-test/test" \
  -H "Content-Type: application/json" \
  -d '{"message": "ping", "source": "test-script"}' 2>/dev/null) || RESPONSE=""

if echo "$RESPONSE" | grep -qi "received"; then
  pass "Test webhook responded"
  echo "  Response: $(echo "$RESPONSE" | head -c 200)"
else
  fail "Test webhook did not respond with 'received'"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
fi

# Test 3: Lead status lookup
echo ""
echo "Test 3: Lead status lookup"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 -X POST "${BASE}/webhook-test/lead-status" \
  -H "Content-Type: application/json" \
  -d '{"name": "John Smith", "phone": "903-555-0100"}' 2>/dev/null) || RESPONSE=""

if echo "$RESPONSE" | grep -qi "John Smith"; then
  pass "Lead lookup found John Smith"
  echo "  Response: $(echo "$RESPONSE" | head -c 200)"
else
  fail "Lead lookup did not return expected data"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
fi

# Test 4: Morning briefing
echo ""
echo "Test 4: Morning briefing"
RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 "${BASE}/webhook/morning-briefing" 2>/dev/null) || RESPONSE=""

if echo "$RESPONSE" | grep -qi "MORNING BRIEFING"; then
  pass "Morning briefing generated"
  echo "  Lead count: $(echo "$RESPONSE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["leadCount"])' 2>/dev/null || echo '?')"
else
  fail "Morning briefing did not return expected data"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
fi

echo ""
echo "Workflow Tests: ${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then exit 1; else exit 0; fi
