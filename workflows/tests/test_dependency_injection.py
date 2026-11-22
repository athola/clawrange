"""Chat commands and the heartbeat use the brain they are handed.

Only app.py may import app: the other modules get the brain, profile and
scheduler passed in (llm_proxy reads them off ``request.app.state``), so an
app built by a factory around another database is served from that database.
"""

import ast
import asyncio
import pathlib

import pytest

import chat_commands
import heartbeat
from brain_db import BrainDB

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def other_db(tmp_path):
    db = BrainDB(str(tmp_path / "other.db"))
    db.init_db()
    yield db
    db._conn.close()


def _imports_app(path: pathlib.Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import) and any(a.name == "app" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "app":
            return True
    return False


def test_only_tests_import_app():
    offenders = sorted(
        str(p.relative_to(WORKFLOWS))
        for p in WORKFLOWS.rglob("*.py")
        if "tests" not in p.parts and p.name != "app.py" and _imports_app(p)
    )
    assert offenders == []


def test_projects_command_reads_the_injected_brain(other_db):
    other_db.upsert_project("injected-only", "acme", "widget")
    text = asyncio.run(
        chat_commands._handle_projects_command("list", "", brain_db=other_db)
    )
    assert "injected-only" in text


def test_task_command_writes_to_the_injected_brain(other_db):
    resp = asyncio.run(
        chat_commands._dispatch_task_command(
            {"type": "create", "description": "only here"}, brain_db=other_db
        )
    )
    assert resp.status_code == 200
    assert [t["description"] for t in other_db.list_tasks()] == ["only here"]


def test_heartbeat_counts_the_injected_queue(other_db):
    other_db.create_task("queued elsewhere")
    state = asyncio.run(heartbeat._gather_system_state(other_db))
    assert "Task queue: 1 pending" in state


def test_deals_block_comes_from_the_given_profile():
    class _Profile:
        raw = {"homelab_deals": {"stack_gaps": {"nas": "no backups"}}}

    assert heartbeat._deals_block(_Profile()) == _Profile.raw["homelab_deals"]
    assert heartbeat._deals_block(None) is None
