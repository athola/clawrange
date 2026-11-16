"""Regression tests for the PR #11 brain review findings (cluster K5).

Each class names the finding it guards: issue numbers (#27, #36, #46, #51)
or review ids (G2, G3, G4, G11, S5).
"""

import asyncio
import logging
import sqlite3
import sys

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import embeddings
from brain import create_brain_router
from brain_db import BrainDB, _chunk_text


@pytest.fixture
def db(tmp_path):
    d = BrainDB(str(tmp_path / "brain.db"))
    d.init_db()
    return d


@pytest.fixture
def vec_db(db):
    if not db._has_vec:
        pytest.skip("sqlite-vec unavailable")
    db.use_vector_table("vec_test_4", 4)
    return db


def _client(db: BrainDB) -> TestClient:
    app = FastAPI()
    app.include_router(create_brain_router(db), prefix="/brain")
    return TestClient(app)


def _cfg4() -> embeddings.EmbedConfig:
    return embeddings.EmbedConfig("u", "m", 4, "k")


# ─── #27 / G2: a failed sqlite-vec load is logged and reported ────


class TestVecStatus:
    def test_enabled_when_extension_loads(self, db):
        if not db._has_vec:
            pytest.skip("sqlite-vec unavailable")
        assert db.vec_status() == {"enabled": True, "reason": None}

    def test_missing_module_is_logged_and_reported(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "sqlite_vec", None)  # import raises
        with caplog.at_level(logging.ERROR):
            d = BrainDB(str(tmp_path / "x.db"))
        status = d.vec_status()
        assert status["enabled"] is False
        assert "sqlite_vec" in status["reason"]
        assert any(
            r.levelno == logging.ERROR and "sqlite-vec" in r.getMessage()
            for r in caplog.records
        )

    def test_load_failure_reason_is_kept(self, tmp_path, monkeypatch, caplog):
        import sqlite_vec

        def boom(conn):
            raise RuntimeError("extension blew up")

        monkeypatch.setattr(sqlite_vec, "load", boom)
        with caplog.at_level(logging.ERROR):
            d = BrainDB(str(tmp_path / "y.db"))
        status = d.vec_status()
        assert status == {
            "enabled": False,
            "reason": "RuntimeError: extension blew up",
        }
        assert "extension blew up" in caplog.text
        d.init_db()
        assert d.use_vector_table("vec_test_4", 4) is False


# ─── G4: healthz counts through the lock, not brain_db._conn ──────


class TestHealthCounts:
    def test_counts_pages_and_vectors(self, vec_db):
        vec_db.upsert_page("a", "Alpha", "note", "alpha body")
        vec_db.upsert_page("b", "Beta", "note", "beta body")
        chunk = vec_db.missing_embeddings(1)[0]
        vec_db.store_embeddings([(chunk["id"], [1.0, 0, 0, 0])])
        assert vec_db.health_counts() == {"pages": 2, "vectors": 1}

    def test_is_serialized(self):
        # _serialize_methods wraps public methods (functools.wraps sets this)
        assert hasattr(BrainDB.health_counts, "__wrapped__")
        assert hasattr(BrainDB.vec_status, "__wrapped__")


# ─── #27: /brain/search reports the mode it actually ran ──────────


