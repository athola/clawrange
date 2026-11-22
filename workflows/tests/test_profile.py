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
            source:
              kind: http_csv
              url: "https://x"
              auth:
                kind: bearer
                token: "${PORTAL_TOKEN}"
            sink:
              kind: crm
              object: leads
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
            - id: s1
              name: S1
              kind: pipeline
              cron: "0 * * * *"
              kwargs:
                connector: ghost
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
            - slug: p1
              owner: o
              repo: r
              topics: []
              subreddits: []
              search_terms: []
              posture: ""
          schedules:
            - id: s1
              name: S1
              kind: crm_digest
              cron: "0 8 * * *"
              kwargs:
                queries:
                  - new_leads_count
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


def test_identity_block_validates_and_rejects_unknown_keys():
    from tenant_profile import Profile, ProfileError, validate

    ok = Profile(
        name="t",
        raw={
            "profile": "t",
            "assistant": {
                "name": "Max",
                "identity": {
                    "name": "Max",
                    "creature": "CoS",
                    "vibe": "sharp",
                    "emoji": "🎯",
                },
            },
        },
    )
    validate(ok)  # should not raise
    bad = Profile(
        name="t",
        raw={
            "profile": "t",
            "assistant": {
                "identity": {"name": "Max", "bogus": "x"},
            },
        },
    )
    try:
        validate(bad)
        assert False, "expected ProfileError"
    except ProfileError as e:
        assert "identity" in str(e)


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


def test_chief_of_staff_profile_loads_with_identity():
    from tenant_profile import load_profile

    p = load_profile("chief-of-staff", profiles_dir=_real_profiles_dir())
    assert p.assistant["identity"]["name"] == "Max"
    assert p.assistant["identity"]["emoji"] == "🎯"


# ─── homelab_deals block ─────────────────────────────────────────────


def _deals_profile(tmp_path, block: str):
    _write_profile(
        tmp_path,
        "d",
        "profile: d\nhomelab_deals:\n" + textwrap.indent(textwrap.dedent(block), "  "),
    )
    return lambda: load_profile(
        "d", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS
    )


def test_homelab_deals_valid_block_loads(tmp_path):
    load = _deals_profile(
        tmp_path,
        """
        sources: {slickdeals: {}, reddit: {subreddits: [homelabsales]}}
        targets:
          - {name: RTX 3090, query: rtx 3090, max_price_great: 750}
        """,
    )
    assert load().raw["homelab_deals"]["targets"][0]["name"] == "RTX 3090"


def test_homelab_deals_unknown_source_raises(tmp_path):
    load = _deals_profile(
        tmp_path,
        """
        sources: {craigslist: {}}
        targets:
          - {name: RTX 3090, query: rtx 3090, max_price_great: 750}
        """,
    )
    with pytest.raises(ProfileError, match="craigslist"):
        load()


def test_homelab_deals_bad_target_raises(tmp_path):
    load = _deals_profile(
        tmp_path,
        """
        sources: {slickdeals: {}}
        targets:
          - {name: RTX 3090, query: rtx 3090, max_price_great: cheap}
        """,
    )
    with pytest.raises(ProfileError, match="max_price_great"):
        load()


def test_marketing_profile_ships_homelab_deals():
    p = load_profile("marketing", profiles_dir=_real_profiles_dir())
    block = p.raw["homelab_deals"]
    assert len(block["targets"]) >= 20
    assert set(block["sources"]) <= profile_mod.KNOWN_DEAL_SOURCE_KINDS


# ─── catch_up flag, income_review block, name hygiene ─────────────


def test_catch_up_must_be_bool(tmp_path):
    _write_profile(
        tmp_path,
        "acme",
        """
        profile: acme
        seeds:
          schedules:
            - id: s1
              name: S1
              kind: morning_digest
              cron: "0 8 * * *"
              catch_up: yes_please
        """,
    )
    with pytest.raises(ProfileError, match="catch_up"):
        load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)


def test_marketing_flags_its_daily_reports_for_catch_up():
    import scheduler

    p = load_profile("marketing", profiles_dir=_real_profiles_dir())
    ids = {s["id"] for s in p.schedules if s.get("catch_up") is True}
    assert ids == {"morning_digest", "homelab_deals", "homelab_report"}
    assert scheduler.CATCH_UP_WINDOW  # window itself stays code-level


def test_income_review_block_must_be_mapping(tmp_path):
    _write_profile(tmp_path, "acme", "profile: acme\nincome_review: nope\n")
    with pytest.raises(ProfileError, match="income_review"):
        load_profile("acme", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)


