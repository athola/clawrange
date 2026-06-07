"""Tests for the NL query router (FR-5.2 / FR-5.3) with a stub LLM."""

from __future__ import annotations

import pytest

from crm.query import answer, route_nl
from crm.sqlite_adapter import SQLiteCRM

pytestmark = pytest.mark.asyncio


TEMPLATES = [
    {
        "name": "new_leads_count",
        "description": "Count leads created within a recent window.",
        "params": {"window": {"type": "duration", "default": "7d"}},
        "sql": "SELECT COUNT(*) AS n FROM leads WHERE created_at >= :since",
    },
    {
        "name": "leads_by_status",
        "description": "Lead counts grouped by status.",
        "params": {},
        "sql": "SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY n DESC",
    },
]


def _stub(response: str):
    async def _llm(_prompt: str) -> str:
        return response

    return _llm


@pytest.fixture()
def crm(tmp_path):
    c = SQLiteCRM(str(tmp_path / "crm.db"))
    c.init()
    c.upsert(
        "leads",
        [
            {"name": "A", "email": "a@x.com", "status": "new"},
            {"name": "B", "email": "b@x.com", "status": "won"},
        ],
        "email",
    )
    return c


async def test_route_nl_selects_template():
    out = await route_nl(
        "how many leads this week?",
        TEMPLATES,
        llm=_stub('{"template": "new_leads_count", "params": {"window": "7d"}}'),
    )
    assert out["template"] == "new_leads_count"
    assert out["params"] == {"window": "7d"}


async def test_route_nl_parses_fenced_json():
    out = await route_nl(
        "status breakdown",
        TEMPLATES,
        llm=_stub('```json\n{"template": "leads_by_status", "params": {}}\n```'),
    )
    assert out["template"] == "leads_by_status"


async def test_route_nl_unknown_template_is_none():
    out = await route_nl(
        "x", TEMPLATES, llm=_stub('{"template": "nonexistent", "params": {}}')
    )
    assert out["template"] is None


async def test_route_nl_malformed_is_none():
    out = await route_nl("x", TEMPLATES, llm=_stub("I have no idea, sorry."))
    assert out["template"] is None


async def test_answer_returns_count(crm):
    res = await answer(
        "how many new leads this week?",
        crm,
        TEMPLATES,
        llm=_stub('{"template": "new_leads_count", "params": {"window": "7d"}}'),
    )
    assert "new_leads_count" in res["answer"]
    assert res["rows"][0]["n"] == 2


async def test_answer_grouped(crm):
    res = await answer(
        "break down leads by status",
        crm,
        TEMPLATES,
        llm=_stub('{"template": "leads_by_status", "params": {}}'),
    )
    assert "new: 1" in res["answer"] and "won: 1" in res["answer"]


async def test_answer_fallback_on_unmappable(crm):
    res = await answer("what is the weather", crm, TEMPLATES, llm=_stub("nope"))
    assert res["template"] is None
    assert "couldn't map" in res["answer"].lower()
