"""/homelab/* and /healthz/homelab, plus the homelab generators."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import auth
import generators
import llm_proxy
import telegram
import tenant_profile
from app import create_app, init_profile
from homelab import service, sources
from homelab.models import Census, Device, Observation
from tenant_profile import Profile

TOKEN = "homelab-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
HA_TOKEN = "ha-secret-token"

BLOCK = {
    "home_assistant": {"url": "http://192.0.2.5:8123", "token": HA_TOKEN},
    "census": {"url": "http://192.0.2.7"},
}
CENSUS = Census(
    when=NOW,
    devices=(Device("00:00:5E:00:53:01", "192.0.2.10", "printer", "br", "ok", True),),
)


@pytest.fixture()
def api(brain_db, monkeypatch, tmp_path):
    monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
    service.reset_actions()
    app = create_app(brain_db=brain_db)
    # the real bearer check, not conftest's bypass
    init_profile(
        app,
        brain_db,
        profile=Profile(name="t", raw={"homelab": BLOCK}),
        soul_path=str(tmp_path / "soul.md"),
    )
    yield TestClient(app)
    service.reset_actions()


@pytest.fixture()
def failing_ha(monkeypatch):
    async def fake(cfg, now, **kw):
        return Observation(
            now=NOW,
            configured=frozenset({"ha", "census"}),
            census=CENSUS,
            errors={"ha": "HTTP 401"},
        )

    monkeypatch.setattr(sources, "observe", fake)


def test_routes_absent_without_homelab_block(brain_db, tmp_path):
    app = create_app(brain_db=brain_db)
    init_profile(
        app,
        brain_db,
        profile=Profile(name="t", raw={}),
        soul_path=str(tmp_path / "soul.md"),
    )
    client = TestClient(app)
    assert client.get("/healthz/homelab").status_code == 404
    assert client.get("/homelab/status", headers=AUTH).status_code == 404


def test_healthz_is_public_route():
    assert auth.is_public("GET", "/healthz/homelab")
    assert not auth.is_public("POST", "/healthz/homelab")
    assert not auth.is_public("GET", "/homelab/status")


def test_status_and_watch_require_bearer(api):
    assert api.get("/homelab/status").status_code == 401
    assert api.post("/homelab/watch").status_code == 401
    assert (
        api.get("/homelab/status", headers={"Authorization": "Bearer x"}).status_code
        == 401
    )


def test_status_summarizes_without_hostnames(api, failing_ha):
    body = api.get("/homelab/status", headers=AUTH).json()
    assert body["sources"]["ha"] == {
        "configured": True,
        "ok": False,
        "error": "HTTP 401",
    }
    assert body["sources"]["census"] == {"configured": True, "ok": True}
    assert body["sources"]["kuma"] == {"configured": False, "ok": None}
    assert body["counts"] == {"devices": 1, "randomized_devices": 0}
    assert "printer" not in json.dumps(body) and "192.0.2" not in json.dumps(body)


def test_watch_runs_now(api, failing_ha, monkeypatch):
    sent = []

    async def sink(text):
        sent.append(text)
        return True

    monkeypatch.setattr(telegram, "notify", sink)
    out = api.post("/homelab/watch", headers=AUTH).json()
    assert out["status"] == "ok" and out["errors"] == {"ha": "HTTP 401"}
    assert api.get("/healthz/homelab").json()["last_run"] is not None


def test_healthz_anonymous_has_no_error_text_or_urls(api, failing_ha):
    api.post("/homelab/watch", headers=AUTH)
    resp = api.get("/healthz/homelab")
    assert resp.status_code == 200
    body = resp.json()
    blob = json.dumps(body)
    assert body["status"] == "degraded"
    # Structural, not substring: the 401 reason must not reach an anonymous
    # caller, but a bare "401" can appear in a timestamp.
    for name, entry in body["sources"].items():
        assert "error" not in entry, name
        assert all(not isinstance(v, str) or "://" not in v for v in entry.values())
    assert "192.0.2" not in blob and HA_TOKEN not in blob and "http" not in blob


def test_healthz_contract_for_the_dashboard(api, failing_ha):
    api.post("/homelab/watch", headers=AUTH)
    sources_ = api.get("/healthz/homelab").json()["sources"]
    assert sources_["kuma"]["configured"] is False and sources_["kuma"]["ok"] is None
    assert sources_["ha"]["configured"] is True and sources_["ha"]["ok"] is False
    assert sources_["census"]["configured"] is True and sources_["census"]["ok"] is True
    assert set(sources_["ha"]) == {"configured", "ok", "last_success", "failures"}


def test_healthz_bearer_gets_error_text(api, failing_ha):
    api.post("/homelab/watch", headers=AUTH)
    body = api.get("/healthz/homelab", headers=AUTH).json()
    assert body["sources"]["ha"]["error"] == "HTTP 401"
    blob = json.dumps(body)
    assert "192.0.2" not in blob and HA_TOKEN not in blob


def test_healthz_wrong_bearer_is_anonymous(api, failing_ha):
    api.post("/homelab/watch", headers=AUTH)
    body = api.get("/healthz/homelab", headers={"Authorization": "Bearer nope"}).json()
    assert "error" not in body["sources"]["ha"]


def test_healthz_disabled_when_env_unset(brain_db, tmp_path):
    app = create_app(brain_db=brain_db)
    init_profile(
        app,
        brain_db,
        profile=Profile(
            name="t",
            raw={"homelab": {"home_assistant": {"url": "", "token": ""}}},
        ),
        soul_path=str(tmp_path / "soul.md"),
    )
    body = TestClient(app).get("/healthz/homelab").json()
    assert body["status"] == "disabled"
    assert all(
        s["configured"] is False and s["ok"] is None for s in body["sources"].values()
    )


# ─── generators ─────────────────────────────────────────────────────


def test_generators_registered():
    assert generators.GENERATORS["homelab_watch"] is generators.homelab_watch_generator
    assert (
        generators.GENERATORS["homelab_report"] is generators.homelab_report_generator
    )


@pytest.fixture()
def gen_rig(brain_db, monkeypatch):
    sent = []

    async def sink(text):
        sent.append(text)
        return True

    async def sink_long(text):
        sent.append(text)
        return (1, 1)

    monkeypatch.setattr(telegram, "notify", sink)
    monkeypatch.setattr(telegram, "notify_long", sink_long)
    observed = []

    async def fake_observe(cfg, now, **kw):
        observed.append(now)
        return Observation(now=NOW, configured=frozenset({"census"}), census=CENSUS)

    monkeypatch.setattr(sources, "observe", fake_observe)

    def use(block):
        monkeypatch.setattr(
            tenant_profile,
            "load_profile",
            lambda name=None, **kw: Profile(
                name="t", raw={"homelab": block} if block else {}
            ),
        )

    return use, sent, observed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["homelab_watch", "homelab_report"])
@pytest.mark.parametrize(
    "block", [None, {"census": {"url": ""}, "home_assistant": {"url": "", "token": ""}}]
)
async def test_generators_are_silent_noops_unconfigured(brain_db, gen_rig, kind, block):
    use, sent, observed = gen_rig
    use(block)
    await generators.GENERATORS[kind](brain_db)
    assert sent == [] and observed == []


@pytest.mark.asyncio
async def test_watch_generator_runs_and_stays_quiet_on_baseline(brain_db, gen_rig):
    use, sent, observed = gen_rig
    use({"census": {"url": "http://192.0.2.7"}})
    await generators.homelab_watch_generator(brain_db)
    assert len(observed) == 1 and sent == []
    assert brain_db.homelab_get("watch") is not None


@pytest.mark.asyncio
async def test_report_generator_sends_a_digest(brain_db, gen_rig):
    use, sent, observed = gen_rig
    use({"census": {"url": "http://192.0.2.7"}})
    await generators.homelab_report_generator(brain_db)
    assert len(sent) == 1 and sent[0].startswith("Homelab daily report")


@pytest.fixture()
def recorded(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        generators,
        "_record_status",
        lambda db, sid, status, when=None: seen.update({sid: status}),
    )
    return seen


def _failing(monkeypatch, sources_failed):
    async def fake(cfg, now, **kw):
        return Observation(
            now=NOW,
            configured=frozenset({"ha", "census"}),
            census=None if "census" in sources_failed else CENSUS,
            errors={s: "HTTP 401" for s in sources_failed},
        )

    monkeypatch.setattr(sources, "observe", fake)


TWO = {
    "home_assistant": {"url": "http://192.0.2.5:8123", "token": HA_TOKEN},
    "census": {"url": "http://192.0.2.7"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["homelab_watch", "homelab_report"])
async def test_schedule_status_is_error_when_every_source_failed(
    brain_db, gen_rig, monkeypatch, recorded, kind
):
    from scheduler import status_kind

    use, _sent, _obs = gen_rig
    use(TWO)
    _failing(monkeypatch, {"ha", "census"})
    await generators.GENERATORS[kind](brain_db, schedule_id=kind)
    text = recorded[kind]
    assert status_kind(text) == "error"
    assert "census" in text and "ha" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["homelab_watch", "homelab_report"])
async def test_schedule_status_names_a_partial_outage(
    brain_db, gen_rig, monkeypatch, recorded, kind
):
    from scheduler import status_kind

    use, _sent, _obs = gen_rig
    use(TWO)
    _failing(monkeypatch, {"ha"})
    await generators.GENERATORS[kind](brain_db, schedule_id=kind)
    text = recorded[kind]
    assert status_kind(text) == "ok" and "sources failed: ha" in text


def test_half_configured_source_is_warned_once_at_mount(brain_db, caplog, tmp_path):
    app = create_app(brain_db=brain_db)
    block = {"home_assistant": {"url": "http://192.0.2.5:8123", "token": ""}}
    with caplog.at_level("WARNING", logger="clawrange.homelab"):
        init_profile(
            app,
            brain_db,
            profile=Profile(name="t", raw={"homelab": block}),
            soul_path=str(tmp_path / "soul.md"),
        )
    msgs = [
        r.getMessage() for r in caplog.records if "partly configured" in r.getMessage()
    ]
    assert len(msgs) == 1 and "ha" in msgs[0]
    assert "192.0.2" not in caplog.text
