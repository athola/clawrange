"""APScheduler 3.x integration for marketing orchestrator.

Embeds AsyncIOScheduler in FastAPI lifespan. Jobs are reconstructed on
every boot from the brain_db `schedules` table (the source of truth),
so the scheduler uses an in-memory jobstore: a serialising jobstore
would try to persist `brain_db` itself (which holds a live sqlite
connection) and fail at startup.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from generators import GENERATORS
from watchdog import register as register_watchdog

logger = logging.getLogger("clawrange.scheduler")

# Daily reports that must arrive even when workflows was down at fire time.
# The jobstore is in-memory, so without this a restart spanning 08:00
# silently drops that day's rundown.
CATCH_UP_SCHEDULES = {"morning_digest", "homelab_deals"}
CATCH_UP_WINDOW = timedelta(hours=4)
CATCH_UP_DELAY = timedelta(seconds=90)  # let Telegram/LLM deps settle


def _last_fire(cron: str, now: datetime, tz_name: str) -> datetime | None:
    """Most recent cron fire at or before `now`, looking back one day."""
    trigger = CronTrigger(**_parse_cron(cron), timezone=tz_name)
    prev = None
    fire = trigger.get_next_fire_time(None, now - timedelta(days=1))
    while fire is not None and fire <= now:
        prev = fire
        fire = trigger.get_next_fire_time(fire, fire + timedelta(seconds=1))
    return prev


def _missed_fire(sched: dict, now: datetime, tz_name: str) -> datetime | None:
    """Return the fire time a catch-up run should cover, or None.

    Due when the latest fire is within CATCH_UP_WINDOW and either nothing
    ran since it, or the run that did recorded a failed Telegram delivery.
    Any other status after the fire counts as handled, so a --reload
    restart or a crashing generator never re-sends on every boot.
    """
    prev = _last_fire(sched["cron"], now, tz_name)
    if prev is None or now - prev > CATCH_UP_WINDOW:
        return None
    last_run = sched.get("last_run")
    if last_run and datetime.fromisoformat(last_run) >= prev:
        if not (sched.get("last_status") or "").startswith("FAILED"):
            return None
    return prev


def _queue_catch_ups(
    scheduler: AsyncIOScheduler, schedules: list, brain_db, tz_name: str
) -> None:
    now = datetime.now(ZoneInfo(tz_name))
    for sched in schedules:
        if sched.get("paused") or sched["kind"] not in CATCH_UP_SCHEDULES:
            continue
        try:
            missed = _missed_fire(sched, now, tz_name)
        except Exception as exc:
            logger.warning("catch-up check failed for %s: %s", sched["id"], exc)
            continue
        if missed is None:
            continue
        kwargs = json.loads(sched.get("kwargs") or "{}")
        kwargs["brain_db"] = brain_db
        scheduler.add_job(
            GENERATORS[sched["kind"]],
            "date",
            run_date=now + CATCH_UP_DELAY,
            id=f"catchup_{sched['id']}",
            kwargs=kwargs,
            replace_existing=True,
        )
        logger.warning(
            "catch-up: %s missed its %s fire; running at boot",
            sched["id"],
            missed.isoformat(),
        )


def init_scheduler(brain_db) -> AsyncIOScheduler | None:
    """Create and configure the scheduler. Returns None on failure."""
    try:
        tz_name = os.getenv("SCHEDULER_TZ", "America/Chicago")
        scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            timezone=tz_name,
            job_defaults={"misfire_grace_time": 300, "coalesce": True},
        )

        # Re-register schedules from the database table
        schedules = brain_db.list_schedules()
        for sched in schedules:
            if not sched.get("paused"):
                _register_job(scheduler, sched, brain_db)
        try:
            _queue_catch_ups(scheduler, schedules, brain_db, tz_name)
        except Exception as exc:  # never let catch-up take the scheduler down
            logger.warning("catch-up scan failed: %s", exc)

        # Heartbeat watchdog: infrastructure, not a tenant schedule, so it
        # registers directly and survives a wiped schedules table.
        register_watchdog(scheduler)

        scheduler.start()
        logger.info("Scheduler started with %d active jobs", len(schedules))
        return scheduler
    except Exception as exc:
        logger.warning("Scheduler init failed (degrading to unscheduled mode): %s", exc)
        return None


def _register_job(scheduler: AsyncIOScheduler, sched: dict, brain_db) -> None:
    """Register a single schedule as an APScheduler job."""

    kind = sched["kind"]
    if kind not in GENERATORS:
        logger.warning("Unknown generator kind: %s", kind)
        return

    kwargs = json.loads(sched.get("kwargs", "{}"))
    kwargs["brain_db"] = brain_db

    job_id = f"marketing_{sched['id']}"

    try:
        existing = scheduler.get_job(job_id)
        if existing:
            scheduler.remove_job(job_id)

        scheduler.add_job(
            GENERATORS[kind],
            "cron",
            **_parse_cron(sched["cron"]),
            id=job_id,
            kwargs=kwargs,
            replace_existing=True,
        )
    except Exception as exc:
        logger.warning("Failed to register job %s: %s", sched["id"], exc)


def _parse_cron(cron_str: str) -> dict:
    """Parse cron expression or duration alias into APScheduler trigger kwargs.

    Supports:
      - 5-field cron: "0 9 * * *"
      - Duration aliases: "every 6h", "every 30m", "every 2d"
    """
    cron_str = cron_str.strip()

    if cron_str.lower().startswith("every "):
        return _parse_duration(cron_str)

    fields = cron_str.split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression: {cron_str}")

    return {
        "minute": fields[0],
        "hour": fields[1],
        "day": fields[2],
        "month": fields[3],
        "day_of_week": fields[4],
    }


def _parse_duration(duration_str: str) -> dict:
    """Convert 'every Nh' or 'every Nm' to cron-like interval kwargs."""

    match = re.match(r"every\s+(\d+)\s*([mhd])", duration_str.lower())
    if not match:
        raise ValueError(f"Invalid duration: {duration_str}")

    amount = int(match.group(1))
    unit = match.group(2)

    if unit == "m":
        if amount < 5:
            amount = 5
        return {"minute": f"*/{amount}"}
    elif unit == "h":
        return {"hour": f"*/{amount}"}
    elif unit == "d":
        return {"day": f"*/{amount}"}

    raise ValueError(f"Unknown duration unit: {unit}")


async def add_schedule(
    scheduler: AsyncIOScheduler | None,
    brain_db,
    schedule_id: str,
    name: str,
    kind: str,
    cron: str,
    kwargs: dict | None = None,
) -> dict:
    """Add a new schedule (persists to DB and registers with APScheduler)."""
    sched = brain_db.upsert_schedule(schedule_id, name, kind, cron, kwargs)
    if scheduler:
        _register_job(scheduler, sched, brain_db)
    return sched


async def remove_schedule(
    scheduler: AsyncIOScheduler | None, brain_db, schedule_id: str
) -> bool:
    """Remove a schedule from DB and APScheduler."""
    if scheduler:
        try:
            scheduler.remove_job(f"marketing_{schedule_id}")
        except Exception:
            pass
    return brain_db.delete_schedule(schedule_id)


async def pause_schedule(
    scheduler: AsyncIOScheduler | None, brain_db, schedule_id: str
) -> dict | None:
    """Pause a schedule."""
    if scheduler:
        try:
            scheduler.pause_job(f"marketing_{schedule_id}")
        except Exception:
            pass
    return brain_db.set_schedule_paused(schedule_id, True)


async def resume_schedule(
    scheduler: AsyncIOScheduler | None, brain_db, schedule_id: str
) -> dict | None:
    """Resume a paused schedule."""
    if scheduler:
        try:
            scheduler.resume_job(f"marketing_{schedule_id}")
        except Exception:
            pass
    return brain_db.set_schedule_paused(schedule_id, False)


async def run_schedule_now(
    scheduler: AsyncIOScheduler | None, brain_db, schedule_id: str
) -> dict:
    """Force-run a schedule immediately."""
    sched = brain_db.get_schedule(schedule_id)
    if not sched:
        raise ValueError(f"Schedule not found: {schedule_id}")

    kind = sched["kind"]
    if kind not in GENERATORS:
        raise ValueError(f"Unknown generator kind: {kind}")

    kwargs = json.loads(sched.get("kwargs", "{}"))
    kwargs["brain_db"] = brain_db

    try:
        await GENERATORS[kind](**kwargs)
        # Generators that record their own outcome (e.g. morning_digest's
        # "delivered N/N msgs") keep it; the catch-up check depends on it.
        after = brain_db.get_schedule(sched["id"]) or {}
        if after.get("last_run") == sched.get("last_run"):
            now = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .isoformat()
            )
            brain_db.update_schedule_status(sched["id"], now, "ok")
        return {"status": "ok", "schedule_id": sched["id"]}
    except Exception as exc:
        now = (
            __import__("datetime")
            .datetime.now(__import__("datetime").timezone.utc)
            .isoformat()
        )
        brain_db.update_schedule_status(sched["id"], now, f"error: {exc}")
        return {"status": "error", "schedule_id": sched["id"], "error": str(exc)}


def list_scheduled_jobs(scheduler: AsyncIOScheduler | None) -> list[dict]:
    """List all APScheduler jobs with next-fire times."""
    if not scheduler:
        return []
    jobs = []
    for job in scheduler.get_jobs():
        next_fire = job.next_run_time
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_fire_time": next_fire.isoformat() if next_fire else None,
            }
        )
    return jobs
