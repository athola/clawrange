"""Scheduler: cron translation, pause/resume lifecycle, validation, catch-up."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import scheduler as sch
from brain_db import BrainDB


@pytest.fixture()
def db(tmp_path):
    d = BrainDB(str(tmp_path / "s.db"))
    d.init_db()
    return d


def _fires(cron: str, start: datetime, n: int = 3) -> list[datetime]:
    trig = CronTrigger(**sch._parse_cron(cron), timezone="UTC")
    out, prev, now = [], None, start
    for _ in range(n):
        nxt = trig.get_next_fire_time(prev, now)
        out.append(nxt)
        prev, now = nxt, nxt
    return out


class TestDayOfWeek:
    START = datetime(2026, 1, 5, 0, 0, tzinfo=ZoneInfo("UTC"))  # a Monday

    def test_one_is_monday(self):
        assert _fires("0 9 * * 1", self.START)[0].weekday() == 0

    def test_zero_and_seven_are_sunday(self):
        assert _fires("0 9 * * 0", self.START)[0].weekday() == 6
        assert _fires("0 9 * * 7", self.START)[0].weekday() == 6

    def test_range(self):
        days = {f.weekday() for f in _fires("0 9 * * 1-5", self.START, 10)}
        assert days == {0, 1, 2, 3, 4}

    def test_list(self):
        days = {f.weekday() for f in _fires("0 9 * * 1,3", self.START, 6)}
        assert days == {0, 2}

    def test_range_through_seven(self):
        days = {f.weekday() for f in _fires("0 9 * * 5-7", self.START, 9)}
        assert days == {4, 5, 6}

    def test_step_follows_cron_semantics(self):
        # cron "*/2" = 0,2,4,6 = Sun, Tue, Thu, Sat
        days = {f.weekday() for f in _fires("0 9 * * */2", self.START, 14)}
        assert days == {6, 1, 3, 5}

    def test_names_pass_through(self):
        assert _fires("0 9 * * mon-fri", self.START)[0].weekday() == 0
        assert sch._parse_cron("0 9 * * *")["day_of_week"] == "*"

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            sch._parse_cron("0 9 * * 8")


class TestValidate:
    def test_ok(self):
        sch.validate_schedule("morning_scan", "0 9 * * 1", {})

    def test_unknown_kind(self):
        with pytest.raises(ValueError, match="kind"):
            sch.validate_schedule("nope", "0 9 * * *", {})

    def test_bad_cron(self):
        with pytest.raises(ValueError):
            sch.validate_schedule("morning_scan", "61 25 * * *", {})
        with pytest.raises(ValueError):
            sch.validate_schedule("morning_scan", "garbage", {})

    def test_kwargs_must_be_json_dict_without_brain_db(self):
        with pytest.raises(ValueError, match="kwargs"):
            sch.validate_schedule("morning_scan", "0 9 * * *", {"x": object()})
        with pytest.raises(ValueError, match="brain_db"):
            sch.validate_schedule("morning_scan", "0 9 * * *", {"brain_db": 1})

    def test_unknown_kwarg_rejected_for_strict_generator(self, monkeypatch, brain_db):
        async def strict(brain_db, only=None):
            return None

        monkeypatch.setitem(sch.GENERATORS, "strict_test", strict)
        sch.validate_schedule("strict_test", "0 9 * * *", {"only": 1})
        with pytest.raises(ValueError, match="bogus"):
            sch.validate_schedule("strict_test", "0 9 * * *", {"bogus": 1})


class TestRoutes:
    def test_create_rejects_bad_input_without_persisting(self, client, brain_db):

        for body in (
            {"id": "x1", "name": "x", "kind": "nope", "cron": "0 9 * * *"},
            {"id": "x1", "name": "x", "kind": "morning_scan", "cron": "bad"},
        ):
            assert client.post("/sched", json=body).status_code == 400
        assert brain_db.get_schedule("x1") is None

    def test_patch_rejects_bad_cron(self, client, brain_db):

        brain_db.upsert_schedule("p1", "p", "morning_scan", "0 9 * * *")
        r = client.patch("/sched/p1", json={"cron": "bad"})
        assert r.status_code == 400
        assert brain_db.get_schedule("p1")["cron"] == "0 9 * * *"

    def test_create_ok(self, client):
        r = client.post(
            "/sched",
            json={
                "id": "ok1",
                "name": "ok",
                "kind": "morning_scan",
                "cron": "0 9 * * 1",
            },
        )
        assert r.status_code == 200


@pytest.mark.asyncio
class TestPauseResume:
    async def _sched(self):
        s = AsyncIOScheduler(timezone="UTC")
        s.start()
        return s

    async def test_resume_registers_job_missing_after_restart(self, db):
        db.upsert_schedule("s1", "S", "morning_scan", "0 9 * * *")
        db.set_schedule_paused("s1", True)
        s = await self._sched()
        try:
            assert s.get_job("marketing_s1") is None
            row = await sch.resume_schedule(s, db, "s1")
            assert row["paused"] == 0
            job = s.get_job("marketing_s1")
            assert job is not None and job.next_run_time is not None
        finally:
            s.shutdown(wait=False)

    async def test_edit_while_paused_stays_paused(self, db):
        db.upsert_schedule("s2", "S", "morning_scan", "0 9 * * *")
        s = await self._sched()
        try:
            await sch.add_schedule(s, db, "s2", "S", "morning_scan", "0 9 * * *")
            await sch.pause_schedule(s, db, "s2")
            await sch.add_schedule(s, db, "s2", "S", "morning_scan", "0 10 * * *")
            job = s.get_job("marketing_s2")
            assert job is None or job.next_run_time is None
            assert db.get_schedule("s2")["paused"] == 1
        finally:
            s.shutdown(wait=False)


class TestCatchUpFlag:
    def test_flag_comes_from_profile(self, monkeypatch):
        import tenant_profile

        prof = tenant_profile.Profile(
            name="t",
            raw={
                "seeds": {
                    "schedules": [
                        {"id": "a", "kind": "morning_scan", "catch_up": True},
                        {"id": "b", "kind": "morning_scan"},
                    ]
                }
            },
        )
        monkeypatch.setattr(tenant_profile, "active_profile", lambda: prof)
        assert sch.catch_up_ids() == {"a"}

    def test_no_profile_means_none(self, monkeypatch):
        import tenant_profile

        monkeypatch.setattr(tenant_profile, "active_profile", lambda: None)
        assert sch.catch_up_ids() == set()


class TestMaintenanceJobs:
    """scan_cache had a pruning method nothing ever called, so the dedup
    table grew without bound. It now runs as a daily internal job."""

    def test_register_maintenance_adds_daily_prune(self, db):
        s = AsyncIOScheduler(timezone="UTC")
        sch.register_maintenance(s, db)
        job = s.get_job("maintenance_prune_scan_cache")
        assert job is not None
        assert "cron" in str(job.trigger)

    def test_prune_job_removes_old_rows(self, db):
        db.mark_seen("reddit_post", "old", "p")
        db._conn.execute("UPDATE scan_cache SET seen_at = '2000-01-01T00:00:00+00:00'")
        db.mark_seen("reddit_post", "new", "p")
        sch.prune_scan_cache_job(db)
        assert not db.is_seen("reddit_post", "old", "p")
        assert db.is_seen("reddit_post", "new", "p")


class TestEmbeddingBackfillJob:
    def test_registered_every_ten_minutes(self, db):
        s = AsyncIOScheduler(timezone="UTC")
        sch.register_maintenance(s, db)
        job = s.get_job("maintenance_embed_backfill")
        assert job is not None and "interval" in str(job.trigger)
        assert job.trigger.interval.total_seconds() == 600
