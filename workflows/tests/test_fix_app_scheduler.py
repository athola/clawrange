"""Regression tests for the PR #11 review findings in app.py and scheduler.py.

Covers #21 (scheduler startup), #22 / S1 (docs routes and the auth sweep),
#25 (schedule status), #28 (/healthz), #26 (scanner 502s), #30 (balance
display), #46 (task status types), #48/#49/#51 (cleanups), G4, G6, #15.
"""

import inspect
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app as app_module
import auth
import generators
import scheduler as sch
from brain_db import BrainDB
from errors import UpstreamError

client = TestClient(app_module.app)
APP_SRC = Path(app_module.__file__).read_text()


@pytest.fixture()
def db(tmp_path):
    d = BrainDB(str(tmp_path / "k4.db"))
    d.init_db()
    return d


@pytest.fixture()
def fresh_state(monkeypatch):
    """Isolate the scheduler module's recorded startup state."""
    monkeypatch.setattr(sch, "_STATE", sch._new_state())
    yield sch._STATE


# ─── #21 scheduler startup ────────────────────────────────────────


@pytest.mark.asyncio
class TestInitScheduler:
    async def test_bad_timezone_still_starts_with_utc(
        self, db, monkeypatch, fresh_state, caplog
    ):
        monkeypatch.setenv("SCHEDULER_TZ", "America/Denvr")
        db.upsert_schedule("s1", "S", "morning_scan", "0 9 * * *")
        with caplog.at_level(logging.ERROR, logger="clawrange.scheduler"):
            s = sch.init_scheduler(db)
        try:
            assert s is not None and s.running
            assert str(s.timezone) == "UTC"
            assert s.get_job("marketing_s1") is not None
            assert s.get_job("heartbeat_watchdog") is not None
            state = sch.scheduler_state()
            assert state["status"] == "degraded"
            assert any("America/Denvr" in e for e in state["errors"])
            assert any("America/Denvr" in r.getMessage() for r in caplog.records)
        finally:
            s.shutdown(wait=False)

    async def test_bad_watchdog_timezone_reported(self, db, monkeypatch, fresh_state):
        monkeypatch.setenv("HEARTBEAT_WATCH_TZ", "Mars/Olympus")
        s = sch.init_scheduler(db)
        try:
            assert s is not None
            assert any("Mars/Olympus" in e for e in sch.scheduler_state()["errors"])
        finally:
            s.shutdown(wait=False)

    async def test_watchdog_survives_schedule_listing_failure(
        self, db, monkeypatch, fresh_state
    ):
        def boom():
            raise RuntimeError("schedules table broken")

        monkeypatch.setattr(db, "list_schedules", boom)
        s = sch.init_scheduler(db)
        try:
            assert s is not None
            assert s.get_job("heartbeat_watchdog") is not None
            assert s.get_job("maintenance_embed_backfill") is not None
            state = sch.scheduler_state()
            assert state["status"] == "degraded"
            assert any("schedules table broken" in e for e in state["errors"])
        finally:
            s.shutdown(wait=False)

    async def test_healthy_start_reports_running(self, db, fresh_state):
        s = sch.init_scheduler(db)
        try:
            state = sch.scheduler_state()
            assert state["status"] == "running"
            assert state["errors"] == []
        finally:
            s.shutdown(wait=False)

    async def test_start_failure_reports_failed(self, db, monkeypatch, fresh_state):
        def no_start(self, *a, **k):
            raise RuntimeError("no event loop")

        monkeypatch.setattr(AsyncIOScheduler, "start", no_start)
        assert sch.init_scheduler(db) is None
        state = sch.scheduler_state()
        assert state["status"] == "failed"
        assert any("no event loop" in e for e in state["errors"])

    async def test_job_error_listener_registered(self, db, fresh_state):
        s = sch.init_scheduler(db)
        try:
            assert any(mask & EVENT_JOB_ERROR for _cb, mask in s._listeners), (
                "no EVENT_JOB_ERROR listener"
            )
        finally:
            s.shutdown(wait=False)


