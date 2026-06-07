"""Tests for the tenant profile loader (FR-1)."""

from __future__ import annotations

import textwrap

import pytest

import tenant_profile as profile_mod
from tenant_profile import ProfileError, load_profile, resolve_env


# Generator kinds the validator should accept. Passed explicitly so the
# loader unit tests don't drag in the heavy generators module.
KNOWN_KINDS = {"pipeline", "crm_digest", "morning_digest", "hot_pulse", "content_idea"}


def _write_profile(base, name, body: str):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "profile.yaml").write_text(textwrap.dedent(body))
    return d


# ─── env interpolation ────────────────────────────────────────────


def test_resolve_env_substitutes_known_vars():
    out = resolve_env({"url": "${HOST}/x", "n": 3}, env={"HOST": "https://h"})
    assert out == {"url": "https://h/x", "n": 3}


def test_resolve_env_missing_var_becomes_empty():
    out = resolve_env("${NOPE}", env={})
    assert out == ""


def test_resolve_env_recurses_lists_and_dicts():
    out = resolve_env({"a": ["${X}", {"b": "${Y}"}]}, env={"X": "1", "Y": "2"})
    assert out == {"a": ["1", {"b": "2"}]}


# ─── loading ──────────────────────────────────────────────────────


def test_load_profile_reads_yaml(tmp_path):
    _write_profile(
        tmp_path,
        "acme",
        """
        profile: acme
        assistant: {name: Acme Bot}
        seeds: {projects: [], schedules: []}
        """,
    )
    p = load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)
    assert p.name == "acme"
    assert p.assistant["name"] == "Acme Bot"


def test_load_profile_resolves_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_TOKEN", "secret-123")
    _write_profile(
        tmp_path,
        "acme",
        """
        profile: acme
        connectors:
          - id: c1
            source: {kind: http_csv, url: "https://x", auth: {kind: bearer, token: "${PORTAL_TOKEN}"}}
            sink: {kind: crm, object: leads}
        """,
    )
    p = load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)
    assert p.connectors[0]["source"]["auth"]["token"] == "secret-123"


def test_load_profile_default_name_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWRANGE_PROFILE", "acme")
    _write_profile(tmp_path, "acme", "profile: acme\n")
    p = load_profile(profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)
    assert p.name == "acme"


def test_load_missing_profile_raises(tmp_path):
    with pytest.raises(ProfileError):
        load_profile("ghost", profiles_dir=tmp_path)


# ─── validation ───────────────────────────────────────────────────


def test_validate_missing_profile_key_raises(tmp_path):
    _write_profile(tmp_path, "acme", "assistant: {name: x}\n")
    with pytest.raises(ProfileError, match="profile"):
        load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)


def test_validate_name_mismatch_raises(tmp_path):
    _write_profile(tmp_path, "acme", "profile: notacme\n")
    with pytest.raises(ProfileError, match="match"):
        load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)


def test_validate_unknown_source_kind_raises(tmp_path):
    _write_profile(
        tmp_path,
        "acme",
        """
        profile: acme
        connectors:
          - id: c1
            source: {kind: telepathy}
            sink: {kind: crm, object: leads}
        """,
    )
    with pytest.raises(ProfileError, match="source kind"):
        load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)


def test_validate_unknown_adapter_raises(tmp_path):
    _write_profile(
        tmp_path,
        "acme",
        """
        profile: acme
        crm: {adapter: oracle}
        """,
    )
    with pytest.raises(ProfileError, match="adapter"):
        load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)


def test_validate_schedule_unknown_kind_raises(tmp_path):
    _write_profile(
        tmp_path,
        "acme",
        """
        profile: acme
        seeds:
          schedules:
            - {id: s1, name: S1, kind: nonexistent, cron: "0 8 * * *"}
        """,
    )
    with pytest.raises(ProfileError, match="kind"):
        load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)


def test_validate_schedule_undefined_connector_raises(tmp_path):
    _write_profile(
        tmp_path,
        "acme",
        """
        profile: acme
        seeds:
          schedules:
            - {id: s1, name: S1, kind: pipeline, cron: "0 * * * *", kwargs: {connector: ghost}}
        connectors:
          - id: real
            source: {kind: http_csv, url: x}
            sink: {kind: crm, object: leads}
        """,
    )
    with pytest.raises(ProfileError, match="connector"):
        load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)


# ─── accessors ────────────────────────────────────────────────────


def test_profile_accessors(tmp_path):
    _write_profile(
        tmp_path,
        "acme",
        """
        profile: acme
        seeds:
          projects:
            - {slug: p1, owner: o, repo: r, topics: [], subreddits: [], search_terms: [], posture: ""}
          schedules:
            - {id: s1, name: S1, kind: crm_digest, cron: "0 8 * * *", kwargs: {queries: [new_leads_count]}}
        connectors:
          - id: c1
            source: {kind: http_csv, url: x}
            sink: {kind: crm, object: leads}
        crm:
          adapter: sqlite
          sqlite: {path: /data/crm.db}
          query_templates:
            - {name: new_leads_count, description: d, params: {}, sql: "SELECT 1"}
        """,
    )
    p = load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)
    assert [x["slug"] for x in p.projects] == ["p1"]
    assert [x["id"] for x in p.schedules] == ["s1"]
    assert p.connector("c1")["id"] == "c1"
    assert p.connector("nope") is None
    assert p.crm["adapter"] == "sqlite"
    assert [t["name"] for t in p.query_templates()] == ["new_leads_count"]


def test_module_exposes_known_kind_sets():
    # Guards against accidental renames the validator depends on.
    assert "http_csv" in profile_mod.KNOWN_SOURCE_KINDS
    assert "crm" in profile_mod.KNOWN_SINK_KINDS
    assert "sqlite" in profile_mod.KNOWN_ADAPTERS


# ─── marketing seed equivalence (regression lock, TR-003) ─────────
#
# The marketing profile must reproduce the original hardcoded
# _DEFAULT_PROJECTS / _DEFAULT_SCHEDULES exactly. Compared against a
# golden snapshot (not the constants) so this lock survives the deletion
# of those constants in T05.

import json  # noqa: E402
import pathlib  # noqa: E402

_GOLDEN = json.loads(
    (pathlib.Path(__file__).parent / "golden_marketing_seeds.json").read_text()
)


def _real_profiles_dir():
    return pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "profiles"


def test_marketing_projects_match_golden():
    p = load_profile("marketing", profiles_dir=_real_profiles_dir())
    assert p.projects == _GOLDEN["projects"]


def test_marketing_schedules_match_golden():
    p = load_profile("marketing", profiles_dir=_real_profiles_dir())
    assert p.schedules == _GOLDEN["schedules"]
