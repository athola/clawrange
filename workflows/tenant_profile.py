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
# Mirrors deal_sources.gather: a typo'd source fails at load, not at 08:30.
KNOWN_DEAL_SOURCE_KINDS = {"ebay", "slickdeals", "servethehome", "reddit"}
KNOWN_OUTREACH_CHANNELS = {"reddit", "hn", "github", "web"}
IDENTITY_FIELDS = {"name", "creature", "vibe", "emoji", "avatar"}
EMOJI_MAX_LEN = 16


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
        # Function-local on purpose: generators imports tenant_profile at
        # module level, so a top-level import here would be circular. The
        # `known_generator_kinds` parameter lets tests inject the registry
        # without importing generators at all.
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

    assistant = profile.raw.get("assistant") or {}
    identity = assistant.get("identity")
    if identity is not None:
        if not isinstance(identity, dict):
            raise ProfileError("assistant.identity must be a mapping")
        unknown = set(identity) - IDENTITY_FIELDS
        if unknown:
            raise ProfileError(
                f"assistant.identity has unknown keys {sorted(unknown)} "
                f"(known: {sorted(IDENTITY_FIELDS)})"
            )
        emoji = identity.get("emoji", "")
        if emoji and len(str(emoji)) > EMOJI_MAX_LEN:
            raise ProfileError("assistant.identity.emoji is too long")

    crm = profile.crm
    if crm is not None:
        adapter = crm.get("adapter")
        if adapter not in KNOWN_ADAPTERS:
            raise ProfileError(
                f"crm.adapter '{adapter}' is unknown (known: {sorted(KNOWN_ADAPTERS)})"
            )

    _validate_timezone(profile.assistant.get("timezone"))
    _validate_search_hygiene(profile.assistant.get("search_hygiene"))
    lists = profile.raw.get("awesome_lists")
    if lists is not None and not (_is_str_list(lists) and all("/" in x for x in lists)):
        raise ProfileError("awesome_lists must be a list of owner/repo names")
    subs = (profile.raw.get("reddit") or {}).get("default_subreddits")
    if subs is not None and not _is_str_list(subs):
        raise ProfileError("reddit.default_subreddits must be a list of names")
    _validate_homelab_deals(profile.raw.get("homelab_deals"))
    _validate_outreach(profile.raw.get("outreach"))

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


def _validate_timezone(tz: Any) -> None:
    if tz in (None, ""):
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProfileError(f"assistant.timezone '{tz}' is not a known zone") from exc


def _validate_search_hygiene(block: Any) -> None:
    if block is None:
        return
    if not isinstance(block, dict):
        raise ProfileError("assistant.search_hygiene must be a mapping")
    for key in ("strip_headings", "blocked_citations"):
        if not _is_str_list(block.get(key) or []):
            raise ProfileError(f"assistant.search_hygiene.{key} must be a list")
    for pattern in block.get("blocked_citations") or []:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ProfileError(
                f"assistant.search_hygiene.blocked_citations: bad regex {pattern!r}"
            ) from exc


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _validate_outreach(block: Any) -> None:
    if block is None:
        return
    if not isinstance(block, dict):
        raise ProfileError("outreach must be a mapping")
    venues = block.get("venue_actions") or {}
    if not isinstance(venues, dict) or not all(
        isinstance(v, str) and v.strip() for v in venues.values()
    ):
        raise ProfileError("outreach.venue_actions must map channel -> action text")
    unknown = set(venues) - KNOWN_OUTREACH_CHANNELS
    if unknown:
        raise ProfileError(
            f"outreach.venue_actions channels {sorted(unknown)} are unknown "
            f"(known: {sorted(KNOWN_OUTREACH_CHANNELS)})"
        )
    if not _is_str_list(block.get("banned_actions") or []):
        raise ProfileError("outreach.banned_actions must be a list of phrases")
    for key in ("audience", "web_venues"):
        if block.get(key) is not None and not isinstance(block[key], str):
            raise ProfileError(f"outreach.{key} must be text")


def _validate_deal_policy(block: dict) -> None:
    share = block.get("too_good_share")
    if share is not None and (
        isinstance(share, bool)
        or not isinstance(share, int | float)
        or not 0 < share < 1
    ):
        raise ProfileError(
            f"homelab_deals.too_good_share must be between 0 and 1, got {share!r}"
        )
    cap = block.get("max_per_target")
    if cap is not None and (
        isinstance(cap, bool) or not isinstance(cap, int) or cap < 1
    ):
        raise ProfileError(
            f"homelab_deals.max_per_target must be a positive integer, got {cap!r}"
        )
    flags = block.get("red_flags") or {}
    if not isinstance(flags, dict) or not all(_is_str_list(v) for v in flags.values()):
        raise ProfileError("homelab_deals.red_flags must map a label -> [phrases]")
    for key in ("part_categories", "memory_check_categories"):
        if block.get(key) is not None and not _is_str_list(block[key]):
            raise ProfileError(f"homelab_deals.{key} must be a list of categories")
    advisor = block.get("advisor") or {}
    if not isinstance(advisor, dict) or not _is_str_list(advisor.get("rules") or []):
        raise ProfileError("homelab_deals.advisor needs audience text and a rules list")
    ebay = (block.get("sources") or {}).get("ebay") or {}
    cats = ebay.get("categories") or {}
    if not isinstance(cats, dict) or not all(str(v).isdigit() for v in cats.values()):
        raise ProfileError(
            "homelab_deals.sources.ebay.categories must map a target category "
            "to a numeric eBay category id"
        )


