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


def test_export_overlay_matches_padded_overlay_entries(db):
    overlay = [{"kind": "persona", "target": "Tone", "content": "  Be terse.  "}]
    pl.seed_overlay(db, "cos", overlay)
    assert len(pl.export_overlay(db, "cos", existing=overlay)) == 1  # no dup
    (row,) = db.list_learnings(profile="cos")
    db.set_learning_status(row["id"], "rejected")
    assert pl.export_overlay(db, "cos", existing=overlay) == []


# ─── #51: /healthz/persona reports writability ─────────────────────


def test_persona_health_reports_writability(db, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a dir")
    targets = {
        "soul": str(tmp_path / "soul.md"),
        "workspace_soul": [str(blocker / "SOUL.md")],
    }
    client = _persona_client(db, lambda p: targets)
    r = client.get("/healthz/persona")
    body = r.json()
    assert body["writable"] == {"soul": True, "ws_soul": False}
    assert sorted(body["targets"]) == ["soul", "workspace_soul"]
    assert str(tmp_path) not in r.text  # public route: no paths


# ─── #15: scan_signals ignores its own signal rows ─────────────────


def test_scan_signals_does_not_repropose(db, monkeypatch):
    monkeypatch.setenv("PERSONA_SIGNAL_LEARNING", "1")
    for i in range(3):
        pl.propose(db, "cos", "persona", "Tone", f"feedback {i}", "feedback")
    assert len(pl.scan_signals(db, "cos")) == 1
    assert pl.scan_signals(db, "cos") == []
    pending = db.list_learnings(profile="cos", status="pending")
    assert sum(r["source"] == "signal" for r in pending) == 1


# ─── #40: profile validation ───────────────────────────────────────

KINDS = {"pipeline", "crm_digest", "persona_reflect"}


def _validate(raw: dict, kinds=KINDS):
    from tenant_profile import validate

    raw.setdefault("profile", "acme")
    validate(Profile("acme", raw), known_generator_kinds=kinds)


def _deals(**over):
    target = {"name": "R740", "query": "r740", "max_price_great": 500}
    target.update(over.pop("target", {}))
    return {"homelab_deals": {"targets": [target], **over}}


@pytest.mark.parametrize(
    "raw",
    [
        _deals(exclude="caddy"),
        _deals(target={"exclude": "caddy"}),
        _deals(target={"exclude": [1, 2]}),
    ],
)
def test_validate_rejects_non_list_exclude(raw):
    from tenant_profile import ProfileError

    with pytest.raises(ProfileError, match="exclude"):
        _validate(raw)


def test_load_targets_rejects_string_exclude():
    from deals import load_targets

    with pytest.raises(ValueError, match="exclude"):
        load_targets(_deals(exclude="caddy")["homelab_deals"])


def _connector(**over):
    c = {
        "id": "portal",
        "source": {"kind": "http_csv", "url": "https://x", "auth": {"kind": "none"}},
        "sink": {"kind": "crm", "object": "leads", "upsert_key": "email"},
    }
    for k, v in over.items():
        c[k] = {**c[k], **v}
    return c


@pytest.mark.parametrize(
    "sink, match",
    [
        ({"upsert_key": "phone"}, "upsert_key"),
        ({"object": "contacts"}, "object"),
    ],
)
def test_validate_sqlite_sink_fields(sink, match):
    from tenant_profile import ProfileError

    with pytest.raises(ProfileError, match=match):
        _validate({"connectors": [_connector(sink=sink)], "crm": {"adapter": "sqlite"}})


def test_validate_rest_sink_fields_not_sqlite_constrained():
    _validate(
        {
            "connectors": [_connector(sink={"upsert_key": "phone"})],
            "crm": {"adapter": "rest"},
        }
    )


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "login_scrape", "auth": {"kind": "bearer", "token": "t"}},
        {"kind": "login_scrape", "auth": {"kind": "login_form"}},
        {"kind": "http_csv", "url": "u", "auth": {"kind": "login_form"}},
    ],
)
def test_validate_auth_kind_matches_source(source):
    from tenant_profile import ProfileError

    c = _connector()
    c["source"] = source
    with pytest.raises(ProfileError, match="auth"):
        _validate({"connectors": [c]})


