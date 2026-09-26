"""Offline walkthrough of the persona meta-learning loop — no API keys, no
containers, no network. Demonstrates the system end to end against a throwaway
brain so anyone who pulls the repo can *see* the proven example work.

Steps shown (all via the same functions the live service uses):
  1. render the profile's base identity + persona
  2. propose a persona enhancement (the operator-feedback path)
  3. approve it  -> appends to the brain + re-renders the read surfaces
  4. show the composed surfaces with the ``## Learned`` overlay
  5. export the approved overlay to a learned.yaml projection (printed, not
     written) — the git-portable artifact a repo-puller would commit.

Usage:
    python3 workflows/scripts/persona_demo.py [profile-name]   # default: chief-of-staff
    make persona-demo [PROFILE=<name>]
"""

import sys
import tempfile
from pathlib import Path

# Allow running from the repo root: make the workflows package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import persona_learning as pl  # noqa: E402
from brain_db import BrainDB  # noqa: E402
from persona import compose_identity, compose_persona, render_all  # noqa: E402
from tenant_profile import load_profile  # noqa: E402


def _rule(label):
    print(f"\n{'─' * 4} {label} {'─' * (60 - len(label))}")


def main(name="chief-of-staff"):
    profile = load_profile(name)
    work = Path(tempfile.mkdtemp(prefix="persona-demo-"))
    db = BrainDB(str(work / "brain.db"))
    db.init_db()

    # Seed any already-approved learnings the profile ships with (learned.yaml).
    pl.seed_overlay(db, name, pl.load_overlay(name))

    targets = {"soul": str(work / "soul.md"), "identity": str(work / "IDENTITY.md")}

    _rule(f"1. base render ({name})")
    print(compose_identity(profile, db.list_learnings(profile=name, status="approved")))

    _rule("2. propose (operator feedback)")
    row = pl.propose(
        db,
        name,
        "persona",
        "Communication",
        "Open every briefing with the single highest-leverage decision.",
        "feedback",
    )
    pending = db.list_learnings(profile=name, status="pending")
    print(
        f"proposed {row['id'][:8]} -> [DRAFT] task {row['task_id']}; "
        f"{len(pending)} pending approval"
    )

    _rule("3. approve -> re-render")
    pl.approve(
        db,
        name,
        row["id"],
        render_fn=lambda lrn: render_all(profile, targets, lrn),
    )
    print(f"approved {row['id'][:8]}; re-rendered {len(targets)} surfaces")

    _rule("4. composed surfaces (note the ## Learned region)")
    learned = db.list_learnings(profile=name, status="approved")
    print(compose_persona(profile, learned))

    _rule("5. export overlay (git-portable learned.yaml)")
    print(
        yaml.safe_dump(
            {"learned": pl.export_overlay(db, name)},
            allow_unicode=True,
            sort_keys=False,
        )
    )

    print(f"(scratch dir: {work})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "chief-of-staff")
