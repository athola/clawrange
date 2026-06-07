"""Tests for the CRM-facing generators (FR-6).

``pipeline_generator`` runs a profile connector into the CRM and reports;
``crm_digest_generator`` formats query templates into a Telegram digest.
Both take injectable ``profile``/``crm`` so tests stay offline, and
``telegram.notify`` is monkeypatched to capture delivery.
"""

from __future__ import annotations

import httpx
import pytest

import generators
import telegram
from crm.sqlite_adapter import SQLiteCRM
from tenant_profile import Profile

CSV = (
    "Full Name,Email Address,Lead Source,Created\n"
    "Ada Lovelace,ada@x.com,portal,2026-01-01\n"
    "Grace Hopper,grace@x.com,referral,2026-01-03\n"
)


class FakeBrain:
    """Minimal brain_db stand-in capturing schedule-status writes."""

    def __init__(self):
        self.status: list[tuple[str, str]] = []

    def update_schedule_status(self, schedule_id, last_run, last_status):
        self.status.append((schedule_id, last_status))


@pytest.fixture()
def crm(tmp_path):
    c = SQLiteCRM(str(tmp_path / "crm.db"))
    c.init()
    return c


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
                    "params": {"window": {"type": "duration", "default": "7d"}},
                    "sql": "SELECT COUNT(*) AS n FROM leads WHERE created_at >= :since",
                },
                {
                    "name": "leads_by_status",
                    "params": {},
                    "sql": "SELECT status, COUNT(*) AS n FROM leads "
                    "GROUP BY status ORDER BY n DESC",
                },
            ],
        },
    }
    return Profile(name="t", raw=raw)


def test_crm_generators_registered():
    assert "pipeline" in generators.GENERATORS
    assert "crm_digest" in generators.GENERATORS


@pytest.mark.asyncio
async def test_pipeline_generator_writes_and_reports(crm, monkeypatch):
    sent = []

    async def fake_notify(msg):
        sent.append(msg)
        return True

    monkeypatch.setattr(telegram, "notify", fake_notify)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=CSV))
    )
    fb = FakeBrain()

    counts = await generators.pipeline_generator(
        fb,
        connector="portal-leads",
        profile=_profile(),
        crm=crm,
        schedule_id="lead-sync",
        http_client=client,
    )

    assert counts["written"] == 2
    assert len(crm.list("leads")) == 2
    assert fb.status and fb.status[-1][0] == "lead-sync"
    assert "2 written" in fb.status[-1][1]
    assert sent  # delivered a one-line summary


@pytest.mark.asyncio
async def test_pipeline_generator_unknown_connector(crm, monkeypatch):
    async def fake_notify(msg):
        return True

    monkeypatch.setattr(telegram, "notify", fake_notify)
    fb = FakeBrain()
    counts = await generators.pipeline_generator(
        fb,
        connector="does-not-exist",
        profile=_profile(),
        crm=crm,
        schedule_id="lead-sync",
    )
    assert counts is None  # graceful: no crash, no rows


@pytest.mark.asyncio
async def test_crm_digest_generator_formats_and_delivers(crm, monkeypatch):
    crm.upsert(
        "leads",
        [
            {"name": "A", "email": "a@x.com", "status": "new"},
            {"name": "B", "email": "b@x.com", "status": "won"},
        ],
        "email",
    )
    sent = []

    async def fake_notify(msg):
        sent.append(msg)
        return True

    monkeypatch.setattr(telegram, "notify", fake_notify)
    fb = FakeBrain()

    digest = await generators.crm_digest_generator(
        fb,
        queries=["new_leads_count", "leads_by_status"],
        profile=_profile(),
        crm=crm,
        schedule_id="crm-digest",
    )

    assert sent and sent[0] == digest
    assert "new_leads_count" in digest
    assert "won" in digest  # leads_by_status row rendered
