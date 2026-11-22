"""Every test that asks for a brain, app or client gets a fresh one.

conftest.py copies one migrated template brain per test and builds the app
around it only when a test asks for ``app`` or ``client``, so nothing a
test writes can reach another test, whatever order they run in.
"""

import pathlib

import auth

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1]


def _only_task_is_mine(brain_db, description):
    brain_db.create_task(description)
    assert [t["description"] for t in brain_db.list_tasks()] == [description]


# Each writes one task and expects to see only its own: in either order, a
# shared brain would fail the second one.
def test_fresh_brain_first(brain_db):
    _only_task_is_mine(brain_db, "first")


def test_fresh_brain_second(brain_db):
    _only_task_is_mine(brain_db, "second")


def test_app_and_client_share_the_tests_brain(brain_db, app, client):
    assert app.state.brain_db is brain_db
    assert client.app is app


def test_client_reads_the_tests_brain(brain_db, client):
    brain_db.create_task("visible")
    assert client.get("/task").json()["total"] == 1


def test_app_skips_the_bearer_check_by_default(app):
    assert auth.require_token in app.dependency_overrides


def test_no_test_shares_a_module_level_app():
    """No module-level TestClient or shared test app to drift back to."""
    tests = WORKFLOWS / "tests"
    assert not (tests / "testapp.py").exists()
    offenders = []
    for p in sorted(tests.glob("*.py")):
        if p.name == pathlib.Path(__file__).name:
            continue
        src = p.read_text()
        if "tests.testapp" in src or "from tests import testapp" in src:
            offenders.append(f"{p.name}: imports the shared test app")
        for line in src.splitlines():
            if line.startswith("client = TestClient("):
                offenders.append(f"{p.name}: module-level TestClient")
    assert offenders == []


# Module state outside the app leaks the same way: scheduler startup errors
# from one test used to turn a later /healthz "degraded".
def _scheduler_starts_clean_then_breaks():
    import scheduler

    assert scheduler.scheduler_state()["errors"] == []
    scheduler._startup_error("test", "left behind")


def test_scheduler_state_fresh_first():
    _scheduler_starts_clean_then_breaks()


def test_scheduler_state_fresh_second():
    _scheduler_starts_clean_then_breaks()
