"""Deterministic renderers: `!home`, `!net` and the daily digest."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from homelab.models import (
    Census,
    Device,
    EntityState,
    KevEntry,
    Monitor,
    Observation,
    WatchConfig,
    WatchState,
)
from homelab.report import (
    render_entity_search,
    render_home,
    render_net,
    render_report,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
CFG = WatchConfig()
ALL = frozenset({"ha", "kuma", "census"})


def ent(eid, state="on", name=None, **kw):
    return EntityState(eid, state, name or eid, **kw)


HA = (
    ent("light.lamp", "on", "Lamp"),
    ent("sensor.hall_batt", "12", "Hall sensor", device_class="battery", unit="%"),
    ent("sensor.door_batt", "55", "Door sensor", device_class="battery", unit="%"),
    ent("sensor.gone", "unavailable", "Garage_*sensor"),
    ent("update.fw", "on", "Firmware"),
    ent("update.old", "off", "Old thing"),
)

KEV = (
    KevEntry("CVE-2000-0001", "2024-01-02", "RouterOS", "RouterOS test flaw", "x"),
    KevEntry("CVE-2000-0002", "2024-02-03", "Winbox", "Winbox test flaw", "y"),
)

DEVICES = (
    Device("00:00:5E:00:53:01", "192.0.2.10", "printer-1", "bridge", "reachable", True),
    Device("00:00:5E:00:53:02", "192.0.2.11", "nas-1", "bridge", "reachable", True),
    Device("02:AA:BB:CC:DD:01", "192.0.2.12", "", "bridge", "stale", True),
)


def census(**kw):
    base = {
        "when": NOW - timedelta(hours=2, minutes=30),
        "devices": DEVICES,
        "mgmt_ports": (22, 53, 8291),
        "router_version": "7.1.2 (stable)",
        "kev": KEV,
    }
    return Census(**{**base, **kw})


def full_obs(**kw):
    base = {
        "now": NOW,
        "configured": ALL,
        "ha": HA,
        "kuma": (
            Monitor("1", "Router", 1, 1.0),
            Monitor("2", "NAS", 0, 0.9),
            Monitor("3", "Cam", 2, 0.99),
        ),
        "census": census(),
        "errors": {},
    }
    return Observation(**{**base, **kw})


def state(**kw):
    base = {
        "learned": {"census": True},
        "census_baseline_at": (NOW - timedelta(days=2)).isoformat(),
        "known_macs": {
            "00:00:5E:00:53:01": (NOW - timedelta(days=2)).isoformat(),
            "00:00:5E:00:53:02": (NOW - timedelta(days=1)).isoformat(),
            "02:AA:BB:CC:DD:01": (NOW - timedelta(hours=3)).isoformat(),
        },
        "baseline_ports": [22, 53, 8291],
    }
    return WatchState(**{**base, **kw})


# ─── !home ──────────────────────────────────────────────────────────


def test_home_summary_counts_and_lists():
    text = render_home(full_obs(), CFG)
    assert "6 entities, 1 unavailable" in text
    assert "Garage\\_\\*sensor" in text
    assert "Hall sensor 12%" in text
    assert "Door sensor" not in text  # 55% is not low
    assert "Updates pending: 1" in text
    assert "Firmware" in text
    assert "Uptime Kuma: 1 of 3 up, 1 pending or in maintenance" in text
    assert "Down: NAS" in text


def test_home_respects_ignore_entities_and_battery_threshold():
    cfg = replace(CFG, ignore_entities=("sensor.gone", "update.*"), battery_low=10)
    text = render_home(full_obs(), cfg)
    assert "0 unavailable" in text
    assert "Low batteries" not in text
    assert "Updates pending" not in text


def test_home_unconfigured_source_is_one_not_configured_line():
    text = render_home(full_obs(configured=frozenset({"kuma"}), ha=None), CFG)
    assert "Home Assistant: not configured" in text
    assert "Uptime Kuma: 1 of 3 up" in text


def test_home_outage_is_not_shown_as_empty_data():
    text = render_home(full_obs(ha=None, kuma=None), CFG)
    assert "Home Assistant: unreachable" in text
    assert "Uptime Kuma: unreachable" in text
    assert "0 entities" not in text
    assert "0 of 0" not in text


def test_home_lists_are_capped():
    many = tuple(ent(f"sensor.s{i:02d}", "unavailable", f"S{i:02d}") for i in range(9))
    text = render_home(full_obs(ha=many), CFG)
    assert "9 entities, 9 unavailable" in text
    assert "and 4 more" in text


def test_home_hostile_names_are_neutralised():
    nasty = (ent("sensor.x", "unavailable", "a\x00_\n[b](c)`" + "z" * 100),)
    text = render_home(full_obs(ha=nasty), CFG)
    assert "\x00" not in text
    assert "z" * 50 not in text
    assert "\\_" in text
    assert "\\[b]" in text


# ─── entity search ──────────────────────────────────────────────────


def test_entity_search_matches_name_or_id_case_insensitively():
    text = render_entity_search(full_obs(), "HALL")
    assert text.splitlines()[0].startswith("1 match")
    assert "Hall sensor (sensor.hall\\_batt): 12 %" in text


def test_entity_search_caps_lines_and_says_how_many_more():
    many = tuple(ent(f"sensor.s{i:02d}", str(i)) for i in range(40))
    text = render_entity_search(full_obs(ha=many), "sensor", limit=15)
    lines = text.splitlines()
    assert lines[0].startswith("40 match")
    assert len(lines) == 1 + 15 + 1
    assert "25 more" in lines[-1]


def test_entity_search_no_match_unconfigured_and_outage():
    assert "No entity matches" in render_entity_search(full_obs(), "zzz")
    off = render_entity_search(full_obs(configured=frozenset(), ha=None), "x")
    assert "not configured" in off
    down = render_entity_search(full_obs(ha=None), "x")
    assert "unreachable" in down
    assert "No entity" not in down


def test_entity_search_escapes_query_echo():
    assert "tv\\_x" in render_entity_search(full_obs(), "tv_x")


# ─── !net ───────────────────────────────────────────────────────────


def test_net_summary():
    text = render_net(full_obs(), state())
    assert "Census: 2.5h old" in text
    assert "Devices: 3 (2 universal, 1 randomized)" in text
    assert "New since baseline: 2" in text
    assert "Management ports: 22, 53, 8291 (matches baseline)" in text
    assert "Router version: 7.1.2 (stable)" in text
    assert "MikroTik KEV: 2 entries" in text
    assert "CVE-2000-0001 (2024-01-02): RouterOS test flaw" in text
    assert "CVE-2000-0002 (2024-02-03)" in text
    assert "no affected versions" in text
    assert "vulnerable" not in text.lower()


def test_net_port_drift_and_missing_optional_files():
    drift = full_obs(census=census(mgmt_ports=(22, 80)))
    assert "baseline 22, 53, 8291" in render_net(drift, state())
    bare = full_obs(
        census=census(mgmt_ports=None, router_version=None, kev=None),
    )
    text = render_net(bare, state())
    assert "Management ports: not scanned" in text
    assert "Router version: unknown" in text
    assert "feed not available" in text


def test_net_no_baseline_yet():
    text = render_net(full_obs(), WatchState())
    assert "baseline not learned yet" in text
    assert "no baseline yet" in text


def test_net_outage_and_unconfigured():
    assert "unreachable" in render_net(full_obs(census=None), state())
    off = render_net(full_obs(configured=frozenset({"ha"}), census=None), state())
    assert "not configured" in off


def test_net_empty_kev_list_and_cap():
    assert "no entries" in render_net(full_obs(census=census(kev=())), state())
    many = tuple(
        KevEntry(f"CVE-2000-{i:04d}", "2024-01-01", "p", "n", "d") for i in range(14)
    )
    text = render_net(full_obs(census=census(kev=many)), state())
    assert "14 entries" in text
    assert "and 4 more" in text


def test_net_hostile_router_version_is_sanitised():
    text = render_net(full_obs(census=census(router_version="7_1*[x]")), state())
    assert "7\\_1\\*\\[x]" in text


# ─── daily report ───────────────────────────────────────────────────


def test_report_combines_home_and_net_with_uptime():
    text = render_report(full_obs(), state(), CFG)
    assert text.startswith("Homelab daily report")
    assert "Home Assistant: 6 entities" in text
    assert "Devices: 3" in text
    assert "Lowest 24h uptime: NAS 90.0%" in text
    assert "OUTAGE" not in text


def test_report_shows_per_source_errors_for_failed_sources():
    obs = full_obs(
        ha=None,
        census=None,
        errors={"ha": "HTTP 502 from http://192.0.2.7:8123", "census": "timeout"},
    )
    text = render_report(obs, state(failures={"ha": 4}), CFG)
    assert "OUTAGE Home Assistant" in text
    assert "(4 failed checks)" in text
    assert "OUTAGE Network census: timeout" in text
    assert "Home Assistant: unreachable" in text
    assert "0 entities" not in text


def test_report_with_nothing_configured():
    text = render_report(full_obs(configured=frozenset()), state(), CFG)
    assert text == "Homelab: no sources configured"


def test_report_omits_unconfigured_sources_with_one_line_each():
    obs = full_obs(configured=frozenset({"kuma"}), ha=None, census=None)
    text = render_report(obs, state(), CFG)
    assert text.count("not configured") == 2
    assert "OUTAGE" not in text


def test_report_text_has_no_unescaped_markdown_from_data():
    obs = full_obs(
        ha=(ent("sensor.a_b", "unavailable", "x_y"),),
        kuma=(Monitor("1", "m*n", 0, 0.5),),
    )
    text = render_report(obs, state(), CFG)
    assert "x\\_y" in text
    assert "m\\*n" in text
    assert "`" not in text
