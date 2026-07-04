"""Tests for persona rendering (FR-2)."""

from __future__ import annotations

import pathlib

from tenant_profile import Profile, load_profile
from persona import render_persona, write_soul


def _real_profiles_dir():
    return pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "profiles"


def test_render_uses_verbatim_persona_markdown():
    p = Profile(
        "x",
        {
            "profile": "x",
            "assistant": {
                "name": "John-117",
                "persona_markdown": "# John-117\n\nThe operator's assistant.",
            },
        },
    )
    out = render_persona(p)
    assert "John-117" in out
    assert "The operator's assistant." in out


def test_render_structured_includes_fields():
    p = Profile(
        "acme",
        {
            "profile": "acme",
            "assistant": {
                "name": "Acme Bot",
                "role": "sales operations assistant",
                "owner": {
                    "name": "Dana",
                    "org": "Acme Co",
                    "context": "Acme sells HVAC.",
                },
                "capabilities": ["Sync leads hourly", "Answer lead questions"],
                "channel": "telegram",
            },
        },
    )
    out = render_persona(p)
    assert "Acme Bot" in out
    assert "sales operations assistant" in out
    assert "Acme Co" in out
    assert "Acme sells HVAC." in out
    assert "Sync leads hourly" in out
    assert "Answer lead questions" in out


def test_render_structured_is_generic():
    p = Profile(
        "acme",
        {
            "profile": "acme",
            "assistant": {
                "name": "Acme Bot",
                "role": "assistant",
                "owner": {"context": "generic context"},
                "capabilities": ["do a thing"],
            },
        },
    )
    out = render_persona(p).lower()
    for banned in ("john-117", "alex", "eridanus", "webai"):
        assert banned not in out


def test_template_and_source_are_generic():
    tmpl = pathlib.Path(__file__).resolve().parents[2] / "openclaw" / "soul.template.md"
    src = pathlib.Path(__file__).resolve().parents[1] / "persona.py"
    for f in (tmpl, src):
        low = f.read_text().lower()
        for banned in ("john-117", "eridanus", "webai", "alex"):
            assert banned not in low, f"{f} leaks '{banned}'"


def test_marketing_render_contains_john117():
    p = load_profile("marketing", profiles_dir=_real_profiles_dir())
    out = render_persona(p)
    assert "John-117" in out


def test_render_identity_fills_fields_and_defaults_name():
    from tenant_profile import Profile
    from persona import render_identity

    p = Profile(
        name="t",
        raw={
            "profile": "t",
            "assistant": {
                "name": "Max",
                "identity": {
                    "creature": "Chief of Staff",
                    "vibe": "sharp",
                    "emoji": "🎯",
                },
            },
        },
    )
    out = render_identity(p)
    assert "**Name:** Max" in out  # defaults to assistant.name
    assert "**Creature:** Chief of Staff" in out
    assert "🎯" in out


def test_write_soul_round_trips(tmp_path):
    p = Profile(
        "acme",
        {
            "profile": "acme",
            "assistant": {"name": "Acme Bot", "role": "assistant", "capabilities": []},
        },
    )
    dest = tmp_path / "soul.md"
    write_soul(p, dest)
    assert dest.read_text() == render_persona(p)


def test_compose_appends_learned_region_and_is_non_destructive():
    from tenant_profile import Profile
    from persona import compose_persona

    p = Profile(name="t", raw={"profile": "t", "assistant": {"name": "Max"}})
    learnings = [
        {
            "kind": "persona",
            "target": "Communication",
            "content": "Lead with the recommendation.",
        }
    ]
    out = compose_persona(p, learnings)
    assert "## Learned" in out
    assert "Communication" in out and "Lead with the recommendation." in out
    # base persona still present
    assert "Max" in out
    # no learnings -> no Learned region
    assert "## Learned" not in compose_persona(p, [])


def test_render_all_writes_targets(tmp_path):
    from tenant_profile import Profile
    from persona import render_all

    p = Profile(name="t", raw={"profile": "t", "assistant": {"name": "Max"}})
    soul = tmp_path / "soul.md"
    ident = tmp_path / "IDENTITY.md"
    res = render_all(p, {"soul": str(soul), "identity": str(ident)}, [])
    assert res == {"soul": True, "identity": True}
    assert "Max" in soul.read_text() and "Name:** Max" in ident.read_text()


def test_render_all_unwritable_target_returns_false(tmp_path):
    """render_all's no-raise contract: an unwritable target reports False
    instead of raising, so approve never 500s after committing DB status."""
    from persona import render_all

    p = Profile("x", {"profile": "x", "assistant": {"name": "Max"}})
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    target = blocker / "soul.md"  # parent is a file -> OSError on write
    result = render_all(p, {"soul": str(target)}, [])
    assert result == {"soul": False}
