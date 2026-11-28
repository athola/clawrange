"""Pure parsers: raw source payloads in, typed values out. No I/O."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from .models import Device, EntityState, KevEntry, Monitor

# ─── RouterOS CLI tables ────────────────────────────────────────────

_ROW_START = re.compile(r"^\s*\d+(?:\s|$)")
_PREFIX = re.compile(r"^\s*(\d+)\s*([A-Za-z]*)\s*$")
_LOOSE_PREFIX = re.compile(r"^\s*(\d+)(?:\s+([A-Z]+)(?=\s))?\s*")
_MAC = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


def _parse_header(line: str) -> tuple[list[str], list[int]] | None:
    """Column names and start offsets of a ` #   ADDRESS  ...` header line."""
    tokens = list(re.finditer(r"\S+", line))
    if not tokens or tokens[0].group() != "#" or len(tokens) < 2:
        return None
    rest = tokens[1:]
    return [t.group() for t in rest], [t.start() for t in rest]


def _aligned(line: str, starts: list[int]) -> bool:
    """True when every column boundary sits on a space, as RouterOS pads."""
    for start in starts:
        if 0 < start <= len(line) and line[start - 1] != " ":
            return False
    return True


def _parse_row(line: str, names: list[str], starts: list[int]) -> dict[str, str] | None:
    if _aligned(line, starts):
        match = _PREFIX.match(line[: starts[0]])
        if match:
            cells = []
            for i, start in enumerate(starts):
                end = starts[i + 1] if i + 1 < len(starts) else len(line)
                cells.append(line[start:end].strip())
            return {
                "#": match.group(1),
                "flags": match.group(2),
                **dict(zip(names, cells, strict=True)),
            }
    # Last resort: a value outran its column. Split on whitespace.
    loose = _LOOSE_PREFIX.match(line)
    if not loose:
        return None
    parts = line[loose.end() :].split(None, len(names) - 1)
    parts += [""] * (len(names) - len(parts))
    return {
        "#": loose.group(1),
        "flags": loose.group(2) or "",
        **dict(zip(names, (p.strip() for p in parts), strict=True)),
    }


def _tables(raw: str) -> list[tuple[list[str], list[dict[str, str]]]]:
    tables: list[tuple[list[str], list[dict[str, str]]]] = []
    header: tuple[list[str], list[int]] | None = None
    for line in raw.splitlines():
        line = line.rstrip()
        parsed = _parse_header(line) if line.lstrip().startswith("#") else None
        if parsed:
            header = parsed
            tables.append((parsed[0], []))
            continue
        if header and _ROW_START.match(line):
            row = _parse_row(line, *header)
            if row:
                tables[-1][1].append(row)
    return tables


def parse_routeros_tables(raw: str) -> list[list[dict[str, str]]]:
    """Parse RouterOS 7 ``print`` output; one list of row dicts per table.

    Each row maps column name to cell text, plus ``#`` (the index) and
    ``flags`` (the letters between index and first column).
    """
    return [rows for _, rows in _tables(raw)]


def _norm_mac(value: str) -> str | None:
    value = value.strip()
    return value.upper() if _MAC.match(value) else None


def devices_from_census(raw: str) -> tuple[Device, ...]:
    """Merge the DHCP lease and ARP tables by MAC, sorted by MAC."""
    leases: dict[str, dict[str, str]] = {}
    arp: dict[str, dict[str, str]] = {}
    for columns, rows in _tables(raw):
        for row in rows:
            mac = _norm_mac(row.get("MAC-ADDRESS", ""))
            if not mac:
                continue
            if "HOST-NAME" in columns:
                prev = leases.get(mac)
                if prev is None:
                    leases[mac] = row
                elif not prev.get("HOST-NAME") and row.get("HOST-NAME"):
                    leases[mac] = row
            if "INTERFACE" in columns:
                arp.setdefault(mac, row)
    devices = []
    for mac in sorted(leases.keys() | arp.keys()):
        lease = leases.get(mac, {})
        entry = arp.get(mac, {})
        devices.append(
            Device(
                mac=mac,
                ip=entry.get("ADDRESS") or lease.get("ADDRESS", ""),
                hostname=lease.get("HOST-NAME", ""),
                interface=entry.get("INTERFACE", ""),
                status=entry.get("STATUS", ""),
                in_lease=mac in leases,
            )
        )
    return tuple(devices)


def has_mac_table(raw: str) -> bool:
    """True when the output holds at least one table with a MAC-ADDRESS column."""
    return any("MAC-ADDRESS" in columns for columns, _ in _tables(raw))


def parse_system_resource(raw: str) -> dict[str, str]:
    """``key: value`` lines of ``/system resource print``."""
    out: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out.setdefault(key.strip(), value.strip())
    return out


def router_version(raw: str) -> str | None:
    return parse_system_resource(raw).get("version") or None


# ─── scanner / feed JSON ────────────────────────────────────────────

_OPEN_ENTRY = re.compile(r"->\s*\[([^\]]*)\]")


def _entry_ports(entry: Any) -> set[int] | None:
    """The ports of one ``"<ip> -> [22,53]"`` entry, None when malformed."""
    match = _OPEN_ENTRY.search(entry) if isinstance(entry, str) else None
    if not match:
        return None
    ports: set[int] = set()
    for part in match.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        if not (part.isascii() and part.isdigit()) or not 0 < int(part) < 65536:
            return None
        ports.add(int(part))
    return ports


def mgmt_ports_skipping(scan: Any) -> tuple[tuple[int, ...] | None, int]:
    """Open ports plus the number of malformed entries that were skipped.

    The ports are None when the payload itself has the wrong shape.
    """
    if not isinstance(scan, dict) or not isinstance(scan.get("open"), list):
        return None, 0
    ports: set[int] = set()
    skipped = 0
    for entry in scan["open"]:
        found = _entry_ports(entry)
        if found is None:
            skipped += 1
        else:
            ports |= found
    return tuple(sorted(ports)), skipped


def parse_mgmt_ports(scan: Any) -> tuple[int, ...] | None:
    """Sorted unique open ports from ``{"open": ["<ip> -> [22,53]"]}``.

    None when the payload or any entry is malformed.
    """
    ports, skipped = mgmt_ports_skipping(scan)
    return None if skipped else ports


def kev_entries(feed: Any) -> tuple[KevEntry, ...]:
    """MikroTik entries of a CISA KEV feed (or a list of entries).

    KEV carries no affected-version data, so nothing here says a router is
    vulnerable; callers must only report that an entry exists.
    """
    if isinstance(feed, dict):
        # CISA's own feed, or the scanner's {"when", "entries": [...]} file.
        rows = feed.get("vulnerabilities", feed.get("entries"))
    else:
        rows = feed
    if not isinstance(rows, list):
        return ()
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("cveID"):
            continue
        if str(row.get("vendorProject", "")).strip().lower() != "mikrotik":
            continue
        out.append(
            KevEntry(
                cve_id=str(row["cveID"]),
                date_added=str(row.get("dateAdded", "")),
                product=str(row.get("product", "")),
                name=str(row.get("vulnerabilityName", "")),
                description=str(row.get("shortDescription", "")),
            )
        )
    return tuple(sorted(out, key=lambda e: e.cve_id))


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


parse_time = _parse_time


def entities_from_states(states: Any) -> tuple[EntityState, ...]:
    """Home Assistant ``/api/states`` payload to entity states."""
    if not isinstance(states, list):
        return ()
    out = []
    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            continue
        attrs = item.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        device_class = attrs.get("device_class")
        unit = attrs.get("unit_of_measurement")
        out.append(
            EntityState(
                entity_id=entity_id,
                state=str(item.get("state", "")),
                name=str(attrs.get("friendly_name") or entity_id),
                device_class=device_class if isinstance(device_class, str) else None,
                unit=unit if isinstance(unit, str) else None,
                last_changed=_parse_time(item.get("last_changed")),
            )
        )
    return tuple(out)


def monitors_from_kuma(page: Any, heartbeat: Any) -> tuple[Monitor, ...]:
    """Uptime Kuma status-page and heartbeat payloads to monitors."""
    groups = page.get("publicGroupList") if isinstance(page, dict) else None
    beats = heartbeat.get("heartbeatList") if isinstance(heartbeat, dict) else None
    uptimes = heartbeat.get("uptimeList") if isinstance(heartbeat, dict) else None
    beats = beats if isinstance(beats, dict) else {}
    uptimes = uptimes if isinstance(uptimes, dict) else {}
    out: list[Monitor] = []
    seen: set[str] = set()
    for group in groups if isinstance(groups, list) else []:
        listed = group.get("monitorList") if isinstance(group, dict) else None
        for mon in listed if isinstance(listed, list) else []:
            if not isinstance(mon, dict) or mon.get("id") is None:
                continue
            mon_id = str(mon["id"])
            if mon_id in seen:
                continue
            seen.add(mon_id)
            history = beats.get(mon_id)
            last = history[-1] if isinstance(history, list) and history else None
            status = last.get("status") if isinstance(last, dict) else None
            uptime = uptimes.get(f"{mon_id}_24")
            out.append(
                Monitor(
                    id=mon_id,
                    name=str(mon.get("name") or mon_id),
                    status=status
                    if isinstance(status, int) and not isinstance(status, bool)
                    else None,
                    uptime_24h=float(uptime)
                    if isinstance(uptime, int | float) and not isinstance(uptime, bool)
                    else None,
                )
            )
    return tuple(out)


# ─── output sanitising ──────────────────────────────────────────────

_MD_SPECIALS = "_*`["


def safe_label(text: str, limit: int = 40) -> str:
    """Make an attacker-chosen string safe to put in a Telegram message.

    Control characters go, whitespace collapses, the result is capped at
    ``limit`` characters (ellipsis included, counted before escaping), and
    legacy Markdown specials are backslash-escaped so one odd hostname cannot
    make Telegram reject the whole message.
    """
    if not isinstance(text, str):
        return ""
    spaced = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    clean = " ".join(spaced.split())
    if len(clean) > limit:
        clean = clean[: max(limit - 1, 0)].rstrip() + "…" if limit > 0 else ""
    return "".join("\\" + ch if ch in _MD_SPECIALS else ch for ch in clean)
