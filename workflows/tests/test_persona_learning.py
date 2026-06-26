import pytest
from brain_db import BrainDB
import persona_learning as pl


@pytest.fixture
def db(tmp_path):
    d = BrainDB(str(tmp_path / "b.db"))
    d.init_db()
    return d


def test_propose_rejects_bad_kind(db):
    with pytest.raises(ValueError):
        pl.propose(db, "cos", "nonsense", "x", "y", "feedback")


def test_propose_creates_draft_and_pending(db):
    row = pl.propose(
        db,
        "cos",
        "persona",
        "Communication",
        "Lead with the recommendation.",
        "feedback",
    )
    assert row["status"] == "pending" and row["task_id"]
    task = db.get_task(row["task_id"])
    assert task["description"].startswith("[DRAFT]")


def test_approve_invokes_render_with_approved(db):
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    seen = {}
    pl.approve(db, "cos", row["id"], render_fn=lambda lrn: seen.update(n=len(lrn)))
    assert seen["n"] == 1
    assert db.get_learning(row["id"])["status"] == "approved"


def test_overlay_export_seed_roundtrip(db):
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    pl.approve(db, "cos", row["id"], render_fn=lambda lrn: None)
    overlay = pl.export_overlay(db, "cos")
    assert overlay and overlay[0]["content"] == "Be terse."
    db2 = BrainDB(":memory:")
    db2.init_db()
    n = pl.seed_overlay(db2, "cos", overlay)
    assert n == 1
    assert pl.export_overlay(db2, "cos")[0]["target"] == "Tone"
