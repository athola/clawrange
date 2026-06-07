"""Connector sources — fetch raw rows from a portal (FR-3.1).

``http_csv`` GETs a CSV export with optional auth. ``login_scrape`` first
POSTs a login form to establish an httpx session (cookie jar), then GETs an
export path behind that session. Both accept an injectable ``client`` so
tests use ``httpx.MockTransport`` instead of the network, and both degrade
gracefully (return ``[]`` + a warning) when their URL is unconfigured —
mirroring the marketing scanners: a missing credential disables a feature,
it does not crash boot.
"""

from __future__ import annotations

import base64
import csv
import io
import logging

import httpx

from .base import Record

logger = logging.getLogger("clawrange.connectors.sources")


def _auth_headers(auth: dict | None) -> dict:
    """Build request headers for the static auth kinds (none/api_key/bearer/basic)."""
    auth = auth or {}
    kind = auth.get("kind", "none")
    if kind == "bearer" and auth.get("token"):
        return {"Authorization": f"Bearer {auth['token']}"}
    if kind == "api_key":
        ak = auth.get("api_key") or {}
        if ak.get("value"):
            return {ak.get("header", "X-API-Key"): ak["value"]}
    if kind == "basic":
        b = auth.get("basic") or {}
        if b.get("username") is not None:
            raw = f"{b.get('username', '')}:{b.get('password', '')}".encode()
            return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
    return {}


def _parse_csv(text: str) -> list[Record]:
    return list(csv.DictReader(io.StringIO(text)))


def _ensure_client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    """Return (client, owned); owned clients are closed by the caller."""
    if client is not None:
        return client, False
    return httpx.Client(timeout=30.0), True


def http_csv(spec: dict, *, client: httpx.Client | None = None) -> list[Record]:
    url = spec.get("url")
    if not url:
        logger.warning("http_csv: no url configured (treating as unconfigured)")
        return []
    c, owned = _ensure_client(client)
    try:
        resp = c.get(url, headers=_auth_headers(spec.get("auth")))
        resp.raise_for_status()
        return _parse_csv(resp.text)
    finally:
        if owned:
            c.close()


def login_scrape(spec: dict, *, client: httpx.Client | None = None) -> list[Record]:
    lf = (spec.get("auth") or {}).get("login_form") or {}
    login_url = lf.get("login_url")
    export_path = lf.get("export_path")
    if not login_url or not export_path:
        logger.warning(
            "login_scrape: missing login_url/export_path (treating as unconfigured)"
        )
        return []
    c, owned = _ensure_client(client)
    try:
        form = {
            lf.get("username_field", "username"): lf.get("username", ""),
            lf.get("password_field", "password"): lf.get("password", ""),
        }
        login = c.post(login_url, data=form)
        login.raise_for_status()
        resp = c.get(export_path)
        resp.raise_for_status()
        return _parse_csv(resp.text)
    finally:
        if owned:
            c.close()
