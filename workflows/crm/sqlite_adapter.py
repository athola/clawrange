"""SQLite CRM — the default, fully-testable backend.

Contract-first like ``brain_db``: all SQL lives here. Leads + activities
tables give both relational (group/count) and time-series (bucketed over
``created_at`` / ``ts``) query substrates. Query templates execute over a
read-only connection so a misconfigured template can never mutate data.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from .adapter import CRMAdapter

logger = logging.getLogger("clawrange.crm")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT,
    email      TEXT UNIQUE,
    phone      TEXT,
    source     TEXT,
    status     TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id),
    kind    TEXT,
    detail  TEXT,
    ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_activities_ts ON activities(ts);
"""

_LEAD_COLUMNS = (
    "name",
    "email",
    "phone",
    "source",
    "status",
    "created_at",
    "updated_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteCRM(CRMAdapter):
    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    # ─── lifecycle ────────────────────────────────────────────────

    def init(self) -> None:
        # check_same_thread=False: FastAPI serves sync endpoints from a
        # threadpool while APScheduler fires jobs from its own thread. The
        # service runs a single uvicorn worker (hard requirement), so there
        # is no cross-process contention; SQLite's own locking plus short,
        # infrequent writes make cross-thread use of this one connection safe.
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn

    def _rw(self) -> sqlite3.Connection:
        if self._conn is None:
            self.init()
        assert self._conn is not None
        return self._conn

    def _ro(self) -> sqlite3.Connection:
        """A read-only connection for query templates (mutation-proof)."""
        self._rw()  # ensure file + schema exist
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ─── writes ───────────────────────────────────────────────────

    def upsert(self, object: str, records: list[dict], upsert_key: str) -> int:
        if object != "leads":
            raise ValueError(f"SQLiteCRM only supports object 'leads', got '{object}'")
        if upsert_key not in _LEAD_COLUMNS:
            raise ValueError(f"upsert_key '{upsert_key}' is not a lead column")

        conn = self._rw()
        now = _now()
        written = 0
        for rec in records:
            row = {k: rec[k] for k in _LEAD_COLUMNS if k in rec}
            row.setdefault("created_at", now)
            row["updated_at"] = now
            row.setdefault("status", "new")
            if upsert_key not in row or row[upsert_key] in (None, ""):
                logger.warning("crm.upsert: skipping record missing %s", upsert_key)
                continue
            cols = list(row)
            placeholders = ", ".join(f":{c}" for c in cols)
            updates = ", ".join(
                f"{c}=excluded.{c}" for c in cols if c not in (upsert_key, "created_at")
            )
            sql = (
                f"INSERT INTO leads ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT({upsert_key}) DO UPDATE SET {updates}"
            )
            conn.execute(sql, row)
            written += 1
        conn.commit()
        return written

    def add_activity(
        self, lead_id: int, kind: str, detail: str = "", ts: str | None = None
    ) -> None:
        conn = self._rw()
        conn.execute(
            "INSERT INTO activities (lead_id, kind, detail, ts) VALUES (?, ?, ?, ?)",
            (lead_id, kind, detail, ts or _now()),
        )
        conn.commit()

    # ─── reads ────────────────────────────────────────────────────

    def list(
        self, object: str, filters: dict | None = None, limit: int = 100
    ) -> list[dict]:
        if object not in ("leads", "activities"):
            raise ValueError(f"unknown object '{object}'")
        conn = self._rw()
        where = ""
        params: list = []
        if filters:
            clauses = []
            for k, v in filters.items():
                clauses.append(f"{k} = ?")
                params.append(v)
            where = " WHERE " + " AND ".join(clauses)
        order = "created_at DESC" if object == "leads" else "ts DESC"
        sql = f"SELECT * FROM {object}{where} ORDER BY {order} LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def run_template(self, template: dict, params: dict) -> list[dict]:
        sql = (template or {}).get("sql", "")
        _guard_readonly(sql)
        conn = self._ro()
        try:
            return [dict(r) for r in conn.execute(sql, params or {}).fetchall()]
        finally:
            conn.close()

    def health(self) -> dict:
        try:
            conn = self._rw()
            leads = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
            return {
                "status": "ok",
                "adapter": "sqlite",
                "path": self.path,
                "leads": leads,
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"status": "error", "adapter": "sqlite", "error": str(exc)}


def _guard_readonly(sql: str) -> None:
    """Reject anything that is not a single read-only SELECT/WITH statement.

    Defense in depth: ``run_template`` also executes on a ``mode=ro``
    connection, but a clear error at the template layer beats a cryptic
    sqlite ``readonly database`` error and blocks multi-statement payloads.
    """
    s = (sql or "").strip().rstrip(";").lstrip()
    if not s:
        raise ValueError("template SQL is empty")
    lowered = s.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError(f"template SQL must be read-only SELECT, got: {sql[:40]!r}")
    # No stacked statements (a remaining ';' means a second statement).
    if ";" in s:
        raise ValueError("template SQL must be a single statement (no ';')")
    forbidden = (
        " insert ",
        " update ",
        " delete ",
        " drop ",
        " alter ",
        " create ",
        " replace ",
        " attach ",
        " pragma ",
    )
    padded = f" {lowered} "
    for kw in forbidden:
        if kw in padded:
            raise ValueError(f"template SQL contains forbidden keyword '{kw.strip()}'")
