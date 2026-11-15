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

# Protected workflows routes need the bearer token (see workflows_auth.sh).
# shellcheck source=scripts/workflows_auth.sh
. ./scripts/workflows_auth.sh
workflows_auth_init || exit 1
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
  -H @"$WF_AUTH_HEADER" \
  -H "Content-Type: application/json" \
  -d '{"message": "ping", "source": "test-script"}' 2>/dev/null) || RESPONSE=""

if echo "$RESPONSE" | grep -qi "received"; then
  pass "Test webhook responded"
  echo "  Response: $(echo "$RESPONSE" | head -c 200)"
else
  fail "Test webhook did not respond with 'received'"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
fi

# Tests 3-4: lead lookup and morning digest. These replaced the n8n
# lead-status and morning-briefing webhooks with CRM query templates
# (lead_lookup) and the crm_digest schedule. The CRM router is mounted only
# when the active profile defines `crm`, so skip, not fail, without one.
skip() { echo "  SKIP: $1"; }
CRM_HEALTH=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 15 "${BASE}/healthz/crm" 2>/dev/null) || CRM_HEALTH="000"

echo ""
echo "Test 3: Lead lookup (CRM lead_lookup template)"
if [ "$CRM_HEALTH" != "200" ]; then
  skip "active profile defines no crm (GET /healthz/crm -> ${CRM_HEALTH})"
else
  RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 -X POST "${BASE}/crm/query/run" \
    -H @"$WF_AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"template": "lead_lookup", "params": {"q": "@"}}' 2>/dev/null) || RESPONSE=""
  if echo "$RESPONSE" | grep -q '"rows"'; then
    pass "lead_lookup answered"
  else
    fail "lead_lookup did not return rows"
    echo "  Response: $(echo "$RESPONSE" | head -c 300)"
  fi
fi

echo ""
echo "Test 4: Morning digest query (CRM new_leads_count template)"
if [ "$CRM_HEALTH" != "200" ]; then
  skip "active profile defines no crm (GET /healthz/crm -> ${CRM_HEALTH})"
else
  RESPONSE=$(curl -sf --connect-timeout 5 --max-time 15 -X POST "${BASE}/crm/query/run" \
    -H @"$WF_AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"template": "new_leads_count", "params": {}}' 2>/dev/null) || RESPONSE=""
  if echo "$RESPONSE" | grep -q '"rows"'; then
    pass "new_leads_count answered"
  else
    fail "new_leads_count did not return rows"
    echo "  Response: $(echo "$RESPONSE" | head -c 300)"
  fi
fi

echo ""
echo "Workflow Tests: ${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then exit 1; else exit 0; fi
