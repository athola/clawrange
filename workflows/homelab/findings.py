"""The watcher: ``evaluate`` turns an observation into alerts. Pure.

Rules (see docs/homelab-integration-plan.md, "Watch"): the first successful
observation of a source only learns a baseline; a failed source never
changes what was learned; alerts fire on transitions, once.
"""

from __future__ import annotations

import copy
import fnmatch
import re
from datetime import UTC, datetime, timedelta

from .models import (
    SOURCES,
    Alert,
    Census,
    Device,
    Observation,
    WatchConfig,
    WatchState,
)
from .parse import safe_label

SOURCE_LABELS = {
    "ha": "Home Assistant",
    "kuma": "Uptime Kuma",
    "census": "Network census",
}
# Reports count both; only "unavailable" ever alerts ("unknown" is routine
# for a sensor that has not reported yet).
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})
ALERT_STATES = frozenset({"unavailable"})
STALE_PARTS = {
    "ports": "Router port scan",
    "router": "Router info",
    "kev": "KEV feed",
}
FLOOD_NAMES = 5
KEV_NOTE = "KEV lists no affected versions; check MikroTik's advisory."


def is_ignored(entity_id: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(entity_id, p) for p in patterns)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_iso(value: str, fallback: datetime) -> datetime:
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return fallback


def _ports_text(ports: list[int] | tuple[int, ...] | set[int]) -> str:
    return ", ".join(str(p) for p in sorted(ports)) or "none"


def evaluate(
    state: WatchState, obs: Observation, cfg: WatchConfig
) -> tuple[WatchState, list[Alert]]:
    """Return the next state and the alerts this observation produces."""
    new = copy.deepcopy(state)
    alerts: list[Alert] = []
    handlers = {"ha": _ha, "kuma": _kuma, "census": _census}
    for source in SOURCES:
        if source not in obs.configured:
            continue
        if getattr(obs, source) is None:
            _source_failed(new, source, cfg, alerts, obs.errors.get(source, ""))
            continue
        _source_ok(new, source, alerts)
        handlers[source](new, obs, cfg, alerts)
    return new, alerts


# ─── source health ──────────────────────────────────────────────────


_CREDENTIAL_ERROR = re.compile(r"HTTP (401|403)\b")


def _down_text(source: str, count: int, reason: str) -> str:
    """Say why, by class only: the raw reason is never echoed into chat."""
    label = SOURCE_LABELS[source]
    streak = f"{count} failed checks in a row"
    if m := _CREDENTIAL_ERROR.match(reason):
        return (
            f"{label} rejected the credentials (HTTP {m.group(1)}); {streak}. "
            "Check the token and its permissions."
        )
    if reason.startswith("TLS verification failed"):
        return (
            f"{label} TLS verification failed; {streak}. "
            "Check the certificate or ca_cert."
        )
    return f"{label} is unreachable ({streak}). Check the workflows logs."


def _source_failed(
    st: WatchState,
    source: str,
    cfg: WatchConfig,
    alerts: list[Alert],
    reason: str = "",
) -> None:
    st.failures[source] = st.failures.get(source, 0) + 1
    count = st.failures[source]
    if count >= cfg.source_down_after and source not in st.down_alerted:
        st.down_alerted.append(source)
        alerts.append(
            Alert(source, f"{source}:down", _down_text(source, count, reason))
        )


def _source_ok(st: WatchState, source: str, alerts: list[Alert]) -> None:
    st.failures.pop(source, None)
    if source in st.down_alerted:
        st.down_alerted.remove(source)
        alerts.append(
            Alert(source, f"{source}:recovered", f"{SOURCE_LABELS[source]} is back.")
        )


# ─── Home Assistant ─────────────────────────────────────────────────


