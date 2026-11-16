"""Connector sources — fetch raw rows from a portal (FR-3.1).

``http_csv`` GETs a CSV export with optional auth. ``login_scrape`` first
POSTs a login form to establish an httpx session (cookie jar), then GETs an
export path behind that session. Both accept an injectable ``client`` so
tests use ``httpx.MockTransport`` instead of the network. An unconfigured
URL (usually an unset ``${VAR}``) raises ``ConnectorNotConfigured`` at sync
time, so the sync records an error instead of "ok (0 written)"; boot is
unaffected.
"""

from __future__ import annotations

import csv
import io
import logging
from urllib.parse import urljoin

import httpx

from http_auth import auth_headers

from .base import ConnectorNotConfigured, Record

logger = logging.getLogger("clawrange.connectors.sources")


def _looks_like_html(resp: httpx.Response) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    return "html" in ctype or resp.text.lstrip()[:1] == "<"


def _parse_csv(text: str) -> list[Record]:
    return list(csv.DictReader(io.StringIO(text)))


def _ensure_client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    """Return (client, owned); owned clients are closed by the caller."""
    if client is not None:
        return client, False
    return httpx.Client(timeout=30.0), True


def _csv_or_raise(resp: httpx.Response, source: str) -> list[Record]:
    if _looks_like_html(resp):
        # A rejected login or expired token often serves a login page with
        # a 200; parsing that as CSV would write junk leads.
        raise ValueError(
            f"{source}: export returned HTML, not CSV (auth likely failed)"
        )
    return _parse_csv(resp.text)


def http_csv(spec: dict, *, client: httpx.Client | None = None) -> list[Record]:
    url = spec.get("url")
    if not url:
        raise ConnectorNotConfigured("http_csv: no url configured")
    c, owned = _ensure_client(client)
    try:
        resp = c.get(url, headers=auth_headers(spec.get("auth")))
        resp.raise_for_status()
        return _csv_or_raise(resp, "http_csv")
    finally:
        if owned:
            c.close()


# 307/308 would make the client re-POST the form, password included, to the
# redirect target; 301/302/303 switch to a body-less GET.
_SAFE_LOGIN_REDIRECTS = {301, 302, 303}


def login_scrape(spec: dict, *, client: httpx.Client | None = None) -> list[Record]:
    lf = (spec.get("auth") or {}).get("login_form") or {}
    login_url = lf.get("login_url")
    export_path = lf.get("export_path")
    if not login_url or not export_path:
        raise ConnectorNotConfigured("login_scrape: missing login_url/export_path")
    c, owned = _ensure_client(client)
    try:
        form = {
            lf.get("username_field", "username"): lf.get("username", ""),
            lf.get("password_field", "password"): lf.get("password", ""),
        }
        # Don't follow: the session cookie is set on this response, and a
        # 307/308 would resend the password to wherever it points.
        login = c.post(login_url, data=form, follow_redirects=False)
        if login.status_code in (307, 308):
            raise ValueError(
                f"login_scrape: login answered a {login.status_code} redirect; "
                "refusing to resend credentials"
            )
        if login.status_code not in _SAFE_LOGIN_REDIRECTS:
            login.raise_for_status()
        resp = c.get(urljoin(login_url, export_path), follow_redirects=True)
        resp.raise_for_status()
        return _csv_or_raise(resp, "login_scrape")
    finally:
        if owned:
            c.close()
