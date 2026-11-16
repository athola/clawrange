"""Heartbeat watchdog: alert Telegram when OpenClaw heartbeats stall.

The digest is the only path from the heartbeat to Telegram, and it only
flushes when a heartbeat prompt arrives — so a wedged or dead OpenClaw
is total silence on the phone, indistinguishable from a quiet hour.
This job runs inside the workflows scheduler, independent of OpenClaw,
and turns that silence into an alert.

The proxy stamps every heartbeat arrival (digest.record_heartbeat_seen
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

import tenant_profile

logger = logging.getLogger("clawrange.watchdog")

# 15 min = 1.5 missed heartbeats at the 10-minute cadence: long enough
# that one slow cycle does not page, short enough to catch a lunchbreak.
STALL_SECS = float(os.getenv("HEARTBEAT_WATCH_STALL_SECS", "900"))
REALERT_SECS = float(os.getenv("HEARTBEAT_WATCH_REALERT_SECS", "7200"))


# Consecutive failed passes before the watchdog reports itself broken.
FAILED_PASS_ALERT = 3
_failed_passes = 0


def _valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ValueError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        return False
    return True


def _tz_name() -> str:
    """HEARTBEAT_WATCH_TZ, else the operator timezone from config.

    An invalid HEARTBEAT_WATCH_TZ falls back to the operator timezone (and
    that to UTC) instead of making every pass raise.
    """
    override = os.getenv("HEARTBEAT_WATCH_TZ", "").strip()
    if override and _valid_tz(override):
        return override
    operator = tenant_profile.operator_timezone()
    return operator if _valid_tz(operator) else "UTC"


_ACTIVE_START, _ACTIVE_END = (
    int(part) for part in os.getenv("HEARTBEAT_WATCH_ACTIVE", "8-20").split("-")
)


def _seen_path() -> str:
    return os.getenv("HEARTBEAT_SEEN_PATH", "/data/heartbeat_seen.json")


def _state_path() -> str:
    return os.getenv("HEARTBEAT_WATCH_STATE", "/data/heartbeat_watchdog.json")


def in_active_hours(now_dt: datetime) -> bool:
    """True inside the heartbeat gate OpenClaw runs (08:00-20:00 local)."""
    return tenant_profile.hour_in_window(now_dt, _ACTIVE_START, _ACTIVE_END, _tz_name())


def _window_open_ts(now_dt: datetime) -> float:
    """Epoch seconds when today's active window opened (local start hour)."""
    local = now_dt.astimezone(ZoneInfo(_tz_name()))
    return local.replace(
        hour=_ACTIVE_START, minute=0, second=0, microsecond=0
    ).timestamp()


def _silent_since(last_seen: float | None, now_dt: datetime) -> float:
    """Start of the current silence: the last heartbeat, but never earlier
    than the window opening. The gate was closed overnight, so a stamp from
    last evening is not 12 hours of missed heartbeats."""
    opened = _window_open_ts(now_dt)
    return opened if last_seen is None else max(float(last_seen), opened)


def stalled(last_seen: float | None, now_ts: float, now_dt: datetime) -> bool:
    """True when heartbeats should be arriving but are not.

    A missing stamp counts as stalled once the window has been open for
    STALL_SECS: on a fresh deploy or a broken proxy path, "never seen a
    heartbeat" is the outage. Right after the window opens, though, the
    first heartbeat has had no time to arrive, so nothing is flagged until
    a full STALL_SECS has elapsed since opening (the grace period).
    """
    if not in_active_hours(now_dt):
        return False
    return (now_ts - _silent_since(last_seen, now_dt)) >= STALL_SECS


