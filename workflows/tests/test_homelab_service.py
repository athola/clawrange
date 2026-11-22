"""Homelab service: watch runs, outbox, health, actions. Synthetic data only."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from errors import UpstreamError
from homelab import service, sources
from homelab.config import HomelabConfig
from homelab.models import Census, Device, EntityState, Observation

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TOKEN = "s3cr3t-token-value"

CFG = HomelabConfig.from_block(
    {
        "home_assistant": {
            "url": "http://192.0.2.5:8123",
            "token": TOKEN,
            "actions": [
                {"service": "light.turn_on", "entities": ["light.kitchen"]},
                {"service": "light.turn_off", "entities": ["light.*"]},
            ],
        },
        "census": {"url": "http://192.0.2.7"},
    }
)
HA_ONLY = HomelabConfig.from_block(
    {"home_assistant": {"url": "http://192.0.2.5:8123", "token": TOKEN}}
)


@pytest.fixture(autouse=True)
def _fresh_actions(monkeypatch):
    service.reset_actions()

    async def states(cfg, **kw):
        return (
            EntityState("light.kitchen", "on", "Kitchen"),
            EntityState("light.hall", "off", "Hall"),
        )

    monkeypatch.setattr(sources, "fetch_ha_states", states)
    yield
    service.reset_actions()


class Sink:
    """A Telegram stand-in that records messages."""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent: list[str] = []

    async def __call__(self, text):
        self.sent.append(text)
        return self.ok


def device(mac, host="printer-1", ip="192.0.2.10"):
    return Device(mac, ip, host, "bridge", "reachable", True)


def census_obs(*devices, now=T0, when=T0):
    return Observation(
        now=now,
        configured=frozenset({"census"}),
        census=Census(when=when, devices=tuple(devices)),
    )


def script(monkeypatch, *observations):
    """Make sources.observe return the given observations in order."""
    queue = list(observations)
    calls = []

    async def fake(cfg, now, **kw):
        calls.append(now)
        return queue.pop(0)

    monkeypatch.setattr(sources, "observe", fake)
    return calls


CENSUS_CFG = HomelabConfig.from_block({"census": {"url": "http://192.0.2.7"}})


# ─── run_watch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unconfigured_is_disabled_and_touches_nothing(brain_db, monkeypatch):
    async def explode(*a, **k):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(sources, "observe", explode)
    sink = Sink()
    out = await service.run_watch(brain_db, HomelabConfig.from_block(None), notify=sink)
    assert out == {"status": "disabled"}
    assert sink.sent == []
    assert brain_db.homelab_get("watch") is None
    assert brain_db.homelab_get("health") is None


@pytest.mark.asyncio
async def test_first_run_learns_baseline_without_alerts(brain_db, monkeypatch):
    script(monkeypatch, census_obs(device("00:00:5E:00:53:01")))
    sink = Sink()
    out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=sink)
    assert out["status"] == "ok" and out["alerts"] == 0
    assert sink.sent == []
    assert "00:00:5E:00:53:01" in brain_db.homelab_get("watch")["known_macs"]


@pytest.mark.asyncio
async def test_new_device_alert_is_one_message(brain_db, monkeypatch):
    script(
        monkeypatch,
        census_obs(device("00:00:5E:00:53:01")),
        census_obs(
            device("00:00:5E:00:53:01"),
            device("00:00:5E:00:53:02", "cam-2", "192.0.2.20"),
            device("00:00:5E:00:53:03", "cam-3", "192.0.2.21"),
        ),
    )
    sink = Sink()
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=sink)
    out = await service.run_watch(
        brain_db, CENSUS_CFG, now=T0 + timedelta(minutes=5), notify=sink
    )
    assert out["alerts"] == 2 and out["delivered"] is True
    assert len(sink.sent) == 1
    lines = sink.sent[0].splitlines()
    assert lines[0] == "Homelab: 2 alert(s)"
    assert len(lines) == 3 and all(line.startswith("- ") for line in lines[1:])
    assert brain_db.homelab_get("outbox") == {"lines": []}


@pytest.mark.asyncio
async def test_failed_fetch_is_recorded_and_state_untouched(brain_db, monkeypatch):
    async def down(cfg, **kw):
        raise UpstreamError("HTTP 401")

    monkeypatch.setattr(sources, "fetch_ha_states", down)
    before = {"known_macs": {}}
    brain_db.homelab_put("watch", before)
    sink = Sink()
    out = await service.run_watch(brain_db, HA_ONLY, now=T0, notify=sink)
    assert out["errors"] == {"ha": "HTTP 401"}
    saved = brain_db.homelab_get("watch")
    assert saved["ha_unavailable_since"] == {} and saved["ha_alerted"] == []
    assert saved["failures"] == {"ha": 1}
    health = brain_db.homelab_get("health")["sources"]["ha"]
    assert health["ok"] is False and health["last_error"] == "HTTP 401"
    assert health["last_success"] is None
    assert sink.sent == []  # one failure is below source_down_after


@pytest.mark.asyncio
async def test_outbox_keeps_undelivered_and_retries(brain_db, monkeypatch):
    script(
        monkeypatch,
        census_obs(device("00:00:5E:00:53:01")),
        census_obs(device("00:00:5E:00:53:01"), device("00:00:5E:00:53:02", "cam-2")),
        census_obs(device("00:00:5E:00:53:01"), device("00:00:5E:00:53:02", "cam-2")),
    )
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    failing = Sink(ok=False)
    out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=failing)
    assert out["delivered"] is False and len(failing.sent) == 1
    queued = brain_db.homelab_get("outbox")["lines"]
    assert len(queued) == 1 and "cam-2" in queued[0]

    good = Sink()
    out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=good)
    assert out["alerts"] == 0 and out["queued"] == 1 and out["delivered"] is True
    assert "cam-2" in good.sent[0]
    assert brain_db.homelab_get("outbox") == {"lines": []}


@pytest.mark.asyncio
async def test_notify_raising_counts_as_undelivered(brain_db, monkeypatch):
    script(
        monkeypatch,
        census_obs(device("00:00:5E:00:53:01")),
        census_obs(device("00:00:5E:00:53:01"), device("00:00:5E:00:53:02", "cam-2")),
    )

    async def boom(text):
        raise RuntimeError("telegram down")

    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=boom)
    assert out["delivered"] is False
    assert len(brain_db.homelab_get("outbox")["lines"]) == 1


@pytest.mark.asyncio
async def test_outbox_is_capped(brain_db, monkeypatch):
    brain_db.homelab_put("outbox", {"lines": [f"old {i}" for i in range(30)]})
    script(monkeypatch, census_obs(device("00:00:5E:00:53:01")))
    out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink(ok=False))
    assert out["delivered"] is False
    lines = brain_db.homelab_get("outbox")["lines"]
    assert len(lines) == service.OUTBOX_CAP
    assert lines[-1] == "old 29"


@pytest.mark.asyncio
async def test_long_alert_list_is_capped_and_overflow_queued(brain_db, monkeypatch):
    long_lines = [f"{i} " + "x" * 300 for i in range(30)]
    brain_db.homelab_put("outbox", {"lines": long_lines[:20]})
    script(monkeypatch, census_obs(device("00:00:5E:00:53:01")))
    sink = Sink()
    out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=sink)
    assert out["delivered"] is True
    assert len(sink.sent) == 1 and len(sink.sent[0]) <= service.MESSAGE_CAP
    assert "more follow" in sink.sent[0]
    rest = brain_db.homelab_get("outbox")["lines"]
    shown = sink.sent[0].count("\n- ")
    assert shown + len(rest) == 20


@pytest.mark.asyncio
async def test_concurrent_runs_are_serialized(brain_db, monkeypatch):
    import asyncio

    active = {"now": 0, "max": 0}

    async def slow(cfg, now, **kw):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1
        return census_obs(device("00:00:5E:00:53:01"))

    monkeypatch.setattr(sources, "observe", slow)
    await asyncio.gather(
        *(service.run_watch(brain_db, CENSUS_CFG, notify=Sink()) for _ in range(3))
    )
    assert active["max"] == 1


# ─── health ─────────────────────────────────────────────────────────


def test_health_disabled_when_nothing_configured(brain_db):
    out = service.health(brain_db, HomelabConfig.from_block(None), detail=True)
    assert out["status"] == "disabled"
    assert all(not s["configured"] for s in out["sources"].values())
    assert out["last_run"] is None


def test_health_degraded_before_first_success(brain_db):
    out = service.health(brain_db, CENSUS_CFG, detail=False)
    assert out["status"] == "degraded"
    assert out["sources"]["census"]["last_success"] is None


@pytest.mark.asyncio
async def test_health_ok_after_success_and_degraded_on_failure(brain_db, monkeypatch):
    script(
        monkeypatch,
        census_obs(device("00:00:5E:00:53:01")),
        Observation(
            now=T0,
            configured=frozenset({"census"}),
            errors={"census": "connection failed"},
        ),
    )
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    ok = service.health(brain_db, CENSUS_CFG, detail=False, now=T0)
    assert ok["status"] == "ok"
    assert ok["sources"]["census"]["last_success"] == T0.isoformat()
    assert ok["last_run"] == T0.isoformat()

    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    bad = service.health(brain_db, CENSUS_CFG, detail=True, now=T0)
    assert bad["status"] == "degraded"
    assert bad["sources"]["census"]["error"] == "connection failed"
    assert bad["sources"]["census"]["failures"] == 1
    # last_success survives the failure
    assert bad["sources"]["census"]["last_success"] == T0.isoformat()


@pytest.mark.asyncio
async def test_health_error_text_only_with_detail_and_no_urls(brain_db, monkeypatch):
    script(
        monkeypatch,
        Observation(
            now=T0,
            configured=frozenset({"census"}),
            errors={"census": "timed out"},
        ),
    )
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    anon = service.health(brain_db, CENSUS_CFG, detail=False)
    assert "error" not in anon["sources"]["census"]
    blob = json.dumps(service.health(brain_db, CENSUS_CFG, detail=True))
    assert "192.0.2" not in blob and TOKEN not in blob


# ─── actions ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_denied_when_not_allowlisted():
    sink = Sink()
    reply = await service.request_action(
        CFG, "light.turn_on", "light.bedroom", T0, sink
    )
    assert reply.startswith("Denied")
    assert sink.sent == []


@pytest.mark.asyncio
async def test_request_denied_for_hard_denied_domain():
    sink = Sink()
    reply = await service.request_action(CFG, "lock.unlock", "lock.door", T0, sink)
    assert "never allowed" in reply
    assert sink.sent == []


@pytest.mark.asyncio
async def test_request_without_ha_is_refused():
    reply = await service.request_action(
        CENSUS_CFG, "light.turn_on", "light.kitchen", T0, Sink()
    )
    assert "not configured" in reply


@pytest.mark.asyncio
async def test_code_goes_to_notify_never_the_reply():
    sink = Sink()
    reply = await service.request_action(
        CFG, "light.turn_on", "light.kitchen", T0, sink
    )
    (message,) = sink.sent
    code = message.split("/home confirm ")[1].split("`")[0]
    assert len(code) == 6 and code.isdigit()
    assert "`light.turn_on`" in message and "`light.kitchen`" in message
    assert "2 minutes" in message
    assert code not in reply
    assert "separately" in reply


@pytest.fixture()
def ha_calls(monkeypatch):
    calls = []

    async def fake(cfg, domain, name, entity_id, **kw):
        calls.append((domain, name, entity_id))

    monkeypatch.setattr(sources, "call_ha_service", fake)
    return calls


@pytest.mark.asyncio
async def test_undeliverable_code_is_discarded(ha_calls):
    sink = Sink(ok=False)
    reply = await service.request_action(
        CFG, "light.turn_on", "light.kitchen", T0, sink
    )
    assert "nothing was run" in reply
    code = sink.sent[0].split("/home confirm ")[1].split("`")[0]
    out = await service.confirm_action(CFG, code, T0, Sink())
    assert out.startswith("Invalid") and ha_calls == []


async def _issue(cfg=CFG, service_name="light.turn_on", entity="light.kitchen"):
    sink = Sink()
    await service.request_action(cfg, service_name, entity, T0, sink)
    return sink.sent[0].split("/home confirm ")[1].split("`")[0]


@pytest.mark.asyncio
async def test_confirm_runs_the_call_once(ha_calls):
    code = await _issue()
    out = await service.confirm_action(CFG, code, T0 + timedelta(seconds=30), Sink())
    assert out.startswith("Done")
    assert ha_calls == [("light", "turn_on", "light.kitchen")]
    again = await service.confirm_action(CFG, code, T0 + timedelta(seconds=31), Sink())
    assert again.startswith("That code was already used")
    assert len(ha_calls) == 1


@pytest.mark.asyncio
async def test_confirm_expires_after_ttl(ha_calls):
    code = await _issue()
    out = await service.confirm_action(CFG, code, T0 + timedelta(seconds=121), Sink())
    assert out.startswith("That code was already used or has expired")
    assert ha_calls == []


@pytest.mark.asyncio
async def test_confirm_reauthorizes_against_current_config(ha_calls):
    code = await _issue()
    narrowed = HomelabConfig.from_block(
        {"home_assistant": {"url": "http://192.0.2.5", "token": TOKEN}}
    )
    out = await service.confirm_action(narrowed, code, T0, Sink())
    assert out.startswith("Denied") and ha_calls == []


@pytest.mark.asyncio
async def test_confirm_reports_ha_failure_without_token(monkeypatch):
    async def fail(cfg, *a, **k):
        raise UpstreamError("HTTP 500")

    monkeypatch.setattr(sources, "call_ha_service", fail)
    code = await _issue()
    out = await service.confirm_action(CFG, code, T0, Sink())
    assert "failed" in out and "500" in out and TOKEN not in out


@pytest.mark.asyncio
async def test_lockout_after_three_bad_codes(ha_calls):
    sink = Sink()
    code = await _issue()
    for i in range(2):
        out = await service.confirm_action(
            CFG, "000000", T0 + timedelta(seconds=i), sink
        )
        assert out == "Invalid or expired code."
    out = await service.confirm_action(CFG, "000001", T0 + timedelta(seconds=3), sink)
    assert "disabled for 10 minutes" in out
    assert len(sink.sent) == 1 and "disabled for 10 minutes" in sink.sent[0]

    # even the right code is refused during the lockout, with no new alert
    out = await service.confirm_action(CFG, code, T0 + timedelta(minutes=5), sink)
    assert "disabled" in out and ha_calls == []
    assert len(sink.sent) == 1

    # after the lockout a fresh code works
    later = T0 + timedelta(minutes=11)
    fresh = Sink()
    await service.request_action(CFG, "light.turn_on", "light.kitchen", later, fresh)
    new_code = fresh.sent[0].split("/home confirm ")[1].split("`")[0]
    out = await service.confirm_action(CFG, new_code, later, sink)
    assert out.startswith("Done")


@pytest.mark.asyncio
async def test_bad_codes_outside_window_do_not_lock():
    sink = Sink()
    for minutes in (0, 11, 22):
        out = await service.confirm_action(
            CFG, "000000", T0 + timedelta(minutes=minutes), sink
        )
        assert out == "Invalid or expired code."
    assert sink.sent == []


# ─── review fixes ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alerts_and_state_are_stored_before_the_send(brain_db, monkeypatch):
    script(
        monkeypatch,
        census_obs(device("00:00:5E:00:53:01")),
        census_obs(device("00:00:5E:00:53:01"), device("00:00:5E:00:53:02", "cam-2")),
    )
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    seen = {}

    async def spy(text):
        seen["outbox"] = brain_db.homelab_get("outbox")["lines"]
        seen["macs"] = brain_db.homelab_get("watch")["known_macs"]
        return True

    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=spy)
    assert any("cam-2" in x for x in seen["outbox"])
    assert "00:00:5E:00:53:02" in seen["macs"]
    assert brain_db.homelab_get("outbox") == {"lines": []}


@pytest.mark.asyncio
async def test_outbox_overflow_keeps_a_note_and_counts(brain_db, monkeypatch, caplog):
    brain_db.homelab_put("outbox", {"lines": [f"old {i}" for i in range(30)]})
    script(monkeypatch, census_obs(device("00:00:5E:00:53:01")))
    with caplog.at_level("WARNING", logger="clawrange.homelab"):
        out = await service.run_watch(
            brain_db, CENSUS_CFG, now=T0, notify=Sink(ok=False)
        )
    lines = brain_db.homelab_get("outbox")["lines"]
    assert out["dropped"] == 11
    assert lines[0] == "11 older homelab alerts dropped; see /net"
    assert lines[-1] == "old 29" and len(lines) == service.OUTBOX_CAP
    assert "dropped 11" in caplog.text


@pytest.mark.asyncio
async def test_repeated_overflow_adds_up_the_dropped_count(brain_db, monkeypatch):
    lines = ["11 older homelab alerts dropped; see /net"]
    lines += [f"old {i}" for i in range(20)]
    brain_db.homelab_put("outbox", {"lines": lines})
    script(monkeypatch, census_obs(device("00:00:5E:00:53:01")))
    out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink(ok=False))
    assert out["dropped"] == 12  # the old note's 11 plus one line
    stored = brain_db.homelab_get("outbox")["lines"]
    assert stored[0] == "12 older homelab alerts dropped; see /net"


@pytest.mark.asyncio
async def test_unreadable_state_alerts_once_and_relearns(brain_db, monkeypatch, caplog):
    brain_db._conn.execute("INSERT INTO homelab_state VALUES ('watch', '[1]', 'x')")
    script(
        monkeypatch,
        census_obs(device("00:00:5E:00:53:01")),
        census_obs(device("00:00:5E:00:53:01")),
    )
    sink = Sink()
    with caplog.at_level("ERROR", logger="clawrange.homelab"):
        out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=sink)
    assert "unreadable" in caplog.text
    assert out["alerts"] == 1
    assert "homelab watch state was unreadable; relearning baselines" in sink.sent[0]
    assert "00:00:5E:00:53:01" in brain_db.homelab_get("watch")["known_macs"]
    sink2 = Sink()
    out = await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=sink2)
    assert out["alerts"] == 0 and sink2.sent == []


@pytest.mark.asyncio
async def test_absent_state_is_not_reported_as_unreadable(brain_db, monkeypatch):
    script(monkeypatch, census_obs(device("00:00:5E:00:53:01")))
    sink = Sink()
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=sink)
    assert sink.sent == []


@pytest.mark.asyncio
async def test_health_degrades_when_the_watch_stops_running(brain_db, monkeypatch):
    script(monkeypatch, census_obs(device("00:00:5E:00:53:01")))
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    soon = service.health(
        brain_db, CENSUS_CFG, detail=False, now=T0 + timedelta(minutes=14)
    )
    assert soon["status"] == "ok"
    late = service.health(
        brain_db, CENSUS_CFG, detail=False, now=T0 + timedelta(minutes=16)
    )
    assert late["status"] == "degraded" and "watch_stalled" in late["problems"]


@pytest.mark.asyncio
async def test_health_degrades_when_the_census_is_stale(brain_db, monkeypatch):
    script(
        monkeypatch,
        census_obs(device("00:00:5E:00:53:01"), when=T0 - timedelta(hours=14)),
    )
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    out = service.health(brain_db, CENSUS_CFG, detail=False, now=T0)
    assert out["status"] == "degraded" and "census_stale" in out["problems"]
    assert out["sources"]["census"]["ok"] is True


@pytest.mark.asyncio
async def test_census_part_errors_show_in_detail_health_only(brain_db, monkeypatch):
    obs = census_obs(device("00:00:5E:00:53:01"))
    obs = Observation(
        now=T0,
        configured=obs.configured,
        census=obs.census,
        errors={"census.ports": "HTTP 500"},
    )
    script(monkeypatch, obs)
    await service.run_watch(brain_db, CENSUS_CFG, now=T0, notify=Sink())
    full = service.health(brain_db, CENSUS_CFG, detail=True, now=T0)
    anon = service.health(brain_db, CENSUS_CFG, detail=False, now=T0)
    assert full["sources"]["census"]["part_errors"] == {"ports": "HTTP 500"}
    assert "part_errors" not in anon["sources"]["census"]
    assert full["status"] == "degraded"


def test_incomplete_source_is_flagged_in_health(brain_db):
    cfg = HomelabConfig.from_block({"home_assistant": {"url": "http://192.0.2.5"}})
    out = service.health(brain_db, cfg, detail=False, now=T0)
    ha = out["sources"]["ha"]
    assert ha["configured"] is False and ha["ok"] is None and ha["incomplete"] is True
    assert out["status"] == "degraded"
    assert "incomplete" not in out["sources"]["kuma"]


@pytest.mark.asyncio
async def test_replayed_code_does_not_count_toward_lockout(ha_calls):
    sink = Sink()
    code = await _issue()
    assert (await service.confirm_action(CFG, code, T0, sink)).startswith("Done")
    for i in range(5):
        out = await service.confirm_action(
            CFG, code, T0 + timedelta(seconds=i + 1), sink
        )
        assert out == "That code was already used or has expired."
    assert sink.sent == []
    fresh = await _issue()
    out = await service.confirm_action(CFG, fresh, T0 + timedelta(seconds=9), sink)
    assert out.startswith("Done")


@pytest.mark.asyncio
async def test_unissued_code_still_counts_toward_lockout():
    sink = Sink()
    for i in range(3):
        out = await service.confirm_action(CFG, f"00000{i}", T0, sink)
    assert "disabled" in out


@pytest.mark.asyncio
async def test_failed_lockout_notice_is_logged(caplog):
    sink = Sink(ok=False)
    with caplog.at_level("WARNING", logger="clawrange.homelab"):
        for i in range(3):
            await service.confirm_action(CFG, f"00000{i}", T0, sink)
    assert "lockout notice" in caplog.text


@pytest.mark.asyncio
async def test_eviction_of_a_pending_code_is_logged(caplog):
    with caplog.at_level("WARNING", logger="clawrange.homelab.actions"):
        for _ in range(6):
            await _issue()
    assert "dropped the oldest unredeemed" in caplog.text


@pytest.mark.asyncio
async def test_unknown_entity_gets_no_code():
    cfg = HomelabConfig.from_block(
        {
            "home_assistant": {
                "url": "http://192.0.2.5",
                "token": TOKEN,
                "actions": [{"service": "light.turn_on", "entities": ["light.*"]}],
            }
        }
    )
    sink = Sink()
    reply = await service.request_action(
        cfg, "light.turn_on", "light.nowhere", T0, sink
    )
    assert reply.startswith("Denied") and "no entity" in reply
    assert sink.sent == []


@pytest.mark.asyncio
async def test_unreadable_ha_means_no_code(monkeypatch):
    async def down(cfg, **kw):
        raise UpstreamError("HTTP 502")

    monkeypatch.setattr(sources, "fetch_ha_states", down)
    sink = Sink()
    reply = await service.request_action(
        CFG, "light.turn_on", "light.kitchen", T0, sink
    )
    assert "nothing was run" in reply and "502" in reply and sink.sent == []


@pytest.mark.asyncio
async def test_notify_exception_is_logged_with_traceback(caplog):
    async def boom(text):
        raise RuntimeError("telegram down")

    with caplog.at_level("ERROR", logger="clawrange.homelab"):
        assert await service._send(boom, "x") is False
    assert caplog.records[0].exc_info is not None


def test_bad_local_file_degrades_health_without_leaking_detail(brain_db, tmp_path):
    cfg = HomelabConfig.from_block(
        {"home_assistant": {"local_file": str(tmp_path / "missing.yaml")}}
    )
    anon = service.health(brain_db, cfg, detail=False, now=T0)
    assert anon["status"] == "degraded"
    assert "ha_local_file" in anon["problems"]
    assert "local_error" not in anon["sources"]["ha"]
    full = service.health(brain_db, cfg, detail=True, now=T0)
    assert "cannot be read" in full["sources"]["ha"]["local_error"]
    assert str(tmp_path) not in json.dumps(full)
