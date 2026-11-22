"""Render a profile's persona files on the host (``make persona``/``make profile``).

The learnings rendered are the brain's approved set projected onto the
git-tracked ``learned.yaml`` overlay (``export_overlay``), so approvals that
were never exported still render and rejected ones drop out. When the brain
DB is missing or unreadable the overlay alone is rendered, with a warning.

Usage:
    python3 workflows/scripts/persona_render.py <profile-name> [brain-db]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running from the repo root: make the workflows package importable.
# The workflows imports below must follow this line.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import persona_learning as pl
from brain_db import BrainDB
from persona import render_all
from tenant_profile import load_profile

DEFAULT_BRAIN = "data/brain/brain.db"
DEFAULT_TARGETS = {
    "soul": "openclaw/soul.md",
    "identity": "openclaw/identity.md",
    "ops_soul": "openclaw/soul-ops.md",
}


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def learnings(name: str, brain_path: str) -> list:
    """Approved learnings to render: brain approvals plus the overlay."""
    overlay = pl.load_overlay(name)
    if not os.path.exists(brain_path):
        # BrainDB() would create an empty DB here; never do that from setup.
        _warn(
            f"no brain DB at {brain_path}: rendering learned.yaml only "
            "(approvals not yet exported are missing)"
        )
        return overlay
    try:
        return pl.export_overlay(BrainDB(brain_path), name, existing=overlay)
    except Exception as exc:
        _warn(
            f"cannot read brain DB {brain_path} ({exc}): rendering "
            "learned.yaml only (approvals not yet exported are missing)"
        )
        return overlay


def render(name: str, brain_path: str = DEFAULT_BRAIN, targets=None) -> dict:
    profile = load_profile(name)
    return render_all(profile, targets or DEFAULT_TARGETS, learnings(name, brain_path))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    results = render(argv[0], argv[1] if len(argv) > 1 else DEFAULT_BRAIN)
    print("rendered ->", {k: ("ok" if v else "failed") for k, v in results.items()})
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
