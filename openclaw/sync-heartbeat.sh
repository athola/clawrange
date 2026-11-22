#!/bin/sh
# Ensure the ClawRange heartbeat automation exists and load
# openclaw/HEARTBEAT.md into its scratch.
#
# OpenClaw 2026.11.5 retired heartbeat config: a heartbeat is an ordinary
# agentTurn automation job with its own schedule, delivery and scratch in the
# state DB, and Gateway startup no longer creates one from openclaw.json.
# `openclaw doctor --fix` converts an existing install's heartbeat into such
# a job. This script finds that job by its message, which starts with the
# marker workflows/heartbeat.py intercepts (_HEARTBEAT_MARKER), creates it on
# a fresh state dir, and reloads the repo's HEARTBEAT.md (the single source)
# into its scratch on every start. A disabled job still counts (list --all),
# so `openclaw automations disable <id>` turns the heartbeat off for good.
#
# Run in the background from the container command; the CLI talks to the
# running gateway, so wait for /healthz first. The CLI's output goes to the
# container log. Any failure also writes $SYNC_HEARTBEAT_MARKER, which
# healthcheck-openclaw.sh reports as unhealthy: a background job's exit
# status is otherwise seen by nobody.
#
# The job's tool cap is max-ops' allow list from the rendered openclaw.json,
# reapplied whenever it drifts. A job without an explicit cap keeps legacy
# sender-policy resolution (doctor --fix warns about a migrated one), and the
# config stays the one place the agent's tools are listed.
#
# Usage: sync-heartbeat.sh <heartbeat-file>
# It also fails the healthcheck if `openclaw secrets audit` finds any
# plaintext credential at rest, such as the proxy token's auth-profile copy
# on an install that has not run the one-time openclaw/secrets-plan.json
# migration (docs/DEPLOYMENT_GUIDE.md), or a credential added later.
#
# Env:   TELEGRAM_CHAT_ID (delivery target for a newly created job)
#        OPENCLAW_CONFIG (default /home/node/.openclaw/openclaw.json)
set -u
FILE="$1"
CONFIG="${OPENCLAW_CONFIG:-/home/node/.openclaw/openclaw.json}"
# Problems that must not stop the heartbeat from loading: collected here and
# reported together at the end.
problems=""
problem() { problems="${problems:+$problems; }$*"; }
# The last meaningful line of CLI output: Node prints deprecation warnings
# after the real error, and they would otherwise become the reported cause.
cli_error() {
  printf '%s\n' "$1" | grep -vE 'DeprecationWarning|--trace-deprecation|^[[:space:]]*$' | tail -n 1
}

# The message the job sends each cycle. It must start with the proxy's
# heartbeat marker, and NO_REPLY is the token OpenClaw automations treat as a
# quiet result (workflows/heartbeat.py QUIET_REPLY).
MESSAGE="ClawRange heartbeat: run the scheduled checks in the job scratch. Do not infer or repeat old tasks from prior chats. If nothing needs attention, reply NO_REPLY."
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

