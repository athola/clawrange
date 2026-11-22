"""ClawRange workflows service: brain, task queue, LLM proxy, scheduler, scanners."""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

import auth
import embeddings
import github_search
import llm_proxy
import reddit_search
import research
import scheduler as scheduler_mod
import tenant_profile
from brain import create_brain_router
from brain_db import BrainDB, TaskStatus
from crm import get_adapter
from crm_api import create_crm_router
from errors import UpstreamError
from generators import seed_from_profile
from homelab_api import create_homelab_router
from llm_proxy import router as llm_router
from persona import render_all
from persona_api import create_persona_router
from scheduler import (
    add_schedule,
    init_scheduler,
    list_scheduled_jobs,
    pause_schedule,
    remove_schedule,
    resume_schedule,
    run_schedule_now,
    status_kind,
    validate_schedule,
)
from tasks_api import TaskCreate, TaskResult
from telegram import notify
from tenant_profile import load_profile


# Wire INFO-level logging for the clawrange.* loggers so cron
# heartbeats and digest delivery confirmations show up in
# `docker logs msp-workflows` without needing a global basicConfig
# (uvicorn already configures its own root handlers).
def _configure_logging() -> None:
    """Idempotent: a second create_app() adds no second handler."""
    root = logging.getLogger("clawrange")
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )
        root.addHandler(handler)


logger = logging.getLogger("clawrange.app")

# ─── Database Initialization ─────────────────────────────────────


def open_brain(path: str | None = None) -> BrainDB:
    """Open and migrate the brain at ``path`` (default ``BRAIN_DB_PATH``)."""
    db = BrainDB(path or os.environ.get("BRAIN_DB_PATH", "/data/brain.db"))
    db.init_db()
    # Vectors live in one table per embedding model (see embeddings.py).
    cfg = embeddings.config()
    db.use_vector_table(cfg.table, cfg.dim)
    return db


# ─── Tenant Profile and Lifespan ─────────────────────────────────


def init_profile(
    app: FastAPI,
    brain_db,
    *,
    profile=None,
    soul_path: str | None = None,
    http_client=None,
) -> Any:
    """Wire a tenant profile into the app (FR-8.1).

    Renders the persona to ``soul.md`` when missing, seeds projects and
    schedules from the profile, and — when the profile defines a CRM — inits
    the adapter on ``app.state.crm`` and mounts the ``/crm`` router. Returns
    the resolved profile. A profile-load failure (e.g. a misspelled
    ``CLAWRANGE_PROFILE``) does not crash boot: it logs at ERROR, sets
    ``app.state.profile_error`` (``/healthz`` then reports degraded) and leaves
    the baseline (no seeds, no CRM) untouched. ``http_client`` is injectable so
    the connector-sync route can be tested offline.
    """
    app.state.profile_error = None
    if profile is None:
        try:
            profile = load_profile()
        except Exception as exc:  # never crash boot on a bad profile
            name = os.environ.get("CLAWRANGE_PROFILE") or "starter"
            app.state.profile_error = f"{name}: {exc}"
            logger.error(
                "init_profile: profile %r failed to load, running with no "
                "profile (nothing seeded, no CRM, UTC): %s",
                name,
                exc,
            )
            return None

    app.state.profile = profile
    seed_from_profile(brain_db, profile)

    # Render the persona only when no soul.md is present, so hand-edits and
    # the marketing verbatim soul are never clobbered on restart. Rendered
    # after seeding so the learned.yaml overlay is in the brain, and written
    # atomically with the approved learnings like every other render.
    soul = (
        soul_path
        or os.environ.get("SOUL_PATH")
        or str(
            os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "openclaw", "soul.md"
            )
        )
    )
    try:
        if not os.path.exists(soul):
            approved = brain_db.list_learnings(profile=profile.name, status="approved")
            if render_all(profile, {"soul": soul}, approved).get("soul"):
                logger.info("init_profile: rendered persona -> %s", soul)
    except Exception as exc:
        logger.warning("init_profile: persona render skipped: %s", exc)

    if profile.crm:
        crm = get_adapter(profile.crm)
        crm.init()
        app.state.crm = crm
        app.include_router(create_crm_router(profile, crm, http_client=http_client))
        logger.info("init_profile: CRM mounted (%s)", crm.health().get("adapter"))

    if profile.homelab is not None:
        app.include_router(create_homelab_router(profile))
        logger.info("init_profile: homelab mounted")

    return profile


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.warn_if_unset(llm_proxy.PROXY_AUTH_TOKEN)
    brain_db = app.state.brain_db
    init_profile(app, brain_db)
    scheduler = init_scheduler(brain_db)
    app.state.scheduler = scheduler
    # Scheduled reports and the heartbeat watchdog are how the operator
    # hears about outages, so a broken start must not be log-only.
    problems = list(scheduler_mod.scheduler_state()["errors"])
    if getattr(app.state, "profile_error", None):
        problems.append(f"profile: {app.state.profile_error}")
    if problems:
        await notify("Workflows started degraded:\n- " + "\n- ".join(problems))
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


