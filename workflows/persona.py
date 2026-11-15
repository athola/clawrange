"""Persona rendering — turn a tenant profile into ``openclaw/soul.md``.

OpenClaw reads ``soul.md`` from a bind mount at runtime, so the persona is
a render artifact produced at setup time (``make profile PROFILE=<name>``)
rather than something the workflows service serves.

Two render paths:

- **verbatim**: if ``assistant.persona_markdown`` is set, it is used as the
  whole body. The marketing profile uses this to preserve its original
  persona byte-for-byte.
- **structured**: otherwise the generic ``soul.template.md`` is filled from
  ``assistant`` fields (name / role / owner / capabilities / channel). This
  is the path a new business owner uses — no prose required, just fields.

This module and the template are kept free of any single-tenant content so
the generic core never leaks one operator's identity into another's deploy.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from tenant_profile import Profile

_log = logging.getLogger("clawrange.persona")

_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "openclaw" / "soul.template.md"
)

_OPS_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "openclaw" / "soul-ops.template.md"
)
_IDENTITY_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "openclaw" / "identity.template.md"
)


def _template() -> str:
    return _TEMPLATE_PATH.read_text()


def _owner_clause(owner: dict) -> str:
    name = owner.get("name")
    org = owner.get("org")
    if name and org:
        return f", {name}'s assistant at {org}"
    if name:
        return f", {name}'s assistant"
    if org:
        return f" at {org}"
    return ""


def _owner_block(owner: dict) -> str:
    lines = []
    if owner.get("name"):
        lines.append(f"- Operator: {owner['name']}")
    if owner.get("org"):
        lines.append(f"- Organization: {owner['org']}")
    if owner.get("context"):
        lines.append(f"- Context: {owner['context']}")
    return "\n".join(lines) if lines else "- (no operator context provided)"


def _capabilities_block(caps: list[str]) -> str:
    if not caps:
        return "- (no capabilities configured yet)"
    return "\n".join(f"- {c}" for c in caps)


def render_persona(profile: Profile) -> str:
    """Render the assistant persona markdown for a profile."""
    a = profile.assistant

    verbatim = a.get("persona_markdown")
    if verbatim:
        return verbatim.rstrip("\n") + "\n"

    owner = a.get("owner") or {}
    rendered = _template()
    substitutions = {
        "{{name}}": a.get("name", "Assistant"),
        "{{role}}": a.get("role", "operations assistant"),
        "{{channel}}": a.get("channel", "telegram"),
        "{{owner_clause}}": _owner_clause(owner),
        "{{owner_block}}": _owner_block(owner),
        "{{capabilities_block}}": _capabilities_block(a.get("capabilities") or []),
    }
    for token, value in substitutions.items():
        rendered = rendered.replace(token, value)
    return rendered.rstrip("\n") + "\n"


def render_identity(profile: Profile) -> str:
    """Render IDENTITY.md from the profile's assistant.identity block."""
    a = profile.assistant
    ident = a.get("identity") or {}
    template = _IDENTITY_TEMPLATE_PATH.read_text()
    subs = {
        "{{name}}": ident.get("name") or a.get("name", "Assistant"),
        "{{creature}}": ident.get("creature", "AI operator"),
        "{{vibe}}": ident.get("vibe", "calm, direct, helpful"),
        "{{emoji}}": ident.get("emoji", "🤖"),
        "{{avatar}}": ident.get("avatar", ""),
    }
    for token, value in subs.items():
        template = template.replace(token, value)
    return template.rstrip("\n") + "\n"


def render_ops_persona(profile: Profile) -> str | None:
    """Render the ops agent's SOUL from ``assistant.ops`` and the owner block.

    Returns None when the profile defines no ops agent. The organization and
    location come from ``owner.org`` / ``owner.location`` (usually ``${VAR}``
    tokens resolved from .env), so no tracked file names an operator.
    """
    a = profile.assistant
    ops = a.get("ops")
    if not ops:
        return None
    owner = a.get("owner") or {}
    name = a.get("name", "Assistant")
    ops_name = ops.get("name") or f"{name} Ops"
    org, location = owner.get("org"), owner.get("location")
    clause = f" for **{org}**" if org else ""
    if org and location:
        clause += f" in {location}"
    subs = {
        "{{ops_title}}": f"{ops_name}: {org} Internal Agent" if org else ops_name,
        "{{name}}": name,
        "{{org_clause}}": clause,
    }
    text = _OPS_TEMPLATE_PATH.read_text()
    for token, value in subs.items():
        text = text.replace(token, value)
    return text.rstrip("\n") + "\n"


def write_soul(profile: Profile, path: str | Path) -> Path:
    """Render the persona and write it to ``path`` (default OpenClaw soul)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_persona(profile))
    return dest


def _learned_region(learnings: list, kind: str) -> str:
    rows = [x for x in learnings if x.get("kind") == kind]
    if not rows:
        return ""
    lines = ["", "## Learned", ""]
    for x in rows:
        # Collapse whitespace so a stored multi-line value can't open its
        # own heading (input validation rejects these; this covers old rows
        # and hand-edited learned.yaml).
        target = " ".join((x.get("target") or "General").split())
        content = " ".join(str(x["content"]).split())
        lines.append(f"- **{target}:** {content}")
    return "\n".join(lines) + "\n"


def compose_persona(profile: Profile, learnings: list) -> str:
    base = render_persona(profile).rstrip("\n") + "\n"
    return (base + _learned_region(learnings, "persona")).rstrip("\n") + "\n"


def compose_identity(profile: Profile, learnings: list) -> str:
    base = render_identity(profile).rstrip("\n") + "\n"
    return (base + _learned_region(learnings, "identity")).rstrip("\n") + "\n"


def _atomic_write(path: Path, text: str) -> bool:
    """Write text atomically; return False (no raise) if dir is unwritable.

    A unique temp file in the target directory plus fsync, then os.replace,
    so concurrent renders never share a temp name and a crash never leaves a
    truncated persona behind.
    """
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        return True
    except OSError as exc:
        _log.warning("persona render: cannot write %s: %s", path, exc)
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)
        return False


def render_all(profile: Profile, targets: dict, learnings: list) -> dict:
    """Render persona+identity to all configured targets. Returns per-target ok."""
    soul = compose_persona(profile, learnings)
    ident = compose_identity(profile, learnings)
    results = {}
    if targets.get("soul"):
        results["soul"] = _atomic_write(Path(targets["soul"]), soul)
    if targets.get("identity"):
        results["identity"] = _atomic_write(Path(targets["identity"]), ident)
    for wp in targets.get("workspace_soul", []):
        results[f"ws_soul:{wp}"] = _atomic_write(Path(wp), soul)
    for wp in targets.get("workspace_identity", []):
        results[f"ws_identity:{wp}"] = _atomic_write(Path(wp), ident)
    ops = render_ops_persona(profile)
    if ops is not None:
        if targets.get("ops_soul"):
            results["ops_soul"] = _atomic_write(Path(targets["ops_soul"]), ops)
        for wp in targets.get("ops_workspace_soul", []):
            results[f"ws_ops_soul:{wp}"] = _atomic_write(Path(wp), ops)
    return results
