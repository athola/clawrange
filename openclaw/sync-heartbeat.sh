#!/bin/sh
# Load openclaw/HEARTBEAT.md into each heartbeat monitor's scratch.
#
# OpenClaw 2026.8+ retired the workspace HEARTBEAT.md: heartbeat context
# lives in a per-job "monitor scratch" in its state DB, and a heartbeat whose
# scratch is effectively empty is skipped (reason=empty-heartbeat-file)
# before it reaches the proxy's interceptor. Run in the background from the
# container command so every start re-syncs the repo file, which stays the
# single source. The CLI talks to the running gateway, so wait for /healthz.
#
# Usage: sync-heartbeat.sh <heartbeat-file>
set -u
FILE="$1"
i=0
until curl -sf -o /dev/null --max-time 5 http://127.0.0.1:18789/healthz; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && { echo "sync-heartbeat: gateway never became healthy" >&2; exit 1; }
  sleep 5
done
# The gateway creates heartbeat monitors shortly after /healthz goes green.
heartbeat_ids() {
  runuser -u node -- openclaw cron list --all --json 2>/dev/null | node -e '
let t = "";
process.stdin.on("data", (d) => (t += d)).on("end", () => {
  const i = t.indexOf("{");
  const jobs = i < 0 ? [] : JSON.parse(t.slice(i)).jobs || [];
  for (const j of jobs) if ((j.payload || {}).kind === "heartbeat") console.log(j.id);
});'
}
i=0
until ids=$(heartbeat_ids) && [ -n "$ids" ]; do
  i=$((i + 1))
  [ "$i" -ge 24 ] && { echo "sync-heartbeat: no heartbeat monitors found" >&2; exit 1; }
  sleep 5
done
for id in $ids; do
  if runuser -u node -- openclaw cron scratch "$id" --file "$FILE" >/dev/null 2>&1; then
    echo "sync-heartbeat: loaded $FILE into $id"
  else
    echo "sync-heartbeat: failed to load scratch for $id" >&2
  fi
done