def _ha(st: WatchState, obs: Observation, cfg: WatchConfig, alerts: list[Alert]):
    assert obs.ha is not None
    first = not st.learned.get("ha")
    st.learned["ha"] = True
    names = {
        e.entity_id: e.name
        for e in obs.ha
        if e.state in ALERT_STATES and not is_ignored(e.entity_id, cfg.ignore_entities)
    }
    bad = set(names)
    st.ha_unavailable_since = {
        k: v for k, v in st.ha_unavailable_since.items() if k in bad
    }
    st.ha_alerted = [k for k in st.ha_alerted if k in bad]
    now_iso = obs.now.isoformat()
    for entity_id in bad:
        st.ha_unavailable_since.setdefault(entity_id, now_iso)
    if first:
        # Already down when the baseline was learned: known-down. It alerts
        # only after it recovers and fails again (recovery clears the mark).
        st.ha_alerted = sorted(bad)
        return
    limit = timedelta(minutes=cfg.unavailable_after_minutes)
    crossed = sorted(
        e
        for e in bad
        if e not in st.ha_alerted
        and obs.now - _parse_iso(st.ha_unavailable_since[e], obs.now) >= limit
    )
    if not crossed:
        return
    st.ha_alerted.extend(crossed)
    minutes = cfg.unavailable_after_minutes
    if len(crossed) > cfg.flood_threshold:
        shown = ", ".join(safe_label(names[e]) for e in crossed[:FLOOD_NAMES])
        more = len(crossed) - FLOOD_NAMES
        tail = f" and {more} more" if more > 0 else ""
        alerts.append(
            Alert(
                "ha",
                "ha:unavailable:flood",
                f"{len(crossed)} Home Assistant entities unavailable for "
                f"{minutes}+ min: {shown}{tail}",
            )
        )
        return
    for entity_id in crossed:
        alerts.append(
            Alert(
                "ha",
                f"ha:unavailable:{entity_id}",
                f"{safe_label(names[entity_id])} ({safe_label(entity_id)}) "
                f"has been unavailable for {minutes}+ min",
            )
        )


# ─── Uptime Kuma ────────────────────────────────────────────────────


def _kuma(st: WatchState, obs: Observation, cfg: WatchConfig, alerts: list[Alert]):
    assert obs.kuma is not None
    first = not st.learned.get("kuma")
    st.learned["kuma"] = True
    for mon in obs.kuma:
        if mon.status not in (0, 1):  # pending, maintenance, unknown: ignore
            continue
        # A monitor first seen after the baseline is assumed to have been up.
        prev = st.kuma_status.get(mon.id, 1)
        st.kuma_status[mon.id] = mon.status
        if first or prev == mon.status:
            continue
        label = safe_label(mon.name)
        if mon.status == 0:
            alerts.append(Alert("kuma", f"kuma:down:{mon.id}", f"{label} is DOWN"))
        else:
            alerts.append(Alert("kuma", f"kuma:up:{mon.id}", f"{label} is back up"))


# ─── census: devices, ports, router, KEV, freshness ─────────────────


def _census(st: WatchState, obs: Observation, cfg: WatchConfig, alerts: list[Alert]):
    cen = obs.census
    assert cen is not None
    first = not st.learned.get("census")
    now_iso = obs.now.isoformat()
    if cen.devices:
        # An empty census is no baseline: the first real one is learned
        # quietly instead of alerting on every device.
        st.learned["census"] = True
        if first:
            st.census_baseline_at = now_iso

    new_devices: list[Device] = []
    for device in sorted(cen.devices, key=lambda d: d.mac):
        if device.mac in st.known_macs:
            continue
        st.known_macs[device.mac] = now_iso
        if first or device.randomized:
            continue
        new_devices.append(device)
    _new_devices(new_devices, cfg, alerts)

    _ports(st, cen.mgmt_ports, alerts)

    if cen.router_version is not None:
        if st.router_version is not None and st.router_version != cen.router_version:
            alerts.append(
                Alert(
                    "census",
                    "census:router",
                    f"RouterOS version changed: {safe_label(st.router_version)} "
                    f"to {safe_label(cen.router_version)}",
                )
            )
        st.router_version = cen.router_version

    if cen.kev is not None:
        version = safe_label(st.router_version or "") or "unknown"
        seen = set(st.seen_kev)
        fresh = sorted(
            (e for e in cen.kev if e.cve_id not in seen), key=lambda e: e.cve_id
        )
        if st.kev_learned:
            for entry in fresh:
                alerts.append(
                    Alert(
                        "census",
                        f"census:kev:{entry.cve_id}",
                        f"New MikroTik KEV entry {safe_label(entry.cve_id)} "
                        f"(added {safe_label(entry.date_added)}): "
                        f"{safe_label(entry.name, 60)}. "
                        f"Router runs {version}. {KEV_NOTE}",
                    )
                )
        st.kev_learned = True
        st.seen_kev = sorted(seen | {e.cve_id for e in cen.kev})

    age = obs.now - _aware(cen.when)
    when_iso = _aware(cen.when).isoformat()
    if age > timedelta(hours=cfg.census_max_age_hours) and (
        st.stale_alerted_when != when_iso
    ):
        st.stale_alerted_when = when_iso
        alerts.append(
            Alert(
                "census",
                "census:stale",
                f"Network census is stale: last run {age.total_seconds() / 3600:.0f}h "
                f"ago (limit {cfg.census_max_age_hours}h). Is the scanner running?",
            )
        )
    _stale_parts(st, cen, obs.now, cfg, alerts)