def test_validate_login_scrape_with_form_ok():
    c = _connector()
    c["source"] = {
        "kind": "login_scrape",
        # ${VAR} tokens resolve to "" before validation: structure, not values.
        "auth": {"kind": "login_form", "login_form": {"login_url": ""}},
    }
    _validate({"connectors": [c]})


@pytest.mark.parametrize(
    "assistant",
    [
        {"name": None},
        {"name": "  "},
        {"name": "Max", "identity": {"name": None}},
        {"name": "Max", "identity": {"name": ""}},
        {"name": "Max", "identity": {"avatar": None}},
        {"name": "Max", "identity": {"vibe": 3}},
    ],
)
def test_validate_rejects_blank_or_null_names(assistant):
    from tenant_profile import ProfileError

    with pytest.raises(ProfileError, match="assistant"):
        _validate({"assistant": assistant})


def test_validate_allows_empty_avatar():
    _validate({"assistant": {"name": "Max", "identity": {"avatar": ""}}})


def test_validate_rejects_bad_cron_with_live_registry():
    from tenant_profile import ProfileError

    raw = {
        "seeds": {
            "schedules": [
                {"id": "s", "kind": "crm_digest", "cron": "61 * * * *", "kwargs": {}}
            ]
        }
    }
    with pytest.raises(ProfileError, match="cron"):
        _validate(raw, kinds=None)


def test_validate_rejects_foreign_profile_name():
    """A copied profile still naming its source would file proposals under
    the wrong profile."""
    from tenant_profile import ProfileError

    raw = {
        "seeds": {
            "schedules": [
                {
                    "id": "r",
                    "kind": "persona_reflect",
                    "cron": "0 8 * * *",
                    "kwargs": {"profile_name": "chief-of-staff"},
                }
            ]
        }
    }
    with pytest.raises(ProfileError, match="profile_name"):
        _validate(raw)


def test_copied_chief_of_staff_reflects_under_its_own_name(tmp_path):
    import pathlib
    import shutil

    from tenant_profile import load_profile

    src = pathlib.Path(__file__).resolve().parents[2] / "config" / "profiles"
    shutil.copytree(src / "chief-of-staff", tmp_path / "myassistant")
    yml = tmp_path / "myassistant" / "profile.yaml"
    yml.write_text(
        yml.read_text().replace("profile: chief-of-staff", "profile: myassistant")
    )
    p = load_profile("myassistant", profiles_dir=tmp_path)
    (reflect,) = [s for s in p.schedules if s["kind"] == "persona_reflect"]
    assert reflect["kwargs"]["profile_name"] == "myassistant"


def _crm(template):
    return {"crm": {"adapter": "sqlite", "query_templates": [template]}}


@pytest.mark.parametrize(
    "template, match",
    [
        ({"name": "t", "params": {"x": {"type": "int"}}, "sql": "SELECT 1"}, "type"),
        (
            {
                "name": "t",
                "params": {
                    "b": {
                        "type": "enum",
                        "values": ["day", "week"],
                        "map": {"day": "%d"},
                    }
                },
                "sql": "SELECT 1",
            },
            "map",
        ),
        (
            {
                "name": "t",
                "params": {"q": {"type": "string"}},
                "sql": "SELECT * FROM leads WHERE name LIKE :like",
            },
            "ESCAPE",
        ),
    ],
)
def test_validate_crm_templates(template, match):
    from tenant_profile import ProfileError

    with pytest.raises(ProfileError, match=match):
        _validate(_crm(template))


# ─── #46 / #15: deal targets ───────────────────────────────────────


def test_load_targets_red_flags_not_shared():
    from deals import load_targets

    block = {
        "red_flags": {"barebone": ["no cpu"]},
        "targets": [
            {"name": "A", "query": "a", "max_price_great": 100},
            {"name": "B", "query": "b", "max_price_great": 100},
        ],
    }
    a, b = load_targets(block)
    a.red_flags["barebone"].append("mutated")
    assert a.red_flags is not b.red_flags
    assert b.red_flags["barebone"] == ["no cpu"]


def test_load_targets_part_and_memory_are_plain_bool():
    from deals import load_targets

    (gpu,) = load_targets(
        {
            "targets": [
                {"name": "G", "query": "g", "max_price_great": 1, "category": "gpu"}
            ]
        }
    )
    assert gpu.part is True and gpu.check_memory is True
    (srv,) = load_targets(
        {"targets": [{"name": "S", "query": "s", "max_price_great": 1}]}
    )
    assert srv.part is False and srv.check_memory is False


