"""The pure watcher: one test per acceptance criterion in the plan."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from homelab.findings import evaluate
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

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
CFG = WatchConfig()
ALL = frozenset({"ha", "kuma", "census"})


def dev(n, host="printer-1", mac=None, ip=None, iface="bridge"):
    mac = mac or f"00:00:5E:00:53:{n:02X}"
    return Device(mac, ip or f"192.0.2.{n}", host, iface, "reachable", True)


def rand_dev(n, host="phone-1"):
    return dev(n, host=host, mac=f"02:AA:BB:CC:DD:{n:02X}")


def kev(n, name="RouterOS test flaw"):
    return KevEntry(f"CVE-2000-{n:04d}", "2024-01-02", "RouterOS", name, "x")


def census(devices=(), when=T0, ports=(22, 53, 8291), version="7.1.2 (stable)", k=()):
    kev_t = None if k is None else tuple(k)
    return Census(when, tuple(devices), ports, version, kev_t)


def ent(eid, state="on", name=None):
    return EntityState(eid, state, name or eid)


def obs(
    now=T0,
    *,
    ha=None,
    kuma=None,
    cen=None,
    configured=ALL,
    errors=None,
):
    return Observation(now, frozenset(configured), ha, kuma, cen, errors or {})


def run(state, *observations, cfg=CFG):
    alerts = []
    for o in observations:
        state, alerts = evaluate(state, o, cfg)
    return state, alerts


def at(minutes=0, hours=0):
    return T0 + timedelta(minutes=minutes, hours=hours)


def baseline_obs():
    return obs(
        ha=(ent("light.lamp"),),
        kuma=(Monitor("1", "Router", 1),),
        cen=census([dev(1)], k=[kev(1)]),
    )


def texts(alerts):
    return "\n".join(a.text for a in alerts)


# ─── 1. baseline ────────────────────────────────────────────────────


def test_first_observation_learns_baseline_and_is_silent():
    first = obs(
        ha=(ent("sensor.a", "unavailable"),),
        kuma=(Monitor("1", "Router", 0),),
        cen=census([dev(1), rand_dev(2)], k=[kev(1)]),
    )
    state, alerts = evaluate(WatchState(), first, CFG)
    assert alerts == []
    assert state.learned == {"ha": True, "kuma": True, "census": True}
    assert set(state.known_macs) == {"00:00:5E:00:53:01", "02:AA:BB:CC:DD:02"}
    assert state.baseline_ports == [22, 53, 8291]
    assert state.seen_kev == ["CVE-2000-0001"]
    # a monitor that was already down at baseline is not a transition
    assert state.kuma_status == {"1": 0}


def test_unchanged_observations_stay_silent():
    state, alerts = run(WatchState(), baseline_obs(), baseline_obs())
    assert alerts == []


def test_evaluate_does_not_mutate_its_input_state():
    state = WatchState()
    before = state.to_dict()
    evaluate(state, baseline_obs(), CFG)
    assert state.to_dict() == before


# ─── 2. failed sources ──────────────────────────────────────────────


def failed(src="census", error="boom"):
    return obs(errors={src: error}, configured={src})


def test_failed_source_leaves_state_untouched_apart_from_the_counter():
    state, _ = run(WatchState(), baseline_obs())
    before = state.to_dict()
    after, alerts = evaluate(state, failed("census"), CFG)
    assert alerts == []
    expected = {**before, "failures": {"census": 1}}
    assert after.to_dict() == expected


def test_outage_is_never_read_as_everything_gone_or_recovered():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, failed("census"), failed("census"))
    assert alerts == []
    # devices stay known: the next real census is compared against them
    assert "00:00:5E:00:53:01" in state.known_macs
    state, alerts = run(state, baseline_obs())
    assert not any("New device" in a.text for a in alerts)


def test_source_down_alert_once_at_threshold_then_recovery_once():
    state, _ = run(WatchState(), baseline_obs())
    state, a1 = run(state, failed("ha"))
    state, a2 = run(state, failed("ha"))
    assert a1 == a2 == []
    state, a3 = run(state, failed("ha"))
    assert len(a3) == 1
    assert a3[0].source == "ha"
    assert "3" in a3[0].text
    state, a4 = run(state, failed("ha"))
    assert a4 == []
    state, a5 = run(state, baseline_obs())
    assert [a.key for a in a5] == ["ha:recovered"]
    state, a6 = run(state, baseline_obs())
    assert a6 == []
    assert state.failures.get("ha", 0) == 0


def test_success_before_threshold_resets_counter_without_alerts():
    state, _ = run(WatchState(), baseline_obs())
    state, _ = run(state, failed("kuma"), failed("kuma"))
    state, alerts = run(state, baseline_obs())
    assert alerts == []
    state, alerts = run(state, failed("kuma"), failed("kuma"))
    assert alerts == []


def test_down_alert_never_leaks_error_text():
    state, _ = run(WatchState(), baseline_obs())
    err = "connect to http://192.0.2.7:8123 refused"
    state, alerts = run(state, *[failed("ha", err)] * 3)
    assert "192.0.2.7" not in texts(alerts)


def test_failure_counts_per_source_independently():
    state, _ = run(WatchState(), baseline_obs())
    mixed = obs(
        ha=None,
        kuma=(Monitor("1", "Router", 1),),
        cen=census([dev(1)], k=[kev(1)]),
        errors={"ha": "x"},
    )
    state, alerts = run(state, mixed, mixed, mixed)
    assert [a.source for a in alerts] == ["ha"]
    assert state.failures == {"ha": 3}


# ─── 3. unconfigured sources ────────────────────────────────────────


def test_unconfigured_source_never_alerts_or_counts():
    only_ha = obs(ha=(ent("light.lamp"),), configured={"ha"})
    state, alerts = run(WatchState(), *[only_ha] * 5)
    assert alerts == []
    assert state.failures == {}
    assert state.learned == {"ha": True}


def test_nothing_configured_is_a_noop():
    state, alerts = run(WatchState(), obs(configured=set()))
    assert alerts == []
    assert state.to_dict() == WatchState().to_dict()


# ─── 4. new devices ─────────────────────────────────────────────────


def test_new_universal_mac_alerts_with_sanitised_details():
    state, _ = run(WatchState(), baseline_obs())
    cen = census([dev(1), dev(7, host="tv_living*room", ip="198.51.100.7")], k=[kev(1)])
    state, alerts = run(state, replace(baseline_obs(), census=cen))
    assert len(alerts) == 1
    text = alerts[0].text
    assert alerts[0].source == "census"
    assert "tv\\_living\\*room" in text
    assert "198.51.100.7" in text
    assert "bridge" in text
    assert "00:00:5E:00:53:07" in text
    # known from now on: no repeat
    state, alerts = run(state, replace(baseline_obs(), census=cen))
    assert alerts == []


def test_randomized_mac_is_learned_and_never_alerted():
    state, _ = run(WatchState(), baseline_obs())
    cen = census([dev(1), rand_dev(9)], k=[kev(1)])
    state, alerts = run(state, replace(baseline_obs(), census=cen))
    assert alerts == []
    assert "02:AA:BB:CC:DD:09" in state.known_macs


def test_new_devices_alert_in_mac_order_and_blank_hostname_is_labelled():
    state, _ = run(WatchState(), baseline_obs())
    cen = census([dev(1), dev(9, host=""), dev(5)], k=[kev(1)])
    state, alerts = run(state, replace(baseline_obs(), census=cen))
    assert [a.key for a in alerts] == [
        "census:new:00:00:5E:00:53:05",
        "census:new:00:00:5E:00:53:09",
    ]
    assert "no hostname" in alerts[1].text


def test_hostile_hostname_is_neutralised():
    state, _ = run(WatchState(), baseline_obs())
    nasty = "evil\x00\n[click](http://x)`" + "A" * 200
    cen = census([dev(1), dev(8, host=nasty)], k=[kev(1)])
    state, alerts = run(state, replace(baseline_obs(), census=cen))
    text = alerts[0].text
    assert "\x00" not in text
    assert "\n" not in text
    assert "\\[click]" in text
    assert "A" * 60 not in text


# ─── 5. management ports ────────────────────────────────────────────


def with_ports(ports):
    return replace(baseline_obs(), census=census([dev(1)], ports=ports, k=[kev(1)]))


def test_port_change_alerts_once_per_distinct_set_then_recovers():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, with_ports((22, 53, 80, 8291)))
    assert len(alerts) == 1
    assert "80" in alerts[0].text
    assert "opened" in alerts[0].text
    state, alerts = run(state, with_ports((22, 53, 80, 8291)))
    assert alerts == []
    state, alerts = run(state, with_ports((22, 53, 80, 443, 8291)))
    assert len(alerts) == 1
    state, alerts = run(state, with_ports((22, 53, 8291)))
    assert [a.key for a in alerts] == ["census:ports:recovered"]
    state, alerts = run(state, with_ports((22, 53, 8291)))
    assert alerts == []


def test_closed_port_is_reported_as_closed():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, with_ports((22, 8291)))
    assert "closed" in alerts[0].text
    assert "53" in alerts[0].text


def test_missing_port_scan_is_not_a_change():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, with_ports(None))
    assert alerts == []
    assert state.baseline_ports == [22, 53, 8291]


def test_ports_first_seen_after_baseline_are_learned_silently():
    first = replace(baseline_obs(), census=census([dev(1)], ports=None, k=[kev(1)]))
    state, _ = run(WatchState(), first)
    assert state.baseline_ports is None
    state, alerts = run(state, with_ports((22, 80)))
    assert alerts == []
    assert state.baseline_ports == [22, 80]


# ─── 6. KEV ─────────────────────────────────────────────────────────


def with_kev(entries, version="7.1.2 (stable)"):
    return replace(baseline_obs(), census=census([dev(1)], version=version, k=entries))


def test_new_kev_entry_alerts_with_version_and_no_vulnerability_claim():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, with_kev([kev(1), kev(2, name="Winbox_test flaw")]))
    assert len(alerts) == 1
    text = alerts[0].text
    assert "CVE-2000-0002" in text
    assert "2024-01-02" in text
    assert "Winbox\\_test flaw" in text
    assert "7.1.2 (stable)" in text
    assert "no affected versions" in text
    assert "advisory" in text
    assert "vulnerable" not in text.lower()
    state, alerts = run(state, with_kev([kev(1), kev(2)]))
    assert alerts == []


def test_kev_alert_says_unknown_when_version_unknown():
    first = replace(baseline_obs(), census=census([dev(1)], version=None, k=[kev(1)]))
    state, _ = run(WatchState(), first)
    state, alerts = run(state, with_kev([kev(1), kev(2)], version=None))
    assert "unknown" in alerts[0].text


def test_missing_kev_feed_is_not_a_change():
    state, _ = run(WatchState(), baseline_obs())
    no_feed = replace(baseline_obs(), census=replace(baseline_obs().census, kev=None))
    state, alerts = run(state, no_feed)
    assert alerts == []
    assert state.seen_kev == ["CVE-2000-0001"]


def test_kev_feed_appearing_after_baseline_is_learned_silently():
    no_feed = replace(baseline_obs(), census=census([dev(1)], k=None))
    state, _ = run(WatchState(), no_feed)
    assert not state.kev_learned
    state, alerts = run(state, with_kev([kev(1), kev(2)]))
    assert alerts == []
    assert state.seen_kev == ["CVE-2000-0001", "CVE-2000-0002"]


# ─── 7. router version ──────────────────────────────────────────────


def test_router_version_change_alerts_old_to_new():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, with_kev([kev(1)], version="7.2 (stable)"))
    assert len(alerts) == 1
    assert "7.1.2 (stable)" in alerts[0].text
    assert "7.2 (stable)" in alerts[0].text
    state, alerts = run(state, with_kev([kev(1)], version="7.2 (stable)"))
    assert alerts == []


def test_missing_version_is_not_a_change():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, with_kev([kev(1)], version=None))
    assert alerts == []
    assert state.router_version == "7.1.2 (stable)"


# ─── 8. stale census ────────────────────────────────────────────────


def stale_obs(when, now):
    return replace(
        baseline_obs(), now=now, census=census([dev(1)], when=when, k=[kev(1)])
    )


def test_stale_census_alerts_once_per_when_value():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, stale_obs(T0, at(hours=12)))
    assert alerts == []
    state, alerts = run(state, stale_obs(T0, at(hours=14)))
    assert [a.key for a in alerts] == ["census:stale"]
    assert "14" in alerts[0].text
    state, alerts = run(state, stale_obs(T0, at(hours=20)))
    assert alerts == []
    state, alerts = run(state, stale_obs(at(hours=21), at(hours=40)))
    assert [a.key for a in alerts] == ["census:stale"]


def test_stale_check_honours_config_and_naive_timestamps():
    state, _ = run(WatchState(), baseline_obs())
    naive = replace(
        baseline_obs(),
        now=at(hours=3),
        census=census([dev(1)], when=T0.replace(tzinfo=None), k=[kev(1)]),
    )
    cfg = replace(CFG, census_max_age_hours=2)
    state, alerts = run(state, naive, cfg=cfg)
    assert [a.key for a in alerts] == ["census:stale"]


# ─── 9. Kuma ────────────────────────────────────────────────────────


def kuma_obs(status, now=T0, name="Router"):
    return replace(baseline_obs(), now=now, kuma=(Monitor("1", name, status),))


def test_kuma_down_then_up_alerts_once_each():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, kuma_obs(0))
    assert [a.key for a in alerts] == ["kuma:down:1"]
    assert "Router" in alerts[0].text
    state, alerts = run(state, kuma_obs(0))
    assert alerts == []
    state, alerts = run(state, kuma_obs(1))
    assert [a.key for a in alerts] == ["kuma:up:1"]
    state, alerts = run(state, kuma_obs(1))
    assert alerts == []


def test_kuma_pending_and_maintenance_do_not_alert_or_reset_status():
    state, _ = run(WatchState(), baseline_obs())
    for status in (2, 3, None):
        state, alerts = run(state, kuma_obs(status))
        assert alerts == []
        assert state.kuma_status == {"1": 1}
    state, _ = run(state, kuma_obs(0))
    state, alerts = run(state, kuma_obs(2))
    assert alerts == []
    # still down underneath: the later up is a real recovery
    state, alerts = run(state, kuma_obs(1))
    assert [a.key for a in alerts] == ["kuma:up:1"]


def test_flapping_through_pending_does_not_realert_down():
    state, _ = run(WatchState(), baseline_obs())
    state, _ = run(state, kuma_obs(0))
    state, alerts = run(state, kuma_obs(2), kuma_obs(0))
    assert alerts == []


def test_new_monitor_that_is_down_alerts_after_baseline():
    state, _ = run(WatchState(), baseline_obs())
    both = replace(
        baseline_obs(), kuma=(Monitor("1", "Router", 1), Monitor("2", "NAS", 0))
    )
    state, alerts = run(state, both)
    assert [a.key for a in alerts] == ["kuma:down:2"]


def test_kuma_name_is_sanitised():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, kuma_obs(0, name="my_nas*"))
    assert "my\\_nas\\*" in alerts[0].text


# ─── 10. Home Assistant unavailable ─────────────────────────────────


def ha_obs(now, **states):
    return replace(
        baseline_obs(),
        now=now,
        ha=tuple(ent(eid.replace("__", "."), st) for eid, st in states.items()),
    )


def test_unavailable_entity_alerts_after_debounce_only_once():
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, ha_obs(T0, sensor__a="unavailable"))
    assert alerts == []
    state, alerts = run(state, ha_obs(at(minutes=29), sensor__a="unavailable"))
    assert alerts == []
    state, alerts = run(state, ha_obs(at(minutes=30), sensor__a="unavailable"))
    assert [a.key for a in alerts] == ["ha:unavailable:sensor.a"]
    state, alerts = run(state, ha_obs(at(minutes=90), sensor__a="unavailable"))
    assert alerts == []


def test_recovery_resets_debounce_silently():
    state, _ = run(WatchState(), baseline_obs())
    state, _ = run(state, ha_obs(T0, sensor__a="unknown"))
    state, alerts = run(state, ha_obs(at(minutes=20), sensor__a="21"))
    assert alerts == []
    assert state.ha_unavailable_since == {}
    # a fresh outage starts a fresh clock
    state, alerts = run(state, ha_obs(at(minutes=25), sensor__a="unavailable"))
    state, alerts = run(state, ha_obs(at(minutes=50), sensor__a="unavailable"))
    assert alerts == []
    state, alerts = run(state, ha_obs(at(minutes=56), sensor__a="unavailable"))
    assert len(alerts) == 1


def test_recovered_and_realerted_entity_can_alert_again():
    state, _ = run(WatchState(), baseline_obs())
    state, _ = run(state, ha_obs(T0, sensor__a="unavailable"))
    state, alerts = run(state, ha_obs(at(minutes=31), sensor__a="unavailable"))
    assert len(alerts) == 1
    state, _ = run(state, ha_obs(at(minutes=32), sensor__a="1"))
    assert state.ha_alerted == []
    state, _ = run(state, ha_obs(at(minutes=33), sensor__a="unavailable"))
    state, alerts = run(state, ha_obs(at(minutes=64), sensor__a="unavailable"))
    assert len(alerts) == 1


def test_entity_that_disappears_is_forgotten():
    state, _ = run(WatchState(), baseline_obs())
    state, _ = run(state, ha_obs(T0, sensor__a="unavailable"))
    state, _ = run(state, ha_obs(at(minutes=5), light__lamp="on"))
    assert state.ha_unavailable_since == {}


def test_flood_collapses_to_one_alert_with_count_and_five_names():
    state, _ = run(WatchState(), baseline_obs())
    names = {f"sensor__s{i:02d}": "unavailable" for i in range(12)}
    state, _ = run(state, ha_obs(T0, **names))
    state, alerts = run(state, ha_obs(at(minutes=31), **names))
    assert len(alerts) == 1
    text = alerts[0].text
    assert "12" in text
    assert text.count("sensor.s") == 5
    assert "7 more" in text
    state, alerts = run(state, ha_obs(at(minutes=60), **names))
    assert alerts == []


def test_exactly_threshold_entities_alert_individually():
    state, _ = run(WatchState(), baseline_obs())
    names = {f"sensor__s{i:02d}": "unavailable" for i in range(10)}
    state, _ = run(state, ha_obs(T0, **names))
    state, alerts = run(state, ha_obs(at(minutes=31), **names))
    assert len(alerts) == 10
    assert [a.key for a in alerts] == sorted(a.key for a in alerts)


def test_ignore_entities_globs_skip_matching_entities():
    cfg = replace(CFG, ignore_entities=("sensor.noisy_*", "update.*"))
    state, _ = run(WatchState(), baseline_obs(), cfg=cfg)
    states = {
        "sensor__noisy_a": "unavailable",
        "update__fw": "unavailable",
        "sensor__real": "unavailable",
    }
    state, _ = run(state, ha_obs(T0, **states), cfg=cfg)
    state, alerts = run(state, ha_obs(at(minutes=31), **states), cfg=cfg)
    assert [a.key for a in alerts] == ["ha:unavailable:sensor.real"]


def test_entity_names_are_sanitised_in_alerts():
    state, _ = run(WatchState(), baseline_obs())
    odd = replace(
        baseline_obs(), ha=(EntityState("sensor.a", "unavailable", "Hall_*[x]"),)
    )
    state, _ = run(state, odd)
    state, alerts = run(state, replace(odd, now=at(minutes=31)))
    assert "Hall\\_\\*\\[x]" in alerts[0].text


def test_alerts_are_not_emitted_for_report_only_content():
    state, _ = run(WatchState(), baseline_obs())
    report_only = replace(
        baseline_obs(),
        ha=(
            EntityState("sensor.b", "5", "Battery", device_class="battery", unit="%"),
            ent("update.fw", "on"),
        ),
    )
    state, alerts = run(state, report_only, replace(report_only, now=at(hours=5)))
    assert alerts == []


# ─── 11. sanitising of everything, ordering ─────────────────────────


def test_every_alert_text_is_single_line_and_markdown_safe():
    state, _ = run(WatchState(), baseline_obs())
    cen = census(
        [dev(1), dev(7, host="a_b*c")],
        ports=(22, 80),
        version="7.9 (stable)",
        k=[kev(1), kev(2, name="x_y")],
        when=T0,
    )
    bad = replace(
        baseline_obs(),
        now=at(hours=20),
        kuma=(Monitor("1", "r_1", 0),),
        census=cen,
    )
    state, alerts = run(state, bad)
    assert len(alerts) >= 5
    for alert in alerts:
        assert "\n" not in alert.text
        assert alert.text.count("`") == alert.text.count("\\`")


def test_alert_order_is_deterministic_by_source_then_kind():
    state, _ = run(WatchState(), baseline_obs())
    bad = replace(
        baseline_obs(),
        now=at(hours=20),
        kuma=(Monitor("1", "Router", 0),),
        census=census(
            [dev(1), dev(7)], ports=(22,), version="9.9", k=[kev(1), kev(2)], when=T0
        ),
    )
    _, first = run(state, bad)
    _, second = run(state, bad)
    assert first == second
    sources = [a.source for a in first]
    assert sources == sorted(sources, key=["ha", "kuma", "census"].index)
    census_keys = [a.key for a in first if a.source == "census"]
    kinds = [k.split(":")[1] for k in census_keys]
    assert kinds == ["new", "ports", "router", "kev", "stale"]


# ─── state serialisation ────────────────────────────────────────────


def test_state_round_trips_through_json():
    import json

    state, _ = run(WatchState(), baseline_obs())
    state, _ = run(state, ha_obs(T0, sensor__a="unavailable"), kuma_obs(0))
    state, _ = run(state, *[failed("ha")] * 3)
    data = json.loads(json.dumps(state.to_dict()))
    assert WatchState.from_dict(data) == state


def test_state_from_dict_tolerates_missing_unknown_and_mistyped():
    assert WatchState.from_dict({}) == WatchState()
    assert WatchState.from_dict(None) == WatchState()
    assert WatchState.from_dict("junk") == WatchState()
    odd = WatchState.from_dict(
        {
            "future_key": 1,
            "known_macs": ["not", "a", "dict"],
            "baseline_ports": "x",
            "seen_kev": 5,
            "failures": {"ha": "three", "kuma": 2},
            "learned": {"ha": 1},
        }
    )
    assert odd.known_macs == {}
    assert odd.baseline_ports is None
    assert odd.seen_kev == []
    assert odd.failures == {"kuma": 2}
    assert odd.learned == {"ha": True}


# ─── review fixes ───────────────────────────────────────────────────


def test_empty_census_does_not_learn_a_baseline():
    empty = obs(configured={"census"}, cen=census([], k=None))
    state, alerts = run(WatchState(), empty)
    assert alerts == [] and not state.learned.get("census")
    assert state.census_baseline_at is None
    # the first real census is the baseline, so its devices stay silent
    real = obs(configured={"census"}, cen=census([dev(1), dev(2)], k=None))
    state, alerts = run(state, real)
    assert alerts == [] and state.learned["census"] is True
    assert set(state.known_macs) == {"00:00:5E:00:53:01", "00:00:5E:00:53:02"}


def flood_obs(n, now=T0):
    devices = [dev(i, host=f"host-{i:02d}") for i in range(1, n + 1)]
    return obs(now=now, configured={"census"}, cen=census(devices, k=None))


def test_new_device_flood_collapses_to_one_alert():
    state, _ = run(WatchState(), flood_obs(1))
    state, alerts = run(state, flood_obs(13))
    assert len(alerts) == 1
    assert "12" in alerts[0].text
    assert alerts[0].text.count("host-") <= 5
    assert len(state.known_macs) == 13


def test_new_devices_at_the_threshold_still_alert_individually():
    state, _ = run(WatchState(), flood_obs(1))
    state, alerts = run(state, flood_obs(11))  # 10 new == threshold
    assert len(alerts) == 10


def part_census(**whens):
    return replace(census([dev(1)], k=[kev(1)]), **whens)


def test_stale_optional_parts_alert_once_per_when_with_distinct_keys():
    base = obs(configured={"census"}, cen=part_census())
    state, _ = run(WatchState(), base)
    old = at(hours=-30)
    stale = obs(
        now=at(hours=1),
        configured={"census"},
        cen=part_census(ports_when=old, router_when=old, kev_when=at(hours=0)),
    )
    state, alerts = run(state, stale)
    assert {a.key for a in alerts} == {"census:stale:ports", "census:stale:router"}
    state, alerts = run(state, stale)
    assert alerts == []
    newer_old = obs(
        now=at(hours=2),
        configured={"census"},
        cen=part_census(
            ports_when=at(hours=-29), router_when=old, kev_when=at(hours=0)
        ),
    )
    state, alerts = run(state, newer_old)
    assert [a.key for a in alerts] == ["census:stale:ports"]


def test_unknown_state_never_alerts():
    state, _ = run(WatchState(), baseline_obs())
    state, _ = run(state, ha_obs(T0, sensor__a="unknown"))
    state, alerts = run(state, ha_obs(at(minutes=120), sensor__a="unknown"))
    assert alerts == []


def test_entity_down_at_baseline_is_known_down_until_it_recovers():
    first = ha_obs(T0, sensor__a="unavailable")
    state, _ = run(WatchState(), first)
    state, alerts = run(state, ha_obs(at(minutes=60), sensor__a="unavailable"))
    assert alerts == []
    state, _ = run(state, ha_obs(at(minutes=61), sensor__a="3"))
    state, _ = run(state, ha_obs(at(minutes=62), sensor__a="unavailable"))
    state, alerts = run(state, ha_obs(at(minutes=93), sensor__a="unavailable"))
    assert [a.key for a in alerts] == ["ha:unavailable:sensor.a"]


@pytest.mark.parametrize(
    "error, phrase",
    [
        ("HTTP 401", "rejected the credentials (HTTP 401)"),
        ("HTTP 403", "rejected the credentials (HTTP 403)"),
        ("TLS verification failed", "TLS verification failed"),
        ("connection failed", "unreachable"),
        ("timed out", "unreachable"),
    ],
)
def test_source_down_alert_names_the_reason_class(error, phrase):
    state, _ = run(WatchState(), baseline_obs())
    state, alerts = run(state, *[failed("ha", error)] * 3)
    assert phrase in alerts[0].text
    if phrase != "unreachable":
        assert "unreachable" not in alerts[0].text
