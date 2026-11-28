"""Homelab settings built from the profile's (env-resolved) ``homelab`` block.

An unset env var resolves to ``""`` in the profile loader, so a source whose
url (and, for Home Assistant, token) is empty is simply not configured:
nothing is fetched, nothing alerts and nothing is an error.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .actions import ActionRule, parse_rules
from .models import WatchConfig

DEFAULT_KUMA_SLUG = "home"

logger = logging.getLogger("clawrange.homelab")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _map(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(x for x in value if isinstance(x, str) and x)


LOCAL_KEYS = frozenset({"ignore_entities", "actions", "kev_reviewed"})
_CVE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True)
class _Local:
    ignores: tuple[str, ...] = ()
    rules: tuple[ActionRule, ...] = ()
    kev_reviewed: frozenset[str] = frozenset()
    error: str = ""


def _read_local(path: str) -> _Local:
    """Load the operator-private ``local_file``.

    The file holds real entity ids, so it lives under the gitignored
    ``config/homelab/`` instead of the committed profile. It can only add to
    the profile's lists. Any problem discards the whole file (fail closed:
    none of its actions are allowed) and returns an error that names the
    problem but never the file's contents or path.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return _Local(error="local_file cannot be read")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        return _Local(error=f"local_file is not valid YAML{where}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return _Local(error="local_file must be a mapping")
    unknown = set(map(str, data)) - LOCAL_KEYS
    if unknown:
        return _Local(
            error=f"local_file has unknown keys (known: {sorted(LOCAL_KEYS)})"
        )
    ignore = data.get("ignore_entities")
    if ignore is not None and (
        not isinstance(ignore, list) or not all(isinstance(x, str) for x in ignore)
    ):
        return _Local(error="local_file ignore_entities must be a list of entity ids")
    try:
        rules = parse_rules(data.get("actions"))
    except ValueError as exc:
        # parse_rules quotes the offending value; keep the reason, drop it.
        return _Local(error="local_file " + re.sub(r"'[^']*'", "'…'", str(exc)))
    reviewed = data.get("kev_reviewed")
    if reviewed is not None and (
        not isinstance(reviewed, list)
        or not all(isinstance(x, str) and _CVE.fullmatch(x) for x in reviewed)
    ):
        return _Local(error="local_file kev_reviewed must be a list of CVE ids")
    return _Local(
        ignores=_str_list(ignore),
        rules=rules,
        kev_reviewed=frozenset(x.upper() for x in reviewed or ()),
    )


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
    # Sources with some but not all of their settings: they stay off, and
    # health names them so a half-set env var is not mistaken for "unused".
    incomplete: frozenset[str] = frozenset()
    # Why home_assistant.local_file was discarded ("" when fine or unset).
    local_error: str = ""

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
        ignore_entities = _str_list(ha.get("ignore_entities"))
        local_path = _text(ha.get("local_file"))
        local = _read_local(local_path) if local_path else _Local()
        watch = WatchConfig(
            battery_low=_int(ha.get("battery_low"), defaults.battery_low),
            unavailable_after_minutes=_int(
                ha.get("unavailable_after_minutes"), defaults.unavailable_after_minutes
            ),
            flood_threshold=_int(ha.get("flood_threshold"), defaults.flood_threshold),
            ignore_entities=ignore_entities + local.ignores,
            kev_reviewed=local.kev_reviewed,
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
        ha_url, ha_token = _text(ha.get("url")), _text(ha.get("token"))
        kuma_url, ca_cert = _text(kuma.get("url")), _text(kuma.get("ca_cert"))
        incomplete = set()
        if bool(ha_url) != bool(ha_token):
            incomplete.add("ha")
        if ca_cert and not kuma_url:
            incomplete.add("kuma")
        return cls(
            ha_url=ha_url.rstrip("/"),
            ha_token=ha_token,
            kuma_url=kuma_url.rstrip("/"),
            kuma_slug=_text(kuma.get("slug")) or DEFAULT_KUMA_SLUG,
            kuma_ca_cert=ca_cert,
            census_url=_text(census.get("url")).rstrip("/"),
            watch=watch,
            rules=rules + local.rules,
            incomplete=frozenset(incomplete),
            local_error=local.error,
        )

    @classmethod
    def from_profile(cls, profile: Any) -> HomelabConfig:
        return cls.from_block(getattr(profile, "homelab", None))


_MISSING = {
    "ha": "needs both a url and a token",
    "kuma": "has a ca_cert but no url",
}


def warn_incomplete(cfg: HomelabConfig) -> None:
    """Log (never the values) each half-configured source; call once at mount."""
    for source in sorted(cfg.incomplete):
        logger.warning(
            "homelab: %s is only partly configured (%s); it stays disabled",
            source,
            _MISSING[source],
        )
