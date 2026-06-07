"""REST CRM adapter — a thin, documented stub that proves the seam.

Its job is to demonstrate that the pluggable ``CRMAdapter`` interface is
real: flip ``crm.adapter: rest`` in a profile and lead writes/reads go to
an HTTP CRM instead of the bundled SQLite one — no pipeline changes.

``upsert``/``list`` are implemented against a generic REST shape (an
``objects`` map of ``"<METHOD> /path"`` endpoints) and are exercised in
tests via an injected httpx client. ``run_template`` deliberately raises:
SaaS CRMs do not run ad-hoc SQL, so analytics there means a server-side
report you map in — out of scope for this reference stub.
"""

from __future__ import annotations

import logging

import httpx

from .adapter import CRMAdapter

logger = logging.getLogger("clawrange.crm.rest")


class RestCRM(CRMAdapter):
    def __init__(self, cfg: dict, client: httpx.Client | None = None):
        self.cfg = cfg or {}
        self.base_url = self.cfg.get("base_url", "")
        self.objects = self.cfg.get("objects", {}) or {}
        self._client = client

    def _headers(self) -> dict:
        auth = self.cfg.get("auth", {}) or {}
        kind = auth.get("kind")
        if kind == "bearer" and auth.get("token"):
            return {"Authorization": f"Bearer {auth['token']}"}
        if kind == "api_key" and auth.get("value"):
            return {auth.get("header", "X-API-Key"): auth["value"]}
        return {}

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(base_url=self.base_url, headers=self._headers(), timeout=15)

    def _endpoint(self, object: str, action: str) -> tuple[str, str]:
        spec = (self.objects.get(object) or {}).get(action)
        if not spec:
            raise ValueError(f"no '{action}' endpoint configured for object '{object}'")
        method, path = spec.split(" ", 1)
        return method.upper(), path.strip()

    def init(self) -> None:
        if not self.base_url:
            logger.warning("RestCRM: no base_url configured (adapter unconfigured)")

    def upsert(self, object: str, records: list[dict], upsert_key: str) -> int:
        method, path = self._endpoint(object, "create")
        client = self._http()
        written = 0
        for rec in records:
            resp = client.request(method, path, json=rec)
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

    def run_template(self, template: dict, params: dict) -> list[dict]:
        raise NotImplementedError(
            "RestCRM.run_template: REST/SaaS CRMs do not execute ad-hoc SQL. "
            "Define a server-side report or saved view in your CRM and map it "
            "here, or use the SQLite adapter for local relational/time-series "
            "analytics."
        )

    def health(self) -> dict:
        return {
            "status": "configured" if self.base_url else "unconfigured",
            "adapter": "rest",
            "base_url": self.base_url,
        }
