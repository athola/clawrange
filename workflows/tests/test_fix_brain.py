"""Regression tests for the PR #11 brain review findings (cluster K5).

Each class names the finding it guards: issue numbers (#27, #36, #46, #51)
or review ids (G2, G3, G4, G11, S5).
"""

import asyncio
import logging
import sys

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import embeddings
from brain import create_brain_router
from brain_db import BrainDB


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
