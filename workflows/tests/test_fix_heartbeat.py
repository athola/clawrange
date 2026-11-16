"""Regression tests for the PR #11 heartbeat, watchdog and digest findings."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import digest
import heartbeat
import llm_proxy
import watchdog
from errors import UpstreamError


@pytest.fixture()
def db():
    from app import brain_db

    return brain_db


@pytest.fixture()
def quiet(monkeypatch):
    """A heartbeat with known balance, no proactive layers due and no
    Telegram: only the behaviour under test runs."""
    monkeypatch.setattr(
        llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=50.0)
    )
    now = time.monotonic()
    heartbeat._proactive_state["stale_tasks"] = now
    heartbeat._proactive_state["llm_thinking"] = now
    monkeypatch.setattr(heartbeat, "_llm_retry_counts", {})
    monkeypatch.setattr(heartbeat, "_balance_unknown_since", None)
    monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))


def _beat() -> None:
    asyncio.run(heartbeat._handle_heartbeat(False))


def _backdate(db, task_id: str, column: str, hours: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    db._conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (stamp, task_id))
    db._conn.commit()


# ─── #23: web-search keywords match whole words ───────────────────


class TestWebKeywordsWordBoundary:
    @pytest.mark.parametrize(
        "desc",
        [
            "Ask John about the invoice",
            "Summarize technology choices for the homelab",
            "Review the brain entries for Johnson",
        ],
    )
    def test_hn_inside_a_word_is_not_web(self, desc):
        assert heartbeat._task_needs_web(desc) is False

    @pytest.mark.parametrize(
        "desc",
        [
            "Check HN for posts about sqlite",
            "What's new on hn today",
            "Search for skrills mentions",
            "Find online reviews",
            "Trending on x.com",
        ],
    )
    def test_whole_keywords_still_match(self, desc):
        assert heartbeat._task_needs_web(desc) is True

    @pytest.mark.parametrize(
        "desc",
        [
            # No research_router target word, so these reach the LLM; the
            # substring match used to give them web search via "search".
            "Research competitor pricing for the homelab NAS",
            "Keep researching rack options under $300",
            "Searching for a cheaper UPS",
        ],
    )
    def test_research_inflections_still_need_web(self, desc):
        assert heartbeat._task_needs_web(desc) is True


# ─── #26 / G5: LLM outage re-pends instead of completing ──────────


class TestLlmUnavailable:
    def test_work_task_raises_when_llm_unreachable(self):
        with (
            patch.object(llm_proxy, "_llm_call", AsyncMock(return_value=None)),
            patch.object(heartbeat, "_build_work_prompt", AsyncMock(return_value="P")),
        ):
            with pytest.raises(heartbeat.LLMUnavailable) as info:
                asyncio.run(heartbeat._llm_work_task("Send weekly invoice"))
        assert isinstance(info.value, UpstreamError)

    @pytest.mark.usefixtures("quiet")
    def test_outage_repends_task_with_one_digest_line(self, db):
        task = db.create_task("Summarize standup notes", source="user")
        with patch.object(llm_proxy, "_llm_call", AsyncMock(return_value=None)):
            _beat()
            _beat()
        after = db.get_task(task["id"])
        assert after["status"] == "pending"
        lines = [ln for ln in digest._digest_state["lines"] if task["id"] in ln]
        assert len(lines) == 1
        assert "Could not reach" not in (after["result"] or "")

    @pytest.mark.usefixtures("quiet")
    def test_outage_gives_up_after_retry_limit(self, db):
        task = db.create_task("Summarize standup notes")
        with patch.object(llm_proxy, "_llm_call", AsyncMock(return_value=None)):
            for _ in range(heartbeat.LLM_RETRY_LIMIT):
                _beat()
        after = db.get_task(task["id"])
        assert after["status"] == "failed"
        assert "LLM" in after["result"]

    @pytest.mark.usefixtures("quiet")
    def test_recovery_completes_and_resets_counter(self, db):
        task = db.create_task("Summarize standup notes")
        with patch.object(llm_proxy, "_llm_call", AsyncMock(return_value=None)):
            _beat()
        with patch.object(llm_proxy, "_llm_call", AsyncMock(return_value="done")):
            _beat()
        assert db.get_task(task["id"])["status"] == "completed"
        assert task["id"] not in heartbeat._llm_retry_counts

    @pytest.mark.usefixtures("quiet")
    def test_scanner_upstream_error_marks_failed(self, db):
        task = db.create_task("Scan reddit for skrills: pain points")
        with patch.object(
            heartbeat.research_router,
            "_try_marketing_scan",
            AsyncMock(side_effect=UpstreamError("reddit unreachable")),
        ):
            _beat()
        after = db.get_task(task["id"])
        assert after["status"] == "failed"
        assert "reddit unreachable" in after["result"]

    @pytest.mark.usefixtures("quiet")
    def test_research_upstream_error_marks_failed(self, db):
        """A plain UpstreamError from the research path is a failure, not
        an LLM outage: never re-pended."""
        task = db.create_task("Research sqlite-vec on GitHub")
        with (
            patch.object(
                heartbeat.research_router,
                "_try_marketing_scan",
                AsyncMock(return_value=None),
            ),
            patch.object(
                heartbeat.research_router,
                "_try_research_task",
                AsyncMock(side_effect=UpstreamError("all channels failed")),
            ),
        ):
            _beat()
        after = db.get_task(task["id"])
        assert after["status"] == "failed"
        assert "all channels failed" in after["result"]
        assert task["id"] not in heartbeat._llm_retry_counts

    @pytest.mark.usefixtures("quiet")
    def test_falsy_scanner_result_is_not_repended(self, db):
        """Only the LLM-unreachable path re-pends; an empty scan result is a
        result."""
        task = db.create_task("Scan reddit for skrills: pain points")
        with patch.object(
            heartbeat.research_router,
            "_try_marketing_scan",
            AsyncMock(return_value=""),
        ):
            _beat()
        assert db.get_task(task["id"])["status"] == "completed"


# ─── brain_db: release and reap ───────────────────────────────────


class TestTaskReleaseAndReap:
    def test_release_moves_active_back_to_pending(self, db):
        task = db.create_task("x")
        db.claim_task(task["id"])
        released = db.release_task(task["id"])
        assert released["status"] == "pending"
        assert released["claimed_at"] is None

    def test_release_rejects_non_active(self, db):
        task = db.create_task("x")
        with pytest.raises(ValueError):
            db.release_task(task["id"])

    def test_claim_stamps_claimed_at(self, db):
        task = db.create_task("x")
        assert db.claim_task(task["id"])["claimed_at"]

    def test_reaps_old_active_tasks(self, db):
        task = db.create_task("research:tome: old dig")
        db.claim_task(task["id"])
        _backdate(db, task["id"], "claimed_at", 30)
        reaped = db.fail_stale_active(hours=6)
        assert [t["id"] for t in reaped] == [task["id"]]
        assert db.get_task(task["id"])["status"] == "failed"

    def test_fresh_claim_on_old_task_is_not_reaped(self, db):
        """A task pending for days and claimed a minute ago is still running."""
        task = db.create_task("research:tome: slow dig")
        _backdate(db, task["id"], "created_at", 72)
        db.claim_task(task["id"])
        assert db.fail_stale_active(hours=6) == []
        assert db.get_task(task["id"])["status"] == "active"

    def test_legacy_active_without_claim_stamp_uses_created_at(self, db):
        task = db.create_task("research:tome: legacy")
        db._conn.execute(
            "UPDATE tasks SET status = 'active', claimed_at = NULL WHERE id = ?",
            (task["id"],),
        )
        db._conn.commit()
        _backdate(db, task["id"], "created_at", 30)
        assert [t["id"] for t in db.fail_stale_active(hours=6)] == [task["id"]]

    def test_migration_adds_claimed_at_to_old_schema(self, tmp_path):
        import sqlite3

        from brain_db import BrainDB

        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, description TEXT NOT NULL,"
            " priority INTEGER DEFAULT 3, status TEXT DEFAULT 'pending',"
            " source TEXT DEFAULT 'system', result TEXT, created_at TEXT NOT NULL,"
            " completed_at TEXT)"
        )
        conn.commit()
        conn.close()
        old = BrainDB(str(path))
        old.init_db()
        task = old.create_task("x")
        assert "claimed_at" in old.claim_task(task["id"])

    @pytest.mark.usefixtures("quiet")
    def test_heartbeat_reaps_with_digest_line(self, db):
        task = db.create_task("research:tome: stuck dig")
        db.claim_task(task["id"])
        _backdate(db, task["id"], "claimed_at", 30)
        _beat()
        assert db.get_task(task["id"])["status"] == "failed"
        assert any(
            task["id"] in ln and "active" in ln for ln in digest._digest_state["lines"]
        )


# ─── #35 / S9: the stale-task nudge is created once ───────────────


def _nudges(db) -> list[dict]:
    return [
        t
        for t in db.list_tasks()
        if t["description"].startswith(heartbeat.STALE_NUDGE_PREFIX)
    ]


class TestStaleNudgeDedup:
    @pytest.fixture(autouse=True)
    def _setup(self, quiet, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_LLM_SUGGEST", "0")

    def _scan(self):
        # Bypass the 30-minute timer so each heartbeat runs the stale scan.
        heartbeat._proactive_state.pop("stale_tasks", None)
        with patch.object(llm_proxy, "_llm_call", AsyncMock(return_value="Looked.")):
            _beat()

    def test_three_heartbeats_create_one_nudge(self, db):
        stale = db.create_task("research:tome: waits for the bridge")
        _backdate(db, stale["id"], "created_at", 5)
        self._scan()  # creates the nudge
        self._scan()  # works the nudge (heartbeat-owned)
        self._scan()  # scans again: must not re-create it
        assert len(_nudges(db)) == 1

    def test_nudge_does_not_count_itself(self, db):
        old_nudge = db.create_task(f"{heartbeat.STALE_NUDGE_PREFIX} 1 task(s) old")
        _backdate(db, old_nudge["id"], "created_at", 30)
        # The only stale pending task is a 30h-old nudge, past the dedup
        # window; it must not trigger a fresh nudge about itself.
        with patch.object(heartbeat, "_next_heartbeat_task", return_value=None):
            heartbeat._proactive_state.pop("stale_tasks", None)
            _beat()
        assert len(_nudges(db)) == 1


# ─── #51: tier-recovery alerts are per tier ───────────────────────


class TestAlertDedup:
    @pytest.mark.usefixtures("quiet")
    def test_each_tripped_tier_gets_its_own_alert(self, db, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_LLM_SUGGEST", "0")
        done = db.create_task("Investigate tier recovery: openrouter-free")
        db.claim_task(done["id"])
        db.complete_task(done["id"], "ok")
        monkeypatch.setattr(
            llm_proxy,
            "_circuit_open",
            lambda name, probe=True: name in {"openrouter-free", "zai-direct"},
        )
        _beat()
        descs = [t["description"] for t in db.list_tasks()]
        assert descs.count("Investigate tier recovery: zai-direct") == 1
        assert descs.count("Investigate tier recovery: openrouter-free") == 1


# ─── #30: unknown balance raises one alert ────────────────────────


class TestUnknownBalanceAlert:
    @pytest.fixture(autouse=True)
    def _setup(self, quiet, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_LLM_SUGGEST", "0")
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(
            llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            llm_proxy, "_balance_status", {"ok_at": None, "error": "HTTP 503"}
        )
        # Unreadable for longer than the alert threshold.
        monkeypatch.setattr(
            heartbeat,
            "_balance_unknown_since",
            time.monotonic() - heartbeat.BALANCE_UNKNOWN_ALERT_SECS - 1,
        )

    def test_brief_lookup_failure_does_not_alert(self, db, monkeypatch):
        monkeypatch.setattr(heartbeat, "_balance_unknown_since", None)
        with patch.object(heartbeat, "_next_heartbeat_task", return_value=None):
            _beat()
        assert self._alerts(db) == []

    def _alerts(self, db):
        return [
            t
            for t in db.list_tasks()
            if t["description"].lower().startswith("balance unknown alert:")
        ]

    def test_failed_lookup_creates_one_alert(self, db):
        with patch.object(heartbeat, "_next_heartbeat_task", return_value=None):
            _beat()
            _beat()
        alerts = self._alerts(db)
        assert len(alerts) == 1
        assert heartbeat._is_status_alert(alerts[0]["description"])

    def test_no_key_means_no_alert(self, db, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY")
        with patch.object(heartbeat, "_next_heartbeat_task", return_value=None):
            _beat()
        assert self._alerts(db) == []

    def test_unknown_alert_does_not_suppress_low_alert(self, db, monkeypatch):
        with patch.object(heartbeat, "_next_heartbeat_task", return_value=None):
            _beat()
            monkeypatch.setattr(
                llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=1.0)
            )
            _beat()
        assert any(
            t["description"].startswith("Low balance alert:") for t in db.list_tasks()
        )

    def test_status_alert_reports_lookup_error(self):
        text = heartbeat._status_alert_result("Balance unknown alert: x", None)
        assert "HTTP 503" in text

    def test_system_state_shows_lookup_error(self, db):
        text = asyncio.run(heartbeat._gather_system_state())
        assert "unknown (lookup failed: HTTP 503)" in text
        assert "not configured" not in text


# ─── #29 / #21: watchdog ──────────────────────────────────────────

CHICAGO = ZoneInfo("America/Chicago")


def _chicago(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=CHICAGO)


@pytest.fixture()
def wd(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEDULER_TZ", "America/Chicago")
    monkeypatch.delenv("HEARTBEAT_WATCH_TZ", raising=False)
    monkeypatch.setenv("HEARTBEAT_WATCH_STATE", str(tmp_path / "watchdog.json"))
    monkeypatch.setenv("HEARTBEAT_SEEN_PATH", str(tmp_path / "seen.json"))
    monkeypatch.setattr(watchdog, "_failed_passes", 0, raising=False)
    return tmp_path


def _seen(tmp_path, **stamp) -> None:
    (tmp_path / "seen.json").write_text(json.dumps(stamp))


def _check(dt: datetime, notify) -> str | None:
    return asyncio.run(watchdog.check(now_ts=dt.timestamp(), now_dt=dt, notify=notify))


def _state(tmp_path) -> dict:
    return json.loads((tmp_path / "watchdog.json").read_text())


class TestWatchdogRecovery:
    def test_window_close_is_not_a_recovery(self, wd):
        """OpenClaw died at 14:00, alert at 14:30; at 20:00 the gate closes
        but OpenClaw is still down: no 'recovered' notice."""
        dead = _chicago(14).timestamp()
        _seen(wd, ts=dead, done_ts=dead)
        notify = AsyncMock(return_value=True)
        assert _check(_chicago(14, 30), notify) == "alerted"
        assert _check(_chicago(20, 5), notify) is None
        assert notify.await_count == 1
        assert _state(wd)["alerting"] is True

    def test_next_morning_without_heartbeat_is_not_a_recovery(self, wd):
        dead = _chicago(14).timestamp()
        _seen(wd, ts=dead, done_ts=dead)
        notify = AsyncMock(return_value=True)
        _check(_chicago(14, 30), notify)
        # 08:05 next day: inside the grace period, still no heartbeat.
        assert _check(_chicago(8, 5, day=16), notify) is None
        assert _state(wd)["alerting"] is True
        # Still dead after the grace: re-alert (yesterday's alert is old).
        assert _check(_chicago(8, 20, day=16), notify) == "realerted"

    def test_heartbeat_after_alert_recovers(self, wd):
        dead = _chicago(14).timestamp()
        _seen(wd, ts=dead, done_ts=dead)
        notify = AsyncMock(return_value=True)
        _check(_chicago(14, 30), notify)
        back = _chicago(14, 40).timestamp()
        _seen(wd, ts=back, done_ts=back)
        assert _check(_chicago(14, 45), notify) == "recovered"


class TestWatchdogCompletionStamp:
    def test_arrivals_without_completion_alert(self, wd):
        """Heartbeats arrive but the handler raises every time: the
        arrival stamp is fresh, the completion stamp is not."""
        now = _chicago(12)
        _seen(wd, ts=now.timestamp() - 60, done_ts=now.timestamp() - 3600)
        notify = AsyncMock(return_value=True)
        assert _check(now, notify) == "alerted"
        text = notify.await_args.args[0]
        assert "not completed" in text
        assert "msp-workflows" in text and "msp-openclaw" not in text

    def test_legacy_stamp_without_done_ts_uses_arrival(self, wd):
        now = _chicago(12)
        _seen(wd, ts=now.timestamp() - 60)
        assert _check(now, AsyncMock(return_value=True)) is None

    def test_arrival_stamp_keeps_completion_stamp(self, wd):
        digest.record_heartbeat_done(now=100.0)
        digest.record_heartbeat_seen(now=200.0)
        data = json.loads((wd / "seen.json").read_text())
        assert data == {"ts": 200.0, "done_ts": 100.0}

    @pytest.mark.usefixtures("quiet")
    def test_handler_stamps_completion(self, wd):
        _beat()
        data = json.loads((wd / "seen.json").read_text())
        assert data["done_ts"] is not None

    @pytest.mark.usefixtures("quiet")
    def test_handler_failure_leaves_completion_stale(self, wd, monkeypatch):
        monkeypatch.setattr(
            digest, "_digest_due", lambda now=None: (_ for _ in ()).throw(OSError())
        )
        with pytest.raises(OSError):
            _beat()
        data = json.loads((wd / "seen.json").read_text())
        assert data.get("done_ts") is None
        assert data["ts"] is not None


class TestWatchdogAlertDelivery:
    def test_first_alert_is_delivered(self, wd):
        _seen(wd, ts=_chicago(9).timestamp(), done_ts=_chicago(9).timestamp())
        notify = AsyncMock(return_value=True)
        assert _check(_chicago(10), notify) == "alerted"
        notify.assert_awaited_once()
        assert "no heartbeat for 60m" in notify.await_args.args[0]
        assert _state(wd) == {
            "alerting": True,
            "last_alert_ts": _chicago(10).timestamp(),
        }

    def test_failed_alert_is_logged_and_retried(self, wd, caplog):
        _seen(wd, ts=_chicago(9).timestamp(), done_ts=_chicago(9).timestamp())
        with caplog.at_level(logging.WARNING, logger="clawrange.watchdog"):
            assert _check(_chicago(10), AsyncMock(return_value=False)) is None
        assert "not delivered" in caplog.text
        assert _check(_chicago(10, 5), AsyncMock(return_value=True)) == "alerted"


class TestWatchdogTimezone:
    def test_bad_watch_tz_falls_back_at_register(self, wd, monkeypatch, caplog):
        monkeypatch.setenv("HEARTBEAT_WATCH_TZ", "America/Denvr")
        scheduler = MagicMock()
        with caplog.at_level(logging.ERROR, logger="clawrange.watchdog"):
            watchdog.register(scheduler)
        scheduler.add_job.assert_called_once()
        assert "America/Denvr" in caplog.text
        assert watchdog._tz_name() == "America/Chicago"

    def test_watch_tz_unset_uses_operator_timezone(self, wd):
        assert watchdog._tz_name() == "America/Chicago"

    def test_valid_watch_tz_overrides_operator_timezone(self, wd, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_WATCH_TZ", "Europe/Berlin")
        assert watchdog._tz_name() == "Europe/Berlin"

    def test_bad_watch_tz_and_bad_operator_tz_fall_back_to_utc(self, wd, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_WATCH_TZ", "Not/AZone")
        monkeypatch.setenv("SCHEDULER_TZ", "Also/NotAZone")
        assert watchdog._tz_name() == "UTC"

    def test_bad_watch_tz_does_not_break_a_pass(self, wd, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_WATCH_TZ", "Not/AZone")
        _seen(wd, ts=time.time(), done_ts=time.time())
        assert asyncio.run(watchdog.check(notify=AsyncMock())) is None

    def test_alert_after_repeated_failed_passes(self, wd, monkeypatch):
        monkeypatch.setattr(watchdog, "check", AsyncMock(side_effect=RuntimeError("x")))
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify)
        for _ in range(watchdog.FAILED_PASS_ALERT):
            asyncio.run(watchdog.check_heartbeat())
        notify.assert_awaited_once()
        assert "watchdog" in notify.await_args.args[0].lower()
        # Further failures do not repeat the alert.
        asyncio.run(watchdog.check_heartbeat())
        notify.assert_awaited_once()

    def test_successful_pass_resets_failure_count(self, wd, monkeypatch):
        monkeypatch.setattr(watchdog, "_failed_passes", 2)
        monkeypatch.setattr(watchdog, "check", AsyncMock(return_value=None))
        asyncio.run(watchdog.check_heartbeat())
        assert watchdog._failed_passes == 0


# ─── #51 / #15: digest state ──────────────────────────────────────


@pytest.fixture()
def fresh_digest(monkeypatch, tmp_path):
    path = tmp_path / "digest.json"
    monkeypatch.setenv("HEARTBEAT_DIGEST_STATE", str(path))
    monkeypatch.setattr(digest, "_digest_state", {"lines": [], "last_flush": None})
    return path


class TestDigestState:
    def test_corrupt_state_is_kept_aside(self, fresh_digest, capsys):
        fresh_digest.write_text('{"lines": ["undelivered item"')
        digest._digest_record("new item")
        aside = fresh_digest.with_name(fresh_digest.name + ".corrupt")
        assert "undelivered item" in aside.read_text()
        assert "corrupt" in capsys.readouterr().out
        assert digest._digest_state["lines"] == ["new item"]

    def test_bad_clock_keeps_lines(self, fresh_digest):
        fresh_digest.write_text(json.dumps({"lines": ["kept"], "last_flush": "x"}))
        digest._digest_load()
        assert digest._digest_state["lines"] == ["kept"]

    def test_failed_sends_cannot_grow_the_buffer_forever(
        self, fresh_digest, monkeypatch
    ):
        """Telegram unset or failing: every flush re-buffers everything."""
        monkeypatch.setattr(digest, "HEARTBEAT_DIGEST_MAX_LINES", 5)
        for i in range(8):
            digest._digest_record(f"item {i}")
        _msgs, _text, owners, taken = digest._digest_take_ex(time.time(), None, [])
        digest._digest_commit(taken, owners, set(range(len(owners))))
        lines = digest._digest_state["lines"]
        assert len(lines) == 5
        assert "4 older digest item(s) dropped" in lines[0]
        assert lines[1:] == ["item 4", "item 5", "item 6", "item 7"]
        assert lines[-1] == "item 7"
        # A second failed flush folds the old marker into the new count.
        for i in range(8, 10):
            digest._digest_record(f"item {i}")
        _msgs, _text, owners, taken = digest._digest_take_ex(time.time(), None, [])
        digest._digest_commit(taken, owners, set(range(len(owners))))
        lines = digest._digest_state["lines"]
        assert len(lines) == 5
        assert "6 older digest item(s) dropped" in lines[0]
        assert lines[-1] == "item 9"

    def test_footer_shows_unknown_balance(self, fresh_digest):
        digest._digest_record("item")
        _m, text, _o, _t = digest._digest_take_ex(
            time.time(), None, [], balance="unknown (lookup failed: HTTP 503)"
        )
        assert "OpenRouter balance: unknown (lookup failed: HTTP 503)" in text

    def test_footer_omits_unconfigured_balance(self, fresh_digest):
        digest._digest_record("item")
        _m, text, _o, _t = digest._digest_take_ex(
            time.time(), None, [], balance="not configured"
        )
        assert "OpenRouter balance" not in text
