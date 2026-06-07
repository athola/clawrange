"""Connector transforms — map/clean raw rows into CRM records (FR-3.2).

``leads_clean`` renames source columns to CRM fields via ``mapping``, trims
whitespace, drops rows missing any ``required`` field, defaults ``status``
to ``new`` and ``created_at`` to now, then dedupes on ``dedup_key``
(last-wins). ``passthrough`` is the identity transform for connectors whose
source already emits CRM-shaped rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .base import Record

logger = logging.getLogger("clawrange.connectors.transforms")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def passthrough(rows: list[Record], spec: dict) -> list[Record]:
    return list(rows)


def leads_clean(rows: list[Record], spec: dict) -> list[Record]:
    spec = spec or {}
    mapping = spec.get("mapping") or {}
    required = spec.get("required") or []
    dedup_key = spec.get("dedup_key")

    cleaned: list[Record] = []
    for raw in rows:
        if mapping:
            rec: Record = {
                dest: raw[src] for src, dest in mapping.items() if src in raw
            }
        else:
            rec = dict(raw)
        rec = {k: (v.strip() if isinstance(v, str) else v) for k, v in rec.items()}

        if any(not rec.get(field) for field in required):
            continue

        rec.setdefault("status", "new")
        if not rec.get("created_at"):
            rec["created_at"] = _now()
        cleaned.append(rec)

    if dedup_key:
        deduped: dict = {}
        for rec in cleaned:
            deduped[rec.get(dedup_key)] = rec  # last row wins
        cleaned = list(deduped.values())

    return cleaned
