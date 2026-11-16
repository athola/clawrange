"""APScheduler 3.x integration for marketing orchestrator.

Embeds AsyncIOScheduler in FastAPI lifespan. Jobs are reconstructed on
every boot from the brain_db `schedules` table (the source of truth),
so the scheduler uses an in-memory jobstore: a serialising jobstore
would try to persist `brain_db` itself (which holds a live sqlite
connection) and fail at startup.
"""

import inspect
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent
from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import tenant_profile
import watchdog
from generators import GENERATORS, _redact_urls
from watchdog import register as register_watchdog

logger = logging.getLogger("clawrange.scheduler")

StatusKind = Literal["ok", "delivery_failed", "error"]


def status_kind(text: str | None) -> StatusKind:
    """Classify a stored schedule last_status.

    One reader for the /sched dashboard and the catch-up check. Errors are
    written as ``error: ...`` / ``error (...)`` and a Reddit outage as
    ``reddit outage, ...``; a Telegram delivery shortfall contains
    ``FAILED`` (``FAILED telegram 1/3`` or ``ok (...; telegram FAILED ...)``).
    """
    s = (text or "").strip()
    if s.lower().startswith(("error", "reddit outage")):
        return "error"
    if "FAILED" in s:
        return "delivery_failed"
    return "ok"


def _new_state() -> dict[str, Any]:
    return {"status": "not_started", "errors": [], "timezone": None}


# Startup outcome of init_scheduler, read by /healthz and the lifespan alert.
_STATE: dict[str, Any] = _new_state()


def scheduler_state() -> dict[str, Any]:
    """``status`` is not_started, running, degraded (running with startup
    errors) or failed (not running); ``errors`` lists what went wrong."""
    return {**_STATE, "errors": list(_STATE["errors"])}


def _startup_error(what: str, exc: BaseException | str) -> None:
    msg = f"{what}: {exc}"
    _STATE["errors"].append(msg)
    logger.error("scheduler startup: %s", msg)


# Daily reports that must arrive even when workflows was down at fire time.
# The jobstore is in-memory, so without this a restart spanning 08:00
# silently drops that day's rundown. Which schedules qualify is profile
# config: a schedule opts in with `catch_up: true`.
CATCH_UP_WINDOW = timedelta(hours=4)
CATCH_UP_DELAY = timedelta(seconds=90)  # let Telegram/LLM deps settle


def catch_up_ids() -> set[str]:
    """Schedule ids the active profile marks `catch_up: true`."""
    import tenant_profile

    profile = tenant_profile.active_profile()
    if profile is None:
        return set()
    return {str(s["id"]) for s in profile.schedules if s.get("catch_up") is True}


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
        if status_kind(sched.get("last_status")) != "delivery_failed":
            return None
    return prev


def _job_kwargs(sched: dict, brain_db) -> dict:
    """Generator kwargs for a schedule row.

    ``schedule_id`` is always the row's own id, so a generator records its
    status on the schedule that fired it, whatever the stored kwargs say.
    """
    kwargs = json.loads(sched.get("kwargs") or "{}")
    kwargs["brain_db"] = brain_db
    kwargs["schedule_id"] = sched["id"]
    return kwargs


def _queue_catch_ups(
    scheduler: AsyncIOScheduler, schedules: list, brain_db, tz_name: str
) -> None:
    now = datetime.now(ZoneInfo(tz_name))
    opted_in = catch_up_ids()
    for sched in schedules:
        if sched.get("paused") or sched["id"] not in opted_in:
            continue
        # One bad row (unknown kind, bad cron or kwargs) skips only itself.
        try:
            missed = _missed_fire(sched, now, tz_name)
            if missed is None:
                continue
            scheduler.add_job(
                GENERATORS[sched["kind"]],
                "date",
                run_date=now + CATCH_UP_DELAY,
                id=f"catchup_{sched['id']}",
                kwargs=_job_kwargs(sched, brain_db),
                replace_existing=True,
            )
        except Exception as exc:
            logger.warning("catch-up failed for %s: %r", sched["id"], exc)
            continue
        logger.warning(
            "catch-up: %s missed its %s fire; running at boot",
            sched["id"],
            missed.isoformat(),
        )


