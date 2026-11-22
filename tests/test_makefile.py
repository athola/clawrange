"""Makefile targets that need .env values must load it themselves.

`make` does not read .env, so a target that renders a profile (which
resolves ${VAR} references) or calls the bearer-protected API silently
gets empty values unless the recipe sources it. These tests dry-run each
target and check that the .env load comes before the first command.
"""

import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NEEDS_ENV = [
    ("persona", ["PROFILE=starter"]),
    ("profile", ["PROFILE=starter"]),
    ("persona-export", ["PROFILE=starter"]),
    ("tome-bridge", []),
    ("retrieval-eval-refresh", []),
    ("retrieval-eval-brain", []),
]


def _dry_run(target: str, args: list[str]) -> str:
    out = subprocess.run(
        ["make", "-n", "-C", ROOT, target, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


@pytest.mark.parametrize(("target", "args"), NEEDS_ENV)
def test_target_loads_env_before_running(target, args):
    script = _dry_run(target, args)
    assert ". ./.env" in script, f"make {target} never loads .env"
    assert script.index(". ./.env") < script.index("uv run"), (
        f"make {target} runs uv before loading .env"
    )
