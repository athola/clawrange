"""Retrieval eval harness: metrics, cache, truncation, end-to-end runs, and
the CI regression gate against the committed fixture baseline."""

import json
import math
import os

import pytest

import retrieval_eval as rev

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "retrieval")


# ─── Metrics ──────────────────────────────────────────────────────


class TestMetrics:
    def test_hit_at_k(self):
        assert rev.hit_at(["a", "b", "c"], {"b"}, 1) == 0.0
        assert rev.hit_at(["a", "b", "c"], {"b"}, 2) == 1.0

    def test_reciprocal_rank(self):
        assert rev.reciprocal_rank(["a", "b", "c"], {"c"}) == pytest.approx(1 / 3)
        assert rev.reciprocal_rank(["a"], {"z"}) == 0.0

    def test_reciprocal_rank_respects_cutoff(self):
        assert rev.reciprocal_rank(["a", "b", "c"], {"c"}, k=2) == 0.0

    def test_ndcg_binary(self):
        assert rev.ndcg_at(["x", "a"], {"a"}, 10) == pytest.approx(1 / math.log2(3))
        assert rev.ndcg_at(["a", "b"], {"a", "b"}, 10) == pytest.approx(1.0)

    def test_score_case_reports_all_metrics(self):
        m = rev.score_ranking(["a", "b"], {"b"})
        assert set(m) == set(rev.METRICS)


class TestPairedBootstrap:
    def test_identical_runs_have_zero_delta(self):
        d = rev.paired_bootstrap([0.5, 1.0, 0.0], [0.5, 1.0, 0.0])
        assert d.mean == 0.0 and d.lo == 0.0 and d.hi == 0.0
        assert not d.significant

    def test_clear_improvement_is_significant(self):
        base = [0.0] * 30
        better = [1.0] * 25 + [0.0] * 5
        d = rev.paired_bootstrap(base, better)
        assert d.mean == pytest.approx(25 / 30)
        assert d.lo > 0 and d.significant

    def test_deterministic_for_a_seed(self):
        a, b = [0, 1, 0, 1, 1], [1, 1, 0, 0, 1]
        assert rev.paired_bootstrap(a, b, seed=7) == rev.paired_bootstrap(a, b, seed=7)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            rev.paired_bootstrap([1.0], [1.0, 0.0])


# ─── Vector cache and truncation ──────────────────────────────────


class TestVectorCache:
    def test_round_trip_through_disk(self, tmp_path):
        path = tmp_path / "v.json"
        cache = rev.VectorCache(str(path), model="m", dim=4)
        cache.put("hello", [0.5, -0.25, 0.125, 1.0])
        cache.save()
        again = rev.VectorCache.load(str(path))
        assert again.model == "m" and again.dim == 4
        assert again.get("hello") == pytest.approx([0.5, -0.25, 0.125, 1.0])

    def test_stored_vectors_are_lowercase_hex(self, tmp_path):
        """base64 once produced 'AKIA' + uppercase, tripping CI's AWS-key
        scan; lowercase hex can never match a secret pattern."""
        path = tmp_path / "v.json"
        cache = rev.VectorCache(str(path), model="m", dim=4)
        cache.put("t", [0.5, -0.25, 0.125, 1.0])
        cache.save()
        stored = json.loads(path.read_text())["vectors"]
        assert all(set(v) <= set("0123456789abcdef") for v in stored.values())

    def test_committed_fixture_cache_is_hex(self):
        stored = json.load(open(os.path.join(FIXTURES, "vectors.json")))["vectors"]
        assert all(set(v) <= set("0123456789abcdef") for v in stored.values())

    def test_keys_depend_on_model(self, tmp_path):
        a = rev.VectorCache(str(tmp_path / "a.json"), model="m1", dim=4)
        b = rev.VectorCache(str(tmp_path / "b.json"), model="m2", dim=4)
        assert a.key("t") != b.key("t")

    def test_missing_lists_uncached_texts(self, tmp_path):
        cache = rev.VectorCache(str(tmp_path / "v.json"), model="m", dim=2)
        cache.put("a", [1.0, 0.0])
        assert cache.missing(["a", "b", "b"]) == ["b"]

    def test_get_unknown_raises_cache_miss(self, tmp_path):
        cache = rev.VectorCache(str(tmp_path / "v.json"), model="m", dim=2)
        with pytest.raises(rev.CacheMiss):
            cache.get("nope")

    def test_wrong_dim_rejected(self, tmp_path):
        cache = rev.VectorCache(str(tmp_path / "v.json"), model="m", dim=3)
        with pytest.raises(ValueError):
            cache.put("a", [1.0, 0.0])