def _validate_homelab_deals(block: dict | None) -> None:
    if block is None:
        return
    _validate_deal_policy(block)
    for source in block.get("sources") or {}:
        if source not in KNOWN_DEAL_SOURCE_KINDS:
            raise ProfileError(
                f"homelab_deals source '{source}' is unknown "
                f"(known: {sorted(KNOWN_DEAL_SOURCE_KINDS)})"
            )
    gaps = block.get("stack_gaps") or {}
    names: set[str] = set()
    for t in block.get("targets") or []:
        name = t.get("name")
        if not name or not t.get("query"):
            raise ProfileError(f"homelab_deals target {t!r} needs name and query")
        price = t.get("max_price_great")
        if isinstance(price, bool) or not isinstance(price, int | float) or price <= 0:
            raise ProfileError(
                f"homelab_deals target '{name}' max_price_great must be a "
                f"positive number, got {price!r}"
            )
        parts = t.get("parts_cost")
        if parts is not None and (
            isinstance(parts, bool) or not isinstance(parts, int | float) or parts <= 0
        ):
            raise ProfileError(
                f"homelab_deals target '{name}' parts_cost must be a "
                f"positive number, got {parts!r}"
            )
        need = t.get("need", "want")
        if need not in ("need", "want", "watch"):
            raise ProfileError(
                f"homelab_deals target '{name}' need must be need, want or "
                f"watch, got {need!r}"
            )
        unknown = [g for g in t.get("fills") or [] if g not in gaps]
        if unknown:
            raise ProfileError(
                f"homelab_deals target '{name}' fills {unknown} not declared "
                f"in stack_gaps (known: {sorted(gaps)})"
            )
        if name in names:
            raise ProfileError(f"homelab_deals target '{name}' is duplicated")
        names.add(name)


_ACTIVE: dict[str, Any] = {}


def active_profile() -> Profile | None:
    """The running tenant's profile, reloaded when its file changes.

    Hot paths (every heartbeat prompt, every report) read tenant config
    through this instead of re-parsing YAML. None when no profile loads.
    """
    name = os.environ.get("CLAWRANGE_PROFILE") or "starter"
    path = default_profiles_dir() / name / "profile.yaml"
    try:
        stamp = (name, path.stat().st_mtime_ns)
    except OSError:
        return None
    if _ACTIVE.get("stamp") != stamp:
        try:
            _ACTIVE.update(stamp=stamp, profile=load_profile(name))
        except Exception as exc:
            logger.warning("active_profile: %s failed to load: %s", name, exc)
            _ACTIVE.update(stamp=stamp, profile=None)
    return _ACTIVE.get("profile")


def operator_name(profile: Profile | None = None) -> str:
    """How prompts and labels refer to the operator: `assistant.owner.name`,
    else "the operator". Keeps any one person's name out of core code."""
    profile = profile or active_profile()
    owner = (profile.assistant.get("owner") or {}) if profile else {}
    return str(owner.get("name") or "").strip() or "the operator"


def search_hygiene(profile: Profile | None = None) -> dict:
    """The persona's `assistant.search_hygiene` block: `strip_headings`
    (persona sections kept out of web-search prompts) and
    `blocked_citations` (regexes for citation URLs/titles to drop)."""
    profile = profile or active_profile()
    return (profile.assistant.get("search_hygiene") or {}) if profile else {}


def default_subreddits(profile: Profile | None = None) -> list[str]:
    """Subreddits for scans that name none: `reddit.default_subreddits`,
    else the union of the tracked projects' subreddits."""
    profile = profile or active_profile()
    if profile is None:
        return []
    configured = (profile.raw.get("reddit") or {}).get("default_subreddits")
    if configured:
        return [str(s) for s in configured]
    subs: dict[str, None] = {}
    for proj in profile.projects:
        for sub in proj.get("subreddits") or []:
            subs.setdefault(str(sub), None)
    return list(subs)


def operator_timezone(profile: Profile | None = None) -> str:
    """The operator's timezone: SCHEDULER_TZ, else the profile's
    `assistant.timezone`, else UTC. One source for schedules, quiet hours,
    the heartbeat watchdog and report headers."""
    env_tz = os.getenv("SCHEDULER_TZ", "").strip()
    if env_tz:
        return env_tz
    profile = profile or active_profile()
    if profile is None:  # a bad profile must not break a clock
        return "UTC"
    return str(profile.assistant.get("timezone") or "UTC")


def load_profile(
    name: str | None = None,
    *,
    env: dict[str, str] | None = None,
    profiles_dir: Path | str | None = None,
    known_generator_kinds: set[str] | None = None,
    validate_profile: bool = True,
) -> Profile:
    """Load, env-resolve, and validate a tenant profile.

    ``name`` defaults to ``CLAWRANGE_PROFILE`` then ``"starter"`` — the
    identity-free profile shipped for fresh clones.
    """
    if env is None:
        env = dict(os.environ)
    name = name or env.get("CLAWRANGE_PROFILE") or "starter"

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