def _stale_parts(
    st: WatchState,
    cen: Census,
    now: datetime,
    cfg: WatchConfig,
    alerts: list[Alert],
) -> None:
    """Each optional file has its own ``when``; alert once per stale value."""
    limit = timedelta(hours=cfg.census_max_age_hours)
    for part, label in STALE_PARTS.items():
        when = getattr(cen, f"{part}_when")
        if when is None:
            continue
        when_iso = _aware(when).isoformat()
        age = now - _aware(when)
        if age <= limit or st.stale_parts.get(part) == when_iso:
            continue
        st.stale_parts[part] = when_iso
        alerts.append(
            Alert(
                "census",
                f"census:stale:{part}",
                f"{label} is stale: last run {age.total_seconds() / 3600:.0f}h "
                f"ago (limit {cfg.census_max_age_hours}h). Is the scanner running?",
            )
        )


def _new_devices(fresh: list[Device], cfg: WatchConfig, alerts: list[Alert]) -> None:
    if len(fresh) > cfg.flood_threshold:
        shown = ", ".join(safe_label(d.hostname) or d.mac for d in fresh[:FLOOD_NAMES])
        more = len(fresh) - FLOOD_NAMES
        tail = f" and {more} more" if more > 0 else ""
        alerts.append(
            Alert(
                "census",
                "census:new:flood",
                f"{len(fresh)} new devices on the network: {shown}{tail}",
            )
        )
        return
    for device in fresh:
        host = safe_label(device.hostname) or "no hostname"
        alerts.append(
            Alert(
                "census",
                f"census:new:{device.mac}",
                f"New device: {host} ({device.mac}) at {safe_label(device.ip)} "
                f"on {safe_label(device.interface) or 'unknown interface'}",
            )
        )


def _ports(
    st: WatchState, current: tuple[int, ...] | None, alerts: list[Alert]
) -> None:
    if current is None:
        return
    now_ports = sorted(set(current))
    if st.baseline_ports is None:
        st.baseline_ports = now_ports
        st.alerted_ports = None
        return
    if now_ports == st.baseline_ports:
        if st.alerted_ports is not None:
            st.alerted_ports = None
            alerts.append(
                Alert(
                    "census",
                    "census:ports:recovered",
                    "Router management ports are back to baseline: "
                    f"{_ports_text(now_ports)}",
                )
            )
        return
    if st.alerted_ports == now_ports:
        return
    st.alerted_ports = now_ports
    base = set(st.baseline_ports)
    alerts.append(
        Alert(
            "census",
            "census:ports",
            "Router management ports changed: "
            f"opened {_ports_text(set(now_ports) - base)}; "
            f"closed {_ports_text(base - set(now_ports))} "
            f"(baseline {_ports_text(base)})",
        )
    )