def test_watch_target_rejects_non_positive_great_price():
    from deals import WatchTarget

    with pytest.raises(ValueError, match="great_price"):
        WatchTarget(name="x", query="x", category="gpu", great_price=0)


def test_unknown_shipping_is_not_insane():
    """Unknown eBay shipping counted as $0 made a listing look 'insane';
    without a known shipping cost it can be great, not insane. Feed posts
    state one price and never itemize shipping, so they are unaffected."""
    from deal_sources import _ebay_listing
    from deals import Listing, WatchTarget, classify

    t = WatchTarget(name="R740", query="r740", category="server", great_price=500)
    unknown = _ebay_listing({"itemId": "1", "title": "R740", "price": {"value": "300"}})
    assert unknown is not None and unknown.shipping is None
    deal = classify(unknown, t, None)
    assert deal is not None and deal.tier == "great"
    known = _ebay_listing(
        {
            "itemId": "2",
            "title": "R740",
            "price": {"value": "300"},
            "shippingOptions": [{"shippingCost": {"value": "0"}}],
        }
    )
    assert known is not None
    assert classify(known, t, None).tier == "insane"
    feed = Listing("r/homelabsales", "3", "[H] R740 $300", "u", price=300.0)
    assert classify(feed, t, None).tier == "insane"


