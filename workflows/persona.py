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

from pathlib import Path

from tenant_profile import Profile

_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "openclaw" / "soul.template.md"
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


def write_soul(profile: Profile, path: str | Path) -> Path:
    """Render the persona and write it to ``path`` (default OpenClaw soul)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_persona(profile))
    return dest
