"""Homelab HTTP API router.

A factory that builds ``/homelab/*`` and ``/healthz/homelab`` bound to a
tenant profile. ``app.py`` mounts it only when the active profile defines a
``homelab`` block, so other deployments expose none of these routes.

``/healthz/homelab`` is public (the dashboard polls it) and reports only
per-source ``configured``/``ok``/``last_success``/``failures``: never a URL,
and error text only for a caller with the bearer token.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

import auth
import telegram
from homelab import service
from homelab.config import HomelabConfig
from homelab.findings import UNAVAILABLE_STATES
from homelab.models import SOURCES, Observation


def _summarize(obs: Observation) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for source in SOURCES:
        configured = source in obs.configured
        entry: dict[str, Any] = {
            "configured": configured,
            "ok": getattr(obs, source) is not None if configured else None,
        }
        if source in obs.errors:
            entry["error"] = obs.errors[source]
        sources[source] = entry
    counts: dict[str, int] = {}
    if obs.ha is not None:
        counts["entities"] = len(obs.ha)
        counts["unavailable"] = sum(1 for e in obs.ha if e.state in UNAVAILABLE_STATES)
    if obs.kuma is not None:
        counts["monitors"] = len(obs.kuma)
        counts["monitors_down"] = sum(1 for m in obs.kuma if m.status == 0)
    if obs.census is not None:
        counts["devices"] = len(obs.census.devices)
        counts["randomized_devices"] = sum(
            1 for d in obs.census.devices if d.randomized
        )
        if obs.census.kev is not None:
            counts["kev_entries"] = len(obs.census.kev)
    return {"when": obs.now.isoformat(), "sources": sources, "counts": counts}


def create_homelab_router(profile) -> APIRouter:
    """Build the homelab router bound to ``profile``'s ``homelab`` block."""
    router = APIRouter(tags=["homelab"])
    cfg = HomelabConfig.from_profile(profile)

    @router.get("/homelab/status")
    async def status():
        if not cfg.configured:
            return {"status": "disabled"}
        return _summarize(await service.live_observation(cfg))

    @router.post("/homelab/watch")
    async def watch_now(request: Request):
        return await service.run_watch(
            request.app.state.brain_db, cfg, notify=telegram.notify
        )

    @router.get("/healthz/homelab")
    def healthz_homelab(request: Request):
        return service.health(
            request.app.state.brain_db,
            cfg,
            detail=auth.is_authenticated(request),
        )

    return router
