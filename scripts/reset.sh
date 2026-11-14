#!/usr/bin/env bash
# Wipe all data and start fresh.
# WARNING: This removes the DeerFlow clone and output and prunes project
# containers. The brain database (data/brain/) is left in place.
# Usage: ./scripts/reset.sh [--force|-f]
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== AI MSP Testbed Reset ==="
echo "This will remove the DeerFlow clone and output and prune project containers."

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
