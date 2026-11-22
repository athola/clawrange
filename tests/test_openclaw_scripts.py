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
        """GIVEN a template whose apiKey is the ${PROXY_AUTH_TOKEN} shorthand
        WHEN rendered over a live file with runtime keys
        THEN runtime keys survive and the shorthand stays literal: OpenClaw
        resolves it as an env SecretRef, so the value never lands on disk."""
        proc, target = _render(tmp_path, json.dumps({"meta": {"x": 1}}))
        assert proc.returncode == 0, proc.stderr
        data = json.loads(target.read_text())
        assert data == {"meta": {"x": 1}, "provider": {"apiKey": "${PROXY_AUTH_TOKEN}"}}

    def test_token_value_is_never_written(self, tmp_path):
        """GIVEN a live file still holding a plaintext apiKey (pre-SecretRef)
        and a template with a structured env SecretRef
        WHEN rendered with the token in the environment
        THEN the ref replaces the old value and the token appears nowhere."""
        ref = {"source": "env", "provider": "default", "id": "PROXY_AUTH_TOKEN"}
        template = tmp_path / "template.json"
        template.write_text(json.dumps({"provider": {"apiKey": ref}}))
        target = tmp_path / "openclaw.json"
        target.write_text(json.dumps({"provider": {"apiKey": "old-plaintext-tok"}}))
        env = {"PATH": os.environ["PATH"], "PROXY_AUTH_TOKEN": "s3cret-token-value"}
        proc = subprocess.run(
            ["node", str(RENDER), str(template), str(target)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        text = target.read_text()
        assert json.loads(text)["provider"]["apiKey"] == ref
        assert "s3cret-token-value" not in text
        assert "old-plaintext-tok" not in text

    def test_empty_token_warns(self, tmp_path):
        proc, target = _render(tmp_path, token="")
        assert proc.returncode == 0
        assert "PROXY_AUTH_TOKEN" in proc.stderr
        assert target.exists()

    @pytest.mark.parametrize(
        "agents",
        [
            {"defaults": {"heartbeat": {"every": "0m"}}},
            {"entries": {"max-ops": {"heartbeat": {"every": "10m"}}}},
        ],
    )
    def test_retired_heartbeat_keys_stop_the_render(self, tmp_path, agents):
        """GIVEN a live openclaw.json still holding pre-2026.11.5 heartbeat keys
        (the deep merge would keep them, and 2026.11.6 rejects the config)
        WHEN render-config runs
        THEN it fails naming doctor --fix and leaves the file for doctor."""
        before = json.dumps({"agents": agents})
        proc, target = _render(tmp_path, before)
        assert proc.returncode != 0
        assert "doctor --fix" in proc.stderr
        assert target.read_text() == before

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


HEARTBEAT_JOB = {
    "id": "hb1",
    "enabled": True,
    "payload": {
        "kind": "agentTurn",
        "message": "ClawRange heartbeat: run the scheduled checks. Reply NO_REPLY.",
    },
}
# A heartbeat OpenClaw created itself (the shape doctor gave the migrated
# job: heartbeat-<agent>, "Heartbeat (<agent>)"), not ClawRange's: it would
# DM the operator on its own schedule and bypass the proxy's interceptor.
FOREIGN_HEARTBEAT = {
    "id": "hb-max",
    "name": "heartbeat-max",
    "displayName": "Heartbeat (max)",
    "enabled": True,
    "payload": {"kind": "agentTurn", "message": "Read HEARTBEAT.md. Reply NO_REPLY."},
    "delivery": {"mode": "announce", "channel": "telegram", "to": "4242"},
}
# Something the operator (or the agent's cron tool) scheduled on purpose.
REMINDER_JOB = {
    "id": "remind1",
    "name": "standup-reminder",
    "enabled": True,
    "payload": {"kind": "agentTurn", "message": "Remind me about standup."},
    "delivery": {"mode": "announce", "channel": "telegram", "to": "4242"},
}
# OpenClaw ships its own agentTurn jobs (memory dreaming); never touch them.
OTHER_JOB = {
    "id": "dream1",
    "enabled": True,
    "payload": {"kind": "agentTurn", "message": "__openclaw_memory_dream__"},
}


def _openclaw(
    bin_dir: Path,
    jobs=None,
    scratch_ok=True,
    add_ok=True,
    audit_findings=(),
) -> Path:
    """Stateful `openclaw automations` stub: list/add/scratch over a JSON file.

    `add` records its argv in add.log and appends a job (id hb-new) carrying
    its --message, so a later `list` returns it like the real gateway does.
    `edit` appends its argv to edit.log and stores --tools as toolsAllow.
    `secrets audit --check` prints `audit_findings` and exits 1 when there
    are any. A failed `add` ends with Node deprecation noise, as the real
    CLI's errors do.
    """
    state = bin_dir.parent / "jobs.json"
    state.write_text(json.dumps([HEARTBEAT_JOB] if jobs is None else jobs))
    path = bin_dir / "openclaw"
    path.write_text(
        f"""#!/usr/bin/env python3
import json, sys
state = {str(state)!r}
args = sys.argv[1:]
if args[0] == "secrets":
    assert args[1:] == ["audit", "--check"], args
    findings = {list(audit_findings)!r}
    for f in findings:
        print("- [PLAINTEXT_FOUND] " + f)
    sys.exit(1 if findings else 0)
assert args[0] == "automations", args
jobs = json.load(open(state))
if args[1] == "list":
    assert "--all" in args and "--json" in args, args
    print("banner line")
    print(json.dumps({{"jobs": jobs, "total": len(jobs)}}))
elif args[1] == "add":
    open({str(bin_dir.parent / "add.log")!r}, "w").write(json.dumps(args[2:]))
    if not {add_ok!r}:
        sys.exit(
            "invalid cron.add params: no route for telegram\\n"
            "(node:1) [DEP_PLUGIN_SDK] DeprecationWarning: x is deprecated"
        )
    msg = args[args.index("--message") + 1]
    jobs.append({{"id": "hb-new", "enabled": True,
                 "payload": {{"kind": "agentTurn", "message": msg}}}})
    json.dump(jobs, open(state, "w"))
    print("created hb-new")
elif args[1] == "edit":
    open({str(bin_dir.parent / "edit.log")!r}, "a").write(json.dumps(args[2:]) + "\\n")
    tools = args[args.index("--tools") + 1].split(",")
    for j in jobs:
        if j["id"] == args[2]:
            j["payload"]["toolsAllow"] = tools
    json.dump(jobs, open(state, "w"))
elif args[1] == "scratch":
    if not {scratch_ok!r}:
        sys.exit(f"scratch: job {{args[2]}} is locked")
    open({str(bin_dir.parent / "scratch.log")!r}, "a").write(args[2] + "\\n")
"""
    )
    path.chmod(0o755)
    return bin_dir.parent


MAX_OPS_TOOLS = ["read", "web_fetch", "cron"]


def _config(root: Path, tools=MAX_OPS_TOOLS) -> Path:
    """A rendered openclaw.json whose max-ops allow list caps the heartbeat."""
    path = root / "openclaw.json"
    entries = {"max-ops": {"tools": {"allow": tools, "deny": ["exec"]}}}
    path.write_text(json.dumps({"agents": {"entries": entries}}))
    return path


def _run(
    script: Path,
    bin_dir: Path,
    marker: Path,
    *args: str,
    chat_id="4242",
    config: Path | None = None,
):
    if config is None:
        config = _config(bin_dir.parent)
    env = {
        "OPENCLAW_CONFIG": str(config),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SYNC_HEARTBEAT_MARKER": str(marker),
        "TELEGRAM_CHAT_ID": chat_id,
    }
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )


def _scratched(root: Path) -> list[str]:
    log = root / "scratch.log"
    return log.read_text().split() if log.exists() else []


@needs_node
class TestSyncHeartbeat:
    def test_loads_scratch_into_existing_heartbeat_job_only(self, tmp_path, stubs):
        """GIVEN a migrated heartbeat job beside OpenClaw's own agentTurn jobs
        WHEN sync-heartbeat runs
        THEN only the heartbeat job gets HEARTBEAT.md and nothing is created."""
        root = _openclaw(stubs, jobs=[OTHER_JOB, HEARTBEAT_JOB])
        proc = _run(SYNC, stubs, tmp_path / "m", "/opt/HEARTBEAT.md")
        assert proc.returncode == 0, proc.stderr
        assert "loaded /opt/HEARTBEAT.md into hb1" in proc.stdout
        assert _scratched(root) == ["hb1"]
        assert not (root / "add.log").exists()

    def test_disabled_heartbeat_job_is_not_recreated(self, tmp_path, stubs):
        """GIVEN the operator disabled the heartbeat job
        WHEN sync-heartbeat runs
        THEN it is not recreated (list --all sees disabled jobs)."""
        root = _openclaw(stubs, jobs=[{**HEARTBEAT_JOB, "enabled": False}])
        proc = _run(SYNC, stubs, tmp_path / "m", "/opt/HEARTBEAT.md")
        assert proc.returncode == 0, proc.stderr
        assert not (root / "add.log").exists()

    def test_creates_intercepted_heartbeat_job_on_fresh_install(self, tmp_path, stubs):
        """GIVEN a fresh state dir with no heartbeat job (2026.11.5+ no longer
        provisions one from config)
        WHEN sync-heartbeat runs
        THEN it creates a max-ops job whose message the proxy intercepts,
        delivering to TELEGRAM_CHAT_ID, and loads HEARTBEAT.md into it."""
        import heartbeat

        root = _openclaw(stubs, jobs=[OTHER_JOB])
        proc = _run(SYNC, stubs, tmp_path / "m", "/opt/HEARTBEAT.md")
        assert proc.returncode == 0, proc.stderr
        add = json.loads((root / "add.log").read_text())
        opt = {add[i]: add[i + 1] for i in range(len(add) - 1) if add[i][:2] == "--"}
        assert heartbeat._is_heartbeat(opt["--message"])
        assert "NO_REPLY" in opt["--message"]
        assert "HEARTBEAT_OK" not in opt["--message"]
        assert opt["--agent"] == "max-ops"
        assert opt["--to"] == "4242"
        assert opt["--channel"] == "telegram"
        # Same shape doctor --fix gives a migrated job: an explicit tool cap
        # (else 2026.11.6 keeps legacy sender-policy resolution), the agent's
        # 600s turn budget, and yielding to foreground chats.
        assert opt["--tools"] == "read,web_fetch,cron"
        assert opt["--timeout-seconds"] == "600"
        assert opt["--active-hours-timezone"] == "user"
        assert "--idle-only" in add
        assert _scratched(root) == ["hb-new"]

    def test_foreign_heartbeat_marks_unhealthy_but_ours_still_loads(
        self, tmp_path, stubs
    ):
        """GIVEN an enabled heartbeat job that is not ClawRange's (what the
        retired agents.defaults.heartbeat "0m" used to prevent)
        WHEN sync-heartbeat runs
        THEN our heartbeat still gets its scratch, and the marker names the
        foreign job and how to turn it off."""
        root = _openclaw(stubs, jobs=[HEARTBEAT_JOB, FOREIGN_HEARTBEAT])
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode != 0
        assert _scratched(root) == ["hb1"]
        text = marker.read_text()
        assert "hb-max" in text and "automations disable" in text

    @pytest.mark.parametrize(
        "job",
        [{**FOREIGN_HEARTBEAT, "enabled": False}, REMINDER_JOB, OTHER_JOB],
        ids=["disabled-foreign-heartbeat", "operator-reminder", "openclaw-dream"],
    )
    def test_other_jobs_do_not_trip_the_guard(self, tmp_path, stubs, job):
        """GIVEN a disabled foreign heartbeat, an operator's own reminder or
        an OpenClaw housekeeping job beside ours
        WHEN sync-heartbeat runs
        THEN nothing is flagged."""
        root = _openclaw(stubs, jobs=[HEARTBEAT_JOB, job])
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode == 0, proc.stderr
        assert not marker.exists()
        assert _scratched(root) == ["hb1"]

    def test_plaintext_secret_at_rest_marks_unhealthy(self, tmp_path, stubs):
        """GIVEN the secrets audit still finds a plaintext credential
        WHEN sync-heartbeat runs
        THEN the heartbeat is still loaded and the marker names the finding."""
        finding = "openclaw.json:channels.telegram.botToken is stored as plaintext."
        root = _openclaw(stubs, audit_findings=[finding])
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode != 0
        assert _scratched(root) == ["hb1"]
        text = marker.read_text()
        assert "plaintext" in text and "channels.telegram.botToken" in text

    def test_cli_error_skips_node_deprecation_noise(self, tmp_path, stubs):
        """GIVEN the job create fails and the CLI prints a Node deprecation
        warning after its error
        WHEN sync-heartbeat reports it
        THEN the marker carries the error, not the warning."""
        _openclaw(stubs, jobs=[], add_ok=False)
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode != 0
        text = marker.read_text()
        assert "no route for telegram" in text
        assert "DeprecationWarning" not in text

    def test_caps_existing_job_tools_from_config(self, tmp_path, stubs):
        """GIVEN a doctor-migrated heartbeat job with no tool cap
        WHEN sync-heartbeat runs
        THEN it sets the cap to max-ops' allow list from openclaw.json."""
        root = _openclaw(stubs, jobs=[OTHER_JOB, HEARTBEAT_JOB])
        proc = _run(SYNC, stubs, tmp_path / "m", "/opt/HEARTBEAT.md")
        assert proc.returncode == 0, proc.stderr
        edits = [json.loads(x) for x in (root / "edit.log").read_text().splitlines()]
        assert edits == [["hb1", "--tools", "read,web_fetch,cron"]]

    def test_matching_tool_cap_is_left_alone(self, tmp_path, stubs):
        """GIVEN a heartbeat job already capped to the config's allow list
        WHEN sync-heartbeat runs
        THEN it does not rewrite the job (no config revision churn)."""
        capped = {
            **HEARTBEAT_JOB,
            "payload": {**HEARTBEAT_JOB["payload"], "toolsAllow": MAX_OPS_TOOLS},
        }
        root = _openclaw(stubs, jobs=[capped])
        proc = _run(SYNC, stubs, tmp_path / "m", "/opt/HEARTBEAT.md")
        assert proc.returncode == 0, proc.stderr
        assert not (root / "edit.log").exists()
        assert _scratched(root) == ["hb1"]

    def test_unreadable_config_fails_loudly(self, tmp_path, stubs):
        """GIVEN openclaw.json has no max-ops allow list
        WHEN sync-heartbeat runs
        THEN it writes the marker instead of running the job uncapped."""
        root = _openclaw(stubs)
        marker = tmp_path / "sync-heartbeat.failed"
        missing = tmp_path / "nope.json"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md", config=missing)
        assert proc.returncode != 0
        assert "tool" in marker.read_text()
        assert not (root / "edit.log").exists()

    def test_fresh_install_without_chat_id_fails_loudly(self, tmp_path, stubs):
        """GIVEN no heartbeat job and an empty TELEGRAM_CHAT_ID
        WHEN sync-heartbeat runs
        THEN it creates nothing and writes the failure marker."""
        root = _openclaw(stubs, jobs=[])
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md", chat_id="")
        assert proc.returncode != 0
        assert not (root / "add.log").exists()
        assert "TELEGRAM_CHAT_ID" in marker.read_text()

    def test_failed_create_prints_cli_output_and_writes_marker(self, tmp_path, stubs):
        """GIVEN no heartbeat job and a gateway that rejects the create
        WHEN sync-heartbeat runs
        THEN the CLI error reaches the log and the marker is written."""
        _openclaw(stubs, jobs=[], add_ok=False)
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode != 0
        assert "no route for telegram" in proc.stderr
        # The healthcheck shows the marker, so it carries the reason too.
        assert "no route for telegram" in marker.read_text()

    def test_scratch_failure_prints_cli_output_and_writes_marker(self, tmp_path, stubs):
        _openclaw(stubs, scratch_ok=False)
        marker = tmp_path / "sync-heartbeat.failed"
        proc = _run(SYNC, stubs, marker, "/opt/HEARTBEAT.md")
        assert proc.returncode != 0
        assert "job hb1 is locked" in proc.stderr
        assert marker.exists()
        assert "hb1" in marker.read_text()

    def test_success_clears_stale_marker(self, tmp_path, stubs):
        _openclaw(stubs)
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


def test_start_command_never_applies_the_secrets_plan():
    """The plan deletes the auth profile an existing heartbeat job is pinned
    to as its inference account, and 2026.11.6 then stops that job ("was
    removed or signed out") instead of falling back. It is a one-time step
    that recreates the job too (docs/DEPLOYMENT_GUIDE.md), never part of an
    ordinary start; the startup secrets audit reports what it would fix."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    command = compose["services"]["openclaw"]["command"][2]
    code = [ln for ln in command.splitlines() if not ln.strip().startswith("#")]
    assert not [ln for ln in code if "secrets apply" in ln]


def test_heartbeat_marker_lives_in_root_owned_tmpfs():
    """/tmp and the state dir are owned by node (uid 1000), so a marker
    there could be swapped for a symlink before root writes it. /run is a
    root-owned tmpfs the node user cannot write to."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    tmpfs = compose["services"]["openclaw"].get("tmpfs") or []
    assert any(str(t).split(":")[0] == "/run" for t in tmpfs)
    for script in (SYNC, HEALTH):
        assert "${SYNC_HEARTBEAT_MARKER:-/run/" in script.read_text()


# ─── start.sh: gitignored soul.md on a fresh clone ────────────────


class TestStartSoul:
    """openclaw/soul.md is gitignored, so a fresh clone has none. Docker then
    creates the bind source as a root-owned directory and OpenClaw starts
    with no persona. start.sh must render it or stop with a clear message."""

    @pytest.fixture
    def repo(self, tmp_path):
        root = tmp_path / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / "openclaw").mkdir()
        shutil.copy(ROOT / "scripts" / "start.sh", root / "scripts" / "start.sh")
        (root / ".env").write_text(
            "OPENROUTER_API_KEY=sk-test\nCLAWRANGE_PROFILE=starter\n"
            "PROXY_AUTH_TOKEN=a-real-token\n"
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        _stub(bin_dir, "docker", f'echo "docker $*" >> "{log}"\n')
        _stub(bin_dir, "curl", "exit 0\n")
        _stub(bin_dir, "uv", "exit 0\n")
        return root, bin_dir, log

    def _start(self, repo, make_body):
        root, bin_dir, log = repo
        _stub(bin_dir, "make", make_body)
        proc = subprocess.run(
            ["bash", str(root / "scripts" / "start.sh")],
            capture_output=True,
            text=True,
            env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            check=False,
            timeout=30,
        )
        calls = log.read_text() if log.exists() else ""
        return proc, calls

    def test_renders_missing_soul_before_compose_up(self, repo):
        root = repo[0]
        proc, calls = self._start(
            repo,
            f'echo "make $*" >> "{repo[2]}"; echo soul > "{root}/openclaw/soul.md"\n',
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "make persona PROFILE=starter" in calls
        assert calls.index("make persona") < calls.index("docker compose up")
        assert (root / "openclaw" / "soul.md").is_file()

    def test_failed_render_stops_before_compose_up(self, repo):
        proc, calls = self._start(repo, "exit 2\n")
        assert proc.returncode != 0
        assert "soul.md" in proc.stdout + proc.stderr
        assert "compose up" not in calls

    def test_soul_directory_fails_fast(self, repo):
        (repo[0] / "openclaw" / "soul.md").mkdir()
        proc, calls = self._start(repo, "exit 0\n")
        assert proc.returncode != 0
        assert "directory" in proc.stdout + proc.stderr
        assert "compose up" not in calls

    def test_existing_soul_is_left_alone(self, repo):
        soul = repo[0] / "openclaw" / "soul.md"
        soul.write_text("operator persona\n")
        proc, calls = self._start(repo, f'echo "make $*" >> "{repo[2]}"\n')
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "make persona" not in calls
        assert soul.read_text() == "operator persona\n"


# ─── PROXY_AUTH_TOKEN: empty or the .env.example placeholder ──────


class TestStartToken(TestStartSoul):
    """.env.example ships PROXY_AUTH_TOKEN empty. A stack started without a
    real token answers 401 on every protected route (the API fails closed),
    so start.sh must stop before compose up and say how to fix it."""

    @pytest.mark.parametrize("value", ["", "generate-a-token-here"])
    def test_missing_or_placeholder_token_stops_before_compose_up(self, repo, value):
        root = repo[0]
        (root / "openclaw" / "soul.md").write_text("persona\n")
        env = (
            (root / ".env")
            .read_text()
            .replace("PROXY_AUTH_TOKEN=a-real-token", f"PROXY_AUTH_TOKEN={value}")
        )
        (root / ".env").write_text(env)
        proc, calls = self._start(repo, "exit 0\n")
        assert proc.returncode != 0
        assert "PROXY_AUTH_TOKEN" in proc.stdout + proc.stderr
        assert "compose up" not in calls


@pytest.mark.parametrize(
    ("value", "ok"),
    [("", False), ("generate-a-token-here", False), ("a-real-token", True)],
)
def test_workflows_auth_init_rejects_placeholder(tmp_path, value, ok):
    script = (
        f'. "{ROOT / "scripts" / "workflows_auth.sh"}"; '
        'workflows_auth_init && cat "$WF_AUTH_HEADER"'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "TMPDIR": str(tmp_path),
            "PROXY_AUTH_TOKEN": value,
        },
        check=False,
        timeout=30,
    )
    assert (proc.returncode == 0) is ok, proc.stderr
    if ok:
        assert "Authorization: Bearer a-real-token" in proc.stdout
    else:
        assert "PROXY_AUTH_TOKEN" in proc.stderr
