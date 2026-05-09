"""ClawRange Workflow Service — replaces n8n with testable Python endpoints."""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from brain import create_brain_router
from brain_db import BrainDB
from llm_proxy import router as llm_router
from telegram import notify

# ─── Database Initialization ─────────────────────────────────────

_db_path = os.environ.get("BRAIN_DB_PATH", "/data/brain.db")
brain_db = BrainDB(_db_path)
brain_db.init_db()

# ─── Scheduler Setup ─────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    from scheduler import init_scheduler

    scheduler = init_scheduler(brain_db)
    app.state.scheduler = scheduler
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="ClawRange Workflows", version="3.0.0", lifespan=lifespan)
app.include_router(llm_router)
app.include_router(create_brain_router(brain_db), prefix="/brain")


# ─── Task Queue (Persistent via BrainDB) ──────────────────────────


class TaskCreate(BaseModel):
    description: str
    priority: int = 3  # 1=urgent, 3=normal, 5=low
    source: str = "user"  # user (via !task / API) or system (heartbeat-generated)


class TaskResult(BaseModel):
    result: str
    status: str = "completed"


@app.post("/task")
def create_task(body: TaskCreate):
    return brain_db.create_task(body.description, body.priority, body.source)


@app.get("/task")
def list_tasks(status: str | None = None):
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
    """Mark a pending task as active — called by max-ops at start of execution."""
    try:
        return brain_db.claim_task(task_id)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 409
        raise HTTPException(status_code=status_code, detail=str(e))


@app.post("/task/{task_id}/result")
def complete_task(task_id: str, body: TaskResult):
    """Store result and mark task complete — called by max-ops via web_fetch."""
    if body.status not in ("completed", "failed"):
        raise HTTPException(
            status_code=400, detail="Status must be completed or failed"
        )
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
def healthz():
    try:
        row = brain_db._conn.execute("SELECT COUNT(*) as cnt FROM pages").fetchone()
        total_pages = row["cnt"] if row else 0
    except Exception:
        total_pages = -1
    return {
        "status": "ok",
        "brain": {
            "db": "ok" if total_pages >= 0 else "error",
            "pages": total_pages,
            "embeddings": brain_db.has_embeddings(),
        },
    }


# ─── Tier Status (direct endpoint, bypasses OpenClaw) ─────────────


@app.get("/tier")
async def tier_status():
    from llm_proxy import (
        CONFIG,
        OPENROUTER_BALANCE_FLOOR,
        _check_openrouter_balance,
        _circuit_open,
        _last_tier_used,
    )

    tiers = []
    for tier in CONFIG["tiers"]:
        name = tier["name"]
        if _circuit_open(name):
            marker = "TRIPPED"
        elif name == _last_tier_used:
            marker = "ACTIVE"
        else:
            marker = "ready"
        tiers.append(
            {"name": name, "status": marker, "description": tier["description"]}
        )

    remaining = await _check_openrouter_balance()
    return {
        "tiers": tiers,
        "last_used": _last_tier_used or "none",
        "balance_remaining": f"${remaining:.2f}"
        if remaining is not None
        else "not configured",
        "balance_floor": f"${OPENROUTER_BALANCE_FLOOR:.2f}",
        "paid_auto_fallback": "off",
    }


@app.post("/tier/notify")
async def tier_notify():
    """Send tier status to Telegram — hit this endpoint to get status in chat."""
    from llm_proxy import (
        CONFIG,
        OPENROUTER_BALANCE_FLOOR,
        _check_openrouter_balance,
        _circuit_open,
        _last_tier_used,
    )

    lines = ["*Tier Status*\n"]
    for tier in CONFIG["tiers"]:
        name = tier["name"]
        if _circuit_open(name):
            marker = "TRIPPED"
        elif name == _last_tier_used:
            marker = "ACTIVE"
        else:
            marker = "ready"
        lines.append(f"  [{marker}] {name}")

    remaining = await _check_openrouter_balance()
    if remaining is not None:
        lines.append(
            f"\nBalance: ${remaining:.2f} (floor: ${OPENROUTER_BALANCE_FLOOR:.2f})"
        )
    else:
        lines.append("\nBalance: not configured")
    lines.append(f"Last used: {_last_tier_used or 'none'}")
    lines.append("Paid fallback: off (use 'paid' keyword)")

    text = "\n".join(lines)
    sent = await notify(text)
    return {"sent": sent, "message": text}


# ─── Test Webhook (Connectivity Canary) ─────────────────────────────


