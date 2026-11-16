"""Tenant profile loader — the declarative layer that owns the
tenant-specific surface (persona, seeds, connector wiring, CRM config).

A profile lives at ``config/profiles/<name>/profile.yaml``. The active
profile is chosen by the ``CLAWRANGE_PROFILE`` env var (default
``starter``), so a fresh clone configures behavior by editing YAML
rather than Python.

Named ``tenant_profile`` (not ``profile``) deliberately: a top-level
module called ``profile`` would shadow the Python stdlib ``profile``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
OPS_FIELDS = {"agent", "name"}
# OpenClaw agent ids name workspace directories (workspace-<id>).
_AGENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
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


_PROFILE_NAME_RE = re.compile(r"[a-z0-9_-]+")


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

    live_registry = known_generator_kinds is None
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
        _validate_source_auth(c.get("id"), src, auth)
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
        # get_adapter defaults to sqlite, which accepts one object and only
        # UNIQUE columns as upsert keys; anything else fails every sync.
        if ((profile.crm or {}).get("adapter") or "sqlite") == "sqlite":
            _validate_sqlite_sink(c.get("id"), sink)

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
    _validate_assistant_text(assistant)

    _validate_ops(assistant.get("ops"))

    crm = profile.crm
    if crm is not None:
        adapter = crm.get("adapter")
        if adapter not in KNOWN_ADAPTERS:
            raise ProfileError(
                f"crm.adapter '{adapter}' is unknown (known: {sorted(KNOWN_ADAPTERS)})"
            )
        for template in crm.get("query_templates") or []:
            _validate_query_template(template)

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
    _validate_income_review(profile.raw.get("income_review"))

    for s in profile.schedules:
        catch_up = s.get("catch_up")
        if catch_up is not None and not isinstance(catch_up, bool):
            raise ProfileError(
                f"schedule '{s.get('id')}' catch_up must be true or false"
            )
        kind = s.get("kind")
        if kind not in known_generator_kinds:
            raise ProfileError(
                f"schedule '{s.get('id')}' has kind '{kind}' not in "
                f"GENERATORS ({sorted(known_generator_kinds)})"
            )
        kwargs = s.get("kwargs") or {}
        connector_ref = kwargs.get("connector")
        if connector_ref is not None and connector_ref not in connector_ids:
            raise ProfileError(
                f"schedule '{s.get('id')}' references undefined connector "
                f"'{connector_ref}'"
            )
        named = kwargs.get("profile_name")
        if named is not None and named != profile.name:
            # A copied profile that still names its source would file its
            # work under the wrong profile; omit it to use this directory.
            raise ProfileError(
                f"schedule '{s.get('id')}' kwargs.profile_name '{named}' is not "
                f"this profile ('{profile.name}'); omit it to default to this one"
            )
        if live_registry:
            # Same check the /sched API runs before persisting (cron syntax,
            # kwargs the generator accepts); the seed path never ran it.
            # Injected test kinds aren't real generators, so it needs the
            # live registry.
            from scheduler import validate_schedule

            try:
                validate_schedule(kind, s.get("cron") or "", s.get("kwargs"))
            except ValueError as exc:
                raise ProfileError(f"schedule '{s.get('id')}': {exc}") from exc


def _validate_source_auth(cid: Any, src: dict, auth: str) -> None:
    """login_scrape needs a login_form; other sources can't use one.

    Checks structure only: ``${VAR}`` values resolve to "" before
    validation, and an unset credential means unconfigured, not invalid.
    """
    if src.get("kind") == "login_scrape":
        form = (src.get("auth") or {}).get("login_form")
        if auth != "login_form" or not isinstance(form, dict):
            raise ProfileError(
                f"connector '{cid}' login_scrape source needs auth.kind "
                "login_form with a login_form mapping"
            )
    elif auth == "login_form":
        raise ProfileError(
            f"connector '{cid}' auth kind login_form needs a login_scrape source"
        )


def _validate_sqlite_sink(cid: Any, sink: dict) -> None:
    from crm.sqlite_adapter import OBJECTS, UNIQUE_LEAD_KEYS

    obj = sink.get("object", "leads")
    if obj not in OBJECTS:
        raise ProfileError(
            f"connector '{cid}' sink object '{obj}' is not supported by the "
            f"sqlite CRM (known: {sorted(OBJECTS)})"
        )
    key = sink.get("upsert_key", "email")
    if key not in UNIQUE_LEAD_KEYS:
        raise ProfileError(
            f"connector '{cid}' sink upsert_key '{key}' must be a unique lead "
            f"column (known: {sorted(UNIQUE_LEAD_KEYS)})"
        )


def _validate_assistant_text(assistant: dict) -> None:
    """Persona fields are substituted into templates as text: null or a
    non-string would raise on every render, and a blank name renders an
    assistant with no name. An empty avatar is fine (no avatar)."""
    if "name" in assistant and not _non_blank(assistant["name"]):
        raise ProfileError("assistant.name must be non-empty text")
    identity = assistant.get("identity") or {}
    for key, value in identity.items():
        if not isinstance(value, str):
            raise ProfileError(f"assistant.identity.{key} must be text")
    if "name" in identity and not identity["name"].strip():
        raise ProfileError("assistant.identity.name must be non-empty text")


def _non_blank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# Mirrors crm.query.coerce_params: any other type would bind the raw value.
KNOWN_QUERY_PARAM_TYPES = {"duration", "enum", "string"}
_LIKE_RE = re.compile(r"\bLIKE\s+:like\b(\s+ESCAPE\b)?", re.IGNORECASE)


def _validate_query_template(t: Any) -> None:
    if not isinstance(t, dict) or not _non_blank(t.get("name")):
        raise ProfileError("crm.query_templates entries need a name")
    name = t["name"]
    params = t.get("params") or {}
    if not isinstance(params, dict):
        raise ProfileError(f"crm template '{name}' params must be a mapping")
    for pname, spec in params.items():
        spec = spec or {}
        ptype = spec.get("type")
        if ptype not in KNOWN_QUERY_PARAM_TYPES:
            raise ProfileError(
                f"crm template '{name}' param '{pname}' has unknown type "
                f"{ptype!r} (known: {sorted(KNOWN_QUERY_PARAM_TYPES)})"
            )
        if ptype == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not values:
                raise ProfileError(
                    f"crm template '{name}' enum '{pname}' needs a values list"
                )
            mapping = spec.get("map")
            if mapping is not None and (
                not isinstance(mapping, dict) or set(values) - set(mapping)
            ):
                raise ProfileError(
                    f"crm template '{name}' enum '{pname}' map must cover every "
                    f"value {values}"
                )
    if any((s or {}).get("type") == "string" for s in params.values()):
        likes = _LIKE_RE.findall(t.get("sql") or "")
        if any(not escape for escape in likes):
            # coerce_params escapes % and _ with a backslash; without the
            # ESCAPE clause SQLite would treat them as wildcards again.
            raise ProfileError(
                f"crm template '{name}': every 'LIKE :like' needs ESCAPE '\\'"
            )


def _validate_timezone(tz: Any) -> None:
    if tz in (None, ""):
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProfileError(f"assistant.timezone '{tz}' is not a known zone") from exc


def _validate_ops(block: Any) -> None:
    """``assistant.ops``: the ops agent's id (its OpenClaw workspace is
    ``workspace-<agent>``) and an optional display name."""
    if block is None:
        return
    if not isinstance(block, dict):
        raise ProfileError("assistant.ops must be a mapping")
    unknown = set(block) - OPS_FIELDS
    if unknown:
        raise ProfileError(
            f"assistant.ops has unknown keys {sorted(unknown)} "
            f"(known: {sorted(OPS_FIELDS)})"
        )
    agent = block.get("agent")
    if not isinstance(agent, str) or not _AGENT_ID_RE.fullmatch(agent):
        raise ProfileError(
            "assistant.ops.agent must be an OpenClaw agent id "
            "(lowercase letters, digits, '-' or '_')"
        )


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


def _validate_income_review(block: Any) -> None:
    if block is None:
        return
    if not isinstance(block, dict) or not isinstance(block.get("checklist", ""), str):
        raise ProfileError("income_review must be a mapping with text `checklist`")


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
    if block.get("exclude") is not None and not _is_str_list(block["exclude"]):
        raise ProfileError("homelab_deals.exclude must be a list of phrases")
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
        if t.get("exclude") is not None and not _is_str_list(t["exclude"]):
            raise ProfileError(
                f"homelab_deals target '{name}' exclude must be a list of phrases"
            )
        if name in names:
            raise ProfileError(f"homelab_deals target '{name}' is duplicated")
        names.add(name)
    # Build the targets the scanner would build, so anything load_targets
    # rejects fails here at load time instead of at 08:30.
    from deals import load_targets

    try:
        load_targets(block)
    except (ValueError, TypeError, KeyError) as exc:
        raise ProfileError(f"homelab_deals: {exc}") from exc


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


def resolve_subreddits(brain_db, subreddits, project_slug) -> list[str]:
    """Subreddits for a scan: explicit ones, else the project's, else the
    profile default. Shared by the /scan API route and the chat command."""
    if subreddits:
        return list(subreddits)
    if project_slug:
        from brain_db import project_list

        project = brain_db.get_project(project_slug)
        if project:
            subs = project_list(project, "subreddits")
            if subs:
                return subs
    return default_subreddits()


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


def hour_in_window(
    now_utc: datetime, start_hour: int, end_hour: int, tz_name: str
) -> bool:
    """Return True when `now_utc` falls inside the half-open
    [start_hour, end_hour) window in the named tz.

    DST-aware via zoneinfo: passing ``America/Chicago`` produces the
    correct CST/CDT offset for any date so the operator's 12am-5am
    sleep window stays anchored to local clock time across the
    daylight-saving boundary.

    End hour is exclusive: a window of (0, 5) covers 00:00:00 through
    04:59:59 local, so the */5 cron fire at exactly 05:00 wakes the
    pulse back up. Wrap-around windows (e.g. 22 → 6) are supported.
    """
    local = now_utc.astimezone(ZoneInfo(tz_name))
    hour = local.hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


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


# Generators that act on one profile's data take it as kwargs.profile_name.
_PROFILE_SCOPED_KINDS = {"persona_reflect"}


def _default_profile_name(raw: dict, name: str) -> None:
    """Fill ``profile_name`` for profile-scoped schedules from the directory,
    so a ``cp -r`` copy of a profile works under its own name."""
    for s in (raw.get("seeds") or {}).get("schedules") or []:
        if isinstance(s, dict) and s.get("kind") in _PROFILE_SCOPED_KINDS:
            kwargs = s.get("kwargs") or {}
            if isinstance(kwargs, dict):
                kwargs.setdefault("profile_name", name)
                s["kwargs"] = kwargs


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
    if not _PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError(f"invalid profile name {name!r}: use [a-z0-9_-]+")

    base = Path(profiles_dir) if profiles_dir is not None else default_profiles_dir()
    path = base / name / "profile.yaml"
    if not path.exists():
        raise ProfileError(f"profile not found: {path}")

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ProfileError(f"profile {name} is not a YAML mapping")
    raw = resolve_env(raw, env)
    _default_profile_name(raw, name)

    profile = Profile(name=name, raw=raw)
    if validate_profile:
        validate(profile, known_generator_kinds=known_generator_kinds)
    return profile
