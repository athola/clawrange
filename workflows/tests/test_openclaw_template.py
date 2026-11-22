"""The OpenClaw config template must stay valid for the pinned OpenClaw.

OpenClaw 2026.11.5 retired heartbeat config: `agents.defaults.heartbeat` and
`agents.entries.*.heartbeat` make the whole config invalid, and the gateway
refuses to start. render-config.js deep-merges this template over the live
openclaw.json on every start, so a heartbeat key here would come back after
`openclaw doctor --fix` removed it. The heartbeat is an automation job now;
openclaw/sync-heartbeat.sh owns its message, and tests/test_openclaw_scripts.py
checks that the message is one workflows/heartbeat.py intercepts.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "openclaw" / "config" / "openclaw.json"
COMPOSE = ROOT / "docker-compose.yml"


def _agents() -> dict:
    return json.loads(TEMPLATE.read_text())["agents"]


def test_template_has_no_retired_default_heartbeat():
    """GIVEN the rendered template
    WHEN OpenClaw 2026.11.5+ validates it
    THEN agents.defaults carries no retired heartbeat block."""
    assert "heartbeat" not in _agents()["defaults"]


def test_template_has_no_retired_agent_heartbeat():
    """GIVEN the rendered template
    WHEN OpenClaw 2026.11.5+ validates it
    THEN no agent entry carries a retired heartbeat block."""
    entries = _agents()["entries"]
    offenders = [agent for agent, entry in entries.items() if "heartbeat" in entry]
    assert not offenders, f"retired heartbeat config on: {offenders}"


def test_heartbeat_agent_still_exists():
    """GIVEN sync-heartbeat.sh creates the job for agent max-ops
    THEN the template still defines that agent."""
    assert "max-ops" in _agents()["entries"]


def test_compose_pins_an_openclaw_without_heartbeat_config():
    """GIVEN this template no longer configures heartbeats
    THEN compose pins an OpenClaw new enough to run them as automations
    (2026.11.3 would start with no heartbeat at all)."""
    image = next(
        line.split("image:", 1)[1].strip()
        for line in COMPOSE.read_text().splitlines()
        if "ghcr.io/openclaw/openclaw:" in line
    )
    version = tuple(int(p) for p in image.rsplit(":", 1)[1].split("."))
    assert version >= (2026, 11, 5), image
