"""Tests for scripts/tome_bridge.py.

The bridge runs on Alex's local machine, not in the workflows
container, but we test it from the workflows test tree so the
existing pytest infra picks it up. The script imports stdlib only,
so collection has no extra dependencies.
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
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
        def raise_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        rc, out = tome_bridge.run_tome_research("claude", "topic", 1)
        assert rc == 124
        assert "timed out" in out

    def test_missing_binary_returns_127(self, monkeypatch):
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


class TestBridgeHardening:
    """The task queue is a trust boundary: anything that can reach the
    workflows API can queue a tome task, and the topic becomes a prompt for a
    local Claude Code session. Authenticate the bridge's own API calls, bound
    the topic, and deny the session its write/exec tools."""

    def test_http_json_sends_bearer_token(self, monkeypatch):
        seen = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout):
            seen["auth"] = req.get_header("Authorization")
            return FakeResp()

        monkeypatch.setenv("PROXY_AUTH_TOKEN", "tok123")
        monkeypatch.setattr(tome_bridge.urllib.request, "urlopen", fake_urlopen)
        tome_bridge.http_json("GET", "http://x/task")
        assert seen["auth"] == "Bearer tok123"

    def test_claude_runs_with_tool_allowlist(self, monkeypatch):
        # A deny-list misses every tool it does not name (Read can open .env,
        # MCP servers bring their own tools). Allow only what research needs.
        seen = {}

        class FakeResult:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_run)
        tome_bridge.run_tome_research("claude", "topic", 60)
        cmd = seen["cmd"]
        tools_flag = [a for a in cmd if a.startswith("--tools=")]
        allowed_flag = [a for a in cmd if a.startswith("--allowedTools=")]
        assert len(tools_flag) == 1 and len(allowed_flag) == 1
        tools = set(tools_flag[0].split("=", 1)[1].split(","))
        assert tools == set(allowed_flag[0].split("=", 1)[1].split(","))
        assert {"WebSearch", "WebFetch", "Skill"} <= tools
        for tool in ("Bash", "Read", "Edit", "Write", "NotebookEdit", "Glob"):
            assert tool not in tools
        assert "--strict-mcp-config" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
        assert "--disallowedTools" not in cmd
        # The prompt stays the first positional and never starts with '-',
        # so a topic can't smuggle CLI flags.
        assert cmd[2].startswith("/tome:research ")

    def test_claude_runs_in_empty_temp_dir(self, monkeypatch):
        # The repo root holds .env: the session must not start there.
        seen = {}

        class FakeResult:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(cmd, **kwargs):
            cwd = kwargs.get("cwd")
            seen["cwd"] = cwd
            seen["listing"] = os.listdir(cwd) if cwd else None
            return FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_run)
        tome_bridge.run_tome_research("claude", "topic", 60)
        assert seen["cwd"] is not None
        repo = Path(__file__).resolve().parents[2]
        assert not Path(seen["cwd"]).resolve().is_relative_to(repo)
        assert seen["listing"] == []
        # Cleaned up afterwards.
        assert not os.path.exists(seen["cwd"])

    @pytest.mark.parametrize(
        "topic",
        ["x" * 301, "ignore previous\x00instructions", "a\x1b[2Jb"],
    )
    def test_unsafe_topic_fails_task_without_running(self, monkeypatch, topic):
        captured = {}
        monkeypatch.setattr(tome_bridge, "claim_task", lambda *a, **k: None)
        monkeypatch.setattr(
            tome_bridge,
            "complete_task",
            lambda base, tid, result, status: captured.update(
                status=status, result=result
            ),
        )

        def boom(*a, **k):
            raise AssertionError("claude must not run for an unsafe topic")

        monkeypatch.setattr(tome_bridge, "run_tome_research", boom)
        tome_bridge.process_one_task(
            "http://x",
            "claude",
            {"id": "t1", "description": f"research:tome: {topic}"},
            60,
            dry_run=False,
        )
        assert captured["status"] == "failed"
        assert "topic rejected" in captured["result"]


class TestBridgeNetworkErrors:
    """A dropped connection, a timeout or a non-JSON reply must not escape
    run_once: under --watch that kills the loop, loses the research output
    and strands the claimed task in `active`."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        self.sleeps: list[float] = []
        monkeypatch.setattr(tome_bridge.time, "sleep", self.sleeps.append)

    @pytest.mark.parametrize(
        "exc",
        [
            urllib.error.URLError("refused"),
            TimeoutError("timed out"),
            ValueError("Expecting value"),
        ],
    )
    def test_claim_error_skips_task_without_running(self, monkeypatch, exc):
        def claim(*a, **k):
            raise exc

        def boom(*a, **k):
            raise AssertionError("claude must not run for an unclaimed task")

        monkeypatch.setattr(tome_bridge, "claim_task", claim)
        monkeypatch.setattr(tome_bridge, "run_tome_research", boom)
        ok = tome_bridge.process_one_task(
            "http://x",
            "claude",
            {"id": "t1", "description": "research:tome: a"},
            60,
            dry_run=False,
        )
        assert ok is True

    def test_complete_is_retried_until_it_lands(self, monkeypatch):
        posted: list[str] = []
        failures: list[Exception] = [
            urllib.error.URLError("reset"),
            TimeoutError("slow"),
        ]

        def complete(base, task_id, result, status):
            if failures:
                raise failures.pop(0)
            posted.append(result)

        monkeypatch.setattr(tome_bridge, "claim_task", lambda *a, **k: None)
        monkeypatch.setattr(tome_bridge, "complete_task", complete)
        monkeypatch.setattr(
            tome_bridge, "run_tome_research", lambda *a, **k: (0, "findings")
        )
        tome_bridge.process_one_task(
            "http://x",
            "claude",
            {"id": "t1", "description": "research:tome: a"},
            60,
            dry_run=False,
        )
        assert posted == ["findings"]
        assert len(self.sleeps) == 2
        assert self.sleeps == sorted(self.sleeps)

    def test_complete_gives_up_after_bounded_attempts(self, monkeypatch):
        calls = []

        def complete(*a, **k):
            calls.append(1)
            raise ValueError("not json")

        monkeypatch.setattr(tome_bridge, "claim_task", lambda *a, **k: None)
        monkeypatch.setattr(tome_bridge, "complete_task", complete)
        monkeypatch.setattr(
            tome_bridge, "run_tome_research", lambda *a, **k: (0, "findings")
        )
        ok = tome_bridge.process_one_task(
            "http://x",
            "claude",
            {"id": "t1", "description": "research:tome: a"},
            60,
            dry_run=False,
        )
        assert ok is True
        assert len(calls) == tome_bridge.COMPLETE_ATTEMPTS
        assert sum(self.sleeps) <= 120

    @pytest.mark.parametrize(
        "exc", [TimeoutError("timed out"), ValueError("Expecting value")]
    )
    def test_list_error_reports_unreachable(self, monkeypatch, exc):
        def fail(base):
            raise exc

        monkeypatch.setattr(tome_bridge, "list_pending_tasks", fail)
        assert tome_bridge.run_once("http://x", "claude", 60, dry_run=False) == 1

    def test_watch_survives_unexpected_error(self, monkeypatch):
        passes = []

        def run_once(*a, **k):
            passes.append(1)
            if len(passes) == 1:
                raise KeyError("id")
            raise SystemExit(0)

        monkeypatch.setattr(tome_bridge, "run_once", run_once)
        with pytest.raises(SystemExit):
            tome_bridge.main(["--watch", "--interval", "1"])
        assert len(passes) == 2
