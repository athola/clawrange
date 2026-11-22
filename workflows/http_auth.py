"""Outbound request auth headers for profile-configured HTTP services.

Connector sources and the REST CRM adapter read the same ``auth`` block from
a profile; this is the one place it becomes request headers.
"""

from __future__ import annotations

import base64


def auth_headers(auth: dict | None) -> dict[str, str]:
    """Headers for the static auth kinds (none/api_key/bearer/basic).

    ``api_key`` takes a nested ``{api_key: {header, value}}`` (the connector
    shape in the profile guide) or flat ``header``/``value`` keys (the REST
    CRM shape); the header defaults to ``X-API-Key``. Anything unset or
    unknown sends no auth header.
    """
    auth = auth or {}
    kind = auth.get("kind", "none")
    if kind == "bearer" and auth.get("token"):
        return {"Authorization": f"Bearer {auth['token']}"}
    if kind == "api_key":
        ak = auth.get("api_key") or auth
        if ak.get("value"):
            return {ak.get("header", "X-API-Key"): ak["value"]}
    if kind == "basic":
        b = auth.get("basic") or {}
        if b.get("username") is not None:
            raw = f"{b.get('username', '')}:{b.get('password', '')}".encode()
            return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
    return {}
