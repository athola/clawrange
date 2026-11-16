"""Regression tests for the last open PR #11 review items (gate 2 mop-up).

Covers #21.g (crashed schedules reach the digest), #46.h (SearchHealth
streak is internal), the stale balance-unknown alert text, search denied by
the balance floor, and 0-row schedule status writes in the CRM generators.
"""

import asyncio
import dataclasses
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent

import digest
import generators
import heartbeat
import llm_proxy
import scheduler as sch
import telegram
from brain_db import BrainDB
from reddit_search import SearchHealth
from tenant_profile import Profile

FLOOR = llm_proxy.OPENROUTER_BALANCE_FLOOR


@pytest.fixture()
def db(tmp_path):
    d = BrainDB(str(tmp_path / "mop.db"))
    d.init_db()
    return d


def _balance(value):
    return patch.object(
        llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=value)
    )


# ─── #21.g: a crashed schedule leaves a digest line ─────────────────


class TestJobErrorDigest:
    def _event(self, job_id, exc):
        return JobExecutionEvent(
            EVENT_JOB_ERROR, job_id, "default", None, exception=exc
        )

    def test_tenant_crash_records_one_redacted_digest_line(self, db):
        db.upsert_schedule("s1", "S", "morning_scan", "0 9 * * *")
        exc = RuntimeError("GET https://x.example/a?token=secret failed")
        sch._job_error_listener(db)(self._event("marketing_s1", exc))
        lines = digest._digest_state["lines"]
        assert len(lines) == 1
        assert "s1" in lines[0] and "RuntimeError" in lines[0]
        assert "secret" not in lines[0]

    def test_digest_line_survives_a_status_write_failure(self, db):
        # No schedule row: the status write raises, the digest still hears.
        sch._job_error_listener(db)(self._event("catchup_gone", ValueError("x")))
        assert len(digest._digest_state["lines"]) == 1
        assert "gone" in digest._digest_state["lines"][0]

    def test_infra_job_crash_adds_no_schedule_line(self, db):
        sch._job_error_listener(db)(self._event("heartbeat_watchdog", ValueError()))
        assert digest._digest_state["lines"] == []


# ─── #46.h: the block streak is internal state ──────────────────────


def test_search_health_block_streak_is_not_an_init_arg():
    (f,) = [f for f in dataclasses.fields(SearchHealth) if f.name == "_block_streak"]
    assert f.init is False
    kwargs: dict[str, Any] = {"_block_streak": 3}
    with pytest.raises(TypeError):
        SearchHealth(**kwargs)
    assert SearchHealth()._block_streak == 0


# ─── Stale "floor check skipped" alert text ─────────────────────────


def test_unknown_balance_alert_says_paid_and_search_are_held():
    text = heartbeat._status_alert_result("Balance unknown alert: x", None)
    assert "floor check skipped" not in text
    assert "paid" in text and "search" in text


def test_balance_at_or_below_floor_is_not_reported_as_active(monkeypatch):
    """Above $0 but at or below the floor, paid and search are denied: the
    closing note must say so, not 'openrouter active'."""
    monkeypatch.setattr(llm_proxy, "OPENROUTER_BALANCE_FLOOR", 10.0)
    for remaining in (4.0, 10.0):
        text = heartbeat._status_alert_result("Low balance alert: x", remaining)
        assert "openrouter active" not in text
        assert "paid and search held" in text
    above = heartbeat._status_alert_result("Low balance alert: x", 25.0)
    assert "openrouter active" in above


# ─── Search denied by the balance floor is not an outage ────────────


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(llm_proxy, "_balance_status", {"ok_at": None, "error": None})