@pytest.mark.parametrize(
    "code",
    [
        # The make persona path: tenant_profile first, nothing else imported.
        "import tenant_profile; tenant_profile.load_profile('lead-crm')",
        # The service path: active_profile() swallows errors and caches None.
        "import app, tenant_profile; assert tenant_profile.active_profile()",
    ],
)
def test_validate_schedule_import_is_cycle_free(tmp_path, code):
    """validate() lazily imports scheduler; a cycle there would surface only
    in a fresh interpreter, where nothing is imported yet."""
    import pathlib
    import subprocess
    import sys

    workflows = pathlib.Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "CLAWRANGE_PROFILE": "lead-crm",
        "BRAIN_DB_PATH": str(tmp_path / "brain.db"),
        "HEARTBEAT_SEEN_PATH": str(tmp_path / "seen.json"),
        "PYTHONPATH": str(workflows),
    }
    env.pop("CLAWRANGE_PROFILES_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_shipped_profiles_validate_with_live_registry():
    from tenant_profile import load_profile

    for name in ("lead-crm", "chief-of-staff", "marketing", "starter"):
        load_profile(name)


# ─── #47 / #15 / #51: CRM and connectors ───────────────────────────

LEAD_CSV = "Email Address,Full Name\nada@x.com,Ada\n"


def _crm_profile(source=None, templates=None):
    return Profile(
        "t",
        {
            "profile": "t",
            "connectors": [
                {
                    "id": "portal",
                    "source": source
                    or {"kind": "http_csv", "url": "https://p/e", "auth": {}},
                    "transform": {
                        "kind": "leads_clean",
                        "mapping": {"Email Address": "email", "Full Name": "name"},
                        "required": ["email"],
                        "dedup_key": "email",
                    },
                    "sink": {"kind": "crm", "object": "leads", "upsert_key": "email"},
                }
            ],
            "crm": {
                "adapter": "sqlite",
                "query_templates": templates
                or [
                    {
                        "name": "recent",
                        "params": {"window": {"type": "duration", "default": "7d"}},
                        "sql": "SELECT COUNT(*) AS n FROM leads "
                        "WHERE created_at >= :since",
                    }
                ],
            },
        },
    )


def _crm_client(tmp_path, *, profile=None, crm=None, handler=None, llm=None):
    import httpx

    from crm.sqlite_adapter import SQLiteCRM
    from crm_api import create_crm_router

    if crm is None:
        crm = SQLiteCRM(str(tmp_path / "crm.db"))
        crm.init()
    http = httpx.Client(
        transport=httpx.MockTransport(
            handler or (lambda req: httpx.Response(200, text=LEAD_CSV))
        )
    )
    api = FastAPI()
    api.include_router(
        create_crm_router(profile or _crm_profile(), crm, llm=llm, http_client=http)
    )
    return TestClient(api), crm


def test_leads_limit_must_be_positive(tmp_path):
    client, _ = _crm_client(tmp_path)
    assert client.get("/crm/leads", params={"limit": -1}).status_code == 422
    assert client.get("/crm/leads", params={"limit": 0}).status_code == 422


def test_sync_keeps_worked_lead_status(tmp_path):
    """Every hourly sync reset each lead's status to 'new'."""
    client, crm = _crm_client(tmp_path)
    assert client.post("/crm/sync/portal").status_code == 200
    lead = crm.list("leads")[0]
    crm.upsert("leads", [{"email": lead["email"], "status": "contacted"}], "email")
    assert client.post("/crm/sync/portal").status_code == 200
    (lead,) = crm.list("leads")
    assert lead["status"] == "contacted"


def test_new_lead_gets_schema_default_status(tmp_path):
    _, crm = _crm_client(tmp_path)
    crm.upsert("leads", [{"email": "n@x.com"}], "email")
    assert crm.list("leads")[0]["status"] == "new"


def test_huge_duration_is_400(tmp_path):
    client, _ = _crm_client(tmp_path)
    r = client.post(
        "/crm/query/run",
        json={"template": "recent", "params": {"window": "99999999999999d"}},
    )
    assert r.status_code == 400


def test_coerce_huge_duration_is_value_error():
    from crm.query import coerce_params

    with pytest.raises(ValueError):
        coerce_params({"params": {"w": {"type": "duration"}}}, {"w": "99999999999999d"})


def test_healthz_crm_hides_path(tmp_path):
    client, crm = _crm_client(tmp_path)
    r = client.get("/healthz/crm")
    assert r.status_code == 200
    assert str(tmp_path) not in r.text
    assert r.json()["adapter"]["status"] == "ok"


def test_healthz_crm_hides_rest_base_url(tmp_path):
    from crm.rest_adapter import RestCRM

    rest = RestCRM({"base_url": "https://secret-crm.internal/api"})
    client, _ = _crm_client(tmp_path, crm=rest)
    r = client.get("/healthz/crm")
    assert "secret-crm" not in r.text
    assert r.json()["adapter"]["status"] == "configured"


def test_healthz_crm_reports_adapter_error(tmp_path):
    from crm.sqlite_adapter import SQLiteCRM

    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    broken = SQLiteCRM(str(blocker / "crm.db"))  # parent is a file
    client, _ = _crm_client(tmp_path, crm=broken)
    r = client.get("/healthz/crm")
    assert r.status_code == 503
    assert r.json()["adapter"]["status"] == "error"
    assert str(tmp_path) not in r.text


def test_leads_clean_keeps_rows_missing_dedup_key():
    from connectors.transforms import leads_clean

    rows = [{"name": "A"}, {"name": "B"}]
    out = leads_clean(rows, {"dedup_key": "email"})
    assert [r["name"] for r in out] == ["A", "B"]


def test_leads_clean_email_dedup_ignores_case():
    from connectors.transforms import leads_clean

    rows = [{"email": "Ada@X.com", "name": "A"}, {"email": "ada@x.com", "name": "B"}]
    out = leads_clean(rows, {"dedup_key": "email"})
    assert len(out) == 1 and out[0]["name"] == "B"
    # The spelling is kept: rewriting it would stop matching a lead already
    # stored under the CRM's case-sensitive UNIQUE(email) and insert a copy.
    assert out[0]["email"] == "ada@x.com"
    assert leads_clean(rows[:1], {})[0]["email"] == "Ada@X.com"
    assert "status" not in out[0]  # schema default covers inserts only


def test_http_csv_rejects_html():
    import httpx

    from connectors.sources import http_csv

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, text="<html>login</html>", headers={"content-type": "text/html"}
            )
        )
    )
    with pytest.raises(ValueError, match="CSV"):
        http_csv({"url": "https://p/e"}, client=client)


def test_http_csv_unset_url_is_not_ok(tmp_path):
    """An unset ${PORTAL_EXPORT_URL} recorded 'ok (0 written)'."""
    from connectors.base import ConnectorNotConfigured
    from connectors.sources import http_csv

    with pytest.raises(ConnectorNotConfigured):
        http_csv({"url": ""})
    client, _ = _crm_client(
        tmp_path, profile=_crm_profile(source={"kind": "http_csv", "url": ""})
    )
    r = client.post("/crm/sync/portal")
    assert r.status_code == 409 and "not configured" in r.json()["detail"]


