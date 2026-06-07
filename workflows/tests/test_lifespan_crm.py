"""Integration tests for profile-driven lifespan wiring (FR-8.1, T17).

``init_profile`` is the testable core of the app lifespan: it renders the
persona, seeds projects/schedules, and (when the profile defines a CRM)
inits the adapter on ``app.state.crm`` and mounts the ``/crm`` router. These
tests drive it with the bundled ``lead-crm`` profile against temp paths.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import init_profile
from brain_db import BrainDB
from tenant_profile import load_profile


def test_lead_crm_profile_validates():
    # Raises ProfileError if the shipped reference profile is malformed
    # (e.g. a schedule kind not in GENERATORS or an undefined connector).
    p = load_profile("lead-crm")
    assert p.crm is not None
    assert p.connector("portal-leads") is not None
    kinds = {s["kind"] for s in p.schedules}
    assert kinds == {"pipeline", "crm_digest"}


def test_init_profile_mounts_crm_and_seeds(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_DB_PATH", str(tmp_path / "crm.db"))
    monkeypatch.setenv("PORTAL_EXPORT_URL", "https://portal.example/export")
    monkeypatch.setenv("PORTAL_TOKEN", "demo-token")
    brain = BrainDB(str(tmp_path / "brain.db"))
    brain.init_db()
    profile = load_profile("lead-crm")

    app = FastAPI()
    init_profile(app, brain, profile=profile, soul_path=str(tmp_path / "soul.md"))

    # persona rendered from the structured fields (no persona_markdown)
    soul = (tmp_path / "soul.md").read_text()
    assert "Sales Assistant" in soul
    assert "Acme Co" in soul

    # schedules seeded from the profile
    sids = {s["id"] for s in brain.list_schedules()}
    assert {"lead-sync", "crm-digest"} <= sids

    # CRM mounted and healthy
    client = TestClient(app)
    health = client.get("/healthz/crm")
    assert health.status_code == 200
    assert health.json()["configured"] is True

    # a connector sync writes leads end-to-end through the mounted router.
    # Re-mount with an injected mock client so /crm/sync hits it, not the net.
    csv = (
        "Full Name,Email Address,Phone,Lead Source,Created\n"
        "Ada,ada@x.com,1,portal,2026-01-01\n"
    )
    mock_client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=csv))
    )
    app2 = FastAPI()
    init_profile(
        app2,
        brain,
        profile=profile,
        soul_path=str(tmp_path / "s2.md"),
        http_client=mock_client,
    )
    synced = TestClient(app2).post("/crm/sync/portal-leads")
    assert synced.status_code == 200
    assert synced.json()["written"] == 1
