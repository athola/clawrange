"""Persona meta-learning pipeline: propose -> draft -> approve -> overlay.

Approved learnings live in the brain (the live store) and project to a
git-trackable learned.yaml overlay (export/seed are inverse operations).
"""

from __future__ import annotations

import logging
import os

VALID_KINDS = {"identity", "persona"}
VALID_SOURCES = {"feedback", "reflect", "scheduled", "signal", "seed"}
CONTENT_MAX = 500

_log = logging.getLogger("clawrange.persona")


def _validate_learning(kind, content, source):
    """Validate a learning's fields. Returns the stripped content. Raises ValueError."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}")
    content = (content or "").strip()
    if not content:
        raise ValueError("content is required")
    if len(content) > CONTENT_MAX:
        raise ValueError(f"content exceeds {CONTENT_MAX} chars")
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {sorted(VALID_SOURCES)}")
    return content


def propose(brain_db, profile_name, kind, target, content, source):
    content = _validate_learning(kind, content, source)
    task = brain_db.create_task(
        f"[DRAFT] persona {kind} enhancement ({target}): {content}",
        priority=3,
        source="persona",
    )
    return brain_db.create_learning(
        profile_name, kind, target, content, source, task_id=task["id"]
    )


def approve(brain_db, profile_name, learning_id, render_fn):
    row = brain_db.get_learning(learning_id)
    if not row:
        raise ValueError("learning not found")
    if row["profile"] != profile_name:
        raise ValueError("learning not found")
    if row["status"] == "approved":
        return row  # idempotent
    brain_db.set_learning_status(learning_id, "approved")
    approved = brain_db.list_learnings(profile=profile_name, status="approved")
    render_fn(approved)
    return brain_db.get_learning(learning_id)


def reject(brain_db, profile_name, learning_id):
    row = brain_db.get_learning(learning_id)
    if not row or row["profile"] != profile_name:
        raise ValueError("learning not found")
    return brain_db.set_learning_status(learning_id, "rejected")


def export_overlay(brain_db, profile_name):
    rows = brain_db.list_learnings(profile=profile_name, status="approved")
    return [
        {
            "kind": r["kind"],
            "target": r["target"],
            "content": r["content"],
            "source": r["source"],
            "approved_at": r["decided_at"],
        }
        for r in rows
    ]


def scan_signals(brain_db, profile_name):
    """Weak outcome-signal source (flagged). Off unless PERSONA_SIGNAL_LEARNING set."""
    if not os.environ.get("PERSONA_SIGNAL_LEARNING"):
        return []
    pending = brain_db.list_learnings(profile=profile_name, status="pending")
    # Repetition heuristic: 3+ pending with same target -> surface a meta-note.
    by_target = {}
    for r in pending:
        by_target.setdefault(r["target"], 0)
        by_target[r["target"]] += 1
    out = []
    for target, count in by_target.items():
        if count >= 3:
            out.append(
                propose(
                    brain_db,
                    profile_name,
                    "persona",
                    target,
                    f"Recurring feedback about '{target}' ({count}x) — consolidate.",
                    "signal",
                )
            )
    return out


def load_overlay(profile_name):
    """Read a profile's git-tracked learned.yaml overlay into a list.

    The inverse-on-disk of export_overlay: returns the ``learned`` list from
    ``config/profiles/<name>/learned.yaml``. Missing file or empty overlay
    returns ``[]`` (never raises) so setup and boot stay resilient.
    """
    import yaml

    from tenant_profile import default_profiles_dir

    path = default_profiles_dir() / profile_name / "learned.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("learned", []) or []


def seed_overlay(brain_db, profile_name, overlay):
    existing = {
        (r["target"], r["content"])
        for r in brain_db.list_learnings(profile=profile_name)
    }
    n = 0
    for item in overlay or []:
        key = (item.get("target"), item.get("content"))
        if key in existing:
            continue
        try:
            _validate_learning(
                item.get("kind", "persona"),
                item.get("content"),
                item.get("source", "seed"),
            )
        except ValueError as exc:
            _log.warning("seed_overlay skipping invalid item %r: %s", item, exc)
            continue
        row = brain_db.create_learning(
            profile_name,
            item.get("kind", "persona"),
            item.get("target"),
            item["content"],
            item.get("source", "seed"),
        )
        brain_db.set_learning_status(row["id"], "approved")
        n += 1
    return n
