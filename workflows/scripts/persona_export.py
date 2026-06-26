"""Write a profile's approved persona learnings to its learned.yaml.

Exports the brain's approved learnings (the live store) into the git-tracked
``config/profiles/<name>/learned.yaml`` overlay so an evolved persona can be
committed and inherited by anyone who pulls the repo. Inverse of the
boot-time ``seed_overlay`` step in ``seed_from_profile``.

Usage:
    python3 workflows/scripts/persona_export.py <profile-name>
"""

import sys
from pathlib import Path

# Allow running from the repo root: make the workflows package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import persona_learning as pl  # noqa: E402
from brain_db import BrainDB  # noqa: E402
from tenant_profile import default_profiles_dir  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "starter"
db = BrainDB("data/brain/brain.db")
db.init_db()
overlay = {"learned": pl.export_overlay(db, name)}
dest = default_profiles_dir() / name / "learned.yaml"
dest.write_text(yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False))
print(f"wrote {dest} ({len(overlay['learned'])} learnings)")