def _brain(request: Request) -> BrainDB:
    """The serving app's brain (route dependency)."""
    return request.app.state.brain_db


def _scheduler(request: Request):
    """The running scheduler, or None (lifespan not run or start failed)."""
    return getattr(request.app.state, "scheduler", None)


Brain = Annotated[BrainDB, Depends(_brain)]
LiveScheduler = Annotated[Any, Depends(_scheduler)]

# The routes below; create_app() mounts them on each app it builds.
router = APIRouter()


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _persona_render_targets(profile):
    """Where an approved learning re-renders the persona.

    The repo's openclaw/ templates only exist on the host (``make persona``);
    in the container app.py lives at /app, so those targets are skipped
    rather than failing on every approve. Workspace targets are the main
    agent's ``workspace`` and, when the profile declares ``assistant.ops``,
    the ops agent's ``workspace-<ops.agent>``; any other agent's workspace
    is left alone. Only directories OpenClaw already created are targeted.
    """
    state = os.environ.get(
        "OPENCLAW_STATE_DIR", os.path.join(_REPO_ROOT, "data", "openclaw-state")
    )
    targets: dict[str, Any] = {}
    repo_openclaw = os.path.join(_REPO_ROOT, "openclaw")
    if os.path.isdir(repo_openclaw):
        targets["soul"] = os.path.join(repo_openclaw, "soul.md")
        targets["identity"] = os.path.join(repo_openclaw, "identity.md")
    main_ws = os.path.join(state, "workspace")
    main = [main_ws] if os.path.isdir(main_ws) else []
    targets["workspace_soul"] = [os.path.join(w, "SOUL.md") for w in main]
    targets["workspace_identity"] = [os.path.join(w, "IDENTITY.md") for w in main]
    # The ops agent (assistant.ops.agent) gets the ops persona, never the
    # main one.
    ops = ((profile.assistant if profile else {}) or {}).get("ops") or {}
    if ops:
        ops_ws = os.path.join(state, f"workspace-{ops['agent']}")
        if os.path.isdir(repo_openclaw):
            targets["ops_soul"] = os.path.join(repo_openclaw, "soul-ops.md")
        targets["ops_workspace_soul"] = (
            [os.path.join(ops_ws, "SOUL.md")] if os.path.isdir(ops_ws) else []
        )
    return targets


def _current_profile(app: FastAPI):
    try:
        return load_profile()
    except Exception as exc:
        # Never substitute a placeholder profile here: /persona routes render
        # to the tenant's real soul.md/identity.md, so a silent fallback would
        # overwrite them with generic content. Prefer the boot-validated
        # profile; otherwise surface the failure.
        profile = getattr(app.state, "profile", None)
        if profile is not None:
            logger.warning(
                "persona: profile reload failed, using boot profile: %s", exc
            )
            return profile
        raise HTTPException(status_code=500, detail=f"profile unavailable: {exc}")


# ─── Task Queue (Persistent via BrainDB) ──────────────────────────


