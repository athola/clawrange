"""Render-target discovery for runtime persona re-render (app.py)."""

import app


def test_workspace_targets_only_main_workspace(tmp_path, monkeypatch):
    """Only the main agent's workspace gets the main persona; other agents'
    workspaces (undeclared ops agents included) are left alone."""
    for ws in ("workspace", "workspace-ops", "workspace-x"):
        (tmp_path / ws).mkdir()
    (tmp_path / "not-a-workspace").mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    t = app._persona_render_targets(None)
    assert t["workspace_soul"] == [str(tmp_path / "workspace" / "SOUL.md")]
    assert t["workspace_identity"] == [str(tmp_path / "workspace" / "IDENTITY.md")]


def test_repo_targets_skipped_when_repo_dir_absent(tmp_path, monkeypatch):
    """Inside the container app.py lives at /app, so the repo's openclaw/
    directory doesn't exist. Those targets used to fail silently on every
    approve. Skip them instead of reporting a write that can never work."""
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(app, "_REPO_ROOT", str(tmp_path / "no-such-repo"))
    t = app._persona_render_targets(None)
    assert "soul" not in t and "identity" not in t
