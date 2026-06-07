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