class TestTruncate:
    def test_truncate_renormalizes(self):
        v = rev.truncate([3.0, 4.0, 12.0], 2)
        assert v == pytest.approx([0.6, 0.8])

    def test_full_dim_is_unchanged_direction(self):
        assert rev.truncate([1.0, 0.0], 2) == pytest.approx([1.0, 0.0])


class TestEmbedText:
    def test_none_header_is_chunk_text(self):
        assert rev.embed_text("body", "Title", "none") == "body"

    def test_title_header_prefixes_once(self):
        assert rev.embed_text("body", "Title", "title") == "Title\n\nbody"
        # The first chunk already starts with the title: don't repeat it.
        assert rev.embed_text("Title\n\nbody", "Title", "title") == "Title\n\nbody"

    def test_unknown_header_rejected(self):
        with pytest.raises(ValueError):
            rev.embed_text("b", "T", "summary")


# ─── End to end on a toy corpus ───────────────────────────────────

TOY_PAGES = [
    {
        "slug": "a/cats",
        "title": "Cats",
        "page_type": "note",
        "compiled": "felines purr",
    },
    {
        "slug": "a/dogs",
        "title": "Dogs",
        "page_type": "note",
        "compiled": "canines bark",
    },
]
TOY_CASES = [
    rev.Case("felines", frozenset({"a/cats"}), ("lexical",)),
    rev.Case("woof animal", frozenset({"a/dogs"}), ("semantic",)),
]


def _toy_vec(text: str) -> list[float]:
    """Deterministic 4-d stand-in for an embedding model."""
    t = text.lower()
    return [
        1.0 if ("cat" in t or "feline" in t) else 0.0,
        1.0 if ("dog" in t or "canine" in t or "woof" in t) else 0.0,
        0.1,
        0.0,
    ]


@pytest.fixture
def toy_cache(tmp_path):
    cache = rev.VectorCache(str(tmp_path / "v.json"), model="toy", dim=4)
    for text in rev.texts_needed(TOY_PAGES, TOY_CASES, [rev.SearchConfig("h")]):
        cache.put(text, _toy_vec(text))
    return cache


class TestRun:
    def test_hybrid_finds_semantic_match(self, toy_cache):
        report = rev.evaluate(
            TOY_PAGES, TOY_CASES, [rev.SearchConfig("hybrid", dim=4)], toy_cache
        )
        res = report["hybrid"]
        assert res.mean["hit@1"] == 1.0
        assert res.per_query["woof animal"]["hit@1"] == 1.0

    def test_keyword_misses_paraphrase(self, toy_cache):
        report = rev.evaluate(
            TOY_PAGES,
            TOY_CASES,
            [rev.SearchConfig("kw", mode="keyword", dim=4)],
            toy_cache,
        )
        assert report["kw"].per_query["woof animal"]["hit@1"] == 0.0
        assert report["kw"].by_tag["lexical"]["hit@1"] == 1.0

    def test_cache_miss_is_loud(self, tmp_path):
        empty = rev.VectorCache(str(tmp_path / "e.json"), model="toy", dim=4)
        with pytest.raises(rev.CacheMiss):
            rev.evaluate(TOY_PAGES, TOY_CASES, [rev.SearchConfig("h", dim=4)], empty)


# ─── CI gate on the committed fixtures ────────────────────────────


@pytest.fixture(scope="module")
def fixture_report():
    corpus = json.load(open(os.path.join(FIXTURES, "corpus.json")))["pages"]
    cases = rev.load_cases(os.path.join(FIXTURES, "golden.json"))
    cache = rev.VectorCache.load(os.path.join(FIXTURES, "vectors.json"))
    return corpus, cases, cache


