"""Heartbeat watchdog: alert Telegram when OpenClaw heartbeats stall.

The digest is the only path from the heartbeat to Telegram, and it only
flushes when a heartbeat prompt arrives — so a wedged or dead OpenClaw
is total silence on the phone, indistinguishable from a quiet hour.
This job runs inside the workflows scheduler, independent of OpenClaw,
and turns that silence into an alert.

The proxy stamps every heartbeat arrival (llm_proxy.record_heartbeat_seen
-> HEARTBEAT_SEEN_PATH). This module reads the stamp and compares it
against the heartbeat cadence OpenClaw is configured for (every 10m,
active hours 08:00-20:00 in agents.defaults.userTimezone).

Alert policy: one alert when the stall is first seen, a re-alert every
HEARTBEAT_WATCH_REALERT_SECS while it continues, one recovery notice
when heartbeats return. Outside active hours the gate is legitimately
closed, so a stale stamp is not an outage.
"""

import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("clawrange.watchdog")

# 15 min = 1.5 missed heartbeats at the 10-minute cadence: long enough
# that one slow cycle does not page, short enough to catch a lunchbreak.
STALL_SECS = float(os.getenv("HEARTBEAT_WATCH_STALL_SECS", "900"))
REALERT_SECS = float(os.getenv("HEARTBEAT_WATCH_REALERT_SECS", "7200"))
TZ_NAME = os.getenv("HEARTBEAT_WATCH_TZ", "America/Chicago")
_ACTIVE_START, _ACTIVE_END = (
    int(part) for part in os.getenv("HEARTBEAT_WATCH_ACTIVE", "8-20").split("-")
)


def _seen_path() -> str:
    return os.getenv("HEARTBEAT_SEEN_PATH", "/data/heartbeat_seen.json")


def _state_path() -> str:
    return os.getenv("HEARTBEAT_WATCH_STATE", "/data/heartbeat_watchdog.json")


def in_active_hours(now_dt: datetime) -> bool:
    """True inside the heartbeat gate OpenClaw runs (08:00-20:00 local)."""
    local = now_dt.astimezone(ZoneInfo(TZ_NAME))
    return _ACTIVE_START <= local.hour < _ACTIVE_END


def stalled(last_seen: float | None, now_ts: float, now_dt: datetime) -> bool:
    """True when heartbeats should be arriving but are not.

    A missing stamp counts as stalled: on a fresh deploy or a broken
    proxy path, "never seen a heartbeat" is the outage, not a pass.
    """
    if not in_active_hours(now_dt):
        return False
    if last_seen is None:
        return True
    return (now_ts - last_seen) >= STALL_SECS


def _read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: dict) -> None:
    try:
        with open(path, "w") as fh:
            json.dump(data, fh)
    except OSError as exc:
        logger.warning("watchdog state write failed (%s): %s", path, exc)


async def check(
    now_ts: float | None = None,
    now_dt: datetime | None = None,
    notify: Callable[[str], Awaitable[bool]] | None = None,
) -> str | None:
    """One watchdog pass. Returns the action taken, or None.

    Actions: "alerted" (stall first seen), "realerted" (still stalled
    past the re-alert interval), "recovered" (heartbeats returned after
    an alert). The clock and the notifier are injectable for tests.
    """
    if now_ts is None:
        now_ts = time.time()
    if now_dt is None:
        now_dt = datetime.now(ZoneInfo(TZ_NAME))
    if notify is None:
        from telegram import notify as telegram_notify

        notify = telegram_notify

    seen = _read_json(_seen_path()).get("ts")
    state = _read_json(_state_path())
    last_alert = state.get("last_alert_ts")
    alerting = bool(state.get("alerting"))

    if not stalled(seen, now_ts, now_dt):
        if alerting:
            await notify(
                "Heartbeat watchdog: heartbeats recovered — Telegram "
                "digest delivery should be back."
            )
            _write_json(_state_path(), {"alerting": False, "last_alert_ts": None})
            return "recovered"
        return None

    if alerting and last_alert is not None:
        if (now_ts - float(last_alert)) < REALERT_SECS:
            return None

    if seen is None:
        detail = "no heartbeat has ever been recorded"
    else:
        minutes = int((now_ts - float(seen)) // 60)
        detail = f"no heartbeat for {minutes}m during active hours"

    ok = await notify(
        f"Heartbeat watchdog: {detail}. Telegram digests are stalled — "
        "check the OpenClaw container (docker logs msp-openclaw)."
    )
    if ok:
        _write_json(_state_path(), {"alerting": True, "last_alert_ts": now_ts})
        return "realerted" if alerting else "alerted"
    return None


async def check_heartbeat() -> None:
    """Scheduler entrypoint: one pass, never raises."""
    try:
        action = await check()
        if action:
            logger.info("heartbeat watchdog: %s", action)
    except Exception:
        logger.exception("heartbeat watchdog pass failed")


def register(scheduler) -> None:
    """Register the 5-minute watchdog pass on a running scheduler."""
    scheduler.add_job(
        check_heartbeat,
        "interval",
        minutes=5,
        id="heartbeat_watchdog",
        replace_existing=True,
    )


__all__ = [
    "check",
    "check_heartbeat",
    "in_active_hours",
    "register",
    "stalled",
]
