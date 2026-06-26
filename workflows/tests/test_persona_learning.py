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


def test_propose_rejects_bad_source(db):
    with pytest.raises(ValueError, match="source must be one of"):
        pl.propose(db, "cos", "persona", "x", "some content", "unknown_source")


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


def test_approve_idempotent(db):
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    call_count = {"n": 0}

    def render_fn(lrn):
        call_count["n"] += 1

    result1 = pl.approve(db, "cos", row["id"], render_fn=render_fn)
    result2 = pl.approve(db, "cos", row["id"], render_fn=render_fn)
    assert call_count["n"] == 1
    assert isinstance(result1, dict)
    assert isinstance(result2, dict)


def test_approve_rejects_cross_profile(db):
    row = pl.propose(db, "a", "persona", "Tone", "Be terse.", "feedback")
    with pytest.raises(ValueError, match="learning not found"):
        pl.approve(db, "b", row["id"], render_fn=lambda lrn: None)
    assert db.get_learning(row["id"])["status"] == "pending"


def test_reject_rejects_cross_profile(db):
    row = pl.propose(db, "a", "persona", "Tone", "Be terse.", "feedback")
    with pytest.raises(ValueError, match="learning not found"):
        pl.reject(db, "b", row["id"])
    assert db.get_learning(row["id"])["status"] == "pending"


def test_reject_unknown_raises(db):
    with pytest.raises(ValueError, match="learning not found"):
        pl.reject(db, "cos", "does-not-exist")


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


def test_seed_overlay_idempotent(db):
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    pl.approve(db, "cos", row["id"], render_fn=lambda lrn: None)
    overlay = pl.export_overlay(db, "cos")

    db2 = BrainDB(":memory:")
    db2.init_db()
    n1 = pl.seed_overlay(db2, "cos", overlay)
    assert n1 == 1

    n2 = pl.seed_overlay(db2, "cos", overlay)
    assert n2 == 0
    assert len(db2.list_learnings(profile="cos")) == 1


def test_seed_overlay_skips_invalid_items(db):
    valid_item = {
        "kind": "persona",
        "target": "Tone",
        "content": "Be terse.",
        "source": "seed",
    }
    invalid_kind = {"kind": "bogus", "target": "x", "content": "x", "source": "seed"}
    invalid_content = {
        "kind": "persona",
        "target": "y",
        "content": "",
        "source": "seed",
    }

    n = pl.seed_overlay(db, "cos", [valid_item, invalid_kind, invalid_content])
    assert n == 1
    assert len(db.list_learnings(profile="cos")) == 1
