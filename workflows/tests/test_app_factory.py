"""create_app() builds an independent app around the brain it is given.

Two apps in one process must not share a database: a task queued through
one app's chat endpoint or task API is invisible to the other.
"""

import os
import pathlib
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import chat_commands
import heartbeat
import llm_proxy
from app import create_app
from brain_db import BrainDB

TOKEN = "factory-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def two_apps(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
    dbs = []
    for name in ("a", "b"):
        db = BrainDB(str(tmp_path / f"{name}.db"))
        db.init_db()
        dbs.append(db)
    # No `with`: the lifespan (scheduler, profile seeding) is not under test.
    apps = [TestClient(create_app(brain_db=db)) for db in dbs]
    yield apps, dbs
    for db in dbs:
        db._conn.close()


def _chat(client, text):
    return client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": text}]},
        headers=AUTH,
    )


def test_chat_task_lands_only_in_its_own_brain(two_apps):
    (_, client_b), (db_a, db_b) = two_apps
    assert _chat(client_b, "!task only in b").status_code == 200
    assert [t["description"] for t in db_b.list_tasks()] == ["only in b"]
    assert db_a.list_tasks() == []


def test_task_api_reads_its_own_brain(two_apps):
    (client_a, client_b), _ = two_apps
    r = client_a.post("/task", json={"description": "a only"}, headers=AUTH)
    assert r.status_code == 200
    assert client_a.get("/task", headers=AUTH).json()["total"] == 1
    assert client_b.get("/task", headers=AUTH).json()["total"] == 0


def test_each_app_has_its_own_state(two_apps):
    (client_a, client_b), (db_a, db_b) = two_apps
    assert client_a.app.state.brain_db is db_a
    assert client_b.app.state.brain_db is db_b
    assert client_a.app is not client_b.app


def test_brain_routes_use_the_given_brain(two_apps):
    (client_a, client_b), (db_a, _) = two_apps
    db_a.upsert_page("note/a", "Note A", "note", "only in a")
    assert client_a.get("/brain/pages/note/a", headers=AUTH).status_code == 200
    assert client_b.get("/brain/pages/note/a", headers=AUTH).status_code == 404


# The handlers take the scheduler and profile as plain arguments; these
# check the hop from request.app.state through chat_completions, where a
# dropped kwarg would fall back to None without failing anything.


def test_sched_command_gets_the_apps_live_scheduler(two_apps, monkeypatch):
    (client_a, _), (db_a, _) = two_apps
    live = object()
    client_a.app.state.scheduler = live
    spy = AsyncMock(return_value="ok")
    monkeypatch.setattr(chat_commands, "_handle_sched_command", spy)
    assert _chat(client_a, "/sched pause nightly").status_code == 200
    spy.assert_awaited_once_with("pause", "nightly", brain_db=db_a, scheduler=live)


def test_heartbeat_gets_the_apps_brain_and_profile(two_apps, monkeypatch):
    (_, client_b), (_, db_b) = two_apps
    profile = object()
    client_b.app.state.profile = profile
    spy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(heartbeat, "_handle_heartbeat", spy)
    assert _chat(client_b, "ClawRange heartbeat: check in").status_code == 200
    spy.assert_awaited_once_with(False, brain_db=db_b, profile=profile)


# `uvicorn --factory app:create_app` serves the app: importing the module
# builds nothing, and the factory with no argument opens BRAIN_DB_PATH.


def test_importing_app_opens_no_brain(tmp_path):
    workflows = pathlib.Path(__file__).resolve().parents[1]
    brain = tmp_path / "brain.db"
    code = (
        "import app; "
        "assert not hasattr(app, 'app'); assert not hasattr(app, 'brain_db')"
    )
    env = {
        **os.environ,
        "BRAIN_DB_PATH": str(brain),
        "HEARTBEAT_SEEN_PATH": str(tmp_path / "seen.json"),
        "PYTHONPATH": str(workflows),
    }
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not brain.exists()


def test_factory_without_a_brain_opens_brain_db_path(tmp_path, monkeypatch):
    brain = tmp_path / "served.db"
    monkeypatch.setenv("BRAIN_DB_PATH", str(brain))
    served = create_app()
    try:
        assert served.state.brain_db._db_path == str(brain)
        assert brain.exists()
    finally:
        served.state.brain_db._conn.close()
