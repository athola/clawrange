"""Heartbeat watchdog tests.

The digest only flushes when a heartbeat prompt arrives, so a dead or
wedged OpenClaw is total silence on the phone. The watchdog runs inside
the workflows scheduler — independent of OpenClaw — and turns that
silence into a Telegram alert.
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import watchdog


@pytest.fixture(autouse=True)
def _operator_in_chicago(monkeypatch):
    """These cases are written in Chicago local time; the operator timezone
    is config (SCHEDULER_TZ / assistant.timezone), so pin it here."""
    monkeypatch.setenv("SCHEDULER_TZ", "America/Chicago")
    monkeypatch.delenv("HEARTBEAT_WATCH_TZ", raising=False)


CHICAGO = ZoneInfo("America/Chicago")


def _chicago(hour: int, minute: int = 0) -> datetime:
    """A datetime inside the watchdog's configured timezone."""
    # Fixed winter date: independent of the day the suite runs (no DST edge).
    return datetime(2026, 1, 15, hour, minute, tzinfo=CHICAGO)


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
        assert watchdog.stalled(None, _chicago(10).timestamp(), _chicago(10)) is True

    def test_recent_heartbeat_not_stalled(self):
        now_ts = _chicago(10).timestamp()
        assert watchdog.stalled(now_ts - 60, now_ts, _chicago(10)) is False

    def test_stalled_after_threshold(self):
        now_ts = _chicago(10).timestamp()
        assert watchdog.stalled(now_ts - 1000, now_ts, _chicago(10)) is True

    def test_silent_outside_active_hours(self):
        """20:00–08:00 local the heartbeat gate is legitimately closed:
        no heartbeats is normal, not an outage."""
        now_ts = _chicago(10).timestamp()
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
        base = _chicago(10).timestamp()
        action = self._run(isolated, base - 3600, base, notify=notify)
        assert action == "alerted"
        notify.assert_awaited_once()
        text = notify.await_args.args[0]
        assert "heartbeat" in text.lower()

    def test_throttles_repeat_alerts(self, isolated):
        notify = AsyncMock()
        base = _chicago(10).timestamp()
        self._run(isolated, base - 3600, base, notify=notify)
        # Still stalled minutes later: no second alert.
        action = self._run(isolated, base - 3700, base + 300, notify=notify)
        assert action is None
        notify.assert_awaited_once()

    def test_realerts_after_interval(self, isolated):
        notify = AsyncMock()
        base = _chicago(10).timestamp()
        self._run(isolated, base - 3600, base, notify=notify)
        # Two hours later, still stalled: nag again.
        action = self._run(isolated, base - 3600 - 7200, base + 7201, notify=notify)
        assert action == "realerted"
        assert notify.await_count == 2

    def test_recovery_notice_then_fresh_alert(self, isolated):
        notify = AsyncMock()
        base = _chicago(10).timestamp()
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
        base = _chicago(10).timestamp()
        action = self._run(isolated, base - 3600, base, hour=23, notify=notify)
        assert action is None
        notify.assert_not_awaited()

    def test_missing_seen_file_alerts(self, isolated):
        """No stamp at all (fresh deploy, misconfigured proxy path):
        that is a stall, not a pass."""
        notify = AsyncMock()
        action = asyncio.run(
            watchdog.check(
                now_ts=_chicago(10).timestamp(), now_dt=_chicago(10), notify=notify
            )
        )
        assert action == "alerted"

    def test_corrupt_state_file_does_not_raise(self, isolated):
        (isolated / "watchdog.json").write_text("{not json")
        notify = AsyncMock()
        base = _chicago(10).timestamp()
        action = self._run(isolated, base - 30, base, notify=notify)
        assert action is None  # healthy, nothing to say


class TestWindowOpenGrace:
    def test_no_alert_right_after_window_opens(self, isolated):
        """Last stamp was 20:00 yesterday; at 08:05 the gate just opened."""
        notify = AsyncMock()
        _write_seen(isolated / "seen.json", _chicago(20).timestamp() - 86400)
        now = _chicago(8, 5)
        action = asyncio.run(
            watchdog.check(now_ts=now.timestamp(), now_dt=now, notify=notify)
        )
        assert action is None
        notify.assert_not_awaited()

    def test_alert_once_stall_threshold_elapsed_since_opening(self, isolated):
        notify = AsyncMock()
        _write_seen(isolated / "seen.json", _chicago(20).timestamp() - 86400)
        now = _chicago(8, 20)
        action = asyncio.run(
            watchdog.check(now_ts=now.timestamp(), now_dt=now, notify=notify)
        )
        assert action == "alerted"
        text = notify.await_args.args[0]
        assert "720m" not in text and "20m" in text

    def test_missing_stamp_also_gets_grace(self, isolated):
        now = _chicago(8, 1)
        assert watchdog.stalled(None, now.timestamp(), now) is False
        later = _chicago(8, 16)
        assert watchdog.stalled(None, later.timestamp(), later) is True


class TestRecoveryAndPersistence:
    def test_undelivered_recovery_notice_keeps_alert_state(self, isolated):
        base = _chicago(10).timestamp()
        stalled_seen = base - 3600
        _write_seen(isolated / "seen.json", stalled_seen)
        asyncio.run(
            watchdog.check(now_ts=base, now_dt=_chicago(10), notify=AsyncMock())
        )
        _write_seen(isolated / "seen.json", base + 50)
        failing = AsyncMock(return_value=False)
        action = asyncio.run(
            watchdog.check(now_ts=base + 120, now_dt=_chicago(10), notify=failing)
        )
        assert action is None
        assert json.loads((isolated / "watchdog.json").read_text())["alerting"] is True
        # Next pass delivers it and clears state.
        ok = AsyncMock(return_value=True)
        action = asyncio.run(
            watchdog.check(now_ts=base + 420, now_dt=_chicago(10), notify=ok)
        )
        assert action == "recovered"
        assert json.loads((isolated / "watchdog.json").read_text())["alerting"] is False

    def test_write_json_is_atomic_replace(self, tmp_path, monkeypatch):
        import os

        target = tmp_path / "state.json"
        target.write_text('{"keep": 1}')
        calls = []
        real = os.replace
        monkeypatch.setattr(
            os, "replace", lambda a, b: (calls.append((a, b)), real(a, b))[1]
        )
        watchdog._write_json(str(target), {"x": 2})
        assert calls == [(f"{target}.tmp", str(target))]
        assert json.loads(target.read_text()) == {"x": 2}
        assert not (tmp_path / "state.json.tmp").exists()
