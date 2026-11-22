#!/bin/sh
# OpenClaw healthcheck — reports whether the gateway is serving.
#
# Reports only. Docker acts on the exit code; a probe must never try to
# restart its own container.
#
# This used to also grep the log for "Telegram polling stuck": it took the
# last "starting provider" line and looked for sendMessage/message_id
# activity after it. That check could not work. OpenClaw logs sendMessage
# only when a send FAILS, so a bot that is delivering normally logs nothing
# and always scored zero activity. Every healthy idle period read as stuck,
# and with retries=1 the container was marked unhealthy 45s after the grace
# period and stayed that way (107 consecutive failures observed while
# /healthz returned ok and digests were being delivered).
#
# It also ran `kill -TERM 1` on that false positive. That never landed
# (RestartCount stayed 0 -- node as PID 1 did not act on it), but a probe
# killing PID 1 is the wrong shape regardless: if auto-restart is wanted,
# that belongs in a restart policy or an autoheal sidecar reacting to the
# unhealthy status, not in the probe itself.
#
# Detecting a wedged Telegram provider needs a positive liveness signal
# OpenClaw does not currently emit. Reporting real health beats a detector
# with no true positives and a guaranteed false one.
#
# It does check one positive failure signal: sync-heartbeat.sh (run in the
# background at start) writes a marker when it could not find or create the
# heartbeat automation job or load HEARTBEAT.md into its scratch. Without
# that job no heartbeat reaches the proxy, so it is reported as unhealthy
# with the recorded cause.

set -e

# /run is a root-owned tmpfs (docker-compose.yml), out of the node user's
# reach. A symlink there is never sync-heartbeat.sh's output, so it is
# ignored rather than read; the read is bounded either way.
MARKER="${SYNC_HEARTBEAT_MARKER:-/run/sync-heartbeat.failed}"

# curl, not wget: OpenClaw 2026.8+ images no longer ship wget.
curl -sf -o /dev/null --max-time 5 http://127.0.0.1:18789/healthz || exit 1

if [ -f "$MARKER" ] && [ ! -L "$MARKER" ]; then
  echo "heartbeat sync failed: $(head -c 512 "$MARKER")"
  exit 1
fi

exit 0
