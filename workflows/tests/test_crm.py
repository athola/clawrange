"""Tests for the pluggable CRM (FR-4)."""

from __future__ import annotations

import pytest

from crm import get_adapter
from crm.sqlite_adapter import SQLiteCRM


@pytest.fixture()
def crm(tmp_path):
    c = SQLiteCRM(str(tmp_path / "crm.db"))
    c.init()
    return c


def _lead(email, **kw):
    base = {"name": "X", "email": email, "source": "portal"}
    base.update(kw)
    return base


# ─── schema / upsert / list ───────────────────────────────────────


def test_init_is_idempotent(tmp_path):
    c = SQLiteCRM(str(tmp_path / "crm.db"))
    c.init()
    c.init()  # second call must not raise
    assert c.health()["status"] == "ok"


def test_upsert_inserts_new(crm):
    n = crm.upsert("leads", [_lead("a@x.com"), _lead("b@x.com")], "email")
    assert n == 2
    rows = crm.list("leads")
    assert {r["email"] for r in rows} == {"a@x.com", "b@x.com"}
    assert all(r["status"] == "new" for r in rows)
    assert all(r["created_at"] for r in rows)


def test_upsert_updates_existing_on_key(crm):
    crm.upsert("leads", [_lead("a@x.com", name="First")], "email")
    crm.upsert("leads", [_lead("a@x.com", name="Second", status="qualified")], "email")
    rows = crm.list("leads")
    assert len(rows) == 1
    assert rows[0]["name"] == "Second"
    assert rows[0]["status"] == "qualified"


def test_list_with_filter(crm):
    crm.upsert(
        "leads",
        [_lead("a@x.com", status="new"), _lead("b@x.com", status="won")],
        "email",
    )
    won = crm.list("leads", filters={"status": "won"})
    assert [r["email"] for r in won] == ["b@x.com"]


# ─── run_template / read-only guard ───────────────────────────────


def test_run_template_select(crm):
    crm.upsert("leads", [_lead("a@x.com"), _lead("b@x.com")], "email")
    tmpl = {"name": "count", "sql": "SELECT COUNT(*) AS n FROM leads"}
    rows = crm.run_template(tmpl, {})
    assert rows[0]["n"] == 2


def test_run_template_binds_params(crm):
    crm.upsert(
        "leads",
        [_lead("a@x.com", status="won"), _lead("b@x.com", status="new")],
        "email",
    )
    tmpl = {
        "name": "by_status",
        "sql": "SELECT COUNT(*) AS n FROM leads WHERE status = :status",
    }
    rows = crm.run_template(tmpl, {"status": "won"})
    assert rows[0]["n"] == 1


def test_run_template_rejects_mutation(crm):
    crm.upsert("leads", [_lead("a@x.com")], "email")
    bad = {"name": "evil", "sql": "DELETE FROM leads"}
    with pytest.raises(ValueError, match="read-only|SELECT"):
        crm.run_template(bad, {})
    # data untouched
    assert (
        crm.run_template({"name": "c", "sql": "SELECT COUNT(*) AS n FROM leads"}, {})[
            0
        ]["n"]
        == 1
    )


def test_run_template_rejects_multi_statement(crm):
    crm.upsert("leads", [_lead("a@x.com")], "email")
    with pytest.raises(ValueError):
        crm.run_template({"name": "x", "sql": "SELECT 1; DROP TABLE leads"}, {})


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM leads",
        "UPDATE leads SET status = 'won'",
        "DROP TABLE leads",
        # A data-modifying CTE reads as a WITH statement to a text check.
        "WITH x AS (SELECT 1) DELETE FROM leads",
    ],
)
def test_readonly_connection_blocks_writes_without_text_guard(crm, monkeypatch, sql):
    """The text guard off, the read-only connection alone must refuse."""
    import crm.sqlite_adapter as sqlite_adapter

    crm.upsert("leads", [_lead("a@x.com")], "email")
    monkeypatch.setattr(sqlite_adapter, "_guard_readonly", lambda sql: None)
    with pytest.raises(ValueError, match="rejected"):
        crm.run_template({"name": "x", "sql": sql}, {})
    rows = crm.list("leads")
    assert [r["status"] for r in rows] == ["new"]


# ─── activities (time-series substrate) ───────────────────────────


def test_record_activity(crm):
    crm.upsert("leads", [_lead("a@x.com")], "email")
    lead_id = crm.list("leads")[0]["id"]
    crm.add_activity(lead_id, "call", "left voicemail", ts="2026-06-01T10:00:00+00:00")
    acts = crm.list("activities", filters={"lead_id": lead_id})
    assert acts[0]["kind"] == "call"


# ─── factory ──────────────────────────────────────────────────────


def test_get_adapter_sqlite(tmp_path):
    cfg = {"adapter": "sqlite", "sqlite": {"path": str(tmp_path / "c.db")}}
    a = get_adapter(cfg)
    assert isinstance(a, SQLiteCRM)


def test_get_adapter_unknown_raises():
    with pytest.raises(ValueError, match="adapter"):
        get_adapter({"adapter": "oracle"})


