"""Imperative shell: fetch Home Assistant, Uptime Kuma and census state.

Every required fetch either returns data or raises ``UpstreamError`` with a
short reason. The reason never contains a URL or the token (httpx error text
embeds the URL, so it is replaced by a fixed phrase), so it is safe to log,
store and show to an authenticated caller. Verification is never switched
off: a private CA is passed as an ``ssl`` context built from ``ca_cert``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from errors import UpstreamError

from .config import HomelabConfig
from .models import Census, EntityState, Monitor, Observation
from .parse import (
    devices_from_census,
    entities_from_states,
    has_mac_table,
    kev_entries,
    mgmt_ports_skipping,
    monitors_from_kuma,
    parse_time,
    router_version,
)

logger = logging.getLogger("clawrange.homelab.sources")

TIMEOUT = httpx.Timeout(10.0)


class NotPublished(UpstreamError):
    """HTTP 404: the file or page is not there (an optional census part)."""


def _reason(exc: Exception) -> str:
    """A short, URL-free description of an httpx failure."""
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.ConnectError):
        cause = str(exc).lower()
        if "certificate" in cause or "ssl" in cause or "tls" in cause:
            return "TLS verification failed"
        return "connection failed"
    if isinstance(exc, ValueError):
        return "response was not valid JSON"
    return f"request failed ({type(exc).__name__})"


def _kuma_verify(cfg: HomelabConfig) -> ssl.SSLContext | bool:
    """TLS verification for Kuma: the system store, or ``ca_cert`` when set."""
    if not cfg.kuma_ca_cert:
        return True
    try:
        return ssl.create_default_context(cafile=cfg.kuma_ca_cert)
    except (OSError, ssl.SSLError) as exc:
        raise UpstreamError("CA certificate could not be loaded") from exc


async def _get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    verify: ssl.SSLContext | bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Any:
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, verify=verify, transport=transport
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            raise NotPublished(_reason(exc)) from None
        raise UpstreamError(_reason(exc)) from None


async def _optional_json(
    url: str, transport: httpx.AsyncBaseTransport | None
) -> tuple[Any | None, str | None]:
    """An optional census file: (payload, reason it could not be read).

    Only a 404 means "not published" and carries no reason. Every other
    failure (5xx, timeout, bad JSON) comes back as a reason, so it shows up
    as a sub-source error instead of passing for a missing file.
    """
    try:
        return await _get_json(url, transport=transport), None
    except NotPublished:
        return None, None
    except UpstreamError as exc:
        return None, str(exc)


# ─── Home Assistant ─────────────────────────────────────────────────


async def fetch_ha_states(
    cfg: HomelabConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[EntityState, ...]:
    payload = await _get_json(
        f"{cfg.ha_url}/api/states",
        headers={"Authorization": f"Bearer {cfg.ha_token}"},
        transport=transport,
    )
    if not isinstance(payload, list):
        raise UpstreamError("unexpected response shape")
    return entities_from_states(payload)


async def call_ha_service(
    cfg: HomelabConfig,
    domain: str,
    service: str,
    entity_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Call one HA service on one entity. Raises UpstreamError on failure."""
    url = (
        f"{cfg.ha_url}/api/services/{quote(domain, safe='')}/{quote(service, safe='')}"
    )
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {cfg.ha_token}"},
                json={"entity_id": entity_id},
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamError(_reason(exc)) from None


# ─── Uptime Kuma ────────────────────────────────────────────────────


