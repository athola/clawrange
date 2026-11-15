"""API-wide bearer-token auth for the workflows service.

Every route requires ``Authorization: Bearer $PROXY_AUTH_TOKEN`` except a
short read-only allowlist: health checks, plus the tier and schedule GETs
that a monitoring dashboard polls. An unset token fails closed. It never
means "open", because the service can be reachable from a tailnet.

The expected token is read from ``llm_proxy.PROXY_AUTH_TOKEN`` so the chat
proxy and the rest of the API share one source of truth.
"""

import hmac
import logging
import re

from fastapi import HTTPException, Request

# (method, path regex) pairs reachable without a token. Keep this list
# read-only and free of anything that reveals brain, task or CRM contents.
_PUBLIC: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/healthz(/[^/]+)?$")),
    ("GET", re.compile(r"^/tier$")),
    ("GET", re.compile(r"^/sched$")),
    ("GET", re.compile(r"^/sched/[^/]+$")),
)


_log = logging.getLogger("clawrange.auth")


def warn_if_unset(token: str) -> bool:
    """Log loudly at startup when the token is empty. Returns True if unset."""
    if token:
        return False
    _log.error(
        "PROXY_AUTH_TOKEN is empty: every API route except the public "
        "allowlist will return 401 until it is set in .env"
    )
    return True


def is_public(method: str, path: str) -> bool:
    """True when ``method path`` is on the unauthenticated allowlist."""
    return any(m == method and rx.match(path) for m, rx in _PUBLIC)


def bearer_ok(authorization: str | None, expected: str) -> bool:
    """Constant-time check of an ``Authorization`` header against ``expected``.

    An empty ``expected`` (token unset) always returns False.
    """
    if not expected or not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(token.encode(), expected.encode())


async def require_token(request: Request) -> None:
    """App-wide dependency: 401 unless the route is public or the token matches."""
    if is_public(request.method, request.url.path):
        return
    import llm_proxy  # local: llm_proxy imports app-level modules lazily

    if not bearer_ok(request.headers.get("authorization"), llm_proxy.PROXY_AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")