@router.post("/task")
def create_task(body: TaskCreate, brain_db: Brain):
    return brain_db.create_task(body.description, body.priority, body.source)


@router.get("/task")
def list_tasks(brain_db: Brain, status: TaskStatus | None = None):
    tasks = brain_db.list_tasks(status)
    return {"tasks": tasks, "total": len(tasks)}


@router.get("/task/{task_id}")
def get_task(task_id: str, brain_db: Brain):
    task = brain_db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/task/{task_id}/claim")
def claim_task(task_id: str, brain_db: Brain):
    """Mark a pending task as active: a worker calls this when it starts."""
    try:
        return brain_db.claim_task(task_id)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 409
        raise HTTPException(status_code=status_code, detail=str(e))


@router.post("/task/{task_id}/result")
def complete_task(task_id: str, body: TaskResult, brain_db: Brain):
    """Store a worker's result and close the task (completed, failed or
    blocked)."""
    if not brain_db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        return brain_db.complete_task(task_id, body.result, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/task/{task_id}")
def cancel_task(task_id: str, brain_db: Brain):
    try:
        return brain_db.cancel_task(task_id)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)


# ─── Health ─────────────────────────────────────────────────────────


@router.get("/healthz")
def healthz(request: Request, brain_db: Brain, scheduler: LiveScheduler):
    """Always 200 while the process serves requests: the container
    entrypoint restarts the service after repeated non-200s, which would
    not fix a bad profile or a broken table. ``status`` says ``degraded``
    when the database, scheduler, profile or vector search is broken.

    The route is public, so anonymous callers get the problem names but no
    error text (it can carry paths and config values); the logs and an
    authenticated request have the detail.
    """
    problems: list[str] = []
    try:
        counts = brain_db.health_counts()
        db_state = "ok"
    except Exception as exc:
        logger.error("healthz: brain query failed: %s", exc)
        counts = {"pages": -1, "vectors": -1}
        db_state = "error"
        problems.append("db")
    vec = brain_db.vec_status()
    if not vec["enabled"]:
        problems.append("vector_search")
    sched = scheduler_mod.scheduler_state()
    running = scheduler
    if sched["status"] in ("failed", "degraded") or (
        running is not None and not running.running
    ):
        problems.append("scheduler")
    profile = getattr(request.app.state, "profile", None)
    profile_error = getattr(request.app.state, "profile_error", None)
    if profile_error:
        problems.append("profile")
    if not auth.is_authenticated(request):
        vec = {"enabled": vec["enabled"]}
        sched = {"status": sched["status"]}
        profile_error = bool(profile_error)
    return {
        "status": "degraded" if problems else "ok",
        "problems": problems,
        "brain": {
            "db": db_state,
            "pages": counts["pages"],
            "embeddings": counts["vectors"] > 0,
            "embedding_model": embeddings.config().model,
            "vectors": counts["vectors"],
            "vector_search": vec,
        },
        "scheduler": sched,
        "profile": {
            "name": profile.name if profile is not None else None,
            "error": profile_error,
        },
    }


# ─── Tier Status (direct endpoint, bypasses OpenClaw) ─────────────


@router.get("/tier")
async def tier_status():
    tiers = llm_proxy.tier_snapshot()
    await llm_proxy._check_openrouter_balance()  # refresh the cache
    return {
        "tiers": tiers,
        "last_used": llm_proxy._last_tier_used or "none",
        "balance_remaining": llm_proxy.describe_balance(),
        "balance_floor": f"${llm_proxy.OPENROUTER_BALANCE_FLOOR:.2f}",
        "paid_auto_fallback": "off",
    }


@router.post("/tier/notify")
async def tier_notify():
    """Send tier status to Telegram — hit this endpoint to get status in chat."""
    lines = ["*Tier Status*\n"]
    lines += [f"  [{t['status']}] {t['name']}" for t in llm_proxy.tier_snapshot()]

    await llm_proxy._check_openrouter_balance()  # refresh the cache
    floor = llm_proxy.OPENROUTER_BALANCE_FLOOR
    lines.append(f"\nBalance: {llm_proxy.describe_balance()} (floor: ${floor:.2f})")
    lines.append(f"Last used: {llm_proxy._last_tier_used or 'none'}")
    lines.append("Paid fallback: off (send !paid or !claude to enable)")

    text = "\n".join(lines)
    sent = await notify(text)
    return {"sent": sent, "message": text}


