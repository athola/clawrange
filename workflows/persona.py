"""Persona rendering — turn a tenant profile into ``openclaw/soul.md``.

OpenClaw reads ``soul.md`` / ``SOUL.md`` and ``IDENTITY.md`` from files, so
the persona is a render artifact: written at setup (``make persona
PROFILE=<name>``) and re-rendered by the workflows service whenever a
learning is approved or rejected. The service also returns the composed
text on ``GET /persona/soul`` and ``GET /persona/identity``.

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
import stat
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


NEW_FILE_MODE = 0o644


def _match_existing(fd: int, path: Path) -> None:
    """Give the temp file the target's mode (and owner, as root).

    mkstemp creates 0600 and os.replace keeps it. The workflows container
    runs as root while OpenClaw reads as uid 1000, so a 0600 root-owned
    SOUL.md would lock the gateway out of its own persona.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        os.fchmod(fd, NEW_FILE_MODE)
        return
    os.fchmod(fd, stat.S_IMODE(st.st_mode))
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.fchown(fd, st.st_uid, st.st_gid)
        except OSError as exc:
            _log.warning("persona render: cannot chown %s: %s", path, exc)


def _atomic_write(path: Path, text: str) -> bool:
    """Write text atomically; return False (no raise) if dir is unwritable.

    A unique temp file in the target directory plus fsync, then os.replace,
    so concurrent renders never share a temp name and a crash never leaves a
    truncated persona behind. The result keeps the replaced file's mode and
    owner (0644 for a new file).
    """
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        _match_existing(fd, path)
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


def planned_writes(profile: Profile, targets: dict) -> list[tuple[str, Path, str]]:
    """``(result key, path, content kind)`` for every file a render writes.

    Identity files are written only when the profile declares an
    ``assistant.identity`` block, and ops files only when it declares
    ``assistant.ops``: otherwise a render would replace hand-written files
    with template defaults.
    """
    a = profile.assistant
    out: list[tuple[str, Path, str]] = []
    if targets.get("soul"):
        out.append(("soul", Path(targets["soul"]), "soul"))
    if a.get("identity") and targets.get("identity"):
        out.append(("identity", Path(targets["identity"]), "identity"))
    for wp in targets.get("workspace_soul", []):
        out.append((f"ws_soul:{wp}", Path(wp), "soul"))
    if a.get("identity"):
        for wp in targets.get("workspace_identity", []):
            out.append((f"ws_identity:{wp}", Path(wp), "identity"))
    if a.get("ops"):
        if targets.get("ops_soul"):
            out.append(("ops_soul", Path(targets["ops_soul"]), "ops"))
        for wp in targets.get("ops_workspace_soul", []):
            out.append((f"ws_ops_soul:{wp}", Path(wp), "ops"))
    return out


def target_writable(path: Path) -> bool:
    """Whether a render could replace ``path`` (it writes a temp file in the
    directory, so the nearest existing ancestor must be a writable dir)."""
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def render_all(profile: Profile, targets: dict, learnings: list) -> dict:
    """Render persona+identity to all configured targets. Returns per-target ok."""
    texts = {
        "soul": compose_persona(profile, learnings),
        "identity": compose_identity(profile, learnings),
        "ops": render_ops_persona(profile) or "",
    }
    return {
        key: _atomic_write(path, texts[kind])
        for key, path, kind in planned_writes(profile, targets)
    }