@pytest.mark.parametrize("bad", ["../etc", "a/b", "A", "", "x.y", "..", "a b"])
def test_profile_name_is_validated_before_path_use(tmp_path, bad):
    with pytest.raises(ProfileError, match="invalid profile name"):
        load_profile(bad or "/", profiles_dir=tmp_path)


class TestResolveSubreddits:
    """One resolution rule for /scan reddit (API and chat command)."""

    class _DB:
        def __init__(self, projects):
            self.projects = projects

        def get_project(self, slug):
            return self.projects.get(slug)

    def test_explicit_wins(self, monkeypatch):
        monkeypatch.setattr(profile_mod, "default_subreddits", lambda: ["dflt"])
        db = self._DB({"p": {"subreddits": '["proj"]'}})
        assert profile_mod.resolve_subreddits(db, ["mine"], "p") == ["mine"]

    def test_project_then_default(self, monkeypatch):
        monkeypatch.setattr(profile_mod, "default_subreddits", lambda: ["dflt"])
        db = self._DB({"p": {"subreddits": '["proj"]'}, "empty": {"subreddits": "[]"}})
        assert profile_mod.resolve_subreddits(db, [], "p") == ["proj"]
        assert profile_mod.resolve_subreddits(db, [], "empty") == ["dflt"]
        assert profile_mod.resolve_subreddits(db, None, "missing") == ["dflt"]
        assert profile_mod.resolve_subreddits(db, None, None) == ["dflt"]


# ─── homelab block ───────────────────────────────────────────────────


def _homelab_profile(tmp_path, block: str):
    _write_profile(
        tmp_path,
        "h",
        "profile: h\nhomelab:\n" + textwrap.indent(textwrap.dedent(block), "  "),
    )
    return lambda: load_profile(
        "h", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS
    )


def test_homelab_absent_is_none(tmp_path):
    _write_profile(tmp_path, "h", "profile: h\n")
    p = load_profile("h", profiles_dir=tmp_path, known_generator_kinds=KNOWN_KINDS)
    assert p.homelab is None


def test_homelab_valid_block_loads_with_unset_env(tmp_path):
    load = _homelab_profile(
        tmp_path,
        """
        home_assistant:
          url: ${HOMELAB_TEST_UNSET_URL}
          token: ${HOMELAB_TEST_UNSET_TOKEN}
          battery_low: 20
          ignore_entities: [sensor.noisy_*]
          actions:
            - {service: light.turn_on, entities: [light.lamp, "light.*"]}
        kuma: {url: "${HOMELAB_TEST_UNSET_KUMA}", slug: home, ca_cert: ""}
        census: {url: "${HOMELAB_TEST_UNSET_CENSUS}", max_age_hours: 13}
        source_down_after: 3
        """,
    )
    assert load().homelab["home_assistant"]["url"] == ""


@pytest.mark.parametrize(
    "block, match",
    [
        ("bogus: 1", "bogus"),
        ("home_assistant: {nope: 1}", "nope"),
        ("kuma: {nope: 1}", "nope"),
        ("census: {nope: 1}", "nope"),
        ("home_assistant: [1]", "mapping"),
        ("source_down_after: 0", "positive"),
        ("source_down_after: true", "positive"),
        ("home_assistant: {battery_low: -1}", "positive"),
        ("home_assistant: {flood_threshold: 1.5}", "positive"),
        ("home_assistant: {unavailable_after_minutes: 0}", "positive"),
        ("census: {max_age_hours: 0}", "positive"),
        ("home_assistant: {ignore_entities: x}", "ignore_entities"),
        ("home_assistant: {ignore_entities: [1]}", "ignore_entities"),
        ("kuma: {slug: [a]}", "text"),
    ],
)
def test_homelab_bad_shape_raises(tmp_path, block, match):
    with pytest.raises(ProfileError, match=match):
        _homelab_profile(tmp_path, block)()


@pytest.mark.parametrize(
    "action",
    [
        "{service: lock.unlock, entities: [lock.door]}",
        "{service: light.turn_on, entities: [lock.door]}",
        "{service: script.turn_on, entities: ['script.*']}",
        "{service: light.turn_on, entities: []}",
        "{service: light.turn_on, entities: [light.a], extra: 1}",
        "{service: homeassistant.restart, entities: [light.a]}",
    ],
)
def test_homelab_hard_denied_or_bad_actions_rejected_at_validation(tmp_path, action):
    load = _homelab_profile(tmp_path, f"home_assistant:\n  actions:\n    - {action}\n")
    with pytest.raises(ProfileError, match="actions"):
        load()