# ─── Test Webhook (Connectivity Canary) ─────────────────────────────


@router.post("/webhook/test")
def test_webhook(body: dict[str, Any] = {}):
    keys = list(body.keys())
    summary = ", ".join(f"{k}={body[k]!r}" for k in keys) if keys else "(empty payload)"
    return {
        "status": "ok",
        "message": f"received: {summary}",
        "receivedAt": datetime.now(UTC).isoformat(),
        "payloadKeys": keys,
        "echo": body,
    }


# ─── Projects (Marketing Orchestrator) ────────────────────────────


class ProjectCreate(BaseModel):
    slug: str
    owner: str
    repo: str
    topics: list[str] = []
    subreddits: list[str] = []
    search_terms: list[str] = []
    posture: str = ""


@router.get("/projects")
def list_projects(brain_db: Brain):
    projects = brain_db.list_projects()
    return {"projects": projects, "total": len(projects)}


@router.get("/projects/{slug}")
def get_project(slug: str, brain_db: Brain):
    project = brain_db.get_project(slug)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/projects")
def upsert_project(body: ProjectCreate, brain_db: Brain):
    return brain_db.upsert_project(
        body.slug,
        body.owner,
        body.repo,
        body.topics,
        body.subreddits,
        body.search_terms,
        body.posture,
    )


@router.delete("/projects/{slug}")
def delete_project(slug: str, brain_db: Brain):
    if not brain_db.delete_project(slug):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": True}


# ─── Schedules (Marketing Orchestrator) ───────────────────────────


class ScheduleCreate(BaseModel):
    id: str
    name: str
    kind: str
    cron: str
    kwargs: dict = {}


class SchedulePatch(BaseModel):
    cron: str | None = None
    kwargs: dict | None = None
    paused: bool | None = None


def _schedule_view(sched: dict[str, Any], full: bool) -> dict[str, Any]:
    """Anonymous callers (the dashboard) get no kwargs and only ok/failed.

    kwargs can hold search queries and project slugs, and the raw status
    text can hold error detail; neither belongs on a public route.
    """
    if full:
        return sched
    view = {k: v for k, v in sched.items() if k != "kwargs"}
    status = sched.get("last_status")
    if status:
        view["last_status"] = "ok" if status_kind(status) == "ok" else "failed"
    return view


def _next_fire_times(scheduler) -> dict[str, str | None]:
    """Schedule id to the next fire time of its registered job."""
    return {
        j["id"].removeprefix("marketing_"): j["next_fire_time"]
        for j in list_scheduled_jobs(scheduler)
        if j["id"].startswith("marketing_")
    }


@router.get("/sched")
def list_schedules(request: Request, brain_db: Brain, scheduler: LiveScheduler):
    scheds = brain_db.list_schedules()
    fires = _next_fire_times(scheduler)
    for s in scheds:
        s["next_fire_time"] = fires.get(s["id"])
    full = auth.is_authenticated(request)
    return {
        "schedules": [_schedule_view(s, full) for s in scheds],
        "total": len(scheds),
    }


@router.get("/sched/{schedule_id}")
def get_schedule(
    schedule_id: str, request: Request, brain_db: Brain, scheduler: LiveScheduler
):
    sched = brain_db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    sched["next_fire_time"] = _next_fire_times(scheduler).get(sched["id"])
    return _schedule_view(sched, auth.is_authenticated(request))


