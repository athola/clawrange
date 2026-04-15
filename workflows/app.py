"""ClawRange Workflow Service — replaces n8n with testable Python endpoints."""

import os
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

app = FastAPI(title="ClawRange Workflows", version="3.0.0")
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
