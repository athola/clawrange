"""Pluggable CRM layer.

The default backend is the bundled SQLite CRM (fully testable, offline).
Any other CRM (REST/SaaS) plugs in by implementing ``CRMAdapter`` and
registering in ``get_adapter`` — the connector pipeline and the query
layer only ever talk to the interface.
"""

from __future__ import annotations

from .adapter import CRMAdapter, get_adapter
from .sqlite_adapter import SQLiteCRM

__all__ = ["CRMAdapter", "get_adapter", "SQLiteCRM"]
