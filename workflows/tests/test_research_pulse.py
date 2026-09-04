"""Tests for the research_pulse generator.

Research only runs via ``POST /research`` or the tome bridge; the LLM
personas have no tool that can issue a POST, so a research task queued
for the agent hits a dead end. ``research_pulse_generator`` closes that
gap server-side: when research has gone stale it enqueues a
``research:tome: <topic>`` task that ``scripts/tome_bridge.py`` already
recognises and runs through the local ``/tome:research`` session.
"""

from datetime import UTC, datetime, timedelta

import pytest

import generators
from app import brain_db

TOME_PREFIX = "research:tome:"


def _pending_descs() -> list[str]:
    return [t["description"] for t in brain_db.list_tasks(status="pending")]


def _backdate_latest_session(hours: int) -> None:
    """Push the most recent research session's created_at into the past."""
    old = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    sid = brain_db.list_research_sessions(limit=1)[0]["id"]
    brain_db._conn.execute(
        "UPDATE research_sessions SET created_at = ? WHERE id = ?", (old, sid)
    )
    brain_db._conn.commit()


def test_registered_in_generators():
    assert "research_pulse" in generators.GENERATORS


@pytest.mark.asyncio
async def test_enqueues_tome_task_when_no_research():
    await generators.research_pulse_generator(
        brain_db, topics=["trade-skill knowledge capture tooling 2025"]
    )
    pending = _pending_descs()
    assert pending == ["research:tome: trade-skill knowledge capture tooling 2025"]


@pytest.mark.asyncio
async def test_skips_when_research_is_fresh():
    # A session created just now is well inside the 24h window.
    brain_db.create_research_session("anything recent", ["discourse"])
    await generators.research_pulse_generator(brain_db, topics=["skrills gaps"])
    assert _pending_descs() == []


@pytest.mark.asyncio
async def test_enqueues_when_research_is_stale():
    brain_db.create_research_session("old topic", ["discourse"])
    _backdate_latest_session(hours=48)
    await generators.research_pulse_generator(brain_db, topics=["skrills gaps"])
    assert _pending_descs() == ["research:tome: skrills gaps"]


@pytest.mark.asyncio
async def test_dedups_already_pending_tome_task():
    brain_db.create_task("research:tome: skrills gaps", source="schedule")
    await generators.research_pulse_generator(brain_db, topics=["skrills gaps"])
    # Still exactly one — no duplicate enqueued.
    assert _pending_descs() == ["research:tome: skrills gaps"]


@pytest.mark.asyncio
async def test_rotates_to_next_unqueued_topic():
    brain_db.create_task("research:tome: topic A", source="schedule")
    await generators.research_pulse_generator(brain_db, topics=["topic A", "topic B"])
    assert "research:tome: topic B" in _pending_descs()


@pytest.mark.asyncio
async def test_falls_back_to_tracked_project_topics():
    brain_db.upsert_project(
        "skrills", "athola", "skrills", topics=["chrome extension knowledge capture"]
    )
    await generators.research_pulse_generator(brain_db)
    assert _pending_descs() == ["research:tome: chrome extension knowledge capture"]


@pytest.mark.asyncio
async def test_skips_when_no_topic_available():
    # No explicit topics and no tracked projects -> nothing to research.
    await generators.research_pulse_generator(brain_db)
    assert _pending_descs() == []
