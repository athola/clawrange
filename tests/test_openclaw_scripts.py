"""Tests for the OpenClaw container start-up scripts.

render-config.js runs under node; sync-heartbeat.sh and the healthcheck run
under sh with stub ``curl``/``runuser``/``openclaw``/``sleep`` on PATH, so
nothing here needs Docker or a running gateway.

Run with: pytest tests/test_openclaw_scripts.py -v
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RENDER = ROOT / "openclaw" / "render-config.js"
SYNC = ROOT / "openclaw" / "sync-heartbeat.sh"
HEALTH = ROOT / "scripts" / "healthcheck-openclaw.sh"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")


# ─── render-config.js ─────────────────────────────────────────────


def _render(tmp_path, target_text=None, token="tok"):
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"provider": {"apiKey": "${PROXY_AUTH_TOKEN}"}}))
    target = tmp_path / "openclaw.json"
    if target_text is not None:
        target.write_text(target_text)
    env = {"PATH": os.environ["PATH"], "PROXY_AUTH_TOKEN": token}
    proc = subprocess.run(
        ["node", str(RENDER), str(template), str(target)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc, target


@needs_node
class TestRenderConfig:
    def test_renders_and_keeps_runtime_keys(self, tmp_path):
        proc, target = _render(tmp_path, json.dumps({"meta": {"x": 1}}))
        assert proc.returncode == 0, proc.stderr
        data = json.loads(target.read_text())
        assert data == {"meta": {"x": 1}, "provider": {"apiKey": "tok"}}

    def test_empty_token_warns(self, tmp_path):
        proc, target = _render(tmp_path, token="")
        assert proc.returncode == 0
        assert "PROXY_AUTH_TOKEN" in proc.stderr
        assert target.exists()

    def test_unparseable_target_is_not_overwritten(self, tmp_path):
        broken = '{"meta": {"half-written": '
        proc, target = _render(tmp_path, broken)
        assert proc.returncode != 0
        assert "refus" in proc.stderr.lower()
        assert target.read_text() == broken
        assert not (tmp_path / "openclaw.json.tmp").exists()


# ─── sync-heartbeat.sh + healthcheck ──────────────────────────────


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "curl", "exit 0\n")
    _stub(bin_dir, "sleep", "exit 0\n")
    # runuser -u node -- CMD...: drop the first three args and run CMD.
    _stub(bin_dir, "runuser", 'shift 3\nexec "$@"\n')
    return bin_dir


def _openclaw(bin_dir: Path, scratch_ok: bool) -> None:
    jobs = json.dumps({"jobs": [{"id": "hb1", "payload": {"kind": "heartbeat"}}]})
    scratch = (
        "exit 0" if scratch_ok else 'echo "scratch: monitor hb1 is locked" >&2; exit 3'
    )
    _stub(
        bin_dir,
        "openclaw",
        f"""case "$2" in
  list) echo 'banner line'; echo '{jobs}' ;;
  scratch) {scratch} ;;
esac
""",
    )


def _run(script: Path, bin_dir: Path, marker: Path, *args: str):
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SYNC_HEARTBEAT_MARKER": str(marker),
    }
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )


@needs_node
class TestSyncHeartbeat:
    def test_scratch_failure_prints_cli_output_and_writes_marker(self, tmp_path, stubs):
        _openclaw(stubs, scratch_ok=False)
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode != 0
        assert "monitor hb1 is locked" in proc.stderr
        assert marker.exists()
        assert "hb1" in marker.read_text()

    def test_success_clears_stale_marker(self, tmp_path, stubs):
        _openclaw(stubs, scratch_ok=True)
        marker = tmp_path / "sync-heartbeat.failed"
        marker.write_text("old failure\n")
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode == 0, proc.stderr
        assert "loaded /opt/HEARTBEAT.md into hb1" in proc.stdout
        assert not marker.exists()

    def test_gateway_never_healthy_writes_marker(self, tmp_path, stubs):
        _stub(stubs, "curl", "exit 7\n")
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode != 0
        assert marker.exists()

    def test_marker_symlink_is_not_followed(self, tmp_path, stubs):
        # The script runs as root; a symlink planted at the marker path must
        # never turn the failure write into an overwrite of its target.
        _openclaw(stubs, scratch_ok=False)
        victim = tmp_path / "victim"
        victim.write_text("precious\n")
        marker = tmp_path / "sync-heartbeat.failed"
        marker.symlink_to(victim)
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode != 0
        assert victim.read_text() == "precious\n"
        assert marker.is_file() and not marker.is_symlink()


class TestHealthcheck:
    def test_marker_symlink_is_ignored(self, tmp_path, stubs):
        secret = tmp_path / "secret"
        secret.write_text("TOKEN=abc\n")
        marker = tmp_path / "sync-heartbeat.failed"
        marker.symlink_to(secret)
        proc = _run(HEALTH, stubs, marker)
        assert "TOKEN" not in proc.stdout + proc.stderr

    def test_marker_output_is_bounded(self, tmp_path, stubs):
        marker = tmp_path / "sync-heartbeat.failed"
        marker.write_text("x" * 10000)
        proc = _run(HEALTH, stubs, marker)
        assert proc.returncode != 0
        assert len(proc.stdout) < 1024

    def test_healthy_without_marker(self, tmp_path, stubs):
        proc = _run(HEALTH, stubs, tmp_path / "absent")
        assert proc.returncode == 0

    def test_unhealthy_when_heartbeat_sync_failed(self, tmp_path, stubs):
        marker = tmp_path / "sync-heartbeat.failed"
        marker.write_text("scratch load failed for hb1\n")
        proc = _run(HEALTH, stubs, marker)
        assert proc.returncode != 0
        assert "hb1" in proc.stdout + proc.stderr

    def test_unhealthy_when_gateway_down(self, tmp_path, stubs):
        _stub(stubs, "curl", "exit 7\n")
        proc = _run(HEALTH, stubs, tmp_path / "absent")
        assert proc.returncode != 0


# ─── compose start command ────────────────────────────────────────


def test_openclaw_start_command_stops_on_first_failure():
    """A failed config render must abort start-up, not launch the gateway
    on a stale or missing openclaw.json."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    command = compose["services"]["openclaw"]["command"]
    assert command[:2] == ["sh", "-c"]
    lines = [
        line.strip()
        for line in command[2].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines[0] == "set -e"


def test_heartbeat_marker_lives_in_root_owned_tmpfs():
    """/tmp and the state dir are owned by node (uid 1000), so a marker
    there could be swapped for a symlink before root writes it. /run is a
    root-owned tmpfs the node user cannot write to."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    tmpfs = compose["services"]["openclaw"].get("tmpfs") or []
    assert any(str(t).split(":")[0] == "/run" for t in tmpfs)
    for script in (SYNC, HEALTH):
        assert "${SYNC_HEARTBEAT_MARKER:-/run/" in script.read_text()
