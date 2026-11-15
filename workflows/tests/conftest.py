"""Test configuration — sets up temp BrainDB before app module is imported."""

import os
import socket
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


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver", ""}
_real_getaddrinfo = socket.getaddrinfo


def _offline_getaddrinfo(host, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else (host or "")
    if name in _LOCAL_HOSTS:
        return _real_getaddrinfo(host, *args, **kwargs)
    raise socket.gaierror(f"network disabled in unit tests: {name}")


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    """Unit tests never touch the network. An unmocked HTTPS call to
    openrouter.ai inside a deadline-timed test made the suite flaky:
    the DNS lookup runs in a worker thread that wait_for can't cancel.
    Failing the lookup instantly keeps every such path fast and offline."""
    monkeypatch.setattr(socket, "getaddrinfo", _offline_getaddrinfo)


@pytest.fixture(autouse=True)
def _reset_proxy_globals(monkeypatch):
    """Fresh llm_proxy module state for every test (circuit breaker,
    balance cache, debounce, proactive timers). The breaker's 60s cooldown
    otherwise leaked across tests, depending on how fast the suite ran."""
    import llm_proxy

    monkeypatch.setattr(llm_proxy, "_circuit_state", {})
    monkeypatch.setattr(llm_proxy, "_notification_last_sent", {})
    monkeypatch.setattr(llm_proxy, "_proactive_state", {})
    monkeypatch.setattr(
        llm_proxy, "_balance_cache", {"remaining": None, "checked_at": 0.0}
    )
    monkeypatch.setattr(llm_proxy, "_last_tier_used", None)
    monkeypatch.setattr(llm_proxy, "_thinking_focus_index", 0)


@pytest.fixture(autouse=True)
def _bypass_api_auth():
    """Skip the app-wide bearer check so route tests needn't send a token.

    test_auth.py removes this override to exercise the real check.
    """
    import auth
    from app import app

    app.dependency_overrides[auth.require_token] = lambda: None
    yield
    app.dependency_overrides.pop(auth.require_token, None)


def _resettable_tables(conn) -> list[str]:
    """User tables, derived from the live schema so the list can't drift.

    Skips SQLite internals, FTS/vec shadow tables (PRAGMA table_list marks
    most as 'shadow'; sqlite-vec's vector chunk tables report as 'table', so
    anything prefixed by a virtual table's name is skipped too) and the
    virtual tables themselves, which are rebuilt separately.
    """
    rows = conn.execute("PRAGMA table_list").fetchall()
    main = [r for r in rows if r["schema"] == "main"]
    virtual = {r["name"] for r in main if r["type"] == "virtual"}
    return [
        r["name"]
        for r in main
        if r["type"] == "table"
        and not r["name"].startswith("sqlite_")
        and not any(r["name"].startswith(v + "_") for v in virtual)
    ]


@pytest.fixture(autouse=True)
def _reset_brain_db():
    """Clear all tables between tests for isolation. Errors surface: a
    failed reset means the next test would see this one's rows."""
    yield
    from app import brain_db

    conn = brain_db._conn
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in _resettable_tables(conn):
            conn.execute(f"DELETE FROM {table}")
        if brain_db._has_vec:
            conn.execute("DELETE FROM chunk_embeddings")
        conn.execute("DELETE FROM pages_fts")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


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