@router.post("/sched")
async def create_schedule(
    body: ScheduleCreate, brain_db: Brain, scheduler: LiveScheduler
):
    try:
        validate_schedule(body.kind, body.cron, body.kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sched = await add_schedule(
        scheduler,
        brain_db,
        body.id,
        body.name,
        body.kind,
        body.cron,
        body.kwargs,
    )
    return sched


@router.patch("/sched/{schedule_id}")
async def update_schedule(
    schedule_id: str, body: SchedulePatch, brain_db: Brain, scheduler: LiveScheduler
):
    if body.paused is not None:
        if body.paused:
            return await pause_schedule(
                scheduler,
                brain_db,
                schedule_id,
            )
        else:
            return await resume_schedule(
                scheduler,
                brain_db,
                schedule_id,
            )

    if body.cron is not None or body.kwargs is not None:
        existing = brain_db.get_schedule(schedule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Schedule not found")
        new_kwargs = (
            body.kwargs
            if body.kwargs is not None
            else json.loads(existing.get("kwargs") or "{}")
        )
        try:
            validate_schedule(
                existing["kind"], body.cron or existing["cron"], new_kwargs
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await add_schedule(
            scheduler,
            brain_db,
            schedule_id,
            existing["name"],
            existing["kind"],
            body.cron or existing["cron"],
            new_kwargs,
        )

    return brain_db.get_schedule(schedule_id)


@router.delete("/sched/{schedule_id}")
async def delete_schedule(schedule_id: str, brain_db: Brain, scheduler: LiveScheduler):
    if not await remove_schedule(
        scheduler,
        brain_db,
        schedule_id,
    ):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"deleted": True}


@router.post("/sched/{schedule_id}/run")
async def run_schedule(schedule_id: str, brain_db: Brain, scheduler: LiveScheduler):
    try:
        return await run_schedule_now(
            scheduler,
            brain_db,
            schedule_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── Ad-hoc Scans (Marketing Orchestrator) ────────────────────────


def _bad_gateway(source: str, exc: Exception | str) -> HTTPException:
    """A scanner that could not run is a 502, never an empty 200 result."""
    return HTTPException(status_code=502, detail=f"{source} search failed: {exc}")


@router.post("/scan/reddit")
async def scan_reddit(body: dict[str, Any], brain_db: Brain):
    topic = body.get("topic", "")
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")

    subreddits = body.get("subreddits", [])
    project_slug = body.get("project_slug")

    subreddits = tenant_profile.resolve_subreddits(brain_db, subreddits, project_slug)

    try:
        results = await reddit_search.search_subreddits(
            topic,
            subreddits,
            since=body.get("since", "7d"),
            limit_per_sub=body.get("limit", 25),
        )
    except UpstreamError as exc:
        raise _bad_gateway("reddit", exc) from exc

    task_id = None
    if body.get("deliver_to_telegram", True) and results:
        desc = f"Reddit scan: '{topic}' found {len(results)} posts across {subreddits}"
        task = brain_db.create_task(desc, priority=3, source="scan")
        task_id = task["id"]

    return {
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "query": topic,
        "subreddits": subreddits,
        "task_id": task_id,
    }


@router.post("/scan/github")
async def scan_github(body: dict[str, Any], brain_db: Brain):
    kind = body.get("kind", "repos")

    if kind == "self_traffic":
        project_slug = body.get("project_slug")
        if not project_slug:
            raise HTTPException(
                status_code=400, detail="project_slug required for self_traffic"
            )
        project = brain_db.get_project(project_slug)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        traffic = await github_search.get_self_traffic(
            project["owner"], project["repo"]
        )
        return {"traffic": traffic.model_dump() if traffic else None}

    topic = body.get("topic", "")
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")

    if kind not in ("repos", "issues"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown kind: {kind}. Use repos, issues, or self_traffic",
        )
    try:
        if kind == "repos":
            results: list = await github_search.search_repos(
                topic,
                min_stars=body.get("min_stars", 0),
                language=body.get("language"),
                limit=body.get("limit", 25),
            )
        else:
            results = await github_search.search_issues(
                topic, limit=body.get("limit", 25)
            )
    except UpstreamError as exc:
        raise _bad_gateway("github", exc) from exc
    return {
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "query": topic,
        "kind": kind,
    }


@router.post("/scan/web")
async def scan_web(body: dict[str, Any]):
    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    result = await llm_proxy._llm_call(prompt, max_tokens=1500, web_search=True)
    if not result:
        raise _bad_gateway("web search", "no tier returned a result")
    return {"result": result, "query": prompt}


# ─── Research Orchestrator ───────────────────────────────────────


class ResearchRequest(BaseModel):
    topic: str
    channels: list[research.ResearchChannel] | None = None
    subreddits: list[str] | None = None
    limit: int = 10
    since: str = "30d"
    min_stars: int = 50


@router.post("/research")
async def run_research(body: ResearchRequest, brain_db: Brain):
    """Run a multi-source research session and return ranked findings.

    Channels default to discourse (Reddit), code (GitHub), and
    discourse_web (the proxy's web-search tier). Findings are deduplicated by URL,
    ranked with authority and recency bonuses, and tagged with a
    confidence flag based on cross-channel triangulation.

    The full session (topic, channels, findings) is persisted in the
    brain so the assistant can recall earlier research without re-running
    the fanout. The response includes `session_id` for follow-up
    queries against `/research/sessions/{id}`.
    """
    if not body.topic.strip():
        raise HTTPException(status_code=400, detail="topic is required")

    kwargs: dict[str, Any] = {
        "limit": body.limit,
        "since": body.since,
        "min_stars": body.min_stars,
    }
    if body.subreddits:
        kwargs["subreddits"] = body.subreddits

    try:
        result = await research.orchestrate_research(
            body.topic, channels=body.channels, **kwargs
        )
    except UpstreamError as exc:  # every channel failed; partials are in errors
        raise _bad_gateway("research", exc) from exc

    session = brain_db.create_research_session(body.topic, result["channels"])
    for f in result["findings"]:
        brain_db.add_research_finding(
            session_id=session["id"],
            source=f["source"],
            channel=f["channel"],
            title=f["title"],
            url=f["url"],
            relevance=f["relevance"],
            summary=f["summary"],
            metadata=f.get("metadata", {}),
        )
    brain_db.complete_research_session(session["id"])

    result["session_id"] = session["id"]
    return result


@router.get("/healthz/research")
def healthz_research():
    """Per-channel research orchestrator readiness report."""
    report = research.channel_health()
    healthy = sum(1 for v in report.values() if v["configured"])
    return {
        "channels": report,
        "configured_count": healthy,
        "total_channels": len(report),
        "status": "ok" if healthy >= 1 else "degraded",
    }


@router.get("/research/sessions")
def list_research_sessions(brain_db: Brain, limit: int = 25):
    """List recent research sessions, newest first."""
    sessions = brain_db.list_research_sessions(limit=limit)
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/research/sessions/{session_id}")
def get_research_session(session_id: str, brain_db: Brain):
    """Return a research session with all findings."""
    session = brain_db.get_research_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Research session not found")
    return session


# ─── App Factory ──────────────────────────────────────────────────


def create_app(brain_db: BrainDB | None = None) -> FastAPI:
    """Build a workflows app around ``brain_db`` (default: ``open_brain()``).

    Every app gets its own state: routes, chat commands and the heartbeat
    read the brain, profile and scheduler off ``request.app.state``, so two
    apps in one process never share a database. The profile and scheduler
    are wired by the lifespan, on startup.
    """
    _configure_logging()
    app = FastAPI(
        title="ClawRange Workflows",
        lifespan=lifespan,
        # Bearer auth on every route except the read-only allowlist in auth.py.
        dependencies=[Depends(auth.require_token)],
        # FastAPI serves its docs routes outside `dependencies`, so they would
        # expose the route and schema map anonymously. Off.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    db = brain_db if brain_db is not None else open_brain()
    app.state.brain_db = db

    def profile_provider():
        return _current_profile(app)

    app.state.profile_provider = profile_provider
    app.include_router(llm_router)
    app.include_router(create_brain_router(db), prefix="/brain")
    app.include_router(
        create_persona_router(db, profile_provider, _persona_render_targets)
    )
    app.include_router(router)
    return app
