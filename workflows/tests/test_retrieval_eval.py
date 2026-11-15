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
        failures = rev.gate(baseline["configs"][name], current.mean)
        assert failures == [], failures

    def test_one_query_regression_fails_the_gate(self):
        base = {m: 0.5 for m in rev.METRICS}
        worse = dict(base, **{"hit@1": 0.5 - 1 / 72})
        assert rev.gate(base, worse)

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
