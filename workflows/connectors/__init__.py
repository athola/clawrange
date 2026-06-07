"""Connector framework — reusable source/transform/sink primitives.

The registries below are the runtime source of truth for connector kinds.
``tenant_profile.KNOWN_*_KINDS`` mirror these so a profile fails validation
at load time rather than at first cron fire. A new primitive is added by
defining the callable in the relevant module and registering it here.
"""

from __future__ import annotations

from .base import Record, Sink, Source, Transform
from .pipeline import run_connector
from .sinks import crm_sink
from .sources import http_csv, login_scrape
from .transforms import leads_clean, passthrough

SOURCES: dict[str, Source] = {
    "http_csv": http_csv,
    "login_scrape": login_scrape,
}

TRANSFORMS: dict[str, Transform] = {
    "leads_clean": leads_clean,
    "passthrough": passthrough,
}

SINKS: dict[str, Sink] = {
    "crm": crm_sink,
}

__all__ = [
    "Record",
    "Source",
    "Transform",
    "Sink",
    "SOURCES",
    "TRANSFORMS",
    "SINKS",
    "run_connector",
    "http_csv",
    "login_scrape",
    "leads_clean",
    "passthrough",
    "crm_sink",
]
