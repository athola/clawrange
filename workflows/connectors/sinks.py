"""Connector sinks — persist records via the CRM adapter (FR-3.3).

The ``crm`` sink is intentionally thin: it delegates to
``CRMAdapter.upsert`` so the same connector works against any backend
(SQLite by default, REST/SaaS by swapping ``crm.adapter`` in the profile).
"""

from __future__ import annotations

import logging

from .base import Record

logger = logging.getLogger("clawrange.connectors.sinks")


def crm_sink(records: list[Record], spec: dict, crm) -> int:
    spec = spec or {}
    object_name = spec.get("object", "leads")
    upsert_key = spec.get("upsert_key", "email")
    if not records:
        return 0
    return crm.upsert(object_name, records, upsert_key)