def test_health_reports_counts(crm):
    crm.upsert("leads", [_lead("a@x.com")], "email")
    h = crm.health()
    assert h["status"] == "ok"
    assert h["leads"] == 1


# ─── query layer: coercion + time-series (FR-5.1, FR-4.2) ─────────

from datetime import UTC, datetime, timedelta  # noqa: E402

from crm.query import coerce_params, find_template, run_query  # noqa: E402


def _iso_days_ago(n):
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


NEW_LEADS_COUNT = {
    "name": "new_leads_count",
    "params": {"window": {"type": "duration", "default": "7d"}},
    "sql": "SELECT COUNT(*) AS n FROM leads WHERE created_at >= :since",
}
LEADS_BY_STATUS = {
    "name": "leads_by_status",
    "params": {},
    "sql": "SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY n DESC",
}
LEADS_OVER_TIME = {
    "name": "leads_over_time",
    "params": {
        "bucket": {
            "type": "enum",
            "values": ["day", "week"],
            "default": "week",
            "bind": "fmt",
            "map": {"day": "%Y-%m-%d", "week": "%Y-%W"},
        },
        "window": {"type": "duration", "default": "60d"},
    },
    "sql": (
        "SELECT strftime(:fmt, created_at) AS bucket, COUNT(*) AS n "
        "FROM leads WHERE created_at >= :since GROUP BY bucket ORDER BY bucket"
    ),
}
LEAD_LOOKUP = {
    "name": "lead_lookup",
    "params": {"q": {"type": "string"}},
    "sql": (
        "SELECT * FROM leads WHERE email LIKE :like ESCAPE '\\' "
        "OR name LIKE :like ESCAPE '\\' LIMIT 10"
    ),
}


def test_coerce_duration_binds_since():
    binds = coerce_params(NEW_LEADS_COUNT, {"window": "7d"})
    assert "since" in binds
    # 'since' should be ~7 days ago
    since = datetime.fromisoformat(binds["since"])
    assert 6 < (datetime.now(UTC) - since).days < 8


def test_coerce_enum_with_map():
    binds = coerce_params(LEADS_OVER_TIME, {"bucket": "day", "window": "30d"})
    assert binds["fmt"] == "%Y-%m-%d"
    assert "since" in binds


def test_coerce_enum_rejects_bad_value():
    with pytest.raises(ValueError, match="one of"):
        coerce_params(LEADS_OVER_TIME, {"bucket": "year"})


def test_coerce_string_binds_like():
    binds = coerce_params(LEAD_LOOKUP, {"q": "acme"})
    assert binds["q"] == "acme"
    assert binds["like"] == "%acme%"


def test_coerce_string_required():
    with pytest.raises(ValueError, match="required"):
        coerce_params(LEAD_LOOKUP, {})


def test_new_leads_count_window(crm):
    crm.upsert(
        "leads",
        [
            _lead("recent@x.com", created_at=_iso_days_ago(2)),
            _lead("old@x.com", created_at=_iso_days_ago(40)),
        ],
        "email",
    )
    rows = run_query(crm, NEW_LEADS_COUNT, {"window": "7d"})
    assert rows[0]["n"] == 1


def test_leads_by_status_groups(crm):
    crm.upsert(
        "leads",
        [
            _lead("a@x.com", status="new"),
            _lead("b@x.com", status="new"),
            _lead("c@x.com", status="won"),
        ],
        "email",
    )
    rows = run_query(crm, LEADS_BY_STATUS, {})
    by = {r["status"]: r["n"] for r in rows}
    assert by == {"new": 2, "won": 1}


def test_leads_over_time_weekly_buckets(crm):
    # three leads in one week, one in a different week
    crm.upsert(
        "leads",
        [
            _lead("a@x.com", created_at="2026-01-05T10:00:00+00:00"),
            _lead("b@x.com", created_at="2026-01-06T10:00:00+00:00"),
            _lead("c@x.com", created_at="2026-01-07T10:00:00+00:00"),
            _lead("d@x.com", created_at="2026-03-01T10:00:00+00:00"),
        ],
        "email",
    )
    rows = run_query(crm, LEADS_OVER_TIME, {"bucket": "week", "window": "9999d"})
    counts = sorted(r["n"] for r in rows)
    assert counts == [1, 3]


def test_find_template():
    templates = [NEW_LEADS_COUNT, LEADS_BY_STATUS]
    assert find_template(templates, "leads_by_status") is LEADS_BY_STATUS
    assert find_template(templates, "nope") is None


# ─── REST adapter seam (FR-4.3) ───────────────────────────────────

import httpx  # noqa: E402

from crm.rest_adapter import RestCRM  # noqa: E402


def test_get_adapter_rest_returns_restcrm():
    a = get_adapter({"adapter": "rest", "rest": {"base_url": "https://crm.example"}})
    assert isinstance(a, RestCRM)


def test_rest_run_template_raises_documented():
    a = RestCRM({"base_url": "https://crm.example"})
    with pytest.raises(NotImplementedError, match="server-side report"):
        a.run_template({"name": "x", "sql": "SELECT 1"}, {})


