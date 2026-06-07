"""CRM adapter interface + factory.

The interface is deliberately minimal — five methods — so a new backend is
cheap to add and the pipeline/query layers stay backend-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class CRMAdapter(ABC):
    """Backend-agnostic CRM contract."""

    @abstractmethod
    def init(self) -> None:
        """Create/verify schema or connectivity. Idempotent."""

    @abstractmethod
    def upsert(self, object: str, records: list[dict], upsert_key: str) -> int:
        """Insert or update records keyed by ``upsert_key``. Returns count written."""

    @abstractmethod
    def list(
        self, object: str, filters: dict | None = None, limit: int = 100
    ) -> list[dict]:
        """Return rows for ``object``, optionally filtered by equality."""

    @abstractmethod
    def run_template(self, template: dict, params: dict) -> list[dict]:
        """Execute a named, parameterized query template. Read-only."""

    @abstractmethod
    def health(self) -> dict:
        """Return a health/status dict (always includes ``status``)."""


def get_adapter(crm_cfg: dict) -> CRMAdapter:
    """Build the CRM adapter named by ``crm_cfg['adapter']`` (default sqlite)."""
    adapter = (crm_cfg or {}).get("adapter", "sqlite")

    if adapter == "sqlite":
        from .sqlite_adapter import SQLiteCRM

        # `or` not a default arg: an unset ${CRM_DB_PATH} resolves to "",
        # which would otherwise become the path. Fall back to the container
        # default whenever the configured path is empty.
        path = (crm_cfg.get("sqlite") or {}).get("path") or "/data/crm.db"
        return SQLiteCRM(path)

    if adapter == "rest":
        from .rest_adapter import RestCRM

        return RestCRM(crm_cfg.get("rest") or {})

    raise ValueError(f"unknown crm adapter '{adapter}' (known: sqlite, rest)")
