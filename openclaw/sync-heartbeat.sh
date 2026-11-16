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
# The CLI's output goes to the container log. Any failure also writes
# $SYNC_HEARTBEAT_MARKER, which healthcheck-openclaw.sh reports as unhealthy:
# a background job's exit status is otherwise seen by nobody.
#
# Usage: sync-heartbeat.sh <heartbeat-file>
set -u
FILE="$1"
#
# This runs as root, so the marker lives in /run: a root-owned tmpfs (see
# docker-compose.yml) the node user cannot write to. /tmp and the state dir
# are owned by node, which could plant a symlink there for root to write
# through. The write still goes via mktemp + mv (mv replaces a symlink
# rather than following it) as a second line of defence.
MARKER="${SYNC_HEARTBEAT_MARKER:-/run/sync-heartbeat.failed}"
rm -f "$MARKER"

fail() {
  echo "sync-heartbeat: $*" >&2
  if tmp=$(mktemp "$MARKER.XXXXXX"); then
    printf '%s\n' "$*" > "$tmp" && mv -f "$tmp" "$MARKER"
  else
    echo "sync-heartbeat: could not write failure marker $MARKER" >&2
  fi
  exit 1
}

i=0
until curl -sf -o /dev/null --max-time 5 http://127.0.0.1:18789/healthz; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && fail "gateway never became healthy"
  sleep 5
done
# The gateway creates heartbeat monitors shortly after /healthz goes green.
# stderr is left on the container log so a CLI error is visible.
heartbeat_ids() {
  runuser -u node -- openclaw cron list --all --json | node -e '
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
  [ "$i" -ge 24 ] && fail "no heartbeat monitors found"
  sleep 5
done
failed=""
for id in $ids; do
  if out=$(runuser -u node -- openclaw cron scratch "$id" --file "$FILE" 2>&1); then
    echo "sync-heartbeat: loaded $FILE into $id"
  else
    echo "sync-heartbeat: failed to load scratch for $id: $out" >&2
    failed="$failed $id"
  fi
done
[ -z "$failed" ] || fail "scratch load failed for:$failed"
