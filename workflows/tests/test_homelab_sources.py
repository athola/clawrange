"""Homelab sources: mocked transports, synthetic data only."""

import json
import ssl
from datetime import UTC, datetime

import httpx
import pytest

from errors import UpstreamError
from homelab import sources
from homelab.config import HomelabConfig

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TOKEN = "s3cr3t-token-value"

LEASES = (
    "Columns: ADDRESS, MAC-ADDRESS, HOST-NAME\r\n"
    " #    ADDRESS       MAC-ADDRESS        HOST-NAME     \r\n"
    " 0    192.0.2.10    00:00:5E:00:53:01  printer-1     \r\n"
)

CFG = HomelabConfig.from_block(
    {
        "home_assistant": {"url": "http://192.0.2.5:8123", "token": TOKEN},
        "kuma": {"url": "https://192.0.2.6", "slug": "home"},
        "census": {"url": "http://192.0.2.7"},
    }
)

STATES = [
    {
        "entity_id": "light.lamp",
        "state": "on",
        "attributes": {"friendly_name": "Lamp"},
        "last_changed": "2026-01-01T10:00:00+00:00",
    }
]
PAGE = {
    "publicGroupList": [{"monitorList": [{"id": 1, "name": "Router", "type": "ping"}]}]
}
BEATS = {"heartbeatList": {"1": [{"status": 1}]}, "uptimeList": {"1_24": 1.0}}
DEVICES = {"when": "2026-01-01T11:00:00+00:00", "raw": LEASES}