@app.post("/webhook/test")
@app.post("/webhook-test/test")
def test_webhook(body: dict[str, Any] = {}):
    keys = list(body.keys())
    summary = ", ".join(f"{k}={body[k]!r}" for k in keys) if keys else "(empty payload)"
    return {
        "status": "ok",
        "message": f"received: {summary}",
        "receivedAt": datetime.now(timezone.utc).isoformat(),
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
    return {
        "projects": brain_db.list_projects(),
        "total": len(brain_db.list_projects()),
    }


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


@app.get("/sched")
async def list_schedules():
    from scheduler import list_scheduled_jobs

    scheds = brain_db.list_schedules()
    jobs = (
        list_scheduled_jobs(app.state.scheduler)
        if hasattr(app.state, "scheduler")
        else []
    )
    job_map = {j["id"].replace("marketing_", ""): j for j in jobs}
    for s in scheds:
        j = job_map.get(s["id"])
        s["next_fire_time"] = j["next_fire_time"] if j else None
    return {"schedules": scheds, "total": len(scheds)}


@app.get("/sched/{schedule_id}")
async def get_schedule(schedule_id: str):
    sched = brain_db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return sched


@app.post("/sched")
async def create_schedule(body: ScheduleCreate):
    from scheduler import add_schedule

    sched = await add_schedule(
        app.state.scheduler if hasattr(app.state, "scheduler") else None,
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
    from scheduler import pause_schedule, resume_schedule, add_schedule

    if body.paused is not None:
        if body.paused:
            return await pause_schedule(
                app.state.scheduler if hasattr(app.state, "scheduler") else None,
                brain_db,
                schedule_id,
            )
        else:
            return await resume_schedule(
                app.state.scheduler if hasattr(app.state, "scheduler") else None,
                brain_db,
                schedule_id,
            )

    if body.cron is not None or body.kwargs is not None:
        existing = brain_db.get_schedule(schedule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return await add_schedule(
            app.state.scheduler if hasattr(app.state, "scheduler") else None,
            brain_db,
            schedule_id,
            existing["name"],
            existing["kind"],
            body.cron or existing["cron"],
            body.kwargs
            if body.kwargs is not None
            else json.loads(existing.get("kwargs", "{}")),
        )

    return brain_db.get_schedule(schedule_id)


@app.delete("/sched/{schedule_id}")
async def delete_schedule(schedule_id: str):
    from scheduler import remove_schedule

    if not await remove_schedule(
        app.state.scheduler if hasattr(app.state, "scheduler") else None,
        brain_db,
        schedule_id,
    ):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"deleted": True}


@app.post("/sched/{schedule_id}/run")
async def run_schedule(schedule_id: str):
    from scheduler import run_schedule_now

    try:
        return await run_schedule_now(
            app.state.scheduler if hasattr(app.state, "scheduler") else None,
            brain_db,
            schedule_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── Ad-hoc Scans (Marketing Orchestrator) ────────────────────────


@app.post("/scan/reddit")
async def scan_reddit(body: dict[str, Any]):
    from reddit_search import search_subreddits

    topic = body.get("topic", "")
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")

    subreddits = body.get("subreddits", [])
    project_slug = body.get("project_slug")

    if not subreddits and project_slug:
        project = brain_db.get_project(project_slug)
        if project:
            subreddits = json.loads(project.get("subreddits", "[]"))

    if not subreddits:
        subreddits = ["ClaudeAI", "LocalLLaMA", "SideProject"]

    results = await search_subreddits(
        topic,
        subreddits,
        since=body.get("since", "7d"),
        limit_per_sub=body.get("limit", 25),
    )

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
    from github_search import search_repos, search_issues, get_self_traffic

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
        traffic = await get_self_traffic(project["owner"], project["repo"])
        return {"traffic": traffic.model_dump() if traffic else None}

    topic = body.get("topic", "")
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")

    if kind == "repos":
        results = await search_repos(
            topic,
            min_stars=body.get("min_stars", 0),
            language=body.get("language"),
            limit=body.get("limit", 25),
        )
        return {
            "results": [r.model_dump() for r in results],
            "total": len(results),
            "query": topic,
            "kind": kind,
        }
    elif kind == "issues":
        results = await search_issues(topic, limit=body.get("limit", 25))
        return {
            "results": [r.model_dump() for r in results],
            "total": len(results),
            "query": topic,
            "kind": kind,
        }

    raise HTTPException(
        status_code=400,
        detail=f"Unknown kind: {kind}. Use repos, issues, or self_traffic",
    )


@app.post("/scan/web")
async def scan_web(body: dict[str, Any]):
    from llm_proxy import _llm_call

    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    result = await _llm_call(prompt, max_tokens=1500, web_search=True)
    return {"result": result or "", "query": prompt}


# ─── Research Orchestrator ───────────────────────────────────────


class ResearchRequest(BaseModel):
    topic: str
    channels: list[str] | None = None
    subreddits: list[str] | None = None
    limit: int = 10
    since: str = "30d"
    min_stars: int = 50


@app.post("/research")
async def research(body: ResearchRequest):
    """Run a multi-source research session and return ranked findings.

    Channels default to discourse (Reddit), code (GitHub), and
    discourse_web (GLM web search). Findings are deduplicated by URL,
    ranked with authority and recency bonuses, and tagged with a
    confidence flag based on cross-channel triangulation.

    The full session (topic, channels, findings) is persisted in the
    brain so John-117 can recall earlier research without re-running
    the fanout. The response includes `session_id` for follow-up
    queries against `/research/sessions/{id}`.
    """
    from research import orchestrate_research

    if not body.topic.strip():
        raise HTTPException(status_code=400, detail="topic is required")

    kwargs: dict[str, Any] = {
        "limit": body.limit,
        "since": body.since,
        "min_stars": body.min_stars,
    }
    if body.subreddits:
        kwargs["subreddits"] = body.subreddits

    result = await orchestrate_research(body.topic, channels=body.channels, **kwargs)

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
