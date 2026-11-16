"""Every operator-tunable HEARTBEAT_* setting the workflows code reads must
reach the container. docker-compose.yml passes variables one by one (never
env_file), so a tunable missing there is silently ignored when set in .env.
Paths and state files are container-internal and stay out."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
INTERNAL = re.compile(r"_(PATH|STATE)$")


def _code_vars() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "workflows").glob("*.py"):
        found |= set(re.findall(r"""["'](HEARTBEAT_[A-Z_]+)["']""", path.read_text()))
    return {v for v in found if not INTERNAL.search(v)}


def _compose_env() -> dict[str, str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["workflows"]["environment"]
    pairs = (e.split("=", 1) for e in env) if isinstance(env, list) else env.items()
    return {k: v for k, v in pairs}


def test_heartbeat_tunables_reach_the_workflows_container():
    missing = sorted(_code_vars() - set(_compose_env()))
    assert not missing, f"not passed to workflows in docker-compose.yml: {missing}"


def test_import_time_integer_tunables_never_get_an_empty_default():
    """digest.py and watchdog.py parse these at import; '' would crash boot."""
    env = _compose_env()
    for name in (
        "HEARTBEAT_DIGEST_MAX_LINES",
        "HEARTBEAT_DIGEST_MAX_MESSAGES",
        "HEARTBEAT_DIGEST_RESULT_LIMIT",
        "HEARTBEAT_WATCH_STALL_SECS",
        "HEARTBEAT_WATCH_REALERT_SECS",
        "HEARTBEAT_WATCH_ACTIVE",
    ):
        value = env.get(name, "")
        assert not value.endswith(":-}"), f"{name} has an empty default: {value}"