def transport(routes, seen=None):
    """routes: path -> (status, body) or a callable(request) -> Response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, text="nope")
        if callable(route):
            return route(request)
        status, body = route
        content = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(status, content=content)

    return httpx.MockTransport(handler)


# ─── Home Assistant ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ha_states_success_sends_bearer():
    seen = []
    t = transport({"/api/states": (200, STATES)}, seen)
    states = await sources.fetch_ha_states(CFG, transport=t)
    assert [s.entity_id for s in states] == ["light.lamp"]
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_ha_401_is_upstream_error_without_token_or_url():
    t = transport({"/api/states": (401, {"message": "401: Unauthorized"})})
    with pytest.raises(UpstreamError) as exc:
        await sources.fetch_ha_states(CFG, transport=t)
    text = str(exc.value)
    assert "401" in text
    assert TOKEN not in text and "192.0.2" not in text


@pytest.mark.asyncio
async def test_ha_timeout_is_upstream_error():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(UpstreamError, match="timed out") as exc:
        await sources.fetch_ha_states(CFG, transport=transport({"/api/states": boom}))
    assert "192.0.2" not in str(exc.value)


@pytest.mark.asyncio
async def test_ha_connect_error_hides_url():
    def boom(request):
        raise httpx.ConnectError("cannot reach http://192.0.2.5:8123", request=request)

    with pytest.raises(UpstreamError) as exc:
        await sources.fetch_ha_states(CFG, transport=transport({"/api/states": boom}))
    assert "192.0.2" not in str(exc.value)


@pytest.mark.asyncio
async def test_ha_bad_json_is_upstream_error():
    t = transport({"/api/states": (200, "<html>not json")})
    with pytest.raises(UpstreamError, match="JSON"):
        await sources.fetch_ha_states(CFG, transport=t)


@pytest.mark.asyncio
async def test_ha_wrong_shape_is_not_read_as_empty():
    t = transport({"/api/states": (200, {"message": "API running."})})
    with pytest.raises(UpstreamError, match="shape"):
        await sources.fetch_ha_states(CFG, transport=t)


@pytest.mark.asyncio
async def test_call_ha_service_posts_entity_id():
    seen = []
    t = transport({"/api/services/light/turn_on": (200, [])}, seen)
    await sources.call_ha_service(CFG, "light", "turn_on", "light.lamp", transport=t)
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {"entity_id": "light.lamp"}
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_call_ha_service_failure_raises_without_token():
    t = transport({"/api/services/light/turn_on": (500, "boom")})
    with pytest.raises(UpstreamError, match="500") as exc:
        await sources.call_ha_service(
            CFG, "light", "turn_on", "light.lamp", transport=t
        )
    assert TOKEN not in str(exc.value)


# ─── Kuma ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kuma_success():
    t = transport(
        {
            "/api/status-page/home": (200, PAGE),
            "/api/status-page/heartbeat/home": (200, BEATS),
        }
    )
    (mon,) = await sources.fetch_kuma(CFG, transport=t)
    assert (mon.name, mon.status) == ("Router", 1)


@pytest.mark.asyncio
async def test_kuma_http_error():
    t = transport(
        {
            "/api/status-page/home": (200, PAGE),
            "/api/status-page/heartbeat/home": (502, "bad gateway"),
        }
    )
    with pytest.raises(UpstreamError, match="502"):
        await sources.fetch_kuma(CFG, transport=t)


@pytest.mark.asyncio
async def test_kuma_page_without_groups_is_error():
    t = transport(
        {
            "/api/status-page/home": (200, {"config": {}}),
            "/api/status-page/heartbeat/home": (200, BEATS),
        }
    )
    with pytest.raises(UpstreamError, match="shape"):
        await sources.fetch_kuma(CFG, transport=t)


def test_kuma_ca_cert_builds_a_verifying_context(monkeypatch):
    calls = []
    real = ssl.create_default_context

    def fake(**kwargs):
        calls.append(kwargs)
        return real()

    monkeypatch.setattr(sources.ssl, "create_default_context", fake)
    cfg = HomelabConfig.from_block(
        {"kuma": {"url": "https://192.0.2.6", "ca_cert": "/config/homelab/ca.pem"}}
    )
    ctx = sources._kuma_verify(cfg)
    assert calls == [{"cafile": "/config/homelab/ca.pem"}]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_kuma_without_ca_cert_uses_default_verification():
    assert sources._kuma_verify(CFG) is True


def test_kuma_unreadable_ca_cert_is_upstream_error(tmp_path):
    cfg = HomelabConfig.from_block(
        {"kuma": {"url": "https://192.0.2.6", "ca_cert": str(tmp_path / "missing.pem")}}
    )
    with pytest.raises(UpstreamError, match="CA certificate"):
        sources._kuma_verify(cfg)


# ─── Census ─────────────────────────────────────────────────────────

SCAN = {"when": "x", "target": "192.0.2.1", "open": ["192.0.2.1 -> [22,53,8291]"]}
ROUTER = {"when": "x", "raw": "version: 7.1.2 (stable)\r\nboard-name: hEX S\r\n"}
KEV = {
    "when": "x",
    "entries": [{"cveID": "CVE-2000-0001", "vendorProject": "MikroTik"}],
}


@pytest.mark.asyncio
async def test_census_full():
    t = transport(
        {
            "/devices.json": (200, DEVICES),
            "/rustscan-hex.json": (200, SCAN),
            "/router.json": (200, ROUTER),
            "/kev-mikrotik.json": (200, KEV),
        }
    )
    census = await sources.fetch_census(CFG, transport=t)
    assert census.when == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    assert [d.mac for d in census.devices] == ["00:00:5E:00:53:01"]
    assert census.mgmt_ports == (22, 53, 8291)
    assert census.router_version == "7.1.2 (stable)"
    assert [k.cve_id for k in census.kev or ()] == ["CVE-2000-0001"]


@pytest.mark.asyncio
async def test_census_optional_404_is_none():
    t = transport({"/devices.json": (200, DEVICES)})
    census = await sources.fetch_census(CFG, transport=t)
    assert census.mgmt_ports is None
    assert census.router_version is None
    assert census.kev is None


@pytest.mark.asyncio
async def test_census_optional_bad_json_is_none():
    t = transport(
        {
            "/devices.json": (200, DEVICES),
            "/rustscan-hex.json": (200, "{not json"),
            "/router.json": (200, "<html>"),
            "/kev-mikrotik.json": (200, "[]]"),
        }
    )
    census = await sources.fetch_census(CFG, transport=t)
    assert (census.mgmt_ports, census.router_version, census.kev) == (None, None, None)


@pytest.mark.asyncio
async def test_census_empty_kev_entries_is_empty_tuple_not_none():
    t = transport(
        {"/devices.json": (200, DEVICES), "/kev-mikrotik.json": (200, {"entries": []})}
    )
    assert (await sources.fetch_census(CFG, transport=t)).kev == ()


@pytest.mark.asyncio
async def test_census_devices_required():
    t = transport({"/rustscan-hex.json": (200, SCAN)})
    with pytest.raises(UpstreamError, match="404"):
        await sources.fetch_census(CFG, transport=t)


@pytest.mark.parametrize(
    "body",
    [{"raw": LEASES}, {"when": "yesterday", "raw": LEASES}, {"when": DEVICES["when"]}],
)
@pytest.mark.asyncio
async def test_census_devices_wrong_shape(body):
    t = transport({"/devices.json": (200, body)})
    with pytest.raises(UpstreamError, match="shape"):
        await sources.fetch_census(CFG, transport=t)


# ─── observe ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observe_unconfigured_fetches_nothing():
    seen = []
    cfg = HomelabConfig.from_block(None)
    obs = await sources.observe(cfg, NOW, transport=transport({}, seen))
    assert seen == []
    assert obs.configured == frozenset()
    assert obs.errors == {}
    assert (obs.ha, obs.kuma, obs.census) == (None, None, None)


@pytest.mark.asyncio
async def test_observe_only_fetches_configured_sources():
    seen = []
    cfg = HomelabConfig.from_block({"census": {"url": "http://192.0.2.7"}})
    t = transport({"/devices.json": (200, DEVICES)}, seen)
    obs = await sources.observe(cfg, NOW, transport=t)
    assert obs.configured == frozenset({"census"})
    assert obs.census is not None
    assert {r.url.path for r in seen}.issubset(
        {"/devices.json", "/rustscan-hex.json", "/router.json", "/kev-mikrotik.json"}
    )


@pytest.mark.asyncio
async def test_observe_failure_is_recorded_per_source():
    t = transport(
        {
            "/api/states": (401, "no"),
            "/api/status-page/home": (200, PAGE),
            "/api/status-page/heartbeat/home": (200, BEATS),
            "/devices.json": (200, DEVICES),
        }
    )
    obs = await sources.observe(CFG, NOW, transport=t)
    assert obs.ha is None and "401" in obs.errors["ha"]
    assert obs.kuma is not None and obs.census is not None
    assert set(obs.errors) == {"ha"}
    assert TOKEN not in json.dumps(dict(obs.errors))


@pytest.mark.asyncio
async def test_observe_unexpected_exception_is_contained(monkeypatch):
    async def boom(cfg, **kw):
        raise RuntimeError("http://192.0.2.5 secret")

    monkeypatch.setattr(sources, "fetch_ha_states", boom)
    cfg = HomelabConfig.from_block(
        {"home_assistant": {"url": "http://192.0.2.5", "token": TOKEN}}
    )
    obs = await sources.observe(cfg, NOW)
    assert obs.ha is None
    assert obs.errors["ha"] == "unexpected error (RuntimeError)"