class TestJobErrorListener:
    def _event(self, job_id, exc):
        return JobExecutionEvent(
            EVENT_JOB_ERROR, job_id, "default", None, exception=exc
        )

    def test_records_error_for_tenant_job(self, db):
        db.upsert_schedule("s1", "S", "morning_scan", "0 9 * * *")
        sch._job_error_listener(db)(self._event("marketing_s1", ValueError("boom")))
        row = db.get_schedule("s1")
        assert row["last_status"].startswith("error")
        assert "boom" in row["last_status"]
        assert sch.status_kind(row["last_status"]) == "error"

    def test_records_error_for_catch_up_job(self, db):
        db.upsert_schedule("s2", "S", "morning_scan", "0 9 * * *")
        sch._job_error_listener(db)(self._event("catchup_s2", RuntimeError("x")))
        assert sch.status_kind(db.get_schedule("s2")["last_status"]) == "error"

    def test_redacts_urls(self, db):
        db.upsert_schedule("s3", "S", "morning_scan", "0 9 * * *")
        exc = RuntimeError("GET https://x.example/a?token=secret failed")
        sch._job_error_listener(db)(self._event("marketing_s3", exc))
        assert "secret" not in db.get_schedule("s3")["last_status"]

    def test_infra_job_error_does_not_raise(self, db):
        sch._job_error_listener(db)(self._event("heartbeat_watchdog", ValueError()))


@pytest.mark.asyncio
class TestStartupAlert:
    async def test_lifespan_alerts_on_scheduler_failure(self, monkeypatch):
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr(app_module, "notify", notify)
        monkeypatch.setattr(app_module, "init_profile", lambda *a, **k: None)
        monkeypatch.setattr(app_module, "init_scheduler", lambda _db: None)
        monkeypatch.setattr(
            sch,
            "_STATE",
            {"status": "failed", "errors": ["start: boom"], "timezone": "UTC"},
        )
        async with app_module.lifespan(FastAPI()):
            pass
        notify.assert_awaited_once()
        assert "boom" in notify.await_args.args[0]

    async def test_lifespan_quiet_when_healthy(self, monkeypatch):
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr(app_module, "notify", notify)
        monkeypatch.setattr(app_module, "init_profile", lambda *a, **k: None)
        monkeypatch.setattr(app_module, "init_scheduler", lambda _db: None)
        monkeypatch.setattr(
            sch, "_STATE", {"status": "running", "errors": [], "timezone": "UTC"}
        )
        async with app_module.lifespan(FastAPI()):
            pass
        notify.assert_not_awaited()


# ─── #15 catch-up loop ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_kind_does_not_abort_other_catch_ups(db, monkeypatch):
    monkeypatch.setattr(sch, "catch_up_ids", lambda: {"bad", "good"})
    monkeypatch.setattr(sch, "_missed_fire", lambda *a: object.__new__(_Fire))
    schedules = [
        {"id": "bad", "kind": "no_such_kind", "cron": "0 8 * * *"},
        {"id": "good", "kind": "morning_scan", "cron": "0 8 * * *"},
    ]
    s = AsyncIOScheduler(timezone="UTC")
    s.start()
    try:
        sch._queue_catch_ups(s, schedules, db, "UTC")
        job = s.get_job("catchup_good")
        assert job is not None
        assert job.kwargs["schedule_id"] == "good"
        assert s.get_job("catchup_bad") is None
    finally:
        s.shutdown(wait=False)


class _Fire:
    def isoformat(self):
        return "2026-11-15T08:00:00"


