"""/persona router — propose/approve/reject persona learnings and re-render."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import persona_learning as pl
from persona import compose_identity, compose_persona, render_all


class Proposal(BaseModel):
    kind: str
    target: str = ""
    content: str
    source: str = "feedback"


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
            return pl.reject(brain_db, p.name, learning_id)
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

    @router.get("/healthz/persona")
    def health():
        p = _profile()
        pending = brain_db.list_learnings(profile=p.name, status="pending")
        return {
            "profile": p.name,
            "pending": len(pending),
            "targets": list(render_targets_fn(p).keys()),
        }

    return router
