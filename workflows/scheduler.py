"""APScheduler 3.x integration for marketing orchestrator.

Embeds AsyncIOScheduler in FastAPI lifespan with SQLAlchemyJobStore
persisting to the same SQLite database. Jobs survive container restarts.
"""

import json
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

logger = logging.getLogger("clawrange.scheduler")


def _get_db_url() -> str:
    db_path = os.environ.get("BRAIN_DB_PATH", "/data/brain.db")
    return f"sqlite:///{db_path}"


def init_scheduler(brain_db) -> AsyncIOScheduler | None:
    """Create and configure the scheduler. Returns None on failure."""
    try:
        jobstore = SQLAlchemyJobStore(url=_get_db_url())
        scheduler = AsyncIOScheduler(
            jobstores={"default": jobstore},
            timezone=os.getenv("SCHEDULER_TZ", "America/Chicago"),
            job_defaults={"misfire_grace_time": 300, "coalesce": True},
        )

        # Re-register schedules from the database table
        schedules = brain_db.list_schedules()
        for sched in schedules:
            if not sched.get("paused"):
                _register_job(scheduler, sched, brain_db)

        scheduler.start()
        logger.info("Scheduler started with %d active jobs", len(schedules))
        return scheduler
    except Exception as exc:
        logger.warning("Scheduler init failed (degrading to unscheduled mode): %s", exc)
        return None


def _register_job(scheduler: AsyncIOScheduler, sched: dict, brain_db) -> None:
    """Register a single schedule as an APScheduler job."""
    from generators import GENERATORS

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
    import re

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

    from generators import GENERATORS

    kind = sched["kind"]
    if kind not in GENERATORS:
        raise ValueError(f"Unknown generator kind: {kind}")

    kwargs = json.loads(sched.get("kwargs", "{}"))
    kwargs["brain_db"] = brain_db

    try:
        await GENERATORS[kind](**kwargs)
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
