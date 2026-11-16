"""Regression tests for the PR #11 review fixes in the persona, profile,
CRM, connector and deal modules (cluster K7)."""

from __future__ import annotations

import os
import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as app_module
import persona_learning as pl
from brain_db import BrainDB
from persona import render_all
from persona_api import create_persona_router
from tenant_profile import Profile


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _profile(**assistant) -> Profile:
    assistant.setdefault("name", "Max")
    return Profile("t", {"profile": "t", "assistant": assistant})


@pytest.fixture
def db(tmp_path):
    d = BrainDB(str(tmp_path / "b.db"))
    d.init_db()
    return d


# ─── #16: rendered files stay readable by OpenClaw ─────────────────


def test_render_new_file_is_world_readable(tmp_path):
    """mkstemp creates 0600; OpenClaw (uid 1000) must still read a file the
    root workflows container rendered."""
    soul = tmp_path / "SOUL.md"
    render_all(_profile(), {"soul": str(soul)}, [])
    assert _mode(soul) == 0o644


def test_render_keeps_existing_file_mode(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("old")
    os.chmod(soul, 0o664)
    render_all(_profile(), {"soul": str(soul)}, [])
    assert _mode(soul) == 0o664
    assert "Max" in soul.read_text()


# ─── #39 / S3: identity and workspace targets ──────────────────────


def test_render_skips_identity_without_identity_block(tmp_path):
    """A profile with no identity block must not overwrite a hand-written
    IDENTITY.md with template defaults."""
    ident = tmp_path / "IDENTITY.md"
    ident.write_text("hand-written")
    ws_ident = tmp_path / "ws" / "IDENTITY.md"
    res = render_all(
        _profile(),
        {"identity": str(ident), "workspace_identity": [str(ws_ident)]},
        [],
    )
    assert ident.read_text() == "hand-written"
    assert not ws_ident.exists()
    assert res == {}


def test_render_writes_identity_with_identity_block(tmp_path):
    ident = tmp_path / "IDENTITY.md"
    res = render_all(
        _profile(identity={"creature": "CoS"}), {"identity": str(ident)}, []
    )
    assert res == {"identity": True}
    assert "CoS" in ident.read_text()


def test_targets_only_main_and_declared_ops_workspace(tmp_path, monkeypatch):
    for ws in ("workspace", "workspace-max-ops", "workspace-other"):
        (tmp_path / ws).mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    prof = _profile(ops={"agent": "max-ops"})
    t = app_module._persona_render_targets(prof)
    assert t["workspace_soul"] == [str(tmp_path / "workspace" / "SOUL.md")]
    assert t["workspace_identity"] == [str(tmp_path / "workspace" / "IDENTITY.md")]
    assert t["ops_workspace_soul"] == [str(tmp_path / "workspace-max-ops" / "SOUL.md")]


def test_targets_without_ops_leave_other_workspaces_alone(tmp_path, monkeypatch):
    """No ops block: an ops agent's workspace must not get the main persona."""
    for ws in ("workspace", "workspace-max-ops"):
        (tmp_path / ws).mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    t = app_module._persona_render_targets(_profile())
    assert t["workspace_soul"] == [str(tmp_path / "workspace" / "SOUL.md")]
    assert "ops_workspace_soul" not in t


def test_targets_skip_missing_main_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    t = app_module._persona_render_targets(_profile())
    assert t["workspace_soul"] == [] and t["workspace_identity"] == []


# ─── S4: make persona / make profile render from brain + overlay ───


def _render_script():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "scripts"
    spec = importlib.util.spec_from_file_location(
        "persona_render", path / "persona_render.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_profile(base, name, identity=True):
    d = base / name
    d.mkdir(parents=True)
    ident = "  identity: {creature: CoS}\n" if identity else ""
    (d / "profile.yaml").write_text(
        f"profile: {name}\nassistant:\n  name: Max\n{ident}"
    )
    return d


def test_persona_render_merges_brain_approvals(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    d = _write_profile(profiles, "cos")
    (d / "learned.yaml").write_text(
        "learned:\n  - {kind: persona, target: Tone, content: From overlay.}\n"
    )
    monkeypatch.setenv("CLAWRANGE_PROFILES_DIR", str(profiles))
    brain = BrainDB(str(tmp_path / "brain.db"))
    brain.init_db()
    row = pl.propose(brain, "cos", "persona", "Style", "From brain.", "feedback")
    brain.set_learning_status(row["id"], "approved")
    out = tmp_path / "out"
    results = _render_script().render(
        "cos",
        str(tmp_path / "brain.db"),
        {"soul": str(out / "soul.md"), "identity": str(out / "identity.md")},
    )
    soul = (out / "soul.md").read_text()
    assert "From overlay." in soul and "From brain." in soul
    assert results == {"soul": True, "identity": True}


def test_persona_render_warns_without_brain(tmp_path, monkeypatch, capsys):
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "cos", identity=False)
    monkeypatch.setenv("CLAWRANGE_PROFILES_DIR", str(profiles))
    missing = tmp_path / "nope" / "brain.db"
    out = tmp_path / "out"
    results = _render_script().render(
        "cos",
        str(missing),
        {"soul": str(out / "soul.md"), "identity": str(out / "identity.md")},
    )
    assert "WARNING" in capsys.readouterr().err
    assert not missing.exists()  # never creates a brain DB
    assert results == {"soul": True}  # no identity block -> identity skipped


# ─── G7 / #51: approve and reject return render results; retry re-renders ──


def test_approve_retry_rerenders(db):
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    calls = []

    def render_fn(approved):
        calls.append(len(approved))
        return {"soul": len(calls) > 1}

    first = pl.approve(db, "cos", row["id"], render_fn=render_fn)
    assert first["render"] == {"soul": False}
    second = pl.approve(db, "cos", row["id"], render_fn=render_fn)
    assert second["render"] == {"soul": True}
    assert calls == [1, 1]


def test_reject_returns_render_and_retry_rerenders(db):
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    pl.approve(db, "cos", row["id"], render_fn=lambda a: {"soul": True})
    calls = []

    def render_fn(approved):
        calls.append(len(approved))
        return {"soul": True}

    out = pl.reject(db, "cos", row["id"], render_fn=render_fn)
    assert out["status"] == "rejected" and out["render"] == {"soul": True}
    again = pl.reject(db, "cos", row["id"], render_fn=render_fn)
    assert again["render"] == {"soul": True}
    assert calls == [0, 0]


def _persona_client(db, targets_fn):
    prof = Profile("cos", {"profile": "cos", "assistant": {"name": "Max"}})
    api = FastAPI()
    api.include_router(create_persona_router(db, lambda: prof, targets_fn))
    return TestClient(api)


def test_reject_api_returns_render_results(db, tmp_path):
    client = _persona_client(db, lambda p: {"soul": str(tmp_path / "soul.md")})
    row = pl.propose(db, "cos", "persona", "Tone", "Be terse.", "feedback")
    client.post(f"/persona/proposals/{row['id']}/approve")
    r = client.post(f"/persona/proposals/{row['id']}/reject")
    assert r.json()["render"] == {"soul": True}


# ─── #46: API callers cannot claim internal sources ────────────────


@pytest.mark.parametrize("source", ["seed", "signal"])
def test_propose_api_rejects_internal_sources(db, source):
    client = _persona_client(db, lambda p: {})
    r = client.post(
        "/persona/propose",
        json={"kind": "persona", "target": "x", "content": "y", "source": source},
    )
    assert r.status_code == 422


def test_seed_overlay_stores_stripped_content_once(db):
    overlay = [{"kind": "persona", "target": "Tone", "content": "  Be terse.  "}]
    assert pl.seed_overlay(db, "cos", overlay) == 1
    assert db.list_learnings(profile="cos")[0]["content"] == "Be terse."
    assert pl.seed_overlay(db, "cos", overlay) == 0  # no re-seed on next boot


# ─── #51: /healthz/persona reports writability ─────────────────────


def test_persona_health_reports_writability(db, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a dir")
    targets = {
        "soul": str(tmp_path / "soul.md"),
        "workspace_soul": [str(blocker / "SOUL.md")],
    }
    client = _persona_client(db, lambda p: targets)
    body = client.get("/healthz/persona").json()
    assert body["writable"] == {
        "soul": True,
        f"ws_soul:{blocker / 'SOUL.md'}": False,
    }
    assert sorted(body["targets"]) == ["soul", "workspace_soul"]


# ─── #15: scan_signals ignores its own signal rows ─────────────────


def test_scan_signals_does_not_repropose(db, monkeypatch):
    monkeypatch.setenv("PERSONA_SIGNAL_LEARNING", "1")
    for i in range(3):
        pl.propose(db, "cos", "persona", "Tone", f"feedback {i}", "feedback")
    assert len(pl.scan_signals(db, "cos")) == 1
    assert pl.scan_signals(db, "cos") == []
    pending = db.list_learnings(profile="cos", status="pending")
    assert sum(r["source"] == "signal" for r in pending) == 1
