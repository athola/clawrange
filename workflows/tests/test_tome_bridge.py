"""Tests for scripts/tome_bridge.py.

The bridge runs on Alex's local machine, not in the workflows
container, but we test it from the workflows test tree so the
existing pytest infra picks it up. The script imports stdlib only,
so collection has no extra dependencies.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Ensure scripts/ is importable.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import tome_bridge  # noqa: E402


class TestExtractTopic:
    """`extract_topic` is the dispatch decision: which tasks does
    the bridge claim? Pure function, easy to verify exhaustively.
    """

    def test_recognizes_colon_prefix(self):
        assert (
            tome_bridge.extract_topic("research:tome: agent platforms")
            == "agent platforms"
        )

    def test_recognizes_bracket_prefix(self):
        assert (
            tome_bridge.extract_topic("[research:tome] agent platforms")
            == "agent platforms"
        )

    def test_recognizes_natural_language_prefix(self):
        assert (
            tome_bridge.extract_topic("Research via tome: agent platforms")
            == "agent platforms"
        )

    def test_case_insensitive(self):
        assert tome_bridge.extract_topic("RESEARCH:TOME: AI agents") == "AI agents"

    def test_strips_extra_whitespace(self):
        assert (
            tome_bridge.extract_topic("research:tome:    agent platforms   ")
            == "agent platforms"
        )

    def test_returns_none_for_unrelated_task(self):
        assert tome_bridge.extract_topic("Reddit scan: claude code") is None
        assert tome_bridge.extract_topic("[DRAFT] Comment for r/...") is None
        assert tome_bridge.extract_topic("") is None
        assert tome_bridge.extract_topic("research is cool") is None


class TestRunTomeResearch:
    """Subprocess wrapper - we mock subprocess.run."""

    def test_success_returns_combined_output(self, monkeypatch):
        import subprocess

        class FakeResult:
            returncode = 0
            stdout = "topic findings\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            return FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc, out = tome_bridge.run_tome_research("claude", "topic", 60)
        assert rc == 0
        assert "topic findings" in out

    def test_timeout_returns_124(self, monkeypatch):
        import subprocess

        def raise_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        rc, out = tome_bridge.run_tome_research("claude", "topic", 1)
        assert rc == 124
        assert "timed out" in out

    def test_missing_binary_returns_127(self, monkeypatch):
        import subprocess

        def raise_fnf(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        rc, out = tome_bridge.run_tome_research("nope-binary", "topic", 60)
        assert rc == 127
        assert "not found" in out


class TestProcessOneTask:
    """End-to-end of the per-task path with HTTP and subprocess mocked."""

    def test_skips_non_tome_task(self, monkeypatch):
        called = {"claim": False, "complete": False, "run": False}

        def claim(*a, **kw):
            called["claim"] = True

        def complete(*a, **kw):
            called["complete"] = True

        def run(*a, **kw):
            called["run"] = True
            return 0, "out"

        monkeypatch.setattr(tome_bridge, "claim_task", claim)
        monkeypatch.setattr(tome_bridge, "complete_task", complete)
        monkeypatch.setattr(tome_bridge, "run_tome_research", run)

        ok = tome_bridge.process_one_task(
            "http://x",
            "claude",
            {"id": "t1", "description": "Reddit scan: foo"},
            60,
            dry_run=False,
        )
        assert ok is False
        assert not any(called.values())

    def test_dry_run_does_not_claim_or_run(self, monkeypatch):
        called = {"claim": False, "run": False}

        def claim(*a, **kw):
            called["claim"] = True

        def run(*a, **kw):
            called["run"] = True
            return 0, "out"

        monkeypatch.setattr(tome_bridge, "claim_task", claim)
        monkeypatch.setattr(tome_bridge, "run_tome_research", run)

        ok = tome_bridge.process_one_task(
            "http://x",
            "claude",
            {"id": "t1", "description": "research:tome: topic"},
            60,
            dry_run=True,
        )
        assert ok is True
        assert called["claim"] is False
        assert called["run"] is False

    def test_full_path_completes_task(self, monkeypatch):
        events: list[tuple[str, dict]] = []

        def claim(base, task_id):
            events.append(("claim", {"id": task_id}))

        def complete(base, task_id, result, status):
            events.append(
                ("complete", {"id": task_id, "status": status, "len": len(result)})
            )

        def run(claude_bin, topic, timeout):
            events.append(("run", {"topic": topic}))
            return 0, "synthesized findings here"

        monkeypatch.setattr(tome_bridge, "claim_task", claim)
        monkeypatch.setattr(tome_bridge, "complete_task", complete)
        monkeypatch.setattr(tome_bridge, "run_tome_research", run)

        ok = tome_bridge.process_one_task(
            "http://x",
            "claude",
            {"id": "abc12345", "description": "research:tome: agent platforms"},
            60,
            dry_run=False,
        )
        assert ok is True
        assert events[0][0] == "claim"
        assert events[1] == ("run", {"topic": "agent platforms"})
        assert events[2][0] == "complete"
        assert events[2][1]["status"] == "completed"

    def test_failed_run_marks_task_failed(self, monkeypatch):
        captured = {}

        def claim(*a, **kw):
            pass

        def complete(base, task_id, result, status):
            captured["status"] = status

        def run(*a, **kw):
            return 1, "tome failed"

        monkeypatch.setattr(tome_bridge, "claim_task", claim)
        monkeypatch.setattr(tome_bridge, "complete_task", complete)
        monkeypatch.setattr(tome_bridge, "run_tome_research", run)

        tome_bridge.process_one_task(
            "http://x",
            "claude",
            {"id": "abc", "description": "[research:tome] thing"},
            60,
            dry_run=False,
        )
        assert captured["status"] == "failed"


class TestRunOnce:
    def test_handles_workflows_unreachable(self, monkeypatch):
        import urllib.error

        def fail(*a, **kw):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(tome_bridge, "list_pending_tasks", fail)
        rc = tome_bridge.run_once("http://x", "claude", 60, dry_run=False)
        assert rc == 1

    def test_iterates_all_pending_tasks(self, monkeypatch):
        monkeypatch.setattr(
            tome_bridge,
            "list_pending_tasks",
            lambda base: [
                {"id": "t1", "description": "research:tome: a"},
                {"id": "t2", "description": "Reddit scan: b"},
                {"id": "t3", "description": "[research:tome] c"},
            ],
        )

        processed: list[str] = []

        def fake_process(base, claude_bin, task, timeout, dry_run):
            topic = tome_bridge.extract_topic(task["description"])
            if topic:
                processed.append(task["id"])
                return True
            return False

        monkeypatch.setattr(tome_bridge, "process_one_task", fake_process)
        rc = tome_bridge.run_once("http://x", "claude", 60, dry_run=False)
        assert rc == 0
        assert processed == ["t1", "t3"]


@pytest.fixture(autouse=True)
def _reset_bridge_state():
    """Ensure no test pollutes module-level state."""
    yield