# Rendered before this script starts, so a missing list is a broken install:
# never run the heartbeat with whatever tools the job happens to have.
TOOLS=$(node -e '
const c = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
const allow = ((((c.agents || {}).entries || {})["max-ops"] || {}).tools || {}).allow;
if (!Array.isArray(allow) || !allow.length) process.exit(1);
console.log(allow.join(","));' "$CONFIG" 2>/dev/null) ||
  fail "no max-ops tools.allow in $CONFIG; refusing to run the heartbeat uncapped"

i=0
until curl -sf -o /dev/null --max-time 5 http://127.0.0.1:18789/healthz; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && fail "gateway never became healthy"
  sleep 5
done


# Print the id of every heartbeat job, enabled or not, suffixed "+cap" when
# its tool cap differs from $TOOLS, and "foreign:<id>:<name>" for each
# enabled heartbeat that is not ClawRange's (named heartbeat-* or "Heartbeat
# (...)", as OpenClaw names its own). Fails (non-zero) when the CLI or its
# JSON fails, so a gateway still warming up is retried rather than read as
# "no job". stderr is left on the container log.
heartbeat_ids() {
  out=$(runuser -u node -- openclaw automations list --all --json) || return 1
  printf '%s' "$out" | TOOLS="$TOOLS" node -e '
let t = "";
process.stdin.on("data", (d) => (t += d)).on("end", () => {
  const i = t.indexOf("{");
  if (i < 0) process.exit(1);
  for (const j of JSON.parse(t.slice(i)).jobs || []) {
    const p = j.payload || {};
    const msg = String(p.message || "").trim().toLowerCase();
    if (p.kind !== "agentTurn") continue;
    if (!msg.startsWith("clawrange heartbeat:")) {
      const name = String(j.name || "");
      const looksHeartbeat =
        name.toLowerCase().startsWith("heartbeat-") ||
        String(j.displayName || "").toLowerCase().startsWith("heartbeat");
      if (j.enabled !== false && looksHeartbeat) console.log("foreign:" + j.id + ":" + name);
      continue;
    }
    const capped = (p.toolsAllow || []).join(",") === process.env.TOOLS;
    console.log(capped ? j.id : j.id + "+cap");
  }
});'
}

# Split heartbeat_ids output into our job tokens ($ids) and foreign
# heartbeats ($foreign).
split_ids() {
  foreign=$(printf '%s\n' "$1" | grep '^foreign:' || true)
  ids=$(printf '%s\n' "$1" | grep -v '^foreign:' || true)
}

i=0
until listed=$(heartbeat_ids); do
  i=$((i + 1))
  [ "$i" -ge 24 ] && fail "could not list automation jobs"
  sleep 5
done
split_ids "$listed"

if [ -z "$ids" ]; then
  [ -n "${TELEGRAM_CHAT_ID:-}" ] ||
    fail "no heartbeat job and TELEGRAM_CHAT_ID is empty; cannot create one"
  if out=$(runuser -u node -- openclaw automations add \
    --name clawrange-heartbeat \
    --agent max-ops \
    --every 10m \
    --session isolated \
    --message "$MESSAGE" \
    --tools "$TOOLS" \
    --timeout-seconds 600 \
    --idle-only \
    --announce --channel telegram --to "$TELEGRAM_CHAT_ID" \
    --active-hours-start 08:00 --active-hours-end 20:00 \
    --active-hours-timezone user 2>&1); then
    echo "sync-heartbeat: created heartbeat job: $out"
  else
    echo "sync-heartbeat: failed to create heartbeat job: $out" >&2
    fail "heartbeat job create failed: $(cli_error "$out")"
  fi
  listed=$(heartbeat_ids) || fail "could not list automation jobs"
  split_ids "$listed"
  [ -n "$ids" ] || fail "heartbeat job missing after create"
fi

failed=""
for tok in $ids; do
  id=${tok%+cap}
  if [ "$id" != "$tok" ]; then
    if out=$(runuser -u node -- openclaw automations edit "$id" --tools "$TOOLS" 2>&1); then
      echo "sync-heartbeat: capped $id tools to $TOOLS"
    else
      echo "sync-heartbeat: failed to cap tools for $id: $out" >&2
      failed="$failed $id"
      continue
    fi
  fi
  if out=$(runuser -u node -- openclaw automations scratch "$id" --file "$FILE" 2>&1); then
    echo "sync-heartbeat: loaded $FILE into $id"
  else
    echo "sync-heartbeat: failed to load scratch for $id: $out" >&2
    failed="$failed $id"
  fi
done
[ -z "$failed" ] || problem "tool cap or scratch load failed for:$failed"

# OpenClaw 2026.8+ gave every agent its own heartbeat unless config turned it
# off; 2026.11.5 retired that config, so guard the outcome instead. A foreign
# heartbeat DMs the operator on its own schedule and bypasses the proxy's
# interceptor. Ours is loaded above either way; disabling the other is the
# operator's call, so report it rather than touch it.
if [ -n "$foreign" ]; then
  names=$(printf '%s\n' "$foreign" | sed 's/^foreign://' | tr '\n' ' ')
  problem "foreign heartbeat job(s) enabled: ${names}(id:name); turn off with: openclaw automations disable <id>"
fi

# Any plaintext credential at rest is readable by whatever can read the state
# dir. Migrating is the operator's step (the proxy token: the one-time plan;
# anything else: openclaw secrets configure), so report it.
if ! out=$(runuser -u node -- openclaw secrets audit --check 2>&1); then
  findings=$(printf '%s\n' "$out" | grep -E '^- \[' | sed 's/^- //' | tr '\n' ' ')
  problem "secrets audit: ${findings:-$(cli_error "$out")}"
fi

[ -z "$problems" ] || fail "$problems"
