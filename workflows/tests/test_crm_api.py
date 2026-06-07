"""Tests for the CRM HTTP API router (FR-7).

The router is a factory mounted only when the active profile defines a CRM.
Tests build a throwaway FastAPI app around it with a temp SQLite CRM, a stub
LLM (for the NL query path), and an injected httpx client (for /crm/sync) so
everything runs offline.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crm.sqlite_adapter import SQLiteCRM
from crm_api import create_crm_router
from tenant_profile import Profile

CSV = (
    "Full Name,Email Address,Lead Source,Created\n"
    "Ada Lovelace,ada@x.com,portal,2026-01-01\n"
    "Grace Hopper,grace@x.com,referral,2026-01-03\n"
)


def _profile() -> Profile:
    raw = {
        "profile": "t",
        "connectors": [
            {
                "id": "portal-leads",
                "source": {
                    "kind": "http_csv",
                    "url": "https://p/e",
                    "auth": {"kind": "none"},
                },
                "transform": {
                    "kind": "leads_clean",
                    "mapping": {
                        "Full Name": "name",
                        "Email Address": "email",
                        "Lead Source": "source",
                        "Created": "created_at",
                    },
                    "required": ["email"],
                    "dedup_key": "email",
                },
                "sink": {"kind": "crm", "object": "leads", "upsert_key": "email"},
            }
        ],
        "crm": {
            "adapter": "sqlite",
            "query_templates": [
                {
                    "name": "new_leads_count",
                    "description": "recent count",
                    "params": {"window": {"type": "duration", "default": "7d"}},
                    "sql": "SELECT COUNT(*) AS n FROM leads WHERE created_at >= :since",
                },
                {
                    "name": "leads_by_status",
                    "description": "by status",
                    "params": {},
                    "sql": "SELECT status, COUNT(*) AS n FROM leads "
                    "GROUP BY status ORDER BY n DESC",
                },
            ],
        },
    }
    return Profile(name="t", raw=raw)


@pytest.fixture()
def client(tmp_path):
    crm = SQLiteCRM(str(tmp_path / "crm.db"))
    crm.init()
    crm.upsert(
        "leads",
        [
            {"name": "A", "email": "a@x.com", "status": "new"},
            {"name": "B", "email": "b@x.com", "status": "won"},
        ],
        "email",
    )

    async def stub_llm(prompt):
        return '{"template": "new_leads_count", "params": {"window": "7d"}}'

    http = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=CSV))
    )

    app = FastAPI()
    app.include_router(
        create_crm_router(_profile(), crm, llm=stub_llm, http_client=http)
    )
    return TestClient(app)


def test_templates_endpoint(client):
    r = client.get("/crm/templates")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["templates"]}
    assert names == {"new_leads_count", "leads_by_status"}


def test_leads_endpoint_filter(client):
    r = client.get("/crm/leads", params={"status": "won"})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert [row["email"] for row in rows] == ["b@x.com"]


def test_query_run_endpoint(client):
    r = client.post(
        "/crm/query/run", json={"template": "leads_by_status", "params": {}}
    )
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert {row["status"] for row in rows} == {"new", "won"}


def test_query_nl_endpoint(client):
    r = client.post("/crm/query", json={"prompt": "how many leads this week?"})
    assert r.status_code == 200
    body = r.json()
    assert body["template"] == "new_leads_count"
    assert "2" in body["answer"]


def test_sync_endpoint(client):
    r = client.post("/crm/sync/portal-leads")
    assert r.status_code == 200
    assert r.json()["written"] == 2


def test_healthz_crm(client):
    r = client.get("/healthz/crm")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["adapter"]["status"] == "ok"
    assert "portal-leads" in body["connectors"]
