"""Homelab settings built from the profile's (env-resolved) ``homelab`` block.

An unset env var resolves to ``""`` in the profile loader, so a source whose
url (and, for Home Assistant, token) is empty is simply not configured:
nothing is fetched, nothing alerts and nothing is an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .actions import ActionRule, parse_rules
from .models import WatchConfig

DEFAULT_KUMA_SLUG = "home"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _map(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class HomelabConfig:
    ha_url: str = ""
    ha_token: str = ""
    kuma_url: str = ""
    kuma_slug: str = DEFAULT_KUMA_SLUG
    kuma_ca_cert: str = ""
    census_url: str = ""
    watch: WatchConfig = field(default_factory=WatchConfig)
    rules: tuple[ActionRule, ...] = ()

    @property
    def configured(self) -> frozenset[str]:
        found = set()
        if self.ha_url and self.ha_token:
            found.add("ha")
        if self.kuma_url:
            found.add("kuma")
        if self.census_url:
            found.add("census")
        return frozenset(found)

    @classmethod
    def from_block(cls, block: Any) -> HomelabConfig:
        """Build from a validated block; a missing block configures nothing."""
        block = _map(block)
        ha = _map(block.get("home_assistant"))
        kuma = _map(block.get("kuma"))
        census = _map(block.get("census"))
        defaults = WatchConfig()
        ignore = ha.get("ignore_entities")
        ignore_entities = (
            tuple(x for x in ignore if isinstance(x, str) and x)
            if isinstance(ignore, list)
            else ()
        )
        watch = WatchConfig(
            battery_low=_int(ha.get("battery_low"), defaults.battery_low),
            unavailable_after_minutes=_int(
                ha.get("unavailable_after_minutes"), defaults.unavailable_after_minutes
            ),
            flood_threshold=_int(ha.get("flood_threshold"), defaults.flood_threshold),
            ignore_entities=ignore_entities,
            census_max_age_hours=_int(
                census.get("max_age_hours"), defaults.census_max_age_hours
            ),
            source_down_after=_int(
                block.get("source_down_after"), defaults.source_down_after
            ),
        )
        try:
            rules = parse_rules(ha.get("actions"))
        except ValueError:
            rules = ()  # validation rejects this at load; fail closed here
        return cls(
            ha_url=_text(ha.get("url")).rstrip("/"),
            ha_token=_text(ha.get("token")),
            kuma_url=_text(kuma.get("url")).rstrip("/"),
            kuma_slug=_text(kuma.get("slug")) or DEFAULT_KUMA_SLUG,
            kuma_ca_cert=_text(kuma.get("ca_cert")),
            census_url=_text(census.get("url")).rstrip("/"),
            watch=watch,
            rules=rules,
        )

    @classmethod
    def from_profile(cls, profile: Any) -> HomelabConfig:
        return cls.from_block(getattr(profile, "homelab", None))
