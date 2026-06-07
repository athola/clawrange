"""CRM HTTP API router (FR-7).

A factory that builds the ``/crm/*`` + ``/healthz/crm`` routes bound to a
specific tenant profile and CRM adapter. ``app.py`` mounts it only when the
active profile defines ``crm``, so a marketing-only deployment exposes none
of these routes. ``llm`` and ``http_client`` are injectable so the NL-query
and connector-sync paths are testable offline.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from crm.query import answer, find_template, run_query

logger = logging.getLogger("clawrange.crm.api")


class QueryPrompt(BaseModel):
    prompt: str


class TemplateRun(BaseModel):
    template: str
    params: dict = {}


def create_crm_router(profile, crm, *, llm=None, http_client=None) -> APIRouter:
    """Build the CRM router bound to ``profile`` + ``crm`` adapter."""
    router = APIRouter(tags=["crm"])
    templates = profile.query_templates()

    def _answer_kwargs() -> dict:
        return {"llm": llm} if llm is not None else {}

    @router.get("/crm/templates")
    def list_templates():
        return {
            "templates": [
                {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "params": t.get("params", {}),
                }
                for t in templates
            ]
        }

    @router.get("/crm/leads")
    def list_leads(
        status: str | None = Query(default=None),
        limit: int = Query(default=100, le=1000),
    ):
        filters = {"status": status} if status else None
        return {"rows": crm.list("leads", filters=filters, limit=limit)}

    @router.post("/crm/query")
    async def query_nl(body: QueryPrompt):
        result = await answer(body.prompt, crm, templates, **_answer_kwargs())
        return result

    @router.post("/crm/query/run")
    def query_run(body: TemplateRun):
        template = find_template(templates, body.template)
        if template is None:
            raise HTTPException(404, f"unknown template '{body.template}'")
        try:
            rows = run_query(crm, template, body.params)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except NotImplementedError as exc:
            raise HTTPException(501, str(exc))
        return {"rows": rows, "template": body.template, "params": body.params}

    @router.post("/crm/sync/{connector_id}")
    def sync_connector(connector_id: str):
        from connectors import run_connector

        spec = profile.connector(connector_id)
        if spec is None:
            raise HTTPException(404, f"unknown connector '{connector_id}'")
        try:
            counts = run_connector(spec, crm, http_client=http_client)
        except Exception as exc:
            logger.warning("crm sync %s failed: %s", connector_id, exc)
            raise HTTPException(502, f"connector sync failed: {exc}")
        return counts

    @router.get("/healthz/crm")
    def healthz_crm():
        return {
            "configured": bool(profile.crm),
            "adapter": crm.health(),
            "connectors": [c.get("id") for c in profile.connectors],
            "templates": [t.get("name") for t in templates],
        }

    return router
