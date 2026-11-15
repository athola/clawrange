"""Render-target discovery for runtime persona re-render (app.py)."""

import app


def test_workspace_targets_discovered_from_state_dir(tmp_path, monkeypatch):
    """Every OpenClaw workspace under the state dir gets the rendered files,
    without the core hard-coding one operator's agent names."""
    for ws in ("workspace", "workspace-ops", "workspace-x"):
        (tmp_path / ws).mkdir()
    (tmp_path / "not-a-workspace").mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    t = app._persona_render_targets(None)
    assert sorted(t["workspace_soul"]) == sorted(
        str(tmp_path / ws / "SOUL.md")
        for ws in ("workspace", "workspace-ops", "workspace-x")
    )
    assert len(t["workspace_identity"]) == 3


def test_repo_targets_skipped_when_repo_dir_absent(tmp_path, monkeypatch):
    """Inside the container app.py lives at /app, so the repo's openclaw/
    directory doesn't exist. Those targets used to fail silently on every
    approve. Skip them instead of reporting a write that can never work."""
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(app, "_REPO_ROOT", str(tmp_path / "no-such-repo"))
    t = app._persona_render_targets(None)
    assert "soul" not in t and "identity" not in t
