#!/usr/bin/env bash
# Test the DeerFlow research layer.
set -euo pipefail

PORT="${DEERFLOW_PORT:-2026}"
BASE="http://localhost:${PORT}"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== DeerFlow Tests ==="

# Test 1: Health check
echo ""
echo "Test 1: Health check"
if curl -sf "${BASE}/api/health" >/dev/null 2>&1; then
  pass "DeerFlow is healthy at ${BASE}"
else
  fail "DeerFlow health check failed at ${BASE}"
  echo "  DeerFlow may not be running. Start with: ./scripts/start.sh --with-deerflow"
  exit 1
fi

# Test 2: Models endpoint
echo ""
echo "Test 2: Available models"
RESPONSE=$(curl -sf "${BASE}/api/models" 2>&1) || RESPONSE=""
if [ -n "$RESPONSE" ] && echo "$RESPONSE" | grep -qi "openrouter\|researcher\|writer"; then
  pass "Models endpoint returned OpenRouter-configured models"
else
  fail "Models endpoint did not return expected configuration"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
fi

# Test 3: Research request
echo ""
echo "Test 3: Research request (may take 30-60 seconds)"
RESPONSE=$(curl -sf -X POST "${BASE}/api/langgraph/runs" \
  -H "Content-Type: application/json" \
  --max-time 120 \
  -d '{
    "input": {
      "messages": [{"role": "user", "content": "What are the top 3 manufactured home lenders in Texas for FHA loans?"}]
    },
    "config": {}
  }' 2>&1) || RESPONSE=""

if echo "$RESPONSE" | grep -qiE "lender|FHA|Texas|mortgage|manufactured"; then
  pass "Research request returned relevant results"
  echo "  Response preview: $(echo "$RESPONSE" | head -c 300)"
else
  fail "Research request did not return expected content"
  echo "  Response: $(echo "$RESPONSE" | head -c 300)"
  echo "  Note: This requires OPENROUTER_API_KEY and may take time."
fi

echo ""
echo "DeerFlow Tests: ${PASS} passed, ${FAIL} failed"
exit "$FAIL"
