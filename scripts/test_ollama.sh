#!/usr/bin/env bash
# Test local Ollama inference.
# Validates whether the hardware can run local models (relevant for Tier 3 onsite nodes).
set -euo pipefail

echo "=== Ollama Local Inference Test ==="

# Step 1: Check if Ollama is installed
if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama is NOT installed."
  echo ""
  case "$(uname -s)" in
    Linux)
      echo "Install on Linux:"
      echo "  curl -fsSL https://ollama.com/install.sh | sh"
      ;;
    Darwin)
      echo "Install on macOS:"
      echo "  brew install ollama"
      echo "  # or download from https://ollama.com/download"
      ;;
    MINGW*|MSYS*|CYGWIN*)
      echo "Install on Windows:"
      echo "  Download from https://ollama.com/download/windows"
      ;;
  esac
  exit 1
fi

echo "Ollama found: $(ollama --version)"

# Step 2: Ensure Ollama daemon is running
if ! ollama list >/dev/null 2>&1; then
  echo "Ollama daemon is not running. Starting it..."
  ollama serve &
  OLLAMA_PID=$!
  trap 'kill $OLLAMA_PID 2>/dev/null || true' EXIT
  # Poll for readiness instead of fixed sleep
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    ollama list >/dev/null 2>&1 && break
    sleep 1
  done
fi

# Step 3: Check for llama3.2:3b model
MODEL="llama3.2:3b"
echo ""
echo "Checking for model: ${MODEL}"

if ollama list 2>/dev/null | grep -q "llama3.2:3b"; then
  echo "Model ${MODEL} is already pulled."
else
  echo "Pulling ${MODEL} (this may take a few minutes on first run)..."
  ollama pull "${MODEL}"
fi

# Step 4: Send test prompt and measure speed
echo ""
echo "Running inference test..."
PROMPT="You are a helpful AI assistant for a manufactured home dealer. A customer asks: What financing options do you offer? Respond in 2 sentences."

# Portable timing: use date +%s (seconds). macOS does not support %N.
START_TIME=$(date +%s)

RESPONSE=$(ollama run "${MODEL}" "${PROMPT}" 2>&1)

END_TIME=$(date +%s)
ELAPSED_S=$(( END_TIME - START_TIME ))
ELAPSED_MS=$(( ELAPSED_S * 1000 ))

echo ""
echo "--- Response ---"
echo "$RESPONSE"
echo "----------------"

# Step 5: Report performance
echo ""
echo "Inference completed in ${ELAPSED_S}s"

# Estimate tokens (rough: ~1.3 tokens per word)
WORD_COUNT=$(echo "$RESPONSE" | wc -w | tr -d ' ')
APPROX_TOKENS=$(( WORD_COUNT * 13 / 10 ))
if [ "$ELAPSED_S" -gt 0 ]; then
  TPS=$(( APPROX_TOKENS / ELAPSED_S ))
  echo "Approximate output: ~${APPROX_TOKENS} tokens at ~${TPS} tokens/second"
else
  echo "Approximate output: ~${APPROX_TOKENS} tokens (too fast to measure)"
fi

echo ""
if [ "${ELAPSED_MS}" -lt 30000 ]; then
  echo "VERDICT: This machine can handle local inference. Suitable for Tier 3 onsite deployment."
elif [ "${ELAPSED_MS}" -lt 60000 ]; then
  echo "VERDICT: Local inference works but is slow. Consider a smaller model or GPU acceleration."
else
  echo "VERDICT: Local inference is too slow for production use. Use cloud inference (OpenRouter) instead."
fi
