"""ClawRange workflows service: brain, task queue, LLM proxy, scheduler, scanners."""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

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
from telegram import notify
from tenant_profile import load_profile

# Wire INFO-level logging for the clawrange.* loggers so cron
# heartbeats and digest delivery confirmations show up in
# `docker logs msp-workflows` without needing a global basicConfig
# (uvicorn already configures its own root handlers).
logging.getLogger("clawrange").setLevel(logging.INFO)
if not logging.getLogger("clawrange").handlers:
    _clawrange_handler = logging.StreamHandler()
    _clawrange_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    logging.getLogger("clawrange").addHandler(_clawrange_handler)

logger = logging.getLogger("clawrange.app")

# ─── Database Initialization ─────────────────────────────────────

_db_path = os.environ.get("BRAIN_DB_PATH", "/data/brain.db")
brain_db = BrainDB(_db_path)
brain_db.init_db()
# Vectors live in one table per embedding model (see embeddings.py).
_embed_cfg = embeddings.config()
brain_db.use_vector_table(_embed_cfg.table, _embed_cfg.dim)

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

    return profile


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.warn_if_unset(llm_proxy.PROXY_AUTH_TOKEN)
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


app = FastAPI(
    title="ClawRange Workflows",
    version="3.0.0",
    lifespan=lifespan,
    # Bearer auth on every route except the read-only allowlist in auth.py.
    dependencies=[Depends(auth.require_token)],
    # FastAPI serves its docs routes outside `dependencies`, so they would
    # expose the route and schema map anonymously. Off.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _scheduler():
    """The running scheduler, or None (lifespan not run or start failed)."""
    return getattr(app.state, "scheduler", None)


app.include_router(llm_router)
app.include_router(create_brain_router(brain_db), prefix="/brain")


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


def _current_profile():
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


app.include_router(
    create_persona_router(brain_db, _current_profile, _persona_render_targets)
)


# ─── Task Queue (Persistent via BrainDB) ──────────────────────────


class TaskCreate(BaseModel):
    description: str = Field(min_length=1, max_length=4000)
    priority: int = 3  # 1=urgent, 3=normal, 5=low
    # user (via !task / API) or system (heartbeat-generated). Internal
    # producers (persona, schedule, scan) write via brain_db directly, so the
    # public API can't impersonate them.
    source: Literal["user", "system"] = "user"

    @field_validator("description")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank")
        return v


class TaskResult(BaseModel):
    result: str
    status: TaskStatus = TaskStatus.COMPLETED

    @field_validator("status")
    @classmethod
    def _closing_status(cls, v: TaskStatus) -> TaskStatus:
        if v not in TaskStatus.RESULTS:
            raise ValueError(f"status must be one of {sorted(TaskStatus.RESULTS)}")
        return v


@app.post("/task")
def create_task(body: TaskCreate):
    return brain_db.create_task(body.description, body.priority, body.source)


@app.get("/task")
def list_tasks(status: TaskStatus | None = None):
    tasks = brain_db.list_tasks(status)
    return {"tasks": tasks, "total": len(tasks)}


@app.get("/task/{task_id}")
def get_task(task_id: str):
    task = brain_db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/task/{task_id}/claim")
def claim_task(task_id: str):
    """Mark a pending task as active: a worker calls this when it starts."""
    try:
        return brain_db.claim_task(task_id)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 409
        raise HTTPException(status_code=status_code, detail=str(e))


@app.post("/task/{task_id}/result")
def complete_task(task_id: str, body: TaskResult):
    """Store a worker's result and close the task (completed, failed or
    blocked)."""
    if not brain_db.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        return brain_db.complete_task(task_id, body.result, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/task/{task_id}")
def cancel_task(task_id: str):
    try:
        return brain_db.cancel_task(task_id)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)


# ─── Health ─────────────────────────────────────────────────────────


@app.get("/healthz")
def healthz(request: Request):
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
    running = _scheduler()
    if sched["status"] in ("failed", "degraded") or (
        running is not None and not running.running
    ):
        problems.append("scheduler")
    profile = getattr(app.state, "profile", None)
    profile_error = getattr(app.state, "profile_error", None)
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
            "embedding_model": _embed_cfg.model,
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


@app.get("/tier")
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


@app.post("/tier/notify")
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


@app.post("/webhook/test")
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


@app.get("/projects")
def list_projects():
    projects = brain_db.list_projects()
    return {"projects": projects, "total": len(projects)}


@app.get("/projects/{slug}")
def get_project(slug: str):
    project = brain_db.get_project(slug)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.post("/projects")
def upsert_project(body: ProjectCreate):
    return brain_db.upsert_project(
        body.slug,
        body.owner,
        body.repo,
        body.topics,
        body.subreddits,
        body.search_terms,
        body.posture,
    )


@app.delete("/projects/{slug}")
def delete_project(slug: str):
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


def _next_fire_times() -> dict[str, str | None]:
    """Schedule id to the next fire time of its registered job."""
    return {
        j["id"].removeprefix("marketing_"): j["next_fire_time"]
        for j in list_scheduled_jobs(_scheduler())
        if j["id"].startswith("marketing_")
    }


@app.get("/sched")
def list_schedules(request: Request):
    scheds = brain_db.list_schedules()
    fires = _next_fire_times()
    for s in scheds:
        s["next_fire_time"] = fires.get(s["id"])
    full = auth.is_authenticated(request)
    return {
        "schedules": [_schedule_view(s, full) for s in scheds],
        "total": len(scheds),
    }


@app.get("/sched/{schedule_id}")
def get_schedule(schedule_id: str, request: Request):
    sched = brain_db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    sched["next_fire_time"] = _next_fire_times().get(sched["id"])
    return _schedule_view(sched, auth.is_authenticated(request))


@app.post("/sched")
async def create_schedule(body: ScheduleCreate):
    try:
        validate_schedule(body.kind, body.cron, body.kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sched = await add_schedule(
        _scheduler(),
        brain_db,
        body.id,
        body.name,
        body.kind,
        body.cron,
        body.kwargs,
    )
    return sched


@app.patch("/sched/{schedule_id}")
async def update_schedule(schedule_id: str, body: SchedulePatch):
    if body.paused is not None:
        if body.paused:
            return await pause_schedule(
                _scheduler(),
                brain_db,
                schedule_id,
            )
        else:
            return await resume_schedule(
                _scheduler(),
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
            _scheduler(),
            brain_db,
            schedule_id,
            existing["name"],
            existing["kind"],
            body.cron or existing["cron"],
            new_kwargs,
        )

    return brain_db.get_schedule(schedule_id)


@app.delete("/sched/{schedule_id}")
async def delete_schedule(schedule_id: str):
    if not await remove_schedule(
        _scheduler(),
        brain_db,
        schedule_id,
    ):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"deleted": True}


@app.post("/sched/{schedule_id}/run")
async def run_schedule(schedule_id: str):
    try:
        return await run_schedule_now(
            _scheduler(),
            brain_db,
            schedule_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── Ad-hoc Scans (Marketing Orchestrator) ────────────────────────


def _bad_gateway(source: str, exc: Exception | str) -> HTTPException:
    """A scanner that could not run is a 502, never an empty 200 result."""
    return HTTPException(status_code=502, detail=f"{source} search failed: {exc}")


@app.post("/scan/reddit")
async def scan_reddit(body: dict[str, Any]):
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


@app.post("/scan/github")
async def scan_github(body: dict[str, Any]):
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


@app.post("/scan/web")
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


@app.post("/research")
async def run_research(body: ResearchRequest):
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


@app.get("/healthz/research")
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


@app.get("/research/sessions")
def list_research_sessions(limit: int = 25):
    """List recent research sessions, newest first."""
    sessions = brain_db.list_research_sessions(limit=limit)
    return {"sessions": sessions, "total": len(sessions)}


@app.get("/research/sessions/{session_id}")
def get_research_session(session_id: str):
    """Return a research session with all findings."""
    session = brain_db.get_research_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Research session not found")
    return session