class TestSearchAllowed:
    @pytest.mark.parametrize("balance", [None, 3.0, FLOOR])
    def test_denied_when_unknown_or_at_floor(self, keys, balance):
        with _balance(balance):
            assert asyncio.run(llm_proxy.search_allowed()) is False

    def test_allowed_above_floor(self, keys):
        with _balance(FLOOR + 40):
            assert asyncio.run(llm_proxy.search_allowed()) is True

    def test_missing_key_is_not_a_floor_denial(self, monkeypatch):
        # No key: the search fails as an outage, not as the floor.
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with _balance(None):
            assert asyncio.run(llm_proxy.search_allowed()) is True


WEB_TASK = "Scan reddit for MSP pain points"


class TestSearchDeniedFallback:
    def _run(self, allowed, replies):
        call = AsyncMock(side_effect=replies)
        build = AsyncMock(return_value="P")
        with (
            patch.object(llm_proxy, "search_allowed", AsyncMock(return_value=allowed)),
            patch.object(llm_proxy, "_llm_call", call),
            patch.object(heartbeat, "_build_work_prompt", build),
        ):
            try:
                return asyncio.run(heartbeat._llm_work_task(WEB_TASK)), call, build
            except heartbeat.LLMUnavailable as exc:
                return exc, call, build

    def test_denied_then_fallback_succeeds(self):
        assert heartbeat._task_needs_web(WEB_TASK)
        result, call, build = self._run(False, [None, "Offline answer."])
        assert isinstance(result, str)
        assert result.endswith("Offline answer.")
        assert "no live web data" in result.lower()
        assert "balance floor" in result.lower()
        # The note must not make a finished result read as blocked.
        assert not heartbeat._needs_input(result)
        assert call.await_count == 2
        assert call.await_args_list[0].kwargs["web_search"] is True
        assert call.await_args_list[1].kwargs.get("web_search", False) is False
        # The fallback prompt is rebuilt without the search-only URL rules.
        assert build.await_args_list[-1].kwargs.get("web_search", False) is False

    def test_denied_then_fallback_fails(self):
        result, call, _ = self._run(False, [None, None])
        assert isinstance(result, heartbeat.LLMUnavailable)
        assert call.await_count == 2

    def test_allowed_search_outage_unchanged(self):
        result, call, _ = self._run(True, [None])
        assert isinstance(result, heartbeat.LLMUnavailable)
        assert call.await_count == 1


# ─── 0-row status writes in the CRM generators ──────────────────────


def _crm_profile():
    return Profile(
        name="t",
        raw={"profile": "t", "crm": {"adapter": "sqlite", "query_templates": []}},
    )


@pytest.fixture()
def crm(tmp_path):
    from crm.sqlite_adapter import SQLiteCRM

    c = SQLiteCRM(str(tmp_path / "crm.db"))
    c.init()
    return c


@pytest.fixture(autouse=True)
def quiet_telegram(monkeypatch):
    monkeypatch.setattr(telegram, "notify_long", AsyncMock(return_value=(1, 1)))


class TestGeneratorsSurviveMissingSchedule:
    def test_pipeline_unknown_connector_warns(self, db, caplog):
        with caplog.at_level(logging.WARNING, logger="clawrange.generators"):
            out = asyncio.run(
                generators.pipeline_generator(
                    db, connector="nope", profile=_crm_profile(), schedule_id="gone"
                )
            )
        assert out is None
        assert "could not update schedule status" in caplog.text

    def test_crm_digest_warns(self, db, crm, caplog):
        with caplog.at_level(logging.WARNING, logger="clawrange.generators"):
            out = asyncio.run(
                generators.crm_digest_generator(
                    db, queries=[], profile=_crm_profile(), crm=crm, schedule_id="gone"
                )
            )
        assert out is not None
        assert "could not update schedule status" in caplog.text

    def test_existing_schedule_still_gets_its_status(self, db):
        db.upsert_schedule("lead", "L", "pipeline", "0 * * * *")
        asyncio.run(
            generators.pipeline_generator(
                db, connector="nope", profile=_crm_profile(), schedule_id="lead"
            )
        )
        row = db.get_schedule("lead")
        assert row["last_status"] == "error (unknown connector nope)"
        assert row["last_run"]
