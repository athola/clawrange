"""/persona router — propose/approve/reject persona learnings and re-render."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import persona_learning as pl
from generators import persona_reflect_generator
from persona import (
    compose_identity,
    compose_persona,
    planned_writes,
    render_all,
    target_writable,
)


class Proposal(BaseModel):
    kind: str
    target: str = ""
    content: str
    # `seed` (learned.yaml) and `signal` (scan_signals) are internal
    # producers that call persona_learning directly; API callers can't
    # claim them.
    source: Literal["feedback", "reflect", "scheduled"] = "feedback"


def _run_reflection(brain_db, profile_name):
    """Override point; default runs the generator. Patched in tests."""
    return asyncio.run(persona_reflect_generator(brain_db, profile_name=profile_name))


def reflect_now(brain_db, profile_name) -> int:
    """One reflection pass plus the flagged signal scan; returns pending count.

    Shared by POST /persona/reflect and the ``!persona reflect`` chat
    command so the two cannot drift. Blocking: call it from a worker thread.
    """
    _run_reflection(brain_db, profile_name)
    # Flagged outcome-signal source; a no-op unless PERSONA_SIGNAL_LEARNING.
    pl.scan_signals(brain_db, profile_name)
    return len(brain_db.list_learnings(profile=profile_name, status="pending"))


def create_persona_router(brain_db, profile_provider, render_targets_fn) -> APIRouter:
    router = APIRouter()

    def _profile():
        return profile_provider()

    def _approved():
        p = _profile()
        return brain_db.list_learnings(profile=p.name, status="approved")

    def _render(approved):
        p = _profile()
        targets = render_targets_fn(p)
        return render_all(p, targets, approved)

    @router.post("/persona/propose")
    def propose(body: Proposal):
        p = _profile()
        try:
            return pl.propose(
                brain_db, p.name, body.kind, body.target, body.content, body.source
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/persona/proposals")
    def proposals(status: str | None = None):
        p = _profile()
        rows = brain_db.list_learnings(profile=p.name, status=status)
        return {"proposals": rows, "total": len(rows)}

    @router.post("/persona/proposals/{learning_id}/approve")
    def approve(learning_id: str):
        p = _profile()
        try:
            return pl.approve(brain_db, p.name, learning_id, render_fn=_render)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.post("/persona/proposals/{learning_id}/reject")
    def reject(learning_id: str):
        p = _profile()
        try:
            return pl.reject(brain_db, p.name, learning_id, render_fn=_render)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.post("/persona/render")
    def render():
        return {"targets": _render(_approved())}

    @router.get("/persona/identity")
    def identity():
        return {"identity": compose_identity(_profile(), _approved())}

    @router.get("/persona/soul")
    def soul():
        return {"soul": compose_persona(_profile(), _approved())}

    @router.post("/persona/reflect")
    def reflect():
        return {"queued": reflect_now(brain_db, _profile().name)}

    @router.get("/healthz/persona")
    def health():
        p = _profile()
        pending = brain_db.list_learnings(profile=p.name, status="pending")
        targets = render_targets_fn(p)
        # Per target group (soul, ws_soul, ...): can every file a render
        # would write be replaced right now? Keyed by group, not path: this
        # route is public and must not reveal the state directory.
        writable: dict[str, bool] = {}
        for key, path, _ in planned_writes(p, targets):
            group = key.split(":", 1)[0]
            writable[group] = writable.get(group, True) and target_writable(path)
        return {
            "profile": p.name,
            "pending": len(pending),
            "targets": list(targets.keys()),
            "writable": writable,
        }

    return router
