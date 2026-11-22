"""Data shapes shared by the homelab parsers, watcher, reports and actions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SOURCES = ("ha", "kuma", "census")


@dataclass(frozen=True)
class Device:
    mac: str  # uppercase, colon separated
    ip: str
    hostname: str
    interface: str
    status: str
    in_lease: bool

    @property
    def randomized(self) -> bool:
        """True for a locally administered (privacy/randomized) MAC."""
        try:
            return bool(int(self.mac[:2], 16) & 0x02)
        except ValueError:
            return False


@dataclass(frozen=True)
class KevEntry:
    cve_id: str
    date_added: str
    product: str
    name: str
    description: str


@dataclass(frozen=True)
class Census:
    when: datetime
    devices: tuple[Device, ...]
    mgmt_ports: tuple[int, ...] | None = None
    router_version: str | None = None
    kev: tuple[KevEntry, ...] | None = None
    # Each optional file's own ``when`` (None: not published or no usable time).
    ports_when: datetime | None = None
    router_when: datetime | None = None
    kev_when: datetime | None = None
    # Optional parts that exist but could not be read: part name -> reason.
    part_errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityState:
    entity_id: str
    state: str
    name: str
    device_class: str | None = None
    unit: str | None = None
    last_changed: datetime | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass(frozen=True)
class Monitor:
    """Uptime Kuma monitor. status: 1 up, 0 down, 2 pending, 3 maintenance."""

    id: str
    name: str
    status: int | None = None
    uptime_24h: float | None = None


@dataclass(frozen=True)
class Observation:
    """One pass over the sources.

    A source that is configured and failed has value None and an entry in
    ``errors``; an unconfigured source is simply absent from ``configured``.
    """

    now: datetime
    configured: frozenset[str]
    ha: tuple[EntityState, ...] | None = None
    kuma: tuple[Monitor, ...] | None = None
    census: Census | None = None
    errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchConfig:
    battery_low: int = 20
    unavailable_after_minutes: int = 30
    flood_threshold: int = 10
    ignore_entities: tuple[str, ...] = ()
    census_max_age_hours: int = 13
    source_down_after: int = 3


@dataclass(frozen=True)
class Alert:
    source: str
    key: str
    text: str


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return list(value) if isinstance(value, list | tuple | set | frozenset) else []


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ports(value: Any) -> list[int] | None:
    if not isinstance(value, list | tuple):
        return None
    return [p for p in value if _is_int(p)]


@dataclass
class WatchState:
    """What the watcher remembers between passes (JSON-safe via to_dict).

    ``evaluate`` never mutates the state it is given; it returns a copy.
    """

    learned: dict[str, bool] = field(default_factory=dict)
    # census
    known_macs: dict[str, str] = field(default_factory=dict)  # mac -> first seen
    census_baseline_at: str | None = None
    baseline_ports: list[int] | None = None
    alerted_ports: list[int] | None = None
    router_version: str | None = None
    kev_learned: bool = False
    seen_kev: list[str] = field(default_factory=list)
    stale_alerted_when: str | None = None
    stale_parts: dict[str, str] = field(default_factory=dict)  # part -> when alerted
    # kuma
    kuma_status: dict[str, int] = field(default_factory=dict)
    # ha
    ha_unavailable_since: dict[str, str] = field(default_factory=dict)
    ha_alerted: list[str] = field(default_factory=list)
    # source health
    failures: dict[str, int] = field(default_factory=dict)
    down_alerted: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "learned": dict(self.learned),
            "known_macs": dict(self.known_macs),
            "census_baseline_at": self.census_baseline_at,
            "baseline_ports": (
                None if self.baseline_ports is None else list(self.baseline_ports)
            ),
            "alerted_ports": (
                None if self.alerted_ports is None else list(self.alerted_ports)
            ),
            "router_version": self.router_version,
            "kev_learned": self.kev_learned,
            "seen_kev": list(self.seen_kev),
            "stale_alerted_when": self.stale_alerted_when,
            "stale_parts": dict(self.stale_parts),
            "kuma_status": dict(self.kuma_status),
            "ha_unavailable_since": dict(self.ha_unavailable_since),
            "ha_alerted": list(self.ha_alerted),
            "failures": dict(self.failures),
            "down_alerted": list(self.down_alerted),
        }

    @classmethod
    def from_dict(cls, data: Any) -> WatchState:
        """Tolerant loader: missing, unknown or mistyped keys fall back."""
        d = _dict(data)
        return cls(
            learned={str(k): bool(v) for k, v in _dict(d.get("learned")).items()},
            known_macs={str(k): str(v) for k, v in _dict(d.get("known_macs")).items()},
            census_baseline_at=_opt_str(d.get("census_baseline_at")),
            baseline_ports=_ports(d.get("baseline_ports")),
            alerted_ports=_ports(d.get("alerted_ports")),
            router_version=_opt_str(d.get("router_version")),
            kev_learned=bool(d.get("kev_learned", False)),
            seen_kev=[str(x) for x in _list(d.get("seen_kev"))],
            stale_alerted_when=_opt_str(d.get("stale_alerted_when")),
            stale_parts={
                str(k): str(v) for k, v in _dict(d.get("stale_parts")).items()
            },
            kuma_status={
                str(k): v for k, v in _dict(d.get("kuma_status")).items() if _is_int(v)
            },
            ha_unavailable_since={
                str(k): str(v) for k, v in _dict(d.get("ha_unavailable_since")).items()
            },
            ha_alerted=[str(x) for x in _list(d.get("ha_alerted"))],
            failures={
                str(k): v for k, v in _dict(d.get("failures")).items() if _is_int(v)
            },
            down_alerted=[str(x) for x in _list(d.get("down_alerted"))],
        )