SCAN_CACHE_MAX_AGE_DAYS = 14


def prune_scan_cache_job(brain_db) -> None:
    """Drop dedup rows older than SCAN_CACHE_MAX_AGE_DAYS."""
    removed = brain_db.prune_scan_cache(SCAN_CACHE_MAX_AGE_DAYS)
    logger.info("maintenance: pruned %d scan_cache rows", removed)


async def embed_backfill_job(brain_db) -> None:
    """Embed brain chunks that lack a vector for the configured model."""
    import embeddings

    result = await embeddings.backfill(brain_db)
    if result.get("stored") or result.get("failed"):
        logger.info("maintenance: embedding backfill %s", result)


def register_maintenance(scheduler, brain_db) -> None:
    """Internal housekeeping jobs (not tenant schedules)."""
    scheduler.add_job(
        prune_scan_cache_job,
        "cron",
        hour=4,
        minute=15,
        id="maintenance_prune_scan_cache",
        kwargs={"brain_db": brain_db},
        replace_existing=True,
    )
    scheduler.add_job(
        embed_backfill_job,
        "interval",
        minutes=10,
        id="maintenance_embed_backfill",
        kwargs={"brain_db": brain_db},
        replace_existing=True,
    )


def _job_error_listener(brain_db):
    """EVENT_JOB_ERROR handler: a crashed tenant job records ``error: ...``
    on its schedule row, so /sched and the dashboard show the failure."""

    def listener(event: JobExecutionEvent) -> None:
        job_id = event.job_id or ""
        exc = event.exception
        logger.error("scheduled job %s crashed: %r", job_id, exc)
        for prefix in ("marketing_", "catchup_"):
            if job_id.startswith(prefix):
                status = _redact_urls(f"error: {type(exc).__name__}: {exc}")[:300]
                try:
                    brain_db.update_schedule_status(
                        job_id[len(prefix) :], datetime.now(UTC).isoformat(), status
                    )
                except Exception as write_exc:
                    logger.warning(
                        "could not record crash of %s: %s", job_id, write_exc
                    )
                return

    return listener


def init_scheduler(brain_db) -> AsyncIOScheduler | None:
    """Create, populate and start the scheduler.

    Each step fails on its own: a bad timezone falls back to UTC, and a
    broken schedules table or catch-up scan still leaves the heartbeat
    watchdog and maintenance jobs running. Every failure is logged at ERROR
    and recorded in ``scheduler_state()``. Returns None only when the
    scheduler itself could not start.
    """
    _STATE.update(_new_state())
    tz_name = tenant_profile.operator_timezone()
    if not tenant_profile.valid_timezone(tz_name):
        _startup_error("timezone", f"unknown timezone {tz_name!r}, using UTC")
        tz_name = "UTC"
    _STATE["timezone"] = tz_name
    # watchdog._tz_name() already falls back to a valid zone, so check the raw
    # override: the watchdog keeps running, but /healthz still shows the typo.
    watch_override = os.getenv("HEARTBEAT_WATCH_TZ", "").strip()
    if watch_override and not tenant_profile.valid_timezone(watch_override):
        _startup_error(
            "watchdog",
            f"unknown HEARTBEAT_WATCH_TZ {watch_override!r}, "
            f"using {watchdog._tz_name()}",
        )

    try:
        scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            timezone=tz_name,
            job_defaults={"misfire_grace_time": 300, "coalesce": True},
        )
        scheduler.add_listener(_job_error_listener(brain_db), EVENT_JOB_ERROR)
    except Exception as exc:
        _startup_error("create", exc)
        _STATE["status"] = "failed"
        return None

    # Heartbeat watchdog and maintenance: infrastructure, not tenant
    # schedules, so they register first and survive a broken schedules table.
    for what, register in (
        ("watchdog", lambda: register_watchdog(scheduler)),
        ("maintenance", lambda: register_maintenance(scheduler, brain_db)),
    ):
        try:
            register()
        except Exception as exc:
            _startup_error(what, exc)

    schedules: list = []
    try:
        schedules = brain_db.list_schedules()
    except Exception as exc:
        _startup_error("schedules", exc)
    for sched in schedules:
        if not sched.get("paused"):
            _register_job(scheduler, sched, brain_db)
    try:
        _queue_catch_ups(scheduler, schedules, brain_db, tz_name)
    except Exception as exc:  # never let catch-up take the scheduler down
        _startup_error("catch-up", exc)

    try:
        scheduler.start()
    except Exception as exc:
        _startup_error("start", exc)
        _STATE["status"] = "failed"
        return None
    _STATE["status"] = "degraded" if _STATE["errors"] else "running"
    logger.info("Scheduler started with %d active jobs", len(schedules))
    return scheduler


