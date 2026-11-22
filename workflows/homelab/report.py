"""Deterministic text renderers for `!home`, `!net` and the daily digest.

Plain text, short lines, safe for Telegram legacy Markdown: every string that
came from a device, entity or monitor goes through ``safe_label`` and this
module adds no Markdown of its own. An outage is shown as an outage, never as
an empty result.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .findings import KEV_NOTE, SOURCE_LABELS, UNAVAILABLE_STATES, is_ignored
from .models import SOURCES, EntityState, Observation, WatchConfig, WatchState
from .parse import safe_label

LIST_CAP = 5
KEV_CAP = 10


def _more(shown: list[str], total: int) -> str:
    text = ", ".join(shown)
    return f"{text} and {total - len(shown)} more" if total > len(shown) else text


def _status_line(obs: Observation, source: str) -> str | None:
    """A one-line stand-in when the source has no data, else None."""
    if source not in obs.configured:
        return f"{SOURCE_LABELS[source]}: not configured"
    if getattr(obs, source) is None:
        return f"{SOURCE_LABELS[source]}: unreachable, no data (outage)"
    return None


def _battery_level(entity: EntityState) -> float | None:
    if entity.device_class != "battery":
        return None
    try:
        return float(entity.state)
    except ValueError:
        return None


def render_home(obs: Observation, cfg: WatchConfig) -> str:
    lines: list[str] = []
    line = _status_line(obs, "ha")
    if line:
        lines.append(line)
    else:
        assert obs.ha is not None
        live = [e for e in obs.ha if not is_ignored(e.entity_id, cfg.ignore_entities)]
        down = sorted(
            (e for e in live if e.state in UNAVAILABLE_STATES),
            key=lambda e: e.entity_id,
        )
        lines.append(f"Home Assistant: {len(obs.ha)} entities, {len(down)} unavailable")
        if down:
            names = [safe_label(e.name) for e in down]
            lines.append("Unavailable: " + _more(names[:LIST_CAP], len(names)))
        low = sorted(
            (
                (level, e)
                for e in live
                if (level := _battery_level(e)) is not None and level < cfg.battery_low
            ),
            key=lambda pair: (pair[0], pair[1].entity_id),
        )
        if low:
            shown = [f"{safe_label(e.name)} {level:g}%" for level, e in low]
            lines.append(
                f"Low batteries (under {cfg.battery_low}%): "
                + _more(shown[:KEV_CAP], len(shown))
            )
        updates = sorted(
            (e for e in live if e.domain == "update" and e.state == "on"),
            key=lambda e: e.entity_id,
        )
        if updates:
            names = [safe_label(e.name) for e in updates]
            lines.append(
                f"Updates pending: {len(updates)} "
                f"({_more(names[:LIST_CAP], len(names))})"
            )
    line = _status_line(obs, "kuma")
    if line:
        lines.append(line)
    else:
        assert obs.kuma is not None
        up = sum(1 for m in obs.kuma if m.status == 1)
        down_mons = [m for m in obs.kuma if m.status == 0]
        other = sum(1 for m in obs.kuma if m.status not in (0, 1))
        extra = f", {other} pending or in maintenance" if other else ""
        lines.append(f"Uptime Kuma: {up} of {len(obs.kuma)} up{extra}")
        if down_mons:
            names = [safe_label(m.name) for m in down_mons]
            lines.append("Down: " + _more(names[:LIST_CAP], len(names)))
    return "\n".join(lines)


def render_entity_search(obs: Observation, query: str, limit: int = 15) -> str:
    line = _status_line(obs, "ha")
    if line:
        return line
    assert obs.ha is not None
    needle = query.strip().lower()
    hits = sorted(
        (
            e
            for e in obs.ha
            if needle in e.entity_id.lower() or needle in e.name.lower()
        ),
        key=lambda e: e.entity_id,
    )
    label = safe_label(query, 30)
    if not hits:
        return f"No entity matches {label}"
    lines = [f"{len(hits)} match(es) for {label}"]
    for e in hits[:limit]:
        unit = f" {safe_label(e.unit, 8)}" if e.unit else ""
        lines.append(
            f"{safe_label(e.name)} ({safe_label(e.entity_id)}): "
            f"{safe_label(e.state, 30)}{unit}"
        )
    if len(hits) > limit:
        lines.append(f"...and {len(hits) - limit} more, narrow the search")
    return "\n".join(lines)


def _new_since_baseline(state: WatchState) -> int | None:
    if not state.census_baseline_at:
        return None
    try:
        base = datetime.fromisoformat(state.census_baseline_at)
        return sum(
            1
            for seen in state.known_macs.values()
            if datetime.fromisoformat(seen) > base
        )
    except ValueError:
        return None


def _ports(ports: list[int] | tuple[int, ...] | None) -> str:
    return ", ".join(str(p) for p in ports) if ports else "none"


def render_net(obs: Observation, state: WatchState) -> str:
    line = _status_line(obs, "census")
    if line:
        return line
    cen = obs.census
    assert cen is not None
    when = cen.when if cen.when.tzinfo else cen.when.replace(tzinfo=UTC)
    age_h = (obs.now - when).total_seconds() / 3600
    random_n = sum(1 for d in cen.devices if d.randomized)
    lines = [
        f"Census: {age_h:.1f}h old",
        f"Devices: {len(cen.devices)} "
        f"({len(cen.devices) - random_n} universal, {random_n} randomized)",
    ]
    new = _new_since_baseline(state)
    lines.append(
        "New since baseline: "
        + ("baseline not learned yet" if new is None else str(new))
    )
    if cen.mgmt_ports is None:
        lines.append("Management ports: not scanned")
    elif state.baseline_ports is None:
        lines.append(f"Management ports: {_ports(cen.mgmt_ports)} (no baseline yet)")
    elif list(cen.mgmt_ports) == state.baseline_ports:
        lines.append(f"Management ports: {_ports(cen.mgmt_ports)} (matches baseline)")
    else:
        lines.append(
            f"Management ports: {_ports(cen.mgmt_ports)} "
            f"(baseline {_ports(state.baseline_ports)})"
        )
    version = safe_label(cen.router_version or "") or "unknown"
    lines.append(f"Router version: {version}")
    if cen.kev is None:
        lines.append("MikroTik KEV: feed not available")
    elif not cen.kev:
        lines.append("MikroTik KEV: no entries")
    else:
        lines.append(f"MikroTik KEV: {len(cen.kev)} entries")
        for entry in cen.kev[:KEV_CAP]:
            lines.append(
                f"{safe_label(entry.cve_id)} ({safe_label(entry.date_added)}): "
                f"{safe_label(entry.name, 60)}"
            )
        if len(cen.kev) > KEV_CAP:
            lines.append(f"...and {len(cen.kev) - KEV_CAP} more")
        lines.append(KEV_NOTE)
    return "\n".join(lines)


def render_report(obs: Observation, state: WatchState, cfg: WatchConfig) -> str:
    if not obs.configured:
        return "Homelab: no sources configured"
    lines = ["Homelab daily report"]
    for source in SOURCES:
        if source in obs.configured and getattr(obs, source) is None:
            detail = safe_label(obs.errors.get(source, ""), 80) or "no detail"
            fails = state.failures.get(source, 0)
            lines.append(
                f"OUTAGE {SOURCE_LABELS[source]}: {detail} ({fails} failed checks)"
            )
    lines.append(render_home(obs, cfg))
    lines.append(render_net(obs, state))
    if obs.kuma:
        worst = sorted(
            (m for m in obs.kuma if m.uptime_24h is not None and m.uptime_24h < 1),
            key=lambda m: (m.uptime_24h or 0, m.id),
        )[:3]
        if worst:
            lines.append(
                "Lowest 24h uptime: "
                + ", ".join(
                    f"{safe_label(m.name)} {(m.uptime_24h or 0) * 100:.1f}%"
                    for m in worst
                )
            )
    return "\n".join(lines)
