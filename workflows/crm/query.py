"""CRM query layer — parameter coercion and the natural-language router.

Templates are declared in the tenant profile (``crm.query_templates``):
each has a ``name``, ``description``, a ``params`` spec, and read-only
``sql``. The LLM never writes SQL — it only *selects* a template by name
and fills its parameters. This module:

- ``coerce_params``: validate/convert raw params into SQL bind values.
- ``run_query``: coerce then execute via the adapter.
- ``route_nl`` / ``answer``: map a natural-language prompt to a template
  + params (LLM-assisted, injectable) and format a reply.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from pytimeparse import parse as _parse_duration

logger = logging.getLogger("clawrange.crm.query")


def _since(duration: str) -> str:
    """ISO timestamp for ``now - duration`` (e.g. '7d', '24h', '30m')."""
    seconds = _parse_duration(str(duration))
    if seconds is None:
        raise ValueError(f"invalid duration: {duration!r}")
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def coerce_params(template: dict, raw: dict | None) -> dict:
    """Convert raw params into SQL bind values per the template's spec.

    Supported param types:
      - ``duration``: binds ``since`` (or ``bind``) to now-minus-duration ISO.
      - ``enum``: validates membership; an optional ``map`` translates the
        chosen value (e.g. day -> '%Y-%m-%d'); binds to ``bind`` or the name.
      - ``string``: binds ``q`` and ``like`` (``%value%``); required.
      - anything else: binds the value under its name.
    """
    raw = raw or {}
    spec_map = (template or {}).get("params", {}) or {}
    binds: dict = {}
    for name, spec in spec_map.items():
        spec = spec or {}
        ptype = spec.get("type")
        value = raw.get(name, spec.get("default"))
        bind = spec.get("bind", name)

        if ptype == "duration":
            binds[spec.get("bind", "since")] = _since(value)
        elif ptype == "enum":
            allowed = spec.get("values", [])
            if value not in allowed:
                raise ValueError(
                    f"param '{name}' must be one of {allowed}, got {value!r}"
                )
            mapping = spec.get("map")
            binds[bind] = mapping[value] if mapping else value
        elif ptype == "string":
            if value in (None, ""):
                raise ValueError(f"param '{name}' is required")
            binds["q"] = value
            binds["like"] = f"%{value}%"
        else:
            binds[bind] = value
    return binds


def run_query(adapter, template: dict, raw: dict | None) -> list[dict]:
    """Coerce params and run a single template through the adapter."""
    return adapter.run_template(template, coerce_params(template, raw))


def find_template(templates: list[dict], name: str | None) -> dict | None:
    for t in templates:
        if t.get("name") == name:
            return t
    return None


# ─── natural-language router (FR-5.2 / FR-5.3) ────────────────────


def _catalog(templates: list[dict]) -> str:
    """Compact template catalog the LLM chooses from."""
    lines = []
    for t in templates:
        params = ", ".join(
            f"{k}({(v or {}).get('type', 'any')})"
            for k, v in (t.get("params") or {}).items()
        )
        lines.append(
            f"- {t['name']}: {t.get('description', '')}"
            + (f" [params: {params}]" if params else "")
        )
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply (tolerates fences/prose)."""
    if not text:
        raise ValueError("empty LLM response")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = fenced.group(1) if fenced else None
    if blob is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        blob = brace.group(0) if brace else None
    if blob is None:
        raise ValueError("no JSON object in LLM response")
    return json.loads(blob)


async def _default_llm(prompt: str) -> str:
    from llm_proxy import _llm_call

    return await _llm_call(prompt, max_tokens=300) or ""


async def route_nl(prompt: str, templates: list[dict], *, llm=_default_llm) -> dict:
    """Map a natural-language prompt to ``{template, params}`` via the LLM.

    The LLM only *selects* a template name and fills params — it never
    writes SQL. A malformed or off-catalog reply yields ``template=None``
    (the caller renders a safe fallback) rather than raising.
    """
    instruction = (
        "You translate a question into ONE of the available CRM report "
        "templates. Respond with ONLY a JSON object: "
        '{"template": "<name>", "params": {<param>: <value>}}.\n\n'
        f"Available templates:\n{_catalog(templates)}\n\n"
        f"Question: {prompt}\n\n"
        "Durations look like '7d', '24h', '30m'. If no template fits, use "
        '{"template": null, "params": {}}.'
    )
    try:
        raw = await llm(instruction)
        parsed = _extract_json(raw)
    except Exception as exc:
        logger.warning("route_nl: could not parse LLM response: %s", exc)
        return {"template": None, "params": {}}

    name = parsed.get("template")
    if find_template(templates, name) is None:
        return {"template": None, "params": {}}
    return {"template": name, "params": parsed.get("params") or {}}


def _format_rows(template: dict, rows: list[dict]) -> str:
    """Deterministically phrase result rows for a Telegram-friendly reply."""
    label = template.get("name", "result")
    if not rows:
        return f"No results for {label}."
    if len(rows) == 1 and len(rows[0]) == 1:
        (value,) = rows[0].values()
        return f"{label}: {value}"
    # rows shaped like {key, n} → "key: n" lines; otherwise compact dicts
    parts = []
    for r in rows[:20]:
        if "n" in r and len(r) == 2:
            other = next(k for k in r if k != "n")
            parts.append(f"{r[other]}: {r['n']}")
        else:
            parts.append(", ".join(f"{k}={v}" for k, v in r.items()))
    return f"{label}:\n" + "\n".join(parts)


async def answer(
    prompt: str, adapter, templates: list[dict], *, llm=_default_llm
) -> dict:
    """End-to-end NL query: route → coerce → run → format."""
    route = await route_nl(prompt, templates, llm=llm)
    template = find_template(templates, route["template"])
    if template is None:
        names = ", ".join(t["name"] for t in templates) or "(none configured)"
        return {
            "answer": f"I couldn't map that to a known report. Available: {names}.",
            "template": None,
            "params": {},
            "rows": [],
        }
    try:
        rows = run_query(adapter, template, route["params"])
    except ValueError as exc:
        return {
            "answer": f"That report needs a valid parameter: {exc}",
            "template": template["name"],
            "params": route["params"],
            "rows": [],
        }
    return {
        "answer": _format_rows(template, rows),
        "template": template["name"],
        "params": route["params"],
        "rows": rows,
    }