def _register_job(scheduler: AsyncIOScheduler, sched: dict, brain_db) -> None:
    """Register a single schedule as an APScheduler job."""

    kind = sched["kind"]
    if kind not in GENERATORS:
        logger.warning("Unknown generator kind: %s", kind)
        return

    job_id = f"marketing_{sched['id']}"

    try:
        kwargs = _job_kwargs(sched, brain_db)
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
      - Duration aliases: "every 6h", "every 30m", "every 1d" (even
        steps only; see _EVEN_STEPS)
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
        "day_of_week": _translate_dow(fields[4]),
    }


_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _translate_dow(field: str) -> str:
    """Standard cron day-of-week (0/7=Sun, 1=Mon) -> APScheduler names.

    APScheduler 3.x numbers Monday as 0, so numeric fields are expanded to
    explicit day names. Name-based items (mon-fri) pass through unchanged.
    """
    if field == "*":
        return field
    out: list[str] = []
    for item in field.split(","):
        if re.search(r"[A-Za-z]", item):
            out.append(item)
            continue
        base, _, step_s = item.partition("/")
        try:
            step = int(step_s) if step_s else 1
            if base == "*":
                lo, hi = 0, 6
            elif "-" in base:
                a, b = base.split("-", 1)
                lo, hi = int(a), int(b)
            else:
                lo = int(base)
                hi = 7 if step_s else lo
        except ValueError:
            raise ValueError(f"Invalid day_of_week: {field}") from None
        if step < 1 or lo < 0 or hi > 7 or lo > hi:
            raise ValueError(f"Invalid day_of_week: {field}")
        out.extend(_DOW_NAMES[d % 7] for d in range(lo, hi + 1, step))
    return ",".join(dict.fromkeys(out))


def validate_schedule(kind: str, cron: str, kwargs: dict | None) -> None:
    """Raise ValueError when a schedule could not run. Used before persisting."""
    if kind not in GENERATORS:
        raise ValueError(f"unknown kind '{kind}' (known: {sorted(GENERATORS)})")
    try:
        CronTrigger(**_parse_cron(cron), timezone="UTC")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid cron '{cron}': {exc}") from exc
    if kwargs is None:
        return
    if not isinstance(kwargs, dict):
        raise ValueError("kwargs must be an object")
    try:
        json.dumps(kwargs)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"kwargs must be JSON-serialisable: {exc}") from exc
    if "brain_db" in kwargs:
        raise ValueError("kwargs may not set brain_db")
    params = inspect.signature(GENERATORS[kind]).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        unknown = sorted(set(kwargs) - set(params))
        if unknown:
            raise ValueError(f"kwargs not accepted by {kind}: {unknown}")


