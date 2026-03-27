#!/usr/bin/env bash
# Wipe all data and start fresh.
# WARNING: This destroys all n8n workflows, DeerFlow output, and Docker volumes.
# Usage: ./scripts/reset.sh [--force|-f]
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== AI MSP Testbed Reset ==="
echo "This will destroy all persistent data (n8n workflows, volumes, DeerFlow output)."

if [ "${1:-}" = "--force" ] || [ "${1:-}" = "-f" ]; then
  confirm="y"
else
  printf "Are you sure? (y/N) "
  read -r confirm
fi

if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

# Stop everything
./scripts/stop.sh --all

# Remove DeerFlow clone
if [ -d deer-flow ]; then
  echo "Removing DeerFlow clone..."
  rm -rf deer-flow
fi

# Remove DeerFlow output
rm -rf /tmp/deerflow-output 2>/dev/null || true

# Prune project containers
echo "Pruning containers..."
docker container prune -f --filter "label=com.docker.compose.project=clawrange" 2>/dev/null || true

echo ""
echo "Reset complete. Run ./scripts/start.sh to rebuild."
