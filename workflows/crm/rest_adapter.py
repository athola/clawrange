"""REST CRM adapter — a thin, documented stub that proves the seam.

Its job is to demonstrate that the pluggable ``CRMAdapter`` interface is
real: flip ``crm.adapter: rest`` in a profile and lead writes/reads go to
an HTTP CRM instead of the bundled SQLite one — no pipeline changes.

``upsert``/``list`` are implemented against a generic REST shape (an
``objects`` map of ``"<METHOD> /path"`` endpoints: ``create`` and ``list``
required for upsert, optional ``update`` with an ``{id}`` placeholder) and
are exercised in
tests via an injected httpx client. ``run_template`` deliberately raises:
SaaS CRMs do not run ad-hoc SQL, so analytics there means a server-side
report you map in — out of scope for this reference stub.
"""

from __future__ import annotations

import logging

import httpx

from http_auth import auth_headers

from .adapter import CRMAdapter, RecordList

logger = logging.getLogger("clawrange.crm.rest")

TIMEOUT_S = 15.0  # per request to the remote CRM


class RestCRM(CRMAdapter):
    def __init__(self, cfg: dict, client: httpx.Client | None = None):
        self.cfg = cfg or {}
        self.base_url = self.cfg.get("base_url", "")
        self.objects = self.cfg.get("objects", {}) or {}
        self._client = client

    def _headers(self) -> dict:
        return auth_headers(self.cfg.get("auth"))

    def _http(self) -> httpx.Client:
        # Built once and reused: a client per call leaked connection pools.
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url, headers=self._headers(), timeout=TIMEOUT_S
            )
        return self._client

    def _endpoint(self, object: str, action: str) -> tuple[str, str]:
        spec = (self.objects.get(object) or {}).get(action)
        if not spec:
            raise ValueError(f"no '{action}' endpoint configured for object '{object}'")
        method, path = spec.split(" ", 1)
        return method.upper(), path.strip()

    def init(self) -> None:
        if not self.base_url:
            logger.warning("RestCRM: no base_url configured (adapter unconfigured)")

    def _existing(self, object: str, upsert_key: str, value) -> dict | None:
        """The remote record whose ``upsert_key`` equals ``value``, if any.

        Asks the list endpoint with ``?<upsert_key>=<value>`` and confirms
        the match client-side, since a CRM may ignore unknown filters.
        """
        for row in self.list(object, filters={upsert_key: value}, limit=1000):
            if isinstance(row, dict) and str(row.get(upsert_key, "")).casefold() == (
                str(value).casefold()
            ):
                return row
        return None

    def upsert(self, object: str, records: list[dict], upsert_key: str) -> int:
        """Create records whose ``upsert_key`` is new; update (with an
        ``update`` endpoint such as ``"PATCH /leads/{id}"``) or skip the
        rest, so a re-sync never duplicates remote leads."""
        method, path = self._endpoint(object, "create")
        try:
            self._endpoint(object, "list")
        except ValueError as exc:
            raise ValueError(
                f"RestCRM.upsert needs a 'list' endpoint for '{object}' to honor "
                f"upsert_key '{upsert_key}'"
            ) from exc
        update = (self.objects.get(object) or {}).get("update")
        client = self._http()
        written = 0
        for rec in records:
            value = rec.get(upsert_key)
            if value in (None, ""):
                logger.warning("RestCRM.upsert: skipping record missing %s", upsert_key)
                continue
            existing = self._existing(object, upsert_key, value)
            if existing is None:
                resp = client.request(method, path, json=rec)
            elif update:
                u_method, u_path = self._endpoint(object, "update")
                rid = existing.get("id", "")
                resp = client.request(u_method, u_path.format(id=rid), json=rec)
            else:
                continue  # already present and no update endpoint: skip
            resp.raise_for_status()
            written += 1
        return written

    def list(
        self, object: str, filters: dict | None = None, limit: int = 100
    ) -> list[dict]:
        method, path = self._endpoint(object, "list")
        resp = self._http().request(method, path, params=filters or {})
        resp.raise_for_status()
        data = resp.json()
        rows = data if isinstance(data, list) else data.get("results", [])
        return rows[:limit]

    def run_template(self, template: dict, params: dict) -> RecordList:
        raise NotImplementedError(
            "RestCRM.run_template: REST/SaaS CRMs do not execute ad-hoc SQL. "
            "Define a server-side report or saved view in your CRM and map it "
            "here, or use the SQLite adapter for local relational/time-series "
            "analytics."
        )

    def health(self) -> dict:
        # No base_url: /healthz/crm is public, and the URL can carry a
        # tenant id or credential.
        return {
            "status": "configured" if self.base_url else "unconfigured",
            "adapter": "rest",
        }
