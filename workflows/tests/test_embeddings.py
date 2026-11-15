"""OpenRouter embeddings, per-model vector tables, backfill.

Vectors from different models are not comparable, so every model+dimension
gets its own table and search only ever reads the configured model's table.
"""

import asyncio

import httpx
import pytest

import embeddings
from brain_db import BrainDB


def _cfg(**over):
    env = {"OPENROUTER_API_KEY": "or-key"}
    env.update(over)
    return embeddings.config(env)


class TestConfig:
    def test_openrouter_is_default(self):
        c = _cfg()
        assert (c.model, c.dim) == ("openai/text-embedding-3-small", 1536)
        assert c.url == "https://openrouter.ai/api/v1/embeddings"
        assert c.api_key == "or-key"

    def test_overrides(self):
        c = _cfg(EMBED_MODEL="qwen/qwen3-embedding-8b", EMBED_DIM="1024")
        assert (c.model, c.dim) == ("qwen/qwen3-embedding-8b", 1024)

    def test_table_name_is_per_model_and_dim(self):
        a, b = _cfg(), _cfg(EMBED_DIM="1024")
        assert a.table != b.table
        assert a.table == "vec_openai_text_embedding_3_small_1536"


class TestRequestBody:
    def test_sends_dimensions(self):
        assert embeddings.request_body(["x"], _cfg())["dimensions"] == 1536


def _fake_request(dim, calls=None, status=200):
    async def fake(inputs, cfg):
        if calls is not None:
            calls.append(list(inputs))
        body = {
            "data": [
                {"index": i, "embedding": [0.1] * dim} for i in range(len(inputs))
            ][::-1]  # out of order on purpose: must be re-sorted by index
        }
        return httpx.Response(status, json=body)

    return fake


class TestEmbedTexts:
    def test_batches_and_orders(self, monkeypatch):
        calls = []
        cfg = _cfg(EMBED_DIM="4")
        monkeypatch.setattr(embeddings, "request", _fake_request(4, calls))
        out = asyncio.run(embeddings.embed_texts(["a"] * 5, cfg, batch=2))
        assert [len(c) for c in calls] == [2, 2, 1]
        assert len(out) == 5 and all(len(v) == 4 for v in out)

    def test_wrong_dimension_is_rejected(self, monkeypatch):
        monkeypatch.setattr(embeddings, "request", _fake_request(3))
        assert asyncio.run(embeddings.embed_texts(["a"], _cfg(EMBED_DIM="4"))) is None

    def test_http_error_is_none(self, monkeypatch):
        monkeypatch.setattr(embeddings, "request", _fake_request(4, status=400))
        assert asyncio.run(embeddings.embed_texts(["a"], _cfg(EMBED_DIM="4"))) is None

    def test_no_key_is_none_without_calling(self, monkeypatch):
        async def boom(*a):
            raise AssertionError("must not call the provider without a key")

        monkeypatch.setattr(embeddings, "request", boom)
        cfg = embeddings.config({})
        assert asyncio.run(embeddings.embed_texts(["a"], cfg)) is None


@pytest.fixture()
def db(tmp_path):
    d = BrainDB(str(tmp_path / "b.db"))
    d.init_db()
    if not d._has_vec:
        pytest.skip("sqlite-vec unavailable")
    d.use_vector_table("vec_test_4", 4)
    return d


class TestVectorTable:
    def test_per_model_table_and_counts(self, db):
        db.upsert_page("a", "Alpha", "concept", "alpha body")
        missing = db.missing_embeddings(10)
        assert missing and db.embedding_count() == 0
        db.store_embeddings([(missing[0]["id"], [1.0, 0, 0, 0])])
        assert db.embedding_count() == 1
        assert db.missing_embeddings(10) == []
        assert db.has_embeddings() is True

    def test_cosine_score(self, db):
        db.upsert_page("a", "Alpha", "concept", "alpha body")
        chunk = db.missing_embeddings(1)[0]
        db.store_embeddings([(chunk["id"], [2.0, 0, 0, 0])])
        hit = db.search_vector([1.0, 0, 0, 0], limit=1)[0]
        assert hit["slug"] == "a" and hit["score"] == pytest.approx(1.0)

    def test_rechunk_orphans_are_pruned(self, db):
        db.upsert_page("a", "Alpha", "concept", "first body")
        db.store_embeddings([(db.missing_embeddings(1)[0]["id"], [1.0, 0, 0, 0])])
        db.upsert_page("a", "Alpha", "concept", "a completely different body")
        assert db.prune_orphan_embeddings() == 1
        assert db.embedding_count() == 0

    def test_no_table_configured_is_inert(self, tmp_path):
        d = BrainDB(str(tmp_path / "c.db"))
        d.init_db()
        assert d.has_embeddings() is False
        assert d.missing_embeddings(5) == []
        assert d.search_vector([0.1] * 4) == []


class TestBackfill:
    def test_embeds_missing_chunks(self, db, monkeypatch):
        db.upsert_page("a", "Alpha", "concept", "alpha body")
        db.upsert_page("b", "Beta", "concept", "beta body")
        monkeypatch.setattr(embeddings, "request", _fake_request(4))
        out = asyncio.run(embeddings.backfill(db, _cfg(EMBED_DIM="4")))
        assert out["stored"] == 2 and db.missing_embeddings(10) == []

    def test_skips_without_key(self, db):
        cfg = embeddings.config({"EMBED_DIM": "4"})
        assert asyncio.run(embeddings.backfill(db, cfg))["skipped"] == "no api key"

    def test_failed_embed_stores_nothing(self, db, monkeypatch):
        db.upsert_page("a", "Alpha", "concept", "alpha body")
        monkeypatch.setattr(embeddings, "request", _fake_request(4, status=500))
        out = asyncio.run(embeddings.backfill(db, _cfg(EMBED_DIM="4")))
        assert out["stored"] == 0 and db.embedding_count() == 0
