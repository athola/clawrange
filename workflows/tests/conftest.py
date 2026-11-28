"""Test configuration: shared fixtures and per-test isolation.

`brain_db`, `app` and `client` are fresh per test (see below); app.py
builds nothing on import, so test modules can import it in any order.
"""

import os
import pathlib
import socket
import tempfile

# Safety net, not an ordering requirement: a test that calls create_app() or
# open_brain() without a path gets a temp brain, never the live /data one.
# Inside the container /data is also where the watchdog's heartbeat stamp
# lives, so a test run must never overwrite it either.
_tmpdir = tempfile.mkdtemp()
os.environ["BRAIN_DB_PATH"] = os.path.join(_tmpdir, "test_brain.db")
os.environ.setdefault(
    "HEARTBEAT_SEEN_PATH", os.path.join(_tmpdir, "heartbeat_seen.json")
)


import pytest  # noqa: E402

# ─── Fresh brain, app and client per test ───────────────────────────
# Lazy on purpose: no autouse fixture asks for these, so a test that never
# names `brain_db`, `app` or `client` pays nothing. A brain is a file copy of
# one migrated template (about 1ms, against about 60ms to migrate a new file);
# the app (about 10ms) is built only for tests that ask for it.


@pytest.fixture(scope="session")
def _brain_template(tmp_path_factory):
    from app import open_brain

    path = tmp_path_factory.mktemp("brain-template") / "brain.db"
    db = open_brain(str(path))
    # Fold the WAL into the main file so a plain copy carries every table.
    db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db._conn.close()
    # The copy must carry the schema: a truncated template would still pass
    # every test, each one quietly migrating from scratch at ~60ms.
    import sqlite3

    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"tasks", "pages"} <= tables, "brain template is missing its schema"
    return path


@pytest.fixture()
def brain_db(_brain_template, tmp_path):
    """A migrated brain of this test's own, removed after the test."""
    import shutil

    from app import open_brain

    path = tmp_path / "_fixture_brain.db"
    shutil.copyfile(_brain_template, path)
    db = open_brain(str(path))
    yield db
    db._conn.close()
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)


@pytest.fixture()
def app(brain_db):
    """A fresh app around this test's brain, bearer check skipped.

    Tests of the real check pop ``auth.require_token`` from
    ``app.dependency_overrides``.
    """
    import auth
    from app import create_app

    api = create_app(brain_db=brain_db)
    api.dependency_overrides[auth.require_token] = lambda: None
    return api


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

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
    balance cache, debounce) and heartbeat state (proactive timers, focus
    rotation). The breaker's 60s cooldown otherwise leaked across tests,
    depending on how fast the suite ran."""
    import llm_proxy

    monkeypatch.setattr(llm_proxy, "_circuit_state", {})
    monkeypatch.setattr(llm_proxy, "_tier_failure_reason", {})
    monkeypatch.setattr(llm_proxy, "_last_call_failures", {})
    monkeypatch.setattr(llm_proxy, "_notification_last_sent", {})
    # String targets: conftest imports no app modules at module level, so
    # the env above is in place before anything reads it.
    monkeypatch.setattr("heartbeat._proactive_state", {})
    monkeypatch.setattr(
        llm_proxy, "_balance_cache", {"remaining": None, "checked_at": None}
    )
    monkeypatch.setattr(llm_proxy, "_balance_status", {"ok_at": None, "error": None})
    monkeypatch.setattr(llm_proxy, "_last_tier_used", None)
    monkeypatch.setattr("heartbeat._thinking_focus_index", 0)
    # Set on the first failed balance lookup. A test that ran a heartbeat
    # while time.monotonic was patched small left it far in the past, and
    # a later heartbeat in the same worker then raised a "balance unknown"
    # task that suppressed LLM thinking (test_heartbeat_llm_thinking).
    monkeypatch.setattr("heartbeat._balance_unknown_since", None)
    # Scheduler startup errors are module state too; left over, they turned
    # a later /healthz "degraded" depending on file order.
    import scheduler

    monkeypatch.setattr(scheduler, "_STATE", scheduler._new_state())


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
    import digest

    monkeypatch.setattr(
        digest, "_digest_state", {"lines": [], "last_flush": time.time()}
    )
    yield
