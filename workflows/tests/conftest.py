"""Test configuration — sets up temp BrainDB before app module is imported."""

import os
import tempfile

# Set BRAIN_DB_PATH BEFORE any test module imports app.py.
# This must happen at module level (not in a fixture) because
# test_app.py does `from app import app` at collection time.
_tmpdir = tempfile.mkdtemp()
os.environ["BRAIN_DB_PATH"] = os.path.join(_tmpdir, "test_brain.db")


import pytest  # noqa: E402


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_brain_db():
    """Clear all tables between tests for isolation."""
    yield
    from app import brain_db

    for table in (
        "persona_learnings",
        "research_findings",
        "research_sessions",
        "tasks",
        "scan_cache",
        "subreddit_stats",
        "schedules",
        "projects",
        "page_tags",
        "tags",
        "links",
        "timeline",
        "page_versions",
        "content_chunks",
        "pages",
    ):
        try:
            brain_db._conn.execute(f"DELETE FROM {table}")
        except Exception:
            pass
    # Rebuild FTS index
    try:
        brain_db._conn.execute("DELETE FROM pages_fts")
        brain_db._conn.execute(
            "INSERT INTO pages_fts(slug, title, compiled) "
            "SELECT slug, title, compiled FROM pages"
        )
    except Exception:
        pass
    brain_db._conn.commit()


@pytest.fixture(autouse=True)
def _isolate_digest_state(tmp_path, monkeypatch):
    """Keep the heartbeat digest off /data and mid-interval by default.

    In production a missing state file means "due now", so a fresh deploy
    does not cost a silent hour. Without this fixture every test that runs a
    heartbeat would flush a digest, and every one would try to write the
    real /data path. Tests that exercise the digest itself override both.
    """
    import time

    monkeypatch.setenv("HEARTBEAT_DIGEST_STATE", str(tmp_path / "digest.json"))
    import llm_proxy

    monkeypatch.setattr(
        llm_proxy, "_digest_state", {"lines": [], "last_flush": time.time()}
    )
    yield
