"""Persona meta-learning pipeline: propose -> draft -> approve -> overlay.

Approved learnings live in the brain (the live store) and project to a
git-trackable learned.yaml overlay (export/seed are inverse operations).
"""

from __future__ import annotations

import logging
import os
import re

import yaml

import tenant_profile

VALID_KINDS = {"identity", "persona"}
VALID_SOURCES = {"feedback", "reflect", "scheduled", "signal", "seed"}
CONTENT_MAX = 500
TARGET_MAX = 80  # interpolated verbatim into rendered headings
# Learnings render as one Markdown bullet in soul.md; a newline or control
# character would let a proposal open its own heading and inject instructions.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_log = logging.getLogger("clawrange.persona")


def _validate_learning(kind, content, source, target=""):
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
    if len(target or "") > TARGET_MAX:
        raise ValueError(f"target exceeds {TARGET_MAX} chars")
    if _CONTROL.search(content) or _CONTROL.search(target or ""):
        raise ValueError("target and content must be a single line of plain text")
    return content


def propose(brain_db, profile_name, kind, target, content, source):
    content = _validate_learning(kind, content, source, target=target)
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
    # Re-approving an approved row still re-renders: it is how an operator
    # retries after a render that failed to write.
    if row["status"] != "approved":
        brain_db.set_learning_status(learning_id, "approved")
    approved = brain_db.list_learnings(profile=profile_name, status="approved")
    rendered = render_fn(approved)
    out = dict(brain_db.get_learning(learning_id))
    # Per-target write results, so a render that wrote nothing is visible.
    out["render"] = rendered
    return out


def reject(brain_db, profile_name, learning_id, render_fn=None):
    row = brain_db.get_learning(learning_id)
    if not row or row["profile"] != profile_name:
        raise ValueError("learning not found")
    # An approved row's content is baked into the rendered files, so
    # rejecting it re-renders the surviving set. Rejecting an already
    # rejected row re-renders too, as the retry for a failed write.
    needs_render = row["status"] in ("approved", "rejected")
    out = dict(brain_db.set_learning_status(learning_id, "rejected"))
    if needs_render and render_fn is not None:
        out["render"] = render_fn(
            brain_db.list_learnings(profile=profile_name, status="approved")
        )
    return out


def export_overlay(brain_db, profile_name, existing=None):
    """Project the brain's approved learnings onto the overlay list.

    ``existing`` is the current learned.yaml list. Entries the brain has
    never seen are kept (the brain may serve another profile, or be fresh).
    An entry is dropped only when the brain rejected it and holds no
    approved copy; a pending re-proposal does not remove it. New approvals
    are appended in the brain's order.
    """
    rows = brain_db.list_learnings(profile=profile_name)
    statuses: dict[tuple, set] = {}
    for r in rows:
        statuses.setdefault((r["target"], r["content"]), set()).add(r["status"])

    def _key(item):
        # seed_overlay stores the stripped content; match on it too, or a
        # padded learned.yaml entry would look unknown to the brain.
        return (item.get("target"), str(item.get("content") or "").strip())

    out = []
    for item in existing or []:
        seen = statuses.get(_key(item), set())
        if "rejected" not in seen or "approved" in seen:
            out.append(item)
    kept = {_key(e) for e in out}
    for r in rows:
        if r["status"] == "approved" and (r["target"], r["content"]) not in kept:
            out.append(
                {
                    "kind": r["kind"],
                    "target": r["target"],
                    "content": r["content"],
                    "source": r["source"],
                    "approved_at": r["decided_at"],
                }
            )
    return out


def scan_signals(brain_db, profile_name):
    """Weak outcome-signal source (flagged). Off unless PERSONA_SIGNAL_LEARNING set."""
    if not os.environ.get("PERSONA_SIGNAL_LEARNING"):
        return []
    pending = brain_db.list_learnings(profile=profile_name, status="pending")
    # Repetition heuristic: 3+ pending with same target -> surface a meta-note.
    # Count only real feedback: the scan's own signal rows must not feed the
    # count, and a target that already has a pending signal is not re-flagged.
    by_target: dict[str, int] = {}
    signalled = set()
    for r in pending:
        if r["source"] == "signal":
            signalled.add(r["target"])
            continue
        by_target[r["target"]] = by_target.get(r["target"], 0) + 1
    out = []
    for target, count in by_target.items():
        if count >= 3 and target not in signalled:
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
    path = tenant_profile.default_profiles_dir() / profile_name / "learned.yaml"
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
        try:
            content = _validate_learning(
                item.get("kind", "persona"),
                item.get("content"),
                item.get("source", "seed"),
                target=item.get("target") or "",
            )
        except ValueError as exc:
            _log.warning("seed_overlay skipping invalid item %r: %s", item, exc)
            continue
        # Compare and store the stripped value, so trailing whitespace in
        # learned.yaml neither re-seeds on every boot nor reaches the render.
        key = (item.get("target"), content)
        if key in existing:
            continue
        row = brain_db.create_learning(
            profile_name,
            item.get("kind", "persona"),
            item.get("target"),
            content,
            item.get("source", "seed"),
        )
        existing.add(key)
        brain_db.set_learning_status(row["id"], "approved")
        n += 1
    return n