class TestSearchModeReported:
    @pytest.fixture
    def client(self, vec_db, monkeypatch):
        monkeypatch.setattr(embeddings, "config", lambda env=None: _cfg4())
        vec_db.upsert_page("net/wifi", "WiFi", "note", "wireless access points")
        return _client(vec_db)

    def test_no_embeddings_reports_keyword_with_reason(self, client):
        data = client.get("/brain/search?q=wireless").json()
        assert data["mode"] == "keyword"
        assert data["fallback"] == "no embeddings yet"
        assert [r["slug"] for r in data["results"]] == ["net/wifi"]

    def test_failed_query_embed_reports_keyword(self, client, vec_db, monkeypatch):
        chunk = vec_db.missing_embeddings(1)[0]
        vec_db.store_embeddings([(chunk["id"], [1.0, 0, 0, 0])])

        async def fail(texts, cfg=None, batch=96):
            return None

        monkeypatch.setattr(embeddings, "embed_texts", fail)
        data = client.get("/brain/search?q=wireless&mode=hybrid").json()
        assert data["mode"] == "keyword"
        assert data["fallback"] == "query embedding failed"

    def test_real_hybrid_has_no_fallback(self, client, vec_db, monkeypatch):
        chunk = vec_db.missing_embeddings(1)[0]
        vec_db.store_embeddings([(chunk["id"], [1.0, 0, 0, 0])])

        async def ok(texts, cfg=None, batch=96):
            return [[1.0, 0, 0, 0] for _ in texts]

        monkeypatch.setattr(embeddings, "embed_texts", ok)
        data = client.get("/brain/search?q=wireless").json()
        assert data["mode"] == "hybrid"
        assert "fallback" not in data

    def test_keyword_mode_has_no_fallback(self, client):
        data = client.get("/brain/search?q=wireless&mode=keyword").json()
        assert data["mode"] == "keyword"
        assert "fallback" not in data


# ─── G3: query embeds use a short timeout, backfill keeps 45s ─────


def _capture_timeout(seen: list):
    async def fake(inputs, cfg):
        seen.append(cfg.timeout)
        data = [{"index": i, "embedding": [0.1] * cfg.dim} for i in range(len(inputs))]
        return httpx.Response(200, json={"data": data})

    return fake


class TestQueryTimeout:
    def test_query_timeout_is_short(self):
        assert 3.0 <= embeddings.QUERY_TIMEOUT <= 5.0
        assert embeddings.TIMEOUT == 45.0

    def test_embed_query_uses_query_timeout(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(embeddings, "request", _capture_timeout(seen))
        vec = asyncio.run(embeddings.embed_query("kitten", _cfg4()))
        assert vec is not None and len(vec) == 4
        assert seen == [embeddings.QUERY_TIMEOUT]

    def test_search_route_uses_query_timeout(self, vec_db, monkeypatch):
        seen: list = []
        monkeypatch.setattr(embeddings, "config", lambda env=None: _cfg4())
        monkeypatch.setattr(embeddings, "request", _capture_timeout(seen))
        vec_db.upsert_page("a", "Alpha", "note", "alpha body")
        chunk = vec_db.missing_embeddings(1)[0]
        vec_db.store_embeddings([(chunk["id"], [1.0, 0, 0, 0])])
        r = _client(vec_db).get("/brain/search?q=alpha")
        assert r.status_code == 200
        assert seen == [embeddings.QUERY_TIMEOUT]

    def test_backfill_keeps_long_timeout(self, vec_db, monkeypatch):
        seen: list = []
        monkeypatch.setattr(embeddings, "request", _capture_timeout(seen))
        vec_db.upsert_page("a", "Alpha", "note", "alpha body")
        asyncio.run(embeddings.backfill(vec_db, _cfg4()))
        assert seen == [embeddings.TIMEOUT]

    def test_api_key_not_in_repr(self):
        # #46 minor: a logged config must not leak the key
        cfg = embeddings.EmbedConfig("u", "m", 4, "secret-key")
        assert "secret-key" not in repr(cfg)

    def test_request_passes_config_timeout_to_httpx(self, monkeypatch):
        seen: list = []

        class FakeClient:
            def __init__(self, timeout):
                seen.append(timeout)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers, json):
                return httpx.Response(200, json={"data": []})

        monkeypatch.setattr(embeddings.httpx, "AsyncClient", FakeClient)
        cfg = embeddings.EmbedConfig("u", "m", 4, "k", timeout=4.0)
        asyncio.run(embeddings.request(["x"], cfg))
        assert seen == [4.0]


# ─── S5: /brain/search must not 500 on bad limit or control chars ─


