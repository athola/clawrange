"""Heartbeat watchdog tests.

The digest only flushes when a heartbeat prompt arrives, so a dead or
wedged OpenClaw is total silence on the phone. The watchdog runs inside
the workflows scheduler — independent of OpenClaw — and turns that
silence into a Telegram alert.
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import watchdog

CHICAGO = ZoneInfo("America/Chicago")


def _chicago(hour: int, minute: int = 0) -> datetime:
    """A datetime inside the watchdog's configured timezone."""
    now = datetime.now(UTC)
    local = now.astimezone(CHICAGO).replace(hour=hour, minute=minute, second=0)
    return local


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Point the watchdog at temp state files and a no-op clock."""
    monkeypatch.setenv("HEARTBEAT_WATCH_STATE", str(tmp_path / "watchdog.json"))
    monkeypatch.setenv("HEARTBEAT_SEEN_PATH", str(tmp_path / "seen.json"))
    return tmp_path


def _write_seen(path, ts: float | None) -> None:
    path.write_text(json.dumps({"ts": ts}))


# ─── Stall decision ───────────────────────────────────────────────


class TestStallDecision:
    def test_never_seen_is_stalled(self):
        assert watchdog.stalled(None, time.time(), _chicago(10)) is True

    def test_recent_heartbeat_not_stalled(self):
        now_ts = time.time()
        assert watchdog.stalled(now_ts - 60, now_ts, _chicago(10)) is False

    def test_stalled_after_threshold(self):
        now_ts = time.time()
        assert watchdog.stalled(now_ts - 1000, now_ts, _chicago(10)) is True

    def test_silent_outside_active_hours(self):
        """20:00–08:00 local the heartbeat gate is legitimately closed:
        no heartbeats is normal, not an outage."""
        now_ts = time.time()
        assert watchdog.stalled(now_ts - 1000, now_ts, _chicago(21)) is False
        assert watchdog.stalled(now_ts - 1000, now_ts, _chicago(5)) is False

    def test_active_hours_boundaries(self):
        assert watchdog.in_active_hours(_chicago(8)) is True
        assert watchdog.in_active_hours(_chicago(19, 59)) is True
        assert watchdog.in_active_hours(_chicago(20)) is False
        assert watchdog.in_active_hours(_chicago(7, 59)) is False


# ─── One watchdog pass ────────────────────────────────────────────


class TestCheck:
    """check() takes an injected clock (now_ts, now_dt) so the alert
    throttle and recovery transitions are deterministic."""

    def _run(self, tmp_path, seen_ts, now_ts, hour=10, notify=None):
        _write_seen(tmp_path / "seen.json", seen_ts)
        return asyncio.run(
            watchdog.check(
                now_ts=now_ts,
                now_dt=_chicago(hour),
                notify=notify or AsyncMock(),
            )
        )

    def test_alerts_on_stall(self, isolated):
        notify = AsyncMock()
        base = time.time()
        action = self._run(isolated, base - 3600, base, notify=notify)
        assert action == "alerted"
        notify.assert_awaited_once()
        text = notify.await_args.args[0]
        assert "heartbeat" in text.lower()

    def test_throttles_repeat_alerts(self, isolated):
        notify = AsyncMock()
        base = time.time()
        self._run(isolated, base - 3600, base, notify=notify)
        # Still stalled minutes later: no second alert.
        action = self._run(isolated, base - 3700, base + 300, notify=notify)
        assert action is None
        notify.assert_awaited_once()

    def test_realerts_after_interval(self, isolated):
        notify = AsyncMock()
        base = time.time()
        self._run(isolated, base - 3600, base, notify=notify)
        # Two hours later, still stalled: nag again.
        action = self._run(isolated, base - 3600 - 7200, base + 7201, notify=notify)
        assert action == "realerted"
        assert notify.await_count == 2

    def test_recovery_notice_then_fresh_alert(self, isolated):
        notify = AsyncMock()
        base = time.time()
        self._run(isolated, base - 3600, base, notify=notify)
        # Heartbeats return at base+50; next pass sees a fresh stamp:
        # one recovery notice, state cleared.
        action = self._run(isolated, base + 50, base + 120, notify=notify)
        assert action == "recovered"
        assert notify.await_count == 2
        # It stalls again right after: alert immediately, not throttled
        # by the previous outage.
        action = self._run(isolated, base + 50, base + 3650, notify=notify)
        assert action == "alerted"
        assert notify.await_count == 3

    def test_quiet_outside_active_hours(self, isolated):
        notify = AsyncMock()
        base = time.time()
        action = self._run(isolated, base - 3600, base, hour=23, notify=notify)
        assert action is None
        notify.assert_not_awaited()

    def test_missing_seen_file_alerts(self, isolated):
        """No stamp at all (fresh deploy, misconfigured proxy path):
        that is a stall, not a pass."""
        notify = AsyncMock()
        action = asyncio.run(
            watchdog.check(now_ts=time.time(), now_dt=_chicago(10), notify=notify)
        )
        assert action == "alerted"

    def test_corrupt_state_file_does_not_raise(self, isolated):
        (isolated / "watchdog.json").write_text("{not json")
        notify = AsyncMock()
        base = time.time()
        action = self._run(isolated, base - 30, base, notify=notify)
        assert action is None  # healthy, nothing to say
