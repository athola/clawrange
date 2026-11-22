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


TOKEN_REF = {"source": "env", "provider": "default", "id": "PROXY_AUTH_TOKEN"}
PLAN = ROOT / "openclaw" / "secrets-plan.json"


def test_proxy_api_key_is_an_env_secret_ref():
    """GIVEN the template render-config.js writes on every start
    THEN the proxy apiKey is an env SecretRef, so openclaw.json never holds
    the token (OpenClaw's secrets audit flagged the plaintext value)."""
    providers = json.loads(TEMPLATE.read_text())["models"]["providers"]
    assert providers["clawrange-proxy"]["apiKey"] == TOKEN_REF


def test_fs_tools_are_confined_to_the_workspace():
    """GIVEN sandboxing is off and max-ops may use `read`
    THEN fs tools are workspace-only, so no agent can read openclaw.json,
    its rollback copies or /proc/<pid>/environ (where the env ref resolves)."""
    tools = json.loads(TEMPLATE.read_text())["tools"]
    assert tools["fs"]["workspaceOnly"] is True


def test_secrets_plan_moves_the_token_out_of_state_and_leaves_env_files():
    """GIVEN the one-time migration in docs/DEPLOYMENT_GUIDE.md
    THEN the plan points the proxy apiKey at the template's env ref, scrubs
    the plaintext auth-profile copy OpenClaw kept in its state DB, and never
    edits .env files."""
    plan = json.loads(PLAN.read_text())
    (target,) = plan["targets"]
    assert target["path"] == "models.providers.clawrange-proxy.apiKey"
    assert target["ref"] == TOKEN_REF
    assert plan["options"]["scrubEnv"] is False
    assert plan["options"]["scrubAuthProfilesForProviderTargets"] is True


def test_compose_mounts_the_secrets_plan_read_only():
    mount = "./openclaw/secrets-plan.json:/opt/secrets-plan.json:ro"
    assert mount in COMPOSE.read_text()