class TestSearchInputHardening:
    @pytest.fixture
    def embedded(self, vec_db, monkeypatch):
        """A page with a vector, and a query embed that always succeeds."""
        monkeypatch.setattr(embeddings, "config", lambda env=None: _cfg4())

        async def ok(texts, cfg=None, batch=96):
            return [[1.0, 0, 0, 0] for _ in texts]

        monkeypatch.setattr(embeddings, "embed_texts", ok)
        vec_db.upsert_page("net/wifi", "WiFi", "note", "wireless access points")
        chunk = vec_db.missing_embeddings(1)[0]
        vec_db.store_embeddings([(chunk["id"], [1.0, 0, 0, 0])])
        return vec_db

    @pytest.mark.parametrize("limit", [2000, 5000, -3, 0])
    @pytest.mark.parametrize("mode", ["vector", "hybrid", "keyword"])
    def test_out_of_range_limit_is_clamped(self, embedded, mode, limit):
        r = _client(embedded).get(f"/brain/search?q=wireless&mode={mode}&limit={limit}")
        assert r.status_code == 200
        assert [x["slug"] for x in r.json()["results"]] == ["net/wifi"]

    @pytest.mark.parametrize("limit", [5000, -3])
    def test_search_vector_clamps_k(self, embedded, limit):
        hits = embedded.search_vector([1.0, 0, 0, 0], limit=limit)
        assert [h["slug"] for h in hits] == ["net/wifi"]

    def test_hybrid_clamps_depth(self, embedded):
        hits = embedded.search_hybrid("wireless", [1.0, 0, 0, 0], limit=3000)
        assert [h["slug"] for h in hits] == ["net/wifi"]

    def test_nul_byte_query_does_not_raise(self, db):
        db.upsert_page("net/wifi", "WiFi", "note", "wireless access points")
        assert db.search_keyword("\x00x") == []
        r = _client(db).get("/brain/search?q=%00x&mode=keyword")
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_control_chars_split_words(self, db):
        db.upsert_page("net/wifi", "WiFi", "note", "wireless access points")
        hits = db.search_keyword("wireless\x00\x1faccess\x7f")
        assert [h["slug"] for h in hits] == ["net/wifi"]

    def test_fts_operational_error_returns_empty(self, db, caplog):
        db.upsert_page("net/wifi", "WiFi", "note", "wireless access points")
        real = db._conn

        class Raising:
            def execute(self, sql, *args):
                if "MATCH" in sql:
                    raise sqlite3.OperationalError("fts5: syntax error")
                return real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(real, name)

        db._conn = Raising()
        with caplog.at_level(logging.WARNING):
            assert db.search_keyword("wireless") == []
        assert "fts5: syntax error" in caplog.text


# ─── G11: no-vector hybrid search ORs the query words ─────────────


class TestHybridAnyTermsFallback:
    def test_vector_without_active_table_uses_any_terms(self, db):
        """A query vector but no vector table (sqlite-vec missing, or no
        model configured) is still keyword-only, so it must OR the words."""
        db.upsert_page("n/a", "Launch", "note", "post Show HN on a weekday")
        assert db._vec_table is None
        hits = db.search_hybrid("Show HN timing", [0.0, 1.0, 0.0, 0.0])
        assert [h["slug"] for h in hits] == ["n/a"]

    def test_route_with_failed_query_embed_uses_any_terms(self, vec_db, monkeypatch):
        monkeypatch.setattr(embeddings, "config", lambda env=None: _cfg4())

        async def fail(texts, cfg=None, batch=96):
            return None

        monkeypatch.setattr(embeddings, "embed_texts", fail)
        vec_db.upsert_page("n/a", "Launch", "note", "post Show HN on a weekday")
        chunk = vec_db.missing_embeddings(1)[0]
        vec_db.store_embeddings([(chunk["id"], [1.0, 0, 0, 0])])
        data = _client(vec_db).get("/brain/search?q=Show+HN+timing").json()
        assert [r["slug"] for r in data["results"]] == ["n/a"]
        assert data["mode"] == "keyword"


# ─── #36: no oversized chunk, and one bad chunk can't stall backfill ─


