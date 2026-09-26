#!/usr/bin/env bash
# Supervisor for uvicorn --reload.
#
# Why this exists: the --reload supervisor keeps PID 1 alive even when
# the app crashes on import (for example, a syntax error lands via the
# bind-mounted source). Docker's restart policy only fires on process
# exit, so the container sat "up (unhealthy)" serving nothing forever
# and never recovered — even after the source was fixed, the wedged
# reloader kept limping along.
#
# What this does: start uvicorn in the background, poll /healthz, and
# exit non-zero after sustained failure so `restart: unless-stopped`
# brings the service back with a fresh process. Docker's restart backoff
# also makes the crashloop visible in `docker ps` instead of a silent
# zombie. A source fix is picked up by the next restart automatically.
set -u

PORT="${WORKFLOWS_PORT:-5678}"
GRACE_SECONDS="${HEALTH_GRACE_SECONDS:-30}"  # boot allowance before probing
MAX_FAILS="${HEALTH_MAX_FAILS:-6}"           # consecutive failures allowed
INTERVAL="${HEALTH_INTERVAL_SECONDS:-10}"    # probe cadence

probe() {
  python3 - "$PORT" <<'PY'
import sys
import urllib.request

try:
    urllib.request.urlopen(
        f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=3
    )
except Exception:
    sys.exit(1)
PY
}

uvicorn app:app --host 0.0.0.0 --port "$PORT" --reload &
UVICORN_PID=$!

# shellcheck disable=SC2317  # reached via trap, not linearly
shutdown() {
  kill "$UVICORN_PID" 2>/dev/null
  wait "$UVICORN_PID" 2>/dev/null
  exit 0
}
trap shutdown TERM INT

# Interruptible sleep: `wait` returns as soon as a trapped signal fires, so
# docker stop (SIGTERM) is handled immediately instead of after the interval.
do_sleep() {
  sleep "$1" &
  wait $!
}

do_sleep "$GRACE_SECONDS"

fails=0
while kill -0 "$UVICORN_PID" 2>/dev/null; do
  if probe; then
    fails=0
  else
    fails=$((fails + 1))
    echo "[entrypoint] healthz failed (${fails}/${MAX_FAILS})" >&2
    if [ "$fails" -ge "$MAX_FAILS" ]; then
      echo "[entrypoint] sustained failure — exiting so docker restarts us" >&2
      kill "$UVICORN_PID" 2>/dev/null
      wait "$UVICORN_PID" 2>/dev/null
      exit 1
    fi
  fi
  do_sleep "$INTERVAL"
done

# uvicorn exited on its own — mirror its exit status
wait "$UVICORN_PID"
exit $?