def _read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: dict) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)  # atomic: a crash never leaves half a file
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
        now_dt = datetime.now(ZoneInfo(_tz_name()))
    if notify is None:
        from telegram import notify as telegram_notify

        notify = telegram_notify

    stamp = _read_json(_seen_path())
    arrived = stamp.get("ts")
    # The completion stamp is written at the end of a successful heartbeat;
    # a handler that raises keeps the arrival stamp fresh but not this one.
    # Files written before it existed only carry "ts".
    seen = stamp.get("done_ts") if "done_ts" in stamp else arrived
    state = _read_json(_state_path())
    last_alert = state.get("last_alert_ts")
    alerting = bool(state.get("alerting"))

    if not in_active_hours(now_dt):
        # The gate is closed: silence is expected and proves nothing either
        # way, so neither alert nor announce a recovery.
        return None

    if not stalled(seen, now_ts, now_dt):
        # Only a heartbeat newer than the alert is a recovery; "not stalled"
        # also covers the grace period right after the window opens.
        returned = seen is not None and (
            last_alert is None or float(seen) > float(last_alert)
        )
        if alerting and returned:
            delivered = await notify(
                "Heartbeat watchdog: heartbeats recovered — Telegram "
                "digest delivery should be back."
            )
            if not delivered:
                # Keep the alert state so the next pass retries the notice.
                logger.warning("watchdog: recovery notice not delivered; retrying")
                return None
            _write_json(_state_path(), {"alerting": False, "last_alert_ts": None})
            return "recovered"
        return None

    if alerting and last_alert is not None:
        if (now_ts - float(last_alert)) < REALERT_SECS:
            return None

    minutes = (
        None if seen is None else int((now_ts - _silent_since(seen, now_dt)) // 60)
    )
    hint = "check the OpenClaw container (docker logs msp-openclaw)"
    if seen is None and arrived is None:
        detail = "no heartbeat has ever been recorded"
    elif arrived is not None and (seen is None or float(arrived) > float(seen)):
        since = f" for {minutes}m" if minutes is not None else ""
        detail = (
            f"heartbeats arrive but have not completed{since} during active "
            "hours (the workflows heartbeat handler is failing)"
        )
        hint = "check the workflows container (docker logs msp-workflows)"
    else:
        detail = f"no heartbeat for {minutes}m during active hours"

    ok = await notify(
        f"Heartbeat watchdog: {detail}. Telegram digests are stalled — {hint}."
    )
    if ok:
        _write_json(_state_path(), {"alerting": True, "last_alert_ts": now_ts})
        return "realerted" if alerting else "alerted"
    # State unchanged, so the next pass retries the alert.
    logger.warning("watchdog: stall alert not delivered (%s); retrying", detail)
    return None


async def check_heartbeat() -> None:
    """Scheduler entrypoint: one pass, never raises.

    A pass that keeps failing would leave the operator with no watchdog at
    all, so FAILED_PASS_ALERT consecutive failures send one Telegram alert.
    """
    global _failed_passes
    try:
        action = await check()
    except Exception as exc:
        logger.exception("heartbeat watchdog pass failed")
        _failed_passes += 1
        if _failed_passes == FAILED_PASS_ALERT:
            from telegram import notify as telegram_notify

            try:
                await telegram_notify(
                    f"Heartbeat watchdog: {_failed_passes} passes in a row "
                    f"failed ({type(exc).__name__}: {str(exc)[:120]}). "
                    "Stalls are not being detected; check the workflows logs."
                )
            except Exception:
                logger.exception("watchdog failure alert could not be sent")
        return
    _failed_passes = 0
    if action:
        logger.info("heartbeat watchdog: %s", action)


def register(scheduler) -> None:
    """Register the 5-minute watchdog pass on a running scheduler.

    Validates HEARTBEAT_WATCH_TZ up front: a typo is logged here once, and
    the passes run on the fallback zone instead of raising every time.
    """
    override = os.getenv("HEARTBEAT_WATCH_TZ", "").strip()
    if override and not _valid_tz(override):
        logger.error(
            "HEARTBEAT_WATCH_TZ=%r is not a valid timezone; watchdog uses %s",
            override,
            _tz_name(),
        )
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