class TestChunkUnpunctuated:
    def test_unpunctuated_text_splits_into_word_windows(self):
        text = " ".join(f"w{i}" for i in range(20_000))
        chunks = _chunk_text(text, max_words=300, overlap_words=50)
        assert len(chunks) >= 20_000 // 300
        assert max(len(c.split()) for c in chunks) <= 350  # window + overlap
        # nothing dropped: every word lands in some chunk
        seen = {w for c in chunks for w in c.split()}
        assert seen == {f"w{i}" for i in range(20_000)}

    def test_long_sentence_among_short_ones(self):
        long = " ".join(["word"] * 700)
        text = f"Short one. {long}. Another short one."
        chunks = _chunk_text(text, max_words=300, overlap_words=0)
        assert max(len(c.split()) for c in chunks) <= 300
        assert sum(len(c.split()) for c in chunks) == len(text.split())


def _provider(dim, calls, refuse="POISON", status_for=None):
    """Fake embeddings endpoint: HTTP 400 for any batch holding `refuse`,
    or `status_for(call_number)` when given."""

    async def fake(inputs, cfg):
        calls.append(list(inputs))
        if status_for is not None and (code := status_for(len(calls))) != 200:
            return httpx.Response(code, json={"error": "nope"})
        if any(refuse in t for t in inputs):
            return httpx.Response(400, json={"error": "input too long"})
        data = [{"index": i, "embedding": [0.1] * dim} for i in range(len(inputs))]
        return httpx.Response(200, json={"data": data})

    return fake


class TestBackfillResilience:
    def _pages(self, db, *bodies):
        for i, body in enumerate(bodies):
            db.upsert_page(f"p/{i}", f"Page {i}", "note", body)

    def test_each_batch_is_stored_as_it_succeeds(self, vec_db, monkeypatch):
        self._pages(vec_db, "alpha", "beta", "gamma")
        calls: list = []
        monkeypatch.setattr(embeddings, "BATCH", 2)
        monkeypatch.setattr(
            embeddings,
            "request",
            _provider(4, calls, status_for=lambda n: 200 if n == 1 else 503),
        )
        out = asyncio.run(embeddings.backfill(vec_db, _cfg4()))
        assert out["stored"] == 2 and out["failed"] == 1
        assert vec_db.embedding_count() == 2
        assert len(vec_db.missing_embeddings(10)) == 1

    def test_rejected_chunk_is_marked_and_queue_moves_on(self, vec_db, monkeypatch):
        self._pages(vec_db, "alpha", "POISON blob", "beta", "gamma")
        calls: list = []
        monkeypatch.setattr(embeddings, "request", _provider(4, calls))
        out = asyncio.run(embeddings.backfill(vec_db, _cfg4()))
        assert out["stored"] == 3 and out["rejected"] == 1
        assert vec_db.missing_embeddings(10) == []
        # the next run has nothing to send
        calls.clear()
        again = asyncio.run(embeddings.backfill(vec_db, _cfg4()))
        assert again["stored"] == 0 and calls == []

    def test_outage_marks_nothing(self, vec_db, monkeypatch):
        self._pages(vec_db, "alpha", "beta")
        for code in (500, 429, 401):
            calls: list = []
            monkeypatch.setattr(
                embeddings,
                "request",
                _provider(4, calls, status_for=lambda n, c=code: c),
            )
            out = asyncio.run(embeddings.backfill(vec_db, _cfg4()))
            assert out["stored"] == 0 and out["failed"] == 2
            assert len(calls) == 1  # no bisecting on a transient failure
            assert len(vec_db.missing_embeddings(10)) == 2

    def test_rewritten_page_is_retried(self, vec_db, monkeypatch):
        self._pages(vec_db, "POISON blob")
        calls: list = []
        monkeypatch.setattr(embeddings, "request", _provider(4, calls))
        asyncio.run(embeddings.backfill(vec_db, _cfg4()))
        assert vec_db.missing_embeddings(10) == []
        vec_db.upsert_page("p/0", "Page 0", "note", "fixed body")
        assert len(vec_db.missing_embeddings(10)) == 1
        out = asyncio.run(embeddings.backfill(vec_db, _cfg4()))
        assert out["stored"] == 1

    def test_rejection_is_per_model_table(self, vec_db, monkeypatch):
        self._pages(vec_db, "POISON blob")
        calls: list = []
        monkeypatch.setattr(embeddings, "request", _provider(4, calls))
        asyncio.run(embeddings.backfill(vec_db, _cfg4()))
        vec_db.use_vector_table("vec_other_4", 4)
        assert len(vec_db.missing_embeddings(10)) == 1

    def test_db_created_without_rejects_table_still_opens(self, tmp_path):
        """A brain.db from main has no embedding_rejects table; init_db adds
        it without touching existing rows."""
        path = str(tmp_path / "old.db")
        old = BrainDB(path)
        old.init_db()
        old.upsert_page("a", "Alpha", "note", "alpha body")
        old._conn.execute("DROP TABLE embedding_rejects")
        old._conn.commit()
        old._conn.close()

        d = BrainDB(path)
        d.init_db()
        if not d._has_vec:
            pytest.skip("sqlite-vec unavailable")
        d.use_vector_table("vec_test_4", 4)
        assert d.get_page("a") is not None
        assert len(d.missing_embeddings(10)) == 1
        d.init_db()  # idempotent


