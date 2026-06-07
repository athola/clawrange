"""Tests for the connector framework (FR-3).

Sources are exercised against an injected ``httpx.MockTransport`` so no
network is touched; transforms and the end-to-end pipeline run against the
bundled SQLite CRM. Mirrors the DI posture used by the CRM/query tests.
"""

from __future__ import annotations

import httpx
import pytest

from connectors import SINKS, SOURCES, TRANSFORMS, run_connector
from connectors.sources import http_csv, login_scrape
from connectors.transforms import leads_clean
from crm.sqlite_adapter import SQLiteCRM

CSV = (
    "Full Name,Email Address,Lead Source,Created\n"
    "Ada Lovelace,ada@x.com,portal,2026-01-01\n"
    ",noname@x.com,portal,2026-01-02\n"
    "Grace Hopper,grace@x.com,referral,2026-01-03\n"
)


def _mock_client(handler, **kw) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), **kw)


# ─── registries (FR-3) ────────────────────────────────────────────


def test_registries_expose_known_kinds():
    assert "http_csv" in SOURCES and "login_scrape" in SOURCES
    assert "leads_clean" in TRANSFORMS and "passthrough" in TRANSFORMS
    assert "crm" in SINKS


# ─── sources: http_csv (FR-3.1) ───────────────────────────────────


def test_http_csv_parses_rows():
    client = _mock_client(lambda r: httpx.Response(200, text=CSV))
    rows = http_csv({"kind": "http_csv", "url": "https://p/e"}, client=client)
    assert len(rows) == 3
    assert rows[0]["Full Name"] == "Ada Lovelace"
    assert rows[0]["Email Address"] == "ada@x.com"


def test_http_csv_bearer_auth_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text=CSV)

    client = _mock_client(handler)
    http_csv(
        {
            "kind": "http_csv",
            "url": "https://p/e",
            "auth": {"kind": "bearer", "token": "T0K"},
        },
        client=client,
    )
    assert seen["auth"] == "Bearer T0K"


def test_http_csv_api_key_header():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, text=CSV)

    client = _mock_client(handler)
    http_csv(
        {
            "kind": "http_csv",
            "url": "https://p/e",
            "auth": {
                "kind": "api_key",
                "api_key": {"header": "X-API-Key", "value": "SECRET"},
            },
        },
        client=client,
    )
    assert seen["key"] == "SECRET"


def test_http_csv_unconfigured_url_degrades():
    # An unresolved ${VAR} resolves to "" -> treat as unconfigured, return [].
    assert http_csv({"kind": "http_csv", "url": ""}) == []


# ─── sources: login_scrape (FR-3.1) ───────────────────────────────


def test_login_scrape_session_gates_export():
    posted = {}

    def handler(request):
        if request.url.path == "/login":
            posted["body"] = request.content.decode()
            return httpx.Response(200, headers={"set-cookie": "session=abc; Path=/"})
        if request.url.path == "/export":
            if "session=abc" not in request.headers.get("cookie", ""):
                return httpx.Response(403, text="forbidden")
            return httpx.Response(200, text=CSV)
        return httpx.Response(404)

    client = _mock_client(handler, base_url="https://portal")
    spec = {
        "kind": "login_scrape",
        "auth": {
            "kind": "login_form",
            "login_form": {
                "login_url": "https://portal/login",
                "username_field": "user",
                "password_field": "pass",
                "username": "dana",
                "password": "pw",
                "export_path": "https://portal/export",
            },
        },
    }
    rows = login_scrape(spec, client=client)
    assert len(rows) == 3
    assert "user=dana" in posted["body"]


# ─── transforms: leads_clean (FR-3.2) ─────────────────────────────


def test_leads_clean_renames_drops_and_dedupes():
    rows = [
        {
            "Full Name": "Ada",
            "Email Address": "ada@x.com",
            "Lead Source": "portal",
            "Created": "2026-01-01",
        },
        {
            "Full Name": "NoEmail",
            "Email Address": "",
            "Lead Source": "portal",
            "Created": "",
        },
        {
            "Full Name": "Ada Again",
            "Email Address": "ada@x.com",
            "Lead Source": "referral",
            "Created": "2026-01-09",
        },
    ]
    spec = {
        "kind": "leads_clean",
        "mapping": {
            "Full Name": "name",
            "Email Address": "email",
            "Lead Source": "source",
            "Created": "created_at",
        },
        "required": ["email"],
        "dedup_key": "email",
    }
    out = leads_clean(rows, spec)
    assert len(out) == 1  # empty-email dropped; dupe collapsed
    assert out[0]["name"] == "Ada Again"  # last wins
    assert out[0]["source"] == "referral"
    assert out[0]["status"] == "new"  # default applied


def test_leads_clean_trims_and_defaults_created_at():
    rows = [
        {
            "Full Name": "  Spaced  ",
            "Email Address": " s@x.com ",
            "Lead Source": "portal",
            "Created": "",
        }
    ]
    spec = {
        "kind": "leads_clean",
        "mapping": {
            "Full Name": "name",
            "Email Address": "email",
            "Lead Source": "source",
            "Created": "created_at",
        },
        "required": ["email"],
        "dedup_key": "email",
    }
    out = leads_clean(rows, spec)
    assert out[0]["name"] == "Spaced"
    assert out[0]["email"] == "s@x.com"
    assert out[0]["created_at"]  # defaulted to now, non-empty


# ─── pipeline: run_connector end-to-end (FR-3.4) ──────────────────


def test_run_connector_end_to_end(tmp_path):
    client = _mock_client(lambda r: httpx.Response(200, text=CSV))
    crm = SQLiteCRM(str(tmp_path / "crm.db"))
    crm.init()
    spec = {
        "id": "portal-leads",
        "source": {"kind": "http_csv", "url": "https://p/e", "auth": {"kind": "none"}},
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
    counts = run_connector(spec, crm, http_client=client)
    assert counts == {"fetched": 3, "kept": 3, "written": 3}
    assert len(crm.list("leads")) == 3


def test_run_connector_unknown_source_raises(tmp_path):
    crm = SQLiteCRM(str(tmp_path / "crm.db"))
    crm.init()
    with pytest.raises(ValueError):
        run_connector({"source": {"kind": "nope"}, "sink": {"kind": "crm"}}, crm)