@pytest.mark.parametrize(
    ("auth", "headers"),
    [
        ({"kind": "bearer", "token": "T"}, {"Authorization": "Bearer T"}),
        ({"kind": "api_key", "value": "K"}, {"X-API-Key": "K"}),
        ({"kind": "api_key", "header": "X-Key", "value": "K"}, {"X-Key": "K"}),
        ({"kind": "api_key", "api_key": {"header": "X-N", "value": "K"}}, {"X-N": "K"}),
        (
            {"kind": "basic", "basic": {"username": "u", "password": "p"}},
            {"Authorization": "Basic dTpw"},
        ),
        ({"kind": "bearer"}, {}),
        (None, {}),
    ],
)
def test_rest_auth_headers_share_the_connector_builder(auth, headers):
    """#14: one auth-header builder; the REST CRM keeps its flat api_key
    shape and gains the connector's nested api_key and basic kinds."""
    assert RestCRM({"base_url": "https://x", "auth": auth})._headers() == headers


def test_rest_health_reports_configured_state():
    assert RestCRM({"base_url": "https://x"}).health()["status"] == "configured"
    assert RestCRM({}).health()["status"] == "unconfigured"


def test_rest_upsert_via_injected_client():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":  # upsert_key lookup: not there yet
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1})

    client = httpx.Client(
        base_url="https://crm.example",
        headers={"Authorization": "Bearer t"},
        transport=httpx.MockTransport(handler),
    )
    crm_rest = RestCRM(
        {
            "base_url": "https://crm.example",
            "objects": {"leads": {"create": "POST /leads", "list": "GET /leads"}},
        },
        client=client,
    )
    n = crm_rest.upsert("leads", [{"email": "a@x.com", "name": "A"}], "email")
    assert n == 1
    post = seen[-1]
    assert post.method == "POST"
    assert post.url.path == "/leads"
    assert post.headers["Authorization"] == "Bearer t"


# ─── review fixes: upsert integrity, SQL guard, limits ────────────


def test_upsert_rejects_non_unique_key(crm):
    """ON CONFLICT(col) needs a UNIQUE index; only email has one. A non-unique
    key used to raise mid-loop and leave earlier rows in an open transaction."""
    with pytest.raises(ValueError, match="unique"):
        crm.upsert("leads", [_lead("a@x.com", phone="1")], "phone")


def test_upsert_failure_rolls_back_whole_batch(crm, monkeypatch):
    real = crm._rw()

    class Exploding:
        """Proxy that fails on the second INSERT."""

        def __init__(self):
            self.n = 0

        def execute(self, sql, *a):
            if sql.startswith("INSERT"):
                self.n += 1
                if self.n == 2:
                    raise RuntimeError("disk full")
            return real.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(crm, "_rw", lambda: Exploding())
    with pytest.raises(RuntimeError):
        crm.upsert("leads", [_lead("a@x.com"), _lead("b@x.com")], "email")
    monkeypatch.undo()
    real.commit()  # a later commit must not flush the half-batch
    assert crm.list("leads") == []


def test_template_with_keyword_text_in_literal_is_allowed(crm):
    """The old substring blacklist rejected harmless SELECTs whose string
    literals contained words like ' delete '."""
    crm.upsert("leads", [_lead("a@x.com", name="please delete me")], "email")
    rows = crm.run_template(
        {"name": "t", "sql": "SELECT name FROM leads WHERE name LIKE '% delete %'"},
        {},
    )
    assert rows == [{"name": "please delete me"}]


def test_template_cte_mutation_rejected(crm):
    crm.upsert("leads", [_lead("a@x.com")], "email")
    with pytest.raises(ValueError):
        crm.run_template(
            {"name": "t", "sql": "WITH x AS (SELECT 1) DELETE FROM leads"}, {}
        )
    assert len(crm.list("leads")) == 1


def test_template_rows_are_capped(crm, monkeypatch):
    import crm.sqlite_adapter as sa

    monkeypatch.setattr(sa, "MAX_TEMPLATE_ROWS", 3)
    crm.upsert("leads", [_lead(f"{i}@x.com") for i in range(5)], "email")
    assert len(crm.run_template({"name": "t", "sql": "SELECT * FROM leads"}, {})) == 3


def test_list_rejects_unknown_filter_column(crm):
    with pytest.raises(ValueError):
        crm.list("leads", filters={"status = 'x' OR 1=1 --": "y"})


def test_unique_keys_match_schema(crm):
    """UNIQUE_LEAD_KEYS must mirror the UNIQUE indexes in _SCHEMA."""
    import crm.sqlite_adapter as sa

    conn = crm._rw()
    unique = set()
    for idx in conn.execute("PRAGMA index_list(leads)").fetchall():
        if idx["unique"]:
            for col in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall():
                unique.add(col["name"])
    assert unique == set(sa.UNIQUE_LEAD_KEYS)


def test_rest_adapter_reuses_one_client():
    """RestCRM built a fresh httpx.Client per call and never closed it."""
    from crm.rest_adapter import RestCRM

    r = RestCRM({"base_url": "https://crm.example"})
    assert r._http() is r._http()