# ─── #46: TaskStatus enum and validated page types ────────────────


class TestTaskStatus:
    def test_members_and_open_set(self):
        from brain_db import TaskStatus

        assert {s.value for s in TaskStatus} == {
            "pending",
            "active",
            "completed",
            "failed",
            "blocked",
            "cancelled",
        }
        assert TaskStatus.OPEN == frozenset({TaskStatus.PENDING, TaskStatus.ACTIVE})
        assert "pending" in TaskStatus.OPEN and "blocked" not in TaskStatus.OPEN

    def test_bad_result_status_message_names_blocked(self, db):
        t = db.create_task("x")
        with pytest.raises(ValueError, match="blocked"):
            db.complete_task(t["id"], "r", "bogus")

    def test_blocked_is_a_valid_result(self, db):
        t = db.create_task("x")
        assert db.complete_task(t["id"], "need input", "blocked")["status"] == "blocked"
        with pytest.raises(ValueError, match="cannot complete"):
            db.complete_task(t["id"], "again", "completed")

    def test_cancel_rules_unchanged(self, db):
        """Completed/failed are final; a blocked task (an open question) can
        still be dismissed by cancelling it."""
        done = db.create_task("done")
        db.complete_task(done["id"], "ok", "completed")
        with pytest.raises(ValueError, match="already completed"):
            db.cancel_task(done["id"])
        blocked = db.create_task("q")
        db.complete_task(blocked["id"], "need input", "blocked")
        assert db.cancel_task(blocked["id"])["status"] == "cancelled"

    def test_list_orders_open_tasks_first(self, db):
        done = db.create_task("done", priority=1)
        db.complete_task(done["id"], "ok", "completed")
        active = db.create_task("active", priority=5)
        db.claim_task(active["id"])
        db.create_task("pending", priority=5)
        assert [t["description"] for t in db.list_tasks()] == [
            "pending",
            "active",
            "done",
        ]


class TestPageTypeValidated:
    def test_upsert_rejects_unknown_type(self, db):
        with pytest.raises(ValueError):
            db.upsert_page("a", "Alpha", "bogus", "body")
        assert db.get_page("a") is None

    def test_upsert_accepts_type_string(self, db):
        assert db.upsert_page("a", "Alpha", "note", "body")["page_type"] == "note"

    def test_list_pages_bogus_type_is_422(self, db):
        db.upsert_page("a", "Alpha", "note", "body")
        client = _client(db)
        assert client.get("/brain/pages?page_type=bogus").status_code == 422
        r = client.get("/brain/pages?page_type=note")
        assert r.status_code == 200 and r.json()["total"] == 1