async def fetch_kuma(
    cfg: HomelabConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[Monitor, ...]:
    verify = _kuma_verify(cfg)
    slug = quote(cfg.kuma_slug, safe="")
    page, beats = await asyncio.gather(
        _get_json(
            f"{cfg.kuma_url}/api/status-page/{slug}",
            verify=verify,
            transport=transport,
        ),
        _get_json(
            f"{cfg.kuma_url}/api/status-page/heartbeat/{slug}",
            verify=verify,
            transport=transport,
        ),
    )
    if not isinstance(page, dict) or not isinstance(page.get("publicGroupList"), list):
        raise UpstreamError("unexpected response shape")
    if not isinstance(beats, dict) or not isinstance(beats.get("heartbeatList"), dict):
        raise UpstreamError("unexpected heartbeat shape")
    monitors = monitors_from_kuma(page, beats)
    if monitors and all(m.status is None for m in monitors):
        # Listed but never beaten: reading that as "nothing is down" would
        # hide a dead Kuma.
        raise UpstreamError("no heartbeat data for any monitor")
    return monitors


# ─── Census ─────────────────────────────────────────────────────────


def _when(payload: Any) -> datetime | None:
    return parse_time(payload.get("when")) if isinstance(payload, dict) else None


async def fetch_census(
    cfg: HomelabConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> Census:
    base = cfg.census_url
    devices, scan, router, kev = await asyncio.gather(
        _get_json(f"{base}/devices.json", transport=transport),
        _optional_json(f"{base}/rustscan-hex.json", transport),
        _optional_json(f"{base}/router.json", transport),
        _optional_json(f"{base}/kev-mikrotik.json", transport),
    )
    when = _when(devices)
    raw = devices.get("raw") if isinstance(devices, dict) else None
    if when is None or not isinstance(raw, str):
        raise UpstreamError("unexpected response shape")
    if not has_mac_table(raw):
        # A login banner or an error page is not a network with no devices.
        raise UpstreamError("census has no lease/ARP table")
    found = devices_from_census(raw)
    if not found:
        raise UpstreamError("census parsed no devices")

    part_errors: dict[str, str] = {}

    def usable(part: str, result: tuple[Any, str | None]) -> Any | None:
        payload, reason = result
        if reason is not None:
            part_errors[part] = reason
        return payload

    scan_payload = usable("ports", scan)
    ports = None
    if scan_payload is not None:
        ports, skipped = mgmt_ports_skipping(scan_payload)
        if ports is None:
            part_errors["ports"] = "unexpected response shape"
        elif skipped:
            part_errors["ports"] = f"{skipped} malformed open entries skipped"

    router_payload = usable("router", router)
    version = None
    if router_payload is not None:
        if isinstance(router_payload, dict) and isinstance(
            router_payload.get("raw"), str
        ):
            version = router_version(router_payload["raw"])
            if version is None:
                part_errors["router"] = "no version in router output"
        else:
            part_errors["router"] = "unexpected response shape"

    kev_payload = usable("kev", kev)
    kev_list = None
    if kev_payload is not None:
        if isinstance(kev_payload, dict) and isinstance(
            kev_payload.get("entries"), list
        ):
            kev_list = kev_entries(kev_payload)
        else:
            part_errors["kev"] = "unexpected response shape"

    return Census(
        when=when,
        devices=found,
        mgmt_ports=ports,
        router_version=version,
        kev=kev_list,
        ports_when=_when(scan_payload),
        router_when=_when(router_payload),
        kev_when=_when(kev_payload),
        part_errors=part_errors,
    )


# ─── One pass ───────────────────────────────────────────────────────


async def observe(
    cfg: HomelabConfig,
    now: datetime,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Observation:
    """Fetch every configured source concurrently.

    A failed source has value None and a short reason in ``errors``; an
    unconfigured source is never fetched.
    """
    configured = cfg.configured
    fetchers: dict[str, Awaitable[Any]] = {}
    if "ha" in configured:
        fetchers["ha"] = fetch_ha_states(cfg, transport=transport)
    if "kuma" in configured:
        fetchers["kuma"] = fetch_kuma(cfg, transport=transport)
    if "census" in configured:
        fetchers["census"] = fetch_census(cfg, transport=transport)
    results = await asyncio.gather(*fetchers.values(), return_exceptions=True)
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for source, result in zip(fetchers, results, strict=True):
        if isinstance(result, UpstreamError):
            errors[source] = str(result)
            # Reasons are URL-free by construction (see _reason).
            logger.warning("homelab %s failed: %s", source, result)
        elif isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            logger.error(
                "homelab %s: unexpected %s",
                source,
                type(result).__name__,
                exc_info=result,
            )
            errors[source] = f"unexpected error ({type(result).__name__})"
        else:
            data[source] = result
    census = data.get("census")
    if census is not None:
        for part, reason in census.part_errors.items():
            errors[f"census.{part}"] = reason
            logger.warning("homelab census.%s: %s", part, reason)
    return Observation(
        now=now,
        configured=configured,
        ha=data.get("ha"),
        kuma=data.get("kuma"),
        census=census,
        errors=errors,
    )
