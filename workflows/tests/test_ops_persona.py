"""Ops-agent persona: rendered from a profile's assistant.ops block and the
operator's org/location (kept in .env), never hard-coded in a tracked file."""

import pytest

import app
from persona import render_all, render_ops_persona
from tenant_profile import Profile, ProfileError, validate


def _profile(ops=None, owner=None):
    assistant = {"name": "Max", "owner": owner or {}}
    if ops is not None:
        assistant["ops"] = ops
    return Profile("x", {"profile": "x", "assistant": assistant})


class TestRenderOpsPersona:
    def test_no_ops_block_renders_nothing(self):
        assert render_ops_persona(_profile()) is None

    def test_org_and_location_come_from_owner(self):
        out = render_ops_persona(
            _profile(
                ops={"agent": "max-ops", "name": "Max Ops"},
                owner={"org": "Acme Hardware", "location": "Springfield"},
            )
        )
        assert out.startswith("# Max Ops: Acme Hardware Internal Agent")
        assert "for **Acme Hardware** in Springfield" in out
        assert "Max's operational counterpart" in out
        assert "{{" not in out

    def test_without_org_names_no_organization(self):
        out = render_ops_persona(_profile(ops={"agent": "ops"}))
        assert out.startswith("# Max Ops\n")
        assert "for **" not in out and "{{" not in out


class TestRenderAllOps:
    def test_ops_targets_get_ops_persona_not_main(self, tmp_path):
        p = _profile(ops={"agent": "max-ops"}, owner={"org": "Acme"})
        main_ws = tmp_path / "workspace" / "SOUL.md"
        ops_ws = tmp_path / "workspace-max-ops" / "SOUL.md"
        ops_repo = tmp_path / "soul-ops.md"
        r = render_all(
            p,
            {
                "workspace_soul": [str(main_ws)],
                "ops_soul": str(ops_repo),
                "ops_workspace_soul": [str(ops_ws)],
            },
            [],
        )
        assert all(r.values())
        assert "operational counterpart" in ops_ws.read_text()
        assert "operational counterpart" in ops_repo.read_text()
        assert "operational counterpart" not in main_ws.read_text()

    def test_ops_targets_skipped_without_ops_block(self, tmp_path):
        r = render_all(_profile(), {"ops_soul": str(tmp_path / "o.md")}, [])
        assert r == {} and not (tmp_path / "o.md").exists()


class TestOpsValidation:
    def test_unknown_key_rejected(self):
        with pytest.raises(ProfileError):
            validate(
                _profile(ops={"agent": "ops", "org": "x"}), known_generator_kinds=set()
            )

    @pytest.mark.parametrize("agent", ["", "Max Ops", "../etc", None])
    def test_agent_must_be_a_plain_id(self, agent):
        with pytest.raises(ProfileError):
            validate(_profile(ops={"agent": agent}), known_generator_kinds=set())

    def test_valid_block_passes(self):
        validate(
            _profile(ops={"agent": "max-ops", "name": "Max Ops"}),
            known_generator_kinds=set(),
        )


class TestOpsRenderTargets:
    def test_ops_workspace_moves_out_of_main_targets(self, tmp_path, monkeypatch):
        """Before, an approve wrote the main persona into every workspace,
        including the ops agent's."""
        for ws in ("workspace", "workspace-max-ops"):
            (tmp_path / ws).mkdir()
        monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
        t = app._persona_render_targets(_profile(ops={"agent": "max-ops"}))
        assert t["workspace_soul"] == [str(tmp_path / "workspace" / "SOUL.md")]
        assert t["ops_workspace_soul"] == [
            str(tmp_path / "workspace-max-ops" / "SOUL.md")
        ]

    def test_without_ops_block_only_main_workspace(self, tmp_path, monkeypatch):
        # An undeclared ops agent's workspace must not get the main persona.
        for ws in ("workspace", "workspace-max-ops"):
            (tmp_path / ws).mkdir()
        monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
        t = app._persona_render_targets(_profile())
        assert t["workspace_soul"] == [str(tmp_path / "workspace" / "SOUL.md")]
        assert t.get("ops_workspace_soul", []) == []