class TestFixtureGate:
    def test_cache_covers_every_text_the_matrix_needs(self, fixture_report):
        corpus, cases, cache = fixture_report
        needed = rev.texts_needed(corpus, cases, rev.DEFAULT_MATRIX)
        assert cache.missing(needed) == []

    @pytest.mark.parametrize("name", rev.GATED_CONFIGS)
    def test_gated_config_has_not_regressed(self, fixture_report, name):
        corpus, cases, cache = fixture_report
        baseline = json.load(open(os.path.join(FIXTURES, "baseline.json")))
        cfg = next(c for c in rev.DEFAULT_MATRIX if c.name == name)
        current = rev.evaluate(corpus, cases, [cfg], cache)[name]
        base = baseline["configs"][name]
        failures = rev.gate(base["mean"], current.mean, base["by_tag"], current.by_tag)
        assert failures == [], failures

    def test_one_query_regression_fails_the_gate(self):
        base = {m: 0.5 for m in rev.METRICS}
        worse = dict(base, **{"hit@1": 0.5 - 1 / 72})
        assert rev.gate(base, worse)

    def test_tag_loss_hidden_by_overall_gain_fails(self):
        """Overall hit@1 can rise while one query type gets worse."""
        base = {m: 0.5 for m in rev.METRICS}
        base_tags = {"lexical": {"hit@1": 1.0}, "semantic": {"hit@1": 0.5}}
        better = dict(base, **{"hit@1": 0.6})
        tags = {"lexical": {"hit@1": 0.9}, "semantic": {"hit@1": 0.7}}
        failures = rev.gate(base, better, base_tags, tags)
        assert failures == ["lexical hit@1 fell from 1.000 to 0.900"]

    def test_production_config_mirrors_real_defaults(self):
        """The gate copies production's knobs into SearchConfig; if the real
        defaults move, this fails instead of the gate silently testing a
        stale copy."""
        import inspect

        from brain_db import BrainDB

        prod = rev.production_config()
        params = inspect.signature(BrainDB.search_hybrid).parameters
        assert prod.rrf_k == params["k"].default
        assert prod.kw_weight == params["kw_weight"].default
        assert prod.vec_weight == params["vec_weight"].default
        assert prod.depth == params["depth"].default
        assert prod.kw_match == params["kw_match"].default
        probe = BrainDB(":memory:")
        assert prod.chunk_words == probe.chunk_max_words
        assert prod.chunk_overlap == probe.chunk_overlap_words


# ─── Real-brain mode (local, operator-private data) ───────────────


class TestRealBrain:
    def test_pages_load_read_only_from_a_brain_db(self, tmp_path):
        from brain_db import BrainDB

        path = tmp_path / "brain.db"
        db = BrainDB(str(path))
        db.init_db()
        db.upsert_page("n/a", "Alpha", "note", "body text")
        db._conn.close()
        pages = rev.load_brain_pages(str(path))
        assert pages == [
            {
                "slug": "n/a",
                "title": "Alpha",
                "page_type": "note",
                "compiled": "body text",
            }
        ]

    def test_harvest_finds_url_encoded_queries_in_sessions(self, tmp_path):
        agent = tmp_path / "max" / "sessions"
        agent.mkdir(parents=True)
        (agent / "s.jsonl").write_text(
            '{"url": "http://x:5678/brain/search?q=lead+conversion&mode=hybrid"}\n'
            '{"url": "http://x/brain/search?q=who%20pays%3F"}\n'
            '{"url": "http://x/brain/search?q=lead+conversion"}\n'
        )
        assert rev.harvest_queries(str(tmp_path)) == ["lead conversion", "who pays?"]

    def test_merge_adds_unlabelled_cases_once(self, tmp_path):
        golden = tmp_path / "golden.json"
        golden.write_text(
            '{"cases": [{"query": "known", "relevant": ["a"], "tags": []}]}'
        )
        added = rev.merge_candidates(str(golden), ["known", "new one"])
        assert added == 1
        data = json.loads(golden.read_text())["cases"]
        assert data[1] == {"query": "new one", "relevant": [], "tags": ["harvested"]}
        assert rev.merge_candidates(str(golden), ["new one"]) == 0

    def test_merge_creates_missing_golden_file(self, tmp_path):
        golden = tmp_path / "sub" / "golden.json"
        assert rev.merge_candidates(str(golden), ["q"]) == 1
        assert json.loads(golden.read_text())["cases"][0]["query"] == "q"

    def test_unlabelled_cases_are_not_scored(self, tmp_path):
        golden = tmp_path / "g.json"
        golden.write_text(
            '{"cases": [{"query": "a", "relevant": ["x"]},'
            ' {"query": "b", "relevant": []}]}'
        )
        assert [c.query for c in rev.load_cases(str(golden))] == ["a"]
