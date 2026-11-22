"""Timestamp helpers shared by the stores (brain, CRM, connectors)."""

from datetime import UTC, datetime


def utcnow_iso() -> str:
    """Current UTC time as ISO 8601 with offset, the format every store
    writes to its created_at / updated_at columns."""
    return datetime.now(UTC).isoformat()
