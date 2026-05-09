"""Tests for persisting research sessions in the brain database.

Sessions are an audit trail John-117 can browse to recall earlier
research and continue digging into a topic without re-running the
full multi-source fanout.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from brain_db import BrainDB


@pytest.fixture()
def db():
    """Fresh in-memory BrainDB per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        b = BrainDB(path)
        b.init_db()
        yield b
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ─── Sessions ─────────────────────────────────────────────────────


class TestResearchSessions:
    def test_create_session_returns_id_and_metadata(self, db):
        s = db.create_research_session(
            topic="agent platforms",
            channels=["discourse", "code"],
        )
        assert s["id"]
        assert s["topic"] == "agent platforms"
        assert s["status"] == "pending"
        assert s["finding_count"] == 0
        assert s["created_at"]

    def test_add_finding_increments_count(self, db):
        s = db.create_research_session("topic", ["discourse"])
        db.add_research_finding(
            s["id"],
            source="reddit",
            channel="discourse",
            title="Post",
            url="https://r/p1",
            relevance=0.6,
            summary="hello",
            metadata={"score": 100},
        )
        loaded = db.get_research_session(s["id"])
        assert loaded["finding_count"] == 1
        assert loaded["findings"][0]["title"] == "Post"
        assert loaded["findings"][0]["metadata"]["score"] == 100

    def test_complete_session_marks_status(self, db):
        s = db.create_research_session("topic", ["discourse"])
        completed = db.complete_research_session(s["id"])
        assert completed["status"] == "complete"
        assert completed["updated_at"]

    def test_list_sessions_descending_by_created(self, db):
        s1 = db.create_research_session("first", ["discourse"])
        s2 = db.create_research_session("second", ["code"])
        listed = db.list_research_sessions(limit=10)
        ids = [row["id"] for row in listed]
        assert s2["id"] == ids[0]
        assert s1["id"] == ids[1]

    def test_list_sessions_respects_limit(self, db):
        for i in range(5):
            db.create_research_session(f"topic-{i}", ["discourse"])
        listed = db.list_research_sessions(limit=3)
        assert len(listed) == 3

    def test_get_unknown_session_returns_none(self, db):
        assert db.get_research_session("nonexistent") is None

    def test_findings_preserve_dedup_by_url_within_session(self, db):
        s = db.create_research_session("topic", ["discourse"])
        db.add_research_finding(
            s["id"], "reddit", "discourse", "A", "https://r/a", 0.4, ""
        )
        # Second insert with same URL is allowed (dedup happens
        # upstream in the orchestrator, not in storage). We just
        # want the schema to permit multiple rows for traceability.
        db.add_research_finding(
            s["id"], "reddit", "discourse", "A v2", "https://r/a", 0.6, ""
        )
        loaded = db.get_research_session(s["id"])
        assert loaded["finding_count"] == 2

    def test_metadata_stored_as_json(self, db):
        s = db.create_research_session("topic", ["code"])
        db.add_research_finding(
            s["id"],
            source="github",
            channel="code",
            title="Repo",
            url="https://g/r",
            relevance=0.7,
            summary="",
            metadata={"stars": 1500, "year": 2026},
        )
        loaded = db.get_research_session(s["id"])
        f = loaded["findings"][0]
        assert f["metadata"]["stars"] == 1500
        assert f["metadata"]["year"] == 2026