# A "*/N" cron step restarts at every hour, day or month boundary, so only
# a step that divides the cycle gives even gaps: "every 7h" fires at
# 0,7,14,21 and then 0 again, three hours later. Month lengths vary, so
# no day step above 1 is even.
_EVEN_STEPS = {
    "m": {5, 6, 10, 12, 15, 20, 30},
    "h": {1, 2, 3, 4, 6, 8, 12, 24},
    "d": {1},
}
_STEP_FIELD = {"m": ("minute", "hour"), "h": ("hour", "day"), "d": ("day", "month")}


def _parse_duration(duration_str: str) -> dict:
    """Convert 'every Nm|Nh|Nd' to cron-like interval kwargs.

    Steps that would fire at uneven gaps are rejected; write a cron
    expression for those.
    """

    match = re.match(r"every\s+(\d+)\s*([mhd])", duration_str.lower())
    if not match:
        raise ValueError(f"Invalid duration: {duration_str}")

    amount = int(match.group(1))
    unit = match.group(2)

    if unit == "m" and amount < 5:
        amount = 5
    field, cycle = _STEP_FIELD[unit]
    if amount not in _EVEN_STEPS[unit]:
        steps = ", ".join(f"{n}{unit}" for n in sorted(_EVEN_STEPS[unit]))
        raise ValueError(
            f"'{duration_str}' does not divide evenly into a {cycle}, so its "
            f"gaps would be uneven; use one of {steps} or a cron expression"
        )
    return {field: f"*/{amount}"}


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
        if sched.get("paused"):
            # Editing a paused schedule must not silently un-pause it; the
            # job is registered fresh when it is resumed.
            _remove_job(scheduler, sched["id"])
        else:
            _register_job(scheduler, sched, brain_db)
    return sched


def _remove_job(scheduler: AsyncIOScheduler, schedule_id: str) -> None:
    try:
        scheduler.remove_job(f"marketing_{schedule_id}")
    except JobLookupError:
        pass


async def remove_schedule(
    scheduler: AsyncIOScheduler | None, brain_db, schedule_id: str
) -> bool:
    """Remove a schedule from DB and APScheduler."""
    if scheduler:
        _remove_job(scheduler, schedule_id)
    return brain_db.delete_schedule(schedule_id)


async def pause_schedule(
    scheduler: AsyncIOScheduler | None, brain_db, schedule_id: str
) -> dict | None:
    """Pause a schedule."""
    if scheduler:
        try:
            scheduler.pause_job(f"marketing_{schedule_id}")
        except JobLookupError:  # paused at boot, so never registered
            pass
    return brain_db.set_schedule_paused(schedule_id, True)


async def resume_schedule(
    scheduler: AsyncIOScheduler | None, brain_db, schedule_id: str
) -> dict | None:
    """Resume a paused schedule."""
    sched = brain_db.set_schedule_paused(schedule_id, False)
    if scheduler and sched:
        # Paused schedules are not registered at boot, so resume_job alone
        # would find nothing after a restart: register from the DB row.
        _register_job(scheduler, sched, brain_db)
    return sched


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

    try:
        await GENERATORS[kind](**_job_kwargs(sched, brain_db))
    except Exception as exc:
        now = datetime.now(UTC).isoformat()
        status = _redact_urls(f"error: {exc}")
        brain_db.update_schedule_status(sched["id"], now, status)
        return {"status": "error", "schedule_id": sched["id"], "error": status}
    # Generators that record their own outcome (e.g. morning_digest's
    # "delivered N/N msgs") keep it; the catch-up check depends on it. They
    # record under the schedule_id passed in, so a changed last_run means
    # this run wrote it.
    after = brain_db.get_schedule(sched["id"]) or {}
    if after.get("last_run") == sched.get("last_run"):
        brain_db.update_schedule_status(
            sched["id"], datetime.now(UTC).isoformat(), "ok"
        )
        after = {**after, "last_status": "ok"}
    last_status = after.get("last_status")
    return {
        "status": status_kind(last_status),
        "schedule_id": sched["id"],
        "last_status": last_status,
    }


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