def test_catch_up_retries_any_delivery_failure():
    """A pipeline 'ok (...; telegram FAILED ...)' row is a failed delivery."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    sched = {
        "cron": "0 8 * * *",
        "last_run": (now - timedelta(minutes=30)).isoformat(),
        "last_status": "ok (3 written; telegram FAILED 0/1 msgs)",
    }
    assert sch._missed_fire(sched, now, "UTC") is not None
    sched["last_status"] = "error: boom"
    assert sch._missed_fire(sched, now, "UTC") is None


# ─── #25 status_kind and schedule ids ────────────────────────────


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("ok", "ok"),
        ("delivered 2/2 msgs (5 items)", "ok"),
        ("ok (3 picks; 4 reqs)", "ok"),
        ("throttled (1.0m of 15m)", "ok"),
        ("quiet hours (entered at 23:00 UTC, resume 07:00)", "ok"),
        ("FAILED telegram 1/3 msgs (x)", "delivery_failed"),
        ("ok (3 written; telegram FAILED 0/1 msgs)", "delivery_failed"),
        ("error: boom", "error"),
        ("error (no crm)", "error"),
        ("error (unknown connector x)", "error"),
        ("reddit outage, 0 picks (8 reqs, 8 failed)", "error"),
    ],
)
def test_status_kind(text, kind):
    assert sch.status_kind(text) == kind


def test_public_sched_view_reports_errors_as_failed():
    view = app_module._schedule_view({"id": "x", "last_status": "error: boom"}, False)
    assert view["last_status"] == "failed"
    view = app_module._schedule_view(
        {"id": "x", "last_status": "reddit outage, 0 picks (n)"}, False
    )
    assert view["last_status"] == "failed"


def test_update_schedule_status_raises_on_unknown_id(db):
    with pytest.raises(ValueError, match="nope"):
        db.update_schedule_status("nope", "2026-11-15T09:00:00", "ok")


@pytest.mark.asyncio
async def test_register_job_injects_schedule_id(db):
    s = AsyncIOScheduler(timezone="UTC")
    s.start()
    try:
        sched = {"id": "pulse-2", "kind": "hot_pulse", "cron": "every 15m"}
        sch._register_job(s, sched, db)
        job = s.get_job("marketing_pulse-2")
        assert job.kwargs["schedule_id"] == "pulse-2"
    finally:
        s.shutdown(wait=False)


@pytest.mark.asyncio
async def test_register_job_bad_kwargs_json_does_not_raise(db):
    s = AsyncIOScheduler(timezone="UTC")
    s.start()
    try:
        sch._register_job(
            s,
            {"id": "b", "kind": "morning_scan", "cron": "0 9 * * *", "kwargs": "{"},
            db,
        )
        assert s.get_job("marketing_b") is None
    finally:
        s.shutdown(wait=False)


@pytest.mark.asyncio
class TestRunScheduleNow:
    async def _run(self, db, monkeypatch, gen):
        monkeypatch.setitem(sch.GENERATORS, "k4_fake", gen)
        db.upsert_schedule("k4-run", "K4", "k4_fake", "0 9 * * *")
        return await sch.run_schedule_now(None, db, "k4-run")

    async def test_passes_schedule_id(self, db, monkeypatch):
        seen = {}

        async def gen(brain_db, schedule_id=None, **kw):
            seen["id"] = schedule_id

        await self._run(db, monkeypatch, gen)
        assert seen["id"] == "k4-run"

    async def test_keeps_recorded_delivery_failure(self, db, monkeypatch):
        async def gen(brain_db, schedule_id=None, **kw):
            generators._record_status(
                brain_db, schedule_id, "FAILED telegram 0/1 msgs (x)"
            )

        out = await self._run(db, monkeypatch, gen)
        assert db.get_schedule("k4-run")["last_status"].startswith("FAILED")
        assert out["status"] == "delivery_failed"

    async def test_writes_ok_when_generator_records_nothing(self, db, monkeypatch):
        async def gen(brain_db, **kw):
            return None

        out = await self._run(db, monkeypatch, gen)
        assert db.get_schedule("k4-run")["last_status"] == "ok"
        assert out["status"] == "ok"

    async def test_exception_records_error(self, db, monkeypatch):
        async def gen(brain_db, **kw):
            raise RuntimeError("kaput")

        out = await self._run(db, monkeypatch, gen)
        assert out["status"] == "error"
        assert sch.status_kind(db.get_schedule("k4-run")["last_status"]) == "error"


@pytest.mark.parametrize("name", ["morning_digest", "hot_pulse", "homelab_deals"])
def test_generators_take_schedule_id(name):
    gen = generators.GENERATORS[name]
    assert "schedule_id" in inspect.signature(gen).parameters
    src = inspect.getsource(gen)
    assert not re.search(r"_record_status\(\s*brain_db,\s*\"", src), (
        f"{name} still records status under a hardcoded id"
    )


@pytest.mark.asyncio
async def test_hot_pulse_records_under_given_id(db, monkeypatch):
    db.upsert_schedule("pulse-b", "P", "hot_pulse", "every 15m")
    monkeypatch.setattr(generators, "_is_quiet_hours", lambda *a: True)
    monkeypatch.setattr(generators, "_HOT_PULSE_IN_QUIET_HOURS", False)
    await generators.hot_pulse_generator(
        db, schedule_id="pulse-b", quiet_hours_tz="UTC"
    )
    assert db.get_schedule("pulse-b")["last_status"].startswith("quiet hours")


# ─── G6 narrow except ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestJobLookupOnly:
    class _Sched:
        def __init__(self, exc):
            self.exc = exc

        def remove_job(self, _id):
            raise self.exc

        def pause_job(self, _id):
            raise self.exc

    async def test_missing_job_is_fine(self, db):
        db.upsert_schedule("g6", "G", "morning_scan", "0 9 * * *")
        s = self._Sched(JobLookupError("marketing_g6"))
        assert (await sch.pause_schedule(s, db, "g6"))["paused"] == 1
        assert await sch.remove_schedule(s, db, "g6") is True

    async def test_other_errors_propagate(self, db):
        db.upsert_schedule("g6b", "G", "morning_scan", "0 9 * * *")
        s = self._Sched(RuntimeError("scheduler broken"))
        with pytest.raises(RuntimeError):
            await sch.pause_schedule(s, db, "g6b")
        with pytest.raises(RuntimeError):
            await sch.remove_schedule(s, db, "g6b")


# ─── #22 / S1 docs routes and the auth sweep ─────────────────────


@pytest.fixture()
def real_auth(monkeypatch):
    import llm_proxy

    monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", "s3cret")
    saved = app_module.app.dependency_overrides.pop(auth.require_token, None)
    yield TestClient(app_module.app, raise_server_exceptions=False)
    if saved is not None:
        app_module.app.dependency_overrides[auth.require_token] = saved


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_routes_not_served(real_auth, path):
    assert real_auth.get(path).status_code != 200
    assert (
        real_auth.get(path, headers={"Authorization": "Bearer s3cret"}).status_code
        != 200
    )


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _all_operations() -> set[tuple[str, str]]:
    """Every (method, path) the app serves.

    Included routers (brain, LLM proxy, persona) are not flattened into
    ``app.routes`` on this FastAPI version, so the generated schema (still
    built in-process with the docs routes off) supplies their paths, and
    ``app.routes`` adds anything left out of the schema.
    """
    ops = {
        (method.upper(), path)
        for path, item in app_module.app.openapi()["paths"].items()
        for method in item
    }
    for route in app_module.app.routes:
        if isinstance(route, APIRoute):
            ops |= {(m, route.path) for m in route.methods or () if m != "HEAD"}
    return ops


def test_every_private_route_requires_token(real_auth):
    checked = 0
    ops = _all_operations()
    assert ("POST", "/v1/chat/completions") in ops
    assert any(p.startswith("/brain/") for _m, p in ops)
    assert any(p.startswith("/persona/") for _m, p in ops)
    for method, raw_path in sorted(ops):
        path = _concrete(raw_path)
        r = real_auth.request(method, path)
        if auth.is_public(method, path):
            assert r.status_code != 401, f"{method} {path} should be public"
        else:
            assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
            checked += 1
    assert checked >= 35  # the sweep really walked the app


# ─── #28 / G4 /healthz ────────────────────────────────────────────


class TestHealthz:
    def test_reads_counts_through_locked_method(self, monkeypatch):
        calls = []

        def counts():
            calls.append(1)
            return {"pages": 7, "vectors": 3}

        monkeypatch.setattr(app_module.brain_db, "health_counts", counts)
        data = client.get("/healthz").json()
        assert calls
        assert data["brain"]["pages"] == 7
        assert data["brain"]["vectors"] == 3
        assert "_conn" not in inspect.getsource(app_module.healthz)

    def test_db_error_is_degraded_but_200(self, monkeypatch):
        def broken():
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(app_module.brain_db, "health_counts", broken)
        r = client.get("/healthz")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "degraded"
        assert data["brain"]["db"] == "error"

    def test_reports_vector_status(self, monkeypatch):
        monkeypatch.setattr(
            app_module.brain_db,
            "vec_status",
            lambda: {"enabled": False, "reason": "sqlite_vec missing"},
        )
        # Error detail is for authenticated callers (test_auth covers the
        # anonymous view).
        monkeypatch.setattr(app_module.llm_proxy, "PROXY_AUTH_TOKEN", "t")
        data = client.get("/healthz", headers={"Authorization": "Bearer t"}).json()
        assert data["brain"]["vector_search"] == {
            "enabled": False,
            "reason": "sqlite_vec missing",
        }
        assert data["status"] == "degraded"

    def test_reports_scheduler_state(self, monkeypatch):
        monkeypatch.setattr(
            sch, "_STATE", {"status": "failed", "errors": ["x"], "timezone": "UTC"}
        )
        r = client.get("/healthz")
        assert r.status_code == 200
        data = r.json()
        assert data["scheduler"]["status"] == "failed"
        assert data["status"] == "degraded"

    def test_reports_profile(self, monkeypatch):
        monkeypatch.setattr(
            app_module.app.state, "profile_error", "profile not found: x", raising=False
        )
        monkeypatch.setattr(app_module.llm_proxy, "PROXY_AUTH_TOKEN", "t")
        data = client.get("/healthz", headers={"Authorization": "Bearer t"}).json()
        assert data["profile"]["error"] == "profile not found: x"
        assert data["status"] == "degraded"


def test_unknown_profile_is_loud_and_recorded(db, monkeypatch, caplog):
    monkeypatch.setenv("CLAWRANGE_PROFILE", "no-such-profile")
    fa = FastAPI()
    with caplog.at_level(logging.ERROR, logger="clawrange.app"):
        assert app_module.init_profile(fa, db) is None
    assert "no-such-profile" in fa.state.profile_error
    assert any(
        r.levelno >= logging.ERROR and "no-such-profile" in r.getMessage()
        for r in caplog.records
    )


# ─── K7 follow-up: atomic soul render with the Learned overlay ───


def test_init_profile_renders_soul_with_learned_overlay(db, tmp_path, monkeypatch):
    from tenant_profile import load_profile

    profile = load_profile("starter")
    row = db.create_learning(profile.name, "persona", "Tone", "Be brief.")
    db.set_learning_status(row["id"], "approved")
    soul = tmp_path / "soul.md"
    app_module.init_profile(FastAPI(), db, profile=profile, soul_path=str(soul))
    text = soul.read_text()
    assert "## Learned" in text and "Be brief." in text
    assert oct(soul.stat().st_mode & 0o777) == "0o644"


def test_init_profile_keeps_existing_soul(db, tmp_path):
    from tenant_profile import load_profile

    soul = tmp_path / "soul.md"
    soul.write_text("hand edited\n")
    app_module.init_profile(
        FastAPI(), db, profile=load_profile("starter"), soul_path=str(soul)
    )
    assert soul.read_text() == "hand edited\n"


# ─── #46 task status types ────────────────────────────────────────


class TestTaskStatusTypes:
    def test_list_bad_status_is_422(self):
        assert client.get("/task?status=typo").status_code == 422

    def test_list_good_status(self):
        assert client.get("/task?status=pending").status_code == 200

    @pytest.mark.parametrize("status", ["bogus", "pending", "cancelled"])
    def test_result_bad_status_is_422(self, status):
        task = client.post("/task", json={"description": "k4"}).json()
        r = client.post(
            f"/task/{task['id']}/result", json={"result": "r", "status": status}
        )
        assert r.status_code == 422

    def test_result_blocked_accepted(self):
        task = client.post("/task", json={"description": "k4 blocked"}).json()
        r = client.post(
            f"/task/{task['id']}/result", json={"result": "?", "status": "blocked"}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "blocked"


# ─── #26 scanner routes map UpstreamError to 502 ──────────────────


class TestUpstream502:
    def _boom(self, *a, **k):
        raise UpstreamError("api down")

    def test_reddit(self, monkeypatch):
        monkeypatch.setattr(app_module.reddit_search, "search_subreddits", self._aboom)
        r = client.post("/scan/reddit", json={"topic": "t", "subreddits": ["x"]})
        assert r.status_code == 502
        assert "api down" in r.json()["detail"]

    async def _aboom(self, *a, **k):
        raise UpstreamError("api down")

    @pytest.mark.parametrize(
        ("kind", "fn"), [("repos", "search_repos"), ("issues", "search_issues")]
    )
    def test_github(self, monkeypatch, kind, fn):
        monkeypatch.setattr(app_module.github_search, fn, self._aboom)
        r = client.post("/scan/github", json={"topic": "t", "kind": kind})
        assert r.status_code == 502
        assert "api down" in r.json()["detail"]

    def test_research(self, monkeypatch):
        monkeypatch.setattr(app_module.research, "orchestrate_research", self._aboom)
        r = client.post("/research", json={"topic": "t"})
        assert r.status_code == 502

    def test_web_none_is_502(self, monkeypatch):
        monkeypatch.setattr(
            app_module.llm_proxy, "_llm_call", AsyncMock(return_value=None)
        )
        r = client.post("/scan/web", json={"prompt": "p"})
        assert r.status_code == 502


def test_github_repos_and_issues_share_response(monkeypatch):
    item = SimpleNamespace(model_dump=lambda: {"n": 1})
    monkeypatch.setattr(
        app_module.github_search, "search_issues", AsyncMock(return_value=[item])
    )
    r = client.post("/scan/github", json={"topic": "t", "kind": "issues"})
    assert r.json() == {
        "results": [{"n": 1}],
        "total": 1,
        "query": "t",
        "kind": "issues",
    }


# ─── #30 balance display ──────────────────────────────────────────


class TestBalanceDisplay:
    def test_tier_uses_describe_balance(self, monkeypatch):
        monkeypatch.setattr(
            app_module.llm_proxy,
            "_check_openrouter_balance",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            app_module.llm_proxy,
            "describe_balance",
            lambda: "unknown (lookup failed: HTTP 500)",
        )
        data = client.get("/tier").json()
        assert data["balance_remaining"] == "unknown (lookup failed: HTTP 500)"

    def test_notify_uses_describe_balance_and_bang_paid(self, monkeypatch):
        monkeypatch.setattr(
            app_module.llm_proxy,
            "_check_openrouter_balance",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            app_module.llm_proxy,
            "describe_balance",
            lambda: "unknown (not checked yet)",
        )
        monkeypatch.setattr(app_module, "notify", AsyncMock(return_value=True))
        msg = client.post("/tier/notify").json()["message"]
        assert "unknown (not checked yet)" in msg
        assert "not configured" not in msg
        assert "!paid" in msg


# ─── #49 / #48 / #51 cleanups ─────────────────────────────────────


def test_projects_listed_once(monkeypatch):
    calls = []

    def list_projects():
        calls.append(1)
        return [{"slug": "a"}]

    monkeypatch.setattr(app_module.brain_db, "list_projects", list_projects)
    assert client.get("/projects").json() == {"projects": [{"slug": "a"}], "total": 1}
    assert len(calls) == 1


def test_scheduler_helper_replaces_hasattr_checks():
    assert "hasattr(app.state" not in APP_SRC
    assert callable(app_module._scheduler)
    assert callable(app_module._next_fire_times)


def test_next_fire_times_without_scheduler(monkeypatch):
    monkeypatch.setattr(app_module.app.state, "scheduler", None, raising=False)
    assert app_module._next_fire_times() == {}


def test_no_agent_names_or_glm_in_app_source():
    assert "max-ops" not in APP_SRC
    assert "GLM" not in APP_SRC
    assert "'paid' keyword" not in APP_SRC


def test_sched_kwargs_schedule_id_is_overridden(db):
    """A profile's explicit kwargs.schedule_id cannot point status elsewhere."""
    db.upsert_schedule("real", "R", "morning_scan", "0 9 * * *", {"schedule_id": "x"})
    kwargs = sch._job_kwargs(db.get_schedule("real"), db)
    assert kwargs["schedule_id"] == "real"
    assert json.loads(db.get_schedule("real")["kwargs"]) == {"schedule_id": "x"}