def test_login_scrape_does_not_resend_password_on_307():
    import httpx

    from connectors.sources import login_scrape

    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(str(request.url))
            return httpx.Response(307, headers={"location": "https://evil/login"})
        return httpx.Response(200, text=LEAD_CSV)

    spec = {
        "auth": {
            "kind": "login_form",
            "login_form": {
                "login_url": "https://portal/login",
                "password": "pw",
                "export_path": "/export",
            },
        }
    }
    with pytest.raises(ValueError, match="redirect"):
        login_scrape(spec, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert posts == ["https://portal/login"]


def test_login_scrape_accepts_302_without_following():
    import httpx

    from connectors.sources import login_scrape

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(302, headers={"location": "/dashboard"})
        return httpx.Response(200, text=LEAD_CSV)

    spec = {
        "auth": {
            "kind": "login_form",
            "login_form": {"login_url": "https://portal/login", "export_path": "/e"},
        }
    }
    rows = login_scrape(
        spec, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert rows == [{"Email Address": "ada@x.com", "Full Name": "Ada"}]
    assert ("POST", "/login") in seen and seen[-1] == ("GET", "/e")


def _rest(handler, objects):
    import httpx

    from crm.rest_adapter import RestCRM

    client = httpx.Client(
        base_url="https://crm.example", transport=httpx.MockTransport(handler)
    )
    return RestCRM({"base_url": "https://crm.example", "objects": objects}, client)


def test_rest_upsert_skips_existing_record():
    import httpx

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "GET":
            email = request.url.params.get("email")
            rows = [{"id": 7, "email": "a@x.com"}] if email == "a@x.com" else []
            return httpx.Response(200, json=rows)
        return httpx.Response(201, json={"id": 8})

    crm = _rest(handler, {"leads": {"create": "POST /leads", "list": "GET /leads"}})
    n = crm.upsert("leads", [{"email": "a@x.com"}, {"email": "b@x.com"}], "email")
    assert n == 1
    assert [c[0] for c in calls if c[0] == "POST"] == ["POST"]


def test_rest_upsert_updates_existing_when_configured():
    import httpx

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200, json={"results": [{"id": 7, "email": "a@x.com"}]}
            )
        return httpx.Response(200, json={})

    crm = _rest(
        handler,
        {
            "leads": {
                "create": "POST /leads",
                "list": "GET /leads",
                "update": "PATCH /leads/{id}",
            }
        },
    )
    assert crm.upsert("leads", [{"email": "a@x.com", "name": "A"}], "email") == 1
    assert ("PATCH", "/leads/7") in calls
    assert not any(m == "POST" for m, _ in calls)


def test_rest_upsert_needs_list_endpoint():
    import httpx

    crm = _rest(lambda r: httpx.Response(201), {"leads": {"create": "POST /leads"}})
    with pytest.raises(ValueError, match="list"):
        crm.upsert("leads", [{"email": "a@x.com"}], "email")


@pytest.mark.parametrize("reply", ["", None])
def test_nl_query_reports_llm_outage(tmp_path, reply):
    from errors import UpstreamError

    async def llm(prompt):
        if reply is None:
            raise UpstreamError("all tiers failed")
        return reply

    client, _ = _crm_client(tmp_path, llm=llm)
    body = client.post("/crm/query", json={"prompt": "how many?"}).json()
    assert body["template"] is None
    assert "unavailable" in body["answer"]
    assert "couldn't map" not in body["answer"]


def test_nl_query_unparseable_reply_still_maps_message(tmp_path):
    async def llm(prompt):
        return "no idea"

    client, _ = _crm_client(tmp_path, llm=llm)
    body = client.post("/crm/query", json={"prompt": "how many?"}).json()
    assert "couldn't map" in body["answer"]


@pytest.mark.asyncio
async def test_pipeline_unset_url_records_error(tmp_path):
    from crm.sqlite_adapter import SQLiteCRM
    from generators import pipeline_generator

    statuses = []

    class Brain:
        def update_schedule_status(self, sid, ts, status):
            statuses.append(status)

    crm = SQLiteCRM(str(tmp_path / "crm.db"))
    crm.init()
    await pipeline_generator(
        Brain(),
        "portal",
        profile=_crm_profile(source={"kind": "http_csv", "url": ""}),
        crm=crm,
        schedule_id="lead-sync",
    )
    assert statuses and statuses[0].startswith("error")
    assert "not configured" in statuses[0] or "no url" in statuses[0]
