#!/usr/bin/env bash
# Test n8n workflows only.
set -euo pipefail

PORT="${N8N_PORT:-5678}"
BASE="http://localhost:${PORT}"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== n8n Tests ==="

# Test 1: Health check
echo ""
echo "Test 1: Health check"
if curl -sf "${BASE}/healthz" >/dev/null 2>&1; then
  pass "n8n is healthy at ${BASE}"
else
  fail "n8n health check failed at ${BASE}"
  echo "  Is the service running? Check: docker compose ps"
  exit 1
fi

# Test 2: Test webhook (connectivity canary)
echo ""
echo "Test 2: Test webhook roundtrip"
RESPONSE=$(curl -sf -X POST "${BASE}/webhook-test/test" \
  -H "Content-Type: application/json" \
  -d '{"message": "ping", "source": "test-n8n-script"}' 2>/dev/null) || RESPONSE=""

if echo "$RESPONSE" | grep -qi "received"; then
  pass "Test webhook responded"
  echo "  Response: $(echo "$RESPONSE" | head -c 200)"
else
  fail "Test webhook did not respond with 'received'"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
  echo "  Note: Workflow must be activated in n8n UI first."
fi

# Test 3: Lead status lookup
echo ""
echo "Test 3: Lead status lookup"
RESPONSE=$(curl -sf -X POST "${BASE}/webhook-test/lead-status" \
  -H "Content-Type: application/json" \
  -d '{"name": "John Smith", "phone": "903-555-0100"}' 2>/dev/null) || RESPONSE=""

if echo "$RESPONSE" | grep -qi "John Smith"; then
  pass "Lead lookup found John Smith"
  echo "  Response: $(echo "$RESPONSE" | head -c 200)"
else
  fail "Lead lookup did not return expected data"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
  echo "  Note: Lead Status Lookup workflow must be activated in n8n UI."
fi

echo ""
echo "n8n Tests: ${PASS} passed, ${FAIL} failed"
exit "$FAIL"
