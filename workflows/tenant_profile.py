"""Tenant profile loader — the declarative layer that owns the
tenant-specific surface (persona, seeds, connector wiring, CRM config).

A profile lives at ``config/profiles/<name>/profile.yaml``. The active
profile is chosen by the ``CLAWRANGE_PROFILE`` env var (default
``marketing``), so a fresh clone configures behavior by editing YAML
rather than Python.

Named ``tenant_profile`` (not ``profile``) deliberately: a top-level
module called ``profile`` would shadow the Python stdlib ``profile``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("clawrange.profile")

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

# Primitive kinds the validator recognizes. The connector registry and CRM
# adapter factory are the runtime source of truth; these mirror them so a
# profile fails fast at load time instead of at first fire.
KNOWN_SOURCE_KINDS = {"http_csv", "login_scrape"}
KNOWN_SINK_KINDS = {"crm"}
KNOWN_TRANSFORM_KINDS = {"leads_clean", "passthrough"}
KNOWN_ADAPTERS = {"sqlite", "rest"}
KNOWN_AUTH_KINDS = {"none", "api_key", "bearer", "basic", "login_form"}


class ProfileError(ValueError):
    """Raised when a profile is missing, malformed, or internally inconsistent."""


def default_profiles_dir() -> Path:
    """Resolve the profiles base directory.

    Honors ``CLAWRANGE_PROFILES_DIR`` (used in containers where the repo
    root differs); otherwise defaults to ``<repo>/config/profiles`` relative
    to this file (``workflows/`` → repo root is its parent).
    """
    env = os.environ.get("CLAWRANGE_PROFILES_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config" / "profiles"


def resolve_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """Recursively substitute ``${VAR}`` tokens from ``env``.

    An unset or empty variable resolves to the empty string and logs a
    warning — matching the marketing scanners' graceful-degradation
    posture (a missing credential disables a feature, it does not crash
    boot).
    """
    if env is None:
        env = dict(os.environ)

    if isinstance(value, str):

        def _repl(m: re.Match) -> str:
            var = m.group(1)
            val = env.get(var, "")
            if val == "":
                logger.warning("profile: unresolved env var ${%s} -> ''", var)
            return val

        return _ENV_RE.sub(_repl, value)
    if isinstance(value, dict):
        return {k: resolve_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env(v, env) for v in value]
    return value


@dataclass
class Profile:
    """Parsed, env-resolved tenant profile."""

    name: str
    raw: dict

    @property
    def assistant(self) -> dict:
        return self.raw.get("assistant") or {}

    @property
    def seeds(self) -> dict:
        return self.raw.get("seeds") or {}

    @property
    def projects(self) -> list[dict]:
        return self.seeds.get("projects") or []

    @property
    def schedules(self) -> list[dict]:
        return self.seeds.get("schedules") or []

    @property
    def connectors(self) -> list[dict]:
        return self.raw.get("connectors") or []

    @property
    def crm(self) -> dict | None:
        return self.raw.get("crm")

    def connector(self, connector_id: str) -> dict | None:
        for c in self.connectors:
            if c.get("id") == connector_id:
                return c
        return None

    def query_templates(self) -> list[dict]:
        return (self.crm or {}).get("query_templates") or []


def validate(
    profile: Profile,
    *,
    known_generator_kinds: set[str] | None = None,
) -> None:
    """Validate structural invariants. Raises ``ProfileError`` on any breach.

    ``known_generator_kinds`` defaults to the live ``GENERATORS`` registry
    (lazy-imported to avoid an import cycle); tests pass an explicit set.
    """
    raw = profile.raw
    if "profile" not in raw:
        raise ProfileError("profile is missing the required 'profile' key")
    if raw["profile"] != profile.name:
        raise ProfileError(
            f"profile key '{raw['profile']}' does not match directory "
            f"name '{profile.name}'"
        )

    if known_generator_kinds is None:
        from generators import GENERATORS

        known_generator_kinds = set(GENERATORS)

    connector_ids = {c.get("id") for c in profile.connectors}

    for c in profile.connectors:
        src = c.get("source") or {}
        if src.get("kind") not in KNOWN_SOURCE_KINDS:
            raise ProfileError(
                f"connector '{c.get('id')}' has unknown source kind "
                f"'{src.get('kind')}' (known: {sorted(KNOWN_SOURCE_KINDS)})"
            )
        auth = (src.get("auth") or {}).get("kind", "none")
        if auth not in KNOWN_AUTH_KINDS:
            raise ProfileError(
                f"connector '{c.get('id')}' has unknown auth kind '{auth}'"
            )
        transform = c.get("transform")
        if transform and transform.get("kind") not in KNOWN_TRANSFORM_KINDS:
            raise ProfileError(
                f"connector '{c.get('id')}' has unknown transform kind "
                f"'{transform.get('kind')}'"
            )
        sink = c.get("sink") or {}
        if sink.get("kind") not in KNOWN_SINK_KINDS:
            raise ProfileError(
                f"connector '{c.get('id')}' has unknown sink kind '{sink.get('kind')}'"
            )

    crm = profile.crm
    if crm is not None:
        adapter = crm.get("adapter")
        if adapter not in KNOWN_ADAPTERS:
            raise ProfileError(
                f"crm.adapter '{adapter}' is unknown (known: {sorted(KNOWN_ADAPTERS)})"
            )

    for s in profile.schedules:
        kind = s.get("kind")
        if kind not in known_generator_kinds:
            raise ProfileError(
                f"schedule '{s.get('id')}' has kind '{kind}' not in "
                f"GENERATORS ({sorted(known_generator_kinds)})"
            )
        connector_ref = (s.get("kwargs") or {}).get("connector")
        if connector_ref is not None and connector_ref not in connector_ids:
            raise ProfileError(
                f"schedule '{s.get('id')}' references undefined connector "
                f"'{connector_ref}'"
            )


def load_profile(
    name: str | None = None,
    *,
    env: dict[str, str] | None = None,
    profiles_dir: Path | str | None = None,
    known_generator_kinds: set[str] | None = None,
    validate_profile: bool = True,
) -> Profile:
    """Load, env-resolve, and validate a tenant profile.

    ``name`` defaults to ``CLAWRANGE_PROFILE`` then ``"marketing"``.
    """
    if env is None:
        env = dict(os.environ)
    name = name or env.get("CLAWRANGE_PROFILE") or "marketing"

    base = Path(profiles_dir) if profiles_dir is not None else default_profiles_dir()
    path = base / name / "profile.yaml"
    if not path.exists():
        raise ProfileError(f"profile not found: {path}")

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ProfileError(f"profile {name} is not a YAML mapping")
    raw = resolve_env(raw, env)

    profile = Profile(name=name, raw=raw)
    if validate_profile:
        validate(profile, known_generator_kinds=known_generator_kinds)
    return profile
