#!/bin/sh
# OpenClaw healthcheck — detects stuck Telegram polling.
#
# Checks /healthz (basic HTTP) then inspects the log file to see if
# the Telegram provider is stuck at "starting provider" without any
# subsequent message activity. Exits 1 (unhealthy) to trigger a
# container restart via Docker's restart policy.

set -e

# 1. Basic HTTP health
wget -q -O /dev/null --timeout=5 http://127.0.0.1:18789/healthz || exit 1

# 2. Grace period — use PID 1 start time for container uptime
CONTAINER_START=$(stat -c %Y /proc/1)
NOW=$(date +%s)
CONTAINER_UPTIME=$((NOW - CONTAINER_START))
[ "$CONTAINER_UPTIME" -lt 120 ] && exit 0

# 3. Check if Telegram polling is stuck
LOG_FILE=$(ls -t /tmp/openclaw/openclaw-*.log 2>/dev/null | head -1)
[ -z "$LOG_FILE" ] && exit 0

# Look for the last "starting provider" entry
LAST_START=$(grep -n 'starting provider' "$LOG_FILE" 2>/dev/null | tail -1 | cut -d: -f1)
[ -z "$LAST_START" ] && exit 0

# Look for actual Telegram activity AFTER the last "starting provider":
# sendMessage, message processing, or successful getUpdates
HAS_ACTIVITY=$(tail -n +"$LAST_START" "$LOG_FILE" 2>/dev/null | grep -c 'sendMessage\|message_id\|telegram.*ok\|telegram.*message')
if [ "$HAS_ACTIVITY" -eq 0 ]; then
    echo "Telegram polling stuck for ${CONTAINER_UPTIME}s — sending SIGTERM to trigger restart"
    kill -TERM 1
    exit 1
fi

exit 0
