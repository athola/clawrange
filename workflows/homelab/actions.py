"""Action policy: which Home Assistant calls may run, and confirmation codes.

Pure: no network, no clock reads (``now`` is injected). The default policy is
deny-all; a profile can only allow-list narrower than the hard deny list.
"""

from __future__ import annotations

import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .parse import safe_label

HARD_DENY_DOMAINS = frozenset(
    {
        "lock",
        "cover",
        "alarm_control_panel",
        "valve",
        "siren",
        "camera",
        "homeassistant",
        "hassio",
        "shell_command",
        "rest_command",
        "python_script",
        "pyscript",
        "recorder",
        "system_log",
    }
)

# Domains whose entities may only be allow-listed by exact id.
EXACT_ONLY_DOMAINS = frozenset({"script", "automation"})

CODE_TTL = timedelta(seconds=120)
MAX_PENDING = 5

_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
_GLOB = re.compile(r"[a-z0-9_]+\.\*")


@dataclass(frozen=True)
class ActionRule:
    service: str  # "domain.service"
    entities: tuple[str, ...]  # entity ids or "domain.*"


def _domain(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def parse_rules(raw: Any) -> tuple[ActionRule, ...]:
    """Validate the profile's ``actions`` list. Raises ValueError."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("actions must be a list")
    rules = []
    for i, item in enumerate(raw):
        where = f"actions[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be a mapping")
        extra = set(item) - {"service", "entities"}
        if extra:
            raise ValueError(f"{where}: unknown keys {sorted(map(str, extra))}")
        service = item.get("service")
        if not isinstance(service, str) or not _ID.fullmatch(service):
            raise ValueError(f"{where}: service must look like 'domain.service'")
        if _domain(service) in HARD_DENY_DOMAINS:
            raise ValueError(f"{where}: service domain {_domain(service)!r} is denied")
        entities = item.get("entities")
        if not isinstance(entities, list) or not entities:
            raise ValueError(f"{where}: entities must be a non-empty list")
        for entity in entities:
            if not isinstance(entity, str):
                raise ValueError(f"{where}: entity ids must be strings")
            is_glob = bool(_GLOB.fullmatch(entity))
            if not is_glob and not _ID.fullmatch(entity):
                raise ValueError(
                    f"{where}: {entity!r} must be an entity id or 'domain.*'"
                )
            domain = _domain(entity)
            if domain in HARD_DENY_DOMAINS:
                raise ValueError(f"{where}: entity domain {domain!r} is denied")
            if is_glob and domain in EXACT_ONLY_DOMAINS:
                raise ValueError(f"{where}: {domain}.* needs exact entity ids")
        rules.append(ActionRule(service, tuple(entities)))
    return tuple(rules)


def authorize(
    rules: tuple[ActionRule, ...], service: str, entity_id: str
) -> str | None:
    """None when allowed, otherwise the reason for the denial."""
    if not isinstance(service, str) or not _ID.fullmatch(service):
        return "malformed service"
    if not isinstance(entity_id, str) or not _ID.fullmatch(entity_id):
        return "malformed entity id"
    for kind, dotted in (("service", service), ("entity", entity_id)):
        domain = _domain(dotted)
        if domain in HARD_DENY_DOMAINS:
            return f"{kind} domain {safe_label(domain)} is never allowed"
    entity_domain = _domain(entity_id)
    for rule in rules:
        if rule.service != service:
            continue
        for pattern in rule.entities:
            if pattern == entity_id:
                return None
            if (
                _GLOB.fullmatch(pattern)
                and _domain(pattern) == entity_domain
                and entity_domain not in EXACT_ONLY_DOMAINS
            ):
                return None
    return "not in the allowlist"


class PendingActions:
    """Single-use confirmation codes for actions awaiting a second message."""

    def __init__(
        self, ttl: timedelta = CODE_TTL, max_pending: int = MAX_PENDING
    ) -> None:
        self._ttl = ttl
        self._max = max_pending
        # code -> (service, entity_id, expires), oldest first
        self._pending: OrderedDict[str, tuple[str, str, datetime]] = OrderedDict()

    def _purge(self, now: datetime) -> None:
        for code in [c for c, (_, _, exp) in self._pending.items() if exp < now]:
            del self._pending[code]

    def issue(self, service: str, entity_id: str, now: datetime) -> str:
        self._purge(now)
        code = f"{secrets.randbelow(1_000_000):06d}"
        while code in self._pending:
            code = f"{secrets.randbelow(1_000_000):06d}"
        self._pending[code] = (service, entity_id, now + self._ttl)
        while len(self._pending) > self._max:
            self._pending.popitem(last=False)
        return code

    def redeem(self, code: str, now: datetime) -> tuple[str, str] | None:
        self._purge(now)
        entry = self._pending.pop(code, None) if isinstance(code, str) else None
        if entry is None:
            return None
        return entry[0], entry[1]
