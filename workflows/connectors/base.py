"""Connector primitives and type contracts.

A connector is a declarative source -> transform -> sink chain wired in a
tenant profile. The code here owns the *kinds* (the reusable primitives);
the profile owns *which* kinds are wired and with what parameters. Sources
yield raw dict rows, transforms map/clean them, and sinks persist them via
the backend-agnostic ``CRMAdapter``.
"""

from __future__ import annotations

from typing import Any, Protocol

# A row at any stage of the pipeline. Sources emit raw rows (source-column
# keys); transforms emit CRM-field rows; sinks consume those.
Record = dict[str, Any]


class Source(Protocol):
    """Fetch raw rows. ``client`` is injectable for tests (httpx)."""

    def __call__(self, spec: dict, *, client: Any | None = None) -> list[Record]: ...


class Transform(Protocol):
    """Map/clean rows according to ``spec``."""

    def __call__(self, rows: list[Record], spec: dict) -> list[Record]: ...


class Sink(Protocol):
    """Persist records via a CRM adapter; return the count written."""

    def __call__(self, records: list[Record], spec: dict, crm: Any) -> int: ...
