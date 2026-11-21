"""Retrieval eval harness: metrics, cache, truncation, end-to-end runs, and
the CI regression gate against the committed fixture baseline."""

import json
import math
import os
import sqlite3

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

    def test_harvest_reads_agent_transcript_sqlite(self, tmp_path):
        db_dir = tmp_path / "max-ops" / "agent"
        db_dir.mkdir(parents=True)
        db = sqlite3.connect(db_dir / "openclaw-agent.sqlite")
        db.execute(
            "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, "
            "event_json TEXT, event_zstd BLOB)"
        )
        for seq, url in enumerate(
            [
                "http://x:5678/brain/search?q=lead+conversion&mode=hybrid",
                "http://x/brain/search?q=who%20pays%3F",
                "http://x/brain/search?q=lead+conversion",
            ]
        ):
            db.execute(
                "INSERT INTO transcript_events VALUES (?, ?, ?, NULL)",
                ("s", seq, json.dumps({"url": url})),
            )
        db.commit()
        db.close()
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


# ─── Edge cases: loaders, ranking modes, harvest ──────────────────


class TestLoadersAndEdges:
    def test_empty_runs_have_zero_delta(self):
        """GIVEN two runs that scored no queries
        WHEN they are compared with a paired bootstrap
        THEN the delta is all zeros instead of a divide-by-zero."""
        assert rev.paired_bootstrap([], []) == rev.Delta(0.0, 0.0, 0.0)

    def test_load_pages_reads_the_pages_key(self, tmp_path):
        """GIVEN a corpus file with a pages list
        WHEN load_pages reads it
        THEN the list is returned unchanged."""
        path = tmp_path / "corpus.json"
        pages = [{"slug": "a", "title": "A", "page_type": "note", "compiled": "x"}]
        path.write_text(json.dumps({"pages": pages}))
        assert rev.load_pages(str(path)) == pages

    def test_vector_only_mode_ranks_by_embedding(self, toy_cache):
        """GIVEN a vector-only config and cached toy vectors
        WHEN a paraphrase query with no keyword overlap is run
        THEN the semantically matching page ranks first."""
        report = rev.evaluate(
            TOY_PAGES,
            TOY_CASES,
            [rev.SearchConfig("vec", mode="vector", dim=4)],
            toy_cache,
        )
        assert report["vec"].per_query["woof animal"]["hit@1"] == 1.0

    def test_no_vector_hybrid_falls_back_to_keywords(self, toy_cache):
        """GIVEN the hybrid-novec mode (embedding outage)
        WHEN a lexical query is run
        THEN keyword matching alone still finds the page."""
        report = rev.evaluate(
            TOY_PAGES,
            TOY_CASES,
            [rev.SearchConfig("nv", mode="hybrid-novec", dim=4)],
            toy_cache,
        )
        assert report["nv"].per_query["felines"]["hit@1"] == 1.0

    def test_unknown_mode_raises(self, toy_cache):
        """GIVEN a config with a mode the harness does not know
        WHEN it is evaluated
        THEN a ValueError names the mode."""
        cfg = rev.SearchConfig("bad", mode="telepathy", dim=4)
        with pytest.raises(ValueError, match="telepathy"):
            rev.evaluate(TOY_PAGES, TOY_CASES, [cfg], toy_cache)

    def test_unknown_header_recipe_raises(self):
        """GIVEN a header recipe that does not exist
        WHEN embed_text is called
        THEN a ValueError is raised."""
        with pytest.raises(ValueError, match="header"):
            rev.embed_text("chunk", "Title", "fancy")

    def test_harvest_skips_noise_and_unreadable_databases(self, tmp_path):
        """GIVEN a tree with a non-sqlite file, a corrupt .sqlite file, a
        directory named like a database, and a good transcript containing a
        '...' placeholder query
        WHEN queries are harvested
        THEN only the real query is returned and nothing raises."""
        (tmp_path / "notes.txt").write_text("brain/search?q=ignored")
        (tmp_path / "corrupt.sqlite").write_text("this is not a database")
        (tmp_path / "dir.sqlite").mkdir()
        good = sqlite3.connect(tmp_path / "ok.sqlite")
        good.execute(
            "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, "
            "event_json TEXT, event_zstd BLOB)"
        )
        for seq, url in enumerate(
            ["http://x/brain/search?q=...", "http://x/brain/search?q=real+one"]
        ):
            good.execute(
                "INSERT INTO transcript_events VALUES ('s', ?, ?, NULL)",
                (seq, json.dumps({"url": url})),
            )
        good.commit()
        good.close()
        assert rev.harvest_queries(str(tmp_path)) == ["real one"]

    def test_harvest_ignores_sqlite_without_transcript_table(self, tmp_path):
        """GIVEN a valid sqlite file with no transcript_events table
        WHEN queries are harvested
        THEN it yields no queries."""
        other = sqlite3.connect(tmp_path / "other.sqlite")
        other.execute("CREATE TABLE t (x)")
        other.commit()
        other.close()
        assert rev.harvest_queries(str(tmp_path)) == []


# ─── Refresh and truncation check (embedding API mocked) ──────────


@pytest.fixture
def fake_embeddings(monkeypatch):
    """Replace the embeddings module's config and network call."""
    import embeddings

    state = {"calls": [], "result": "ok"}
    cfg = embeddings.EmbedConfig(url="http://x", model="toy", dim=4, api_key="k")
    monkeypatch.setattr(embeddings, "config", lambda env=None: cfg)

    async def fake_embed(texts, cfg=None, batch=64):
        state["calls"].append(list(texts))
        if state["result"] is None:
            return None
        return [_toy_vec(t) for t in texts]

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    state["cfg"] = cfg
    state["module"] = embeddings
    return state


class TestRefresh:
    def test_refresh_embeds_only_missing_texts_and_saves(
        self, tmp_path, fake_embeddings
    ):
        """GIVEN a cache holding one of two needed texts
        WHEN _refresh runs
        THEN only the missing text is embedded and the cache is persisted."""
        path = tmp_path / "v.json"
        cache = rev.VectorCache(str(path), "toy", 4)
        cache.put("cat", _toy_vec("cat"))
        assert rev._refresh(cache, ["cat", "dog"]) == 1
        assert fake_embeddings["calls"] == [["dog"]]
        assert rev.VectorCache.load(str(path)).missing(["cat", "dog"]) == []

    def test_refresh_with_nothing_missing_makes_no_request(self, fake_embeddings):
        """GIVEN a cache that already has every text
        WHEN _refresh runs
        THEN it returns 0 without calling the API."""
        cache = rev.VectorCache("unused.json", "toy", 4)
        cache.put("cat", _toy_vec("cat"))
        assert rev._refresh(cache, ["cat"]) == 0
        assert fake_embeddings["calls"] == []

    def test_refresh_without_api_key_exits(self, monkeypatch, fake_embeddings):
        """GIVEN no OPENROUTER_API_KEY
        WHEN _refresh runs
        THEN it exits with a message instead of calling the API."""
        import dataclasses

        keyless = dataclasses.replace(fake_embeddings["cfg"], api_key="")
        monkeypatch.setattr(
            fake_embeddings["module"], "config", lambda env=None: keyless
        )
        cache = rev.VectorCache("unused.json", "toy", 4)
        with pytest.raises(SystemExit, match="OPENROUTER_API_KEY"):
            rev._refresh(cache, ["cat"])
        assert fake_embeddings["calls"] == []

    def test_refresh_leaves_cache_unchanged_on_api_failure(
        self, tmp_path, fake_embeddings
    ):
        """GIVEN the embedding request fails
        WHEN _refresh runs
        THEN it exits and nothing is written to disk."""
        fake_embeddings["result"] = None
        path = tmp_path / "v.json"
        cache = rev.VectorCache(str(path), "toy", 4)
        with pytest.raises(SystemExit, match="failed"):
            rev._refresh(cache, ["cat"])
        assert not path.exists()

    def test_check_truncation_is_one_for_matching_vectors(self, fake_embeddings):
        """GIVEN a cached vector equal to the API's native vector
        WHEN check_truncation compares them
        THEN the cosine is 1."""
        cache = rev.VectorCache("unused.json", "toy", 4)
        cache.put("cat", _toy_vec("cat"))
        cos = rev.check_truncation(cache, "cat", dim=4)
        assert cos == pytest.approx(1.0, abs=1e-3)

    def test_check_truncation_exits_on_api_failure(self, fake_embeddings):
        """GIVEN the native embedding request fails
        WHEN check_truncation runs
        THEN it exits instead of returning a bogus score."""
        fake_embeddings["result"] = None
        cache = rev.VectorCache("unused.json", "toy", 4)
        cache.put("cat", _toy_vec("cat"))
        with pytest.raises(SystemExit, match="failed"):
            rev.check_truncation(cache, "cat", dim=4)


# ─── Report and CLI ───────────────────────────────────────────────


@pytest.fixture
def fixtures_copy(tmp_path):
    import shutil

    dest = tmp_path / "fx"
    shutil.copytree(FIXTURES, dest)
    return dest


class TestFormatReport:
    def test_report_lists_every_config_and_the_significance_legend(self, toy_cache):
        """GIVEN a report with a baseline and a keyword-only config
        WHEN it is formatted
        THEN both rows and the CI legend appear."""
        report = rev.evaluate(
            TOY_PAGES,
            TOY_CASES,
            [
                rev.SearchConfig("production", dim=4),
                rev.SearchConfig("kw", mode="keyword", dim=4),
            ],
            toy_cache,
        )
        text = rev.format_report(report)
        assert "production" in text and "kw" in text
        assert "95% CI excludes zero" in text


class TestMain:
    def test_main_prints_report_for_fixtures(self, fixtures_copy, capsys):
        """GIVEN a complete fixture directory
        WHEN main runs with no flags
        THEN it prints the report and exits 0."""
        assert rev.main(["--fixtures", str(fixtures_copy)]) == 0
        out = capsys.readouterr().out
        assert "production" in out and "no-vectors" in out

    def test_main_reports_cache_misses_and_fails(self, fixtures_copy, capsys):
        """GIVEN a vector cache missing every text
        WHEN main runs
        THEN it returns 1 and tells the operator to refresh."""
        empty = rev.VectorCache(str(fixtures_copy / "vectors.json"), "toy", 4)
        empty.save()
        assert rev.main(["--fixtures", str(fixtures_copy)]) == 1
        assert "--refresh" in capsys.readouterr().out

    def test_main_write_baseline_matches_committed_baseline(
        self, fixtures_copy, capsys
    ):
        """GIVEN a fixture copy with a damaged baseline
        WHEN main runs with --write-baseline
        THEN baseline.json is rewritten with the gated configs and the
        committed baseline still gates clean against it."""
        (fixtures_copy / "baseline.json").write_text("{}")
        assert rev.main(["--fixtures", str(fixtures_copy), "--write-baseline"]) == 0
        assert "wrote baseline.json" in capsys.readouterr().out
        body = json.loads((fixtures_copy / "baseline.json").read_text())
        assert set(body["configs"]) == set(rev.GATED_CONFIGS)
        with open(os.path.join(FIXTURES, "baseline.json")) as f:
            committed = json.load(f)
        for name in rev.GATED_CONFIGS:
            written = body["configs"][name]["mean"]
            assert rev.gate(committed["configs"][name]["mean"], written) == []

    def test_main_exits_when_golden_set_is_missing(self, fixtures_copy):
        """GIVEN no golden set at the expected path
        WHEN main runs
        THEN it exits suggesting --harvest."""
        (fixtures_copy / "golden.json").unlink()
        with pytest.raises(SystemExit, match="--harvest"):
            rev.main(["--fixtures", str(fixtures_copy)])

    def test_main_exits_when_no_cases_are_labelled(self, fixtures_copy):
        """GIVEN a golden set with only unlabelled cases
        WHEN main runs
        THEN it exits saying nothing is labelled."""
        (fixtures_copy / "golden.json").write_text(
            '{"cases": [{"query": "q", "relevant": []}]}'
        )
        with pytest.raises(SystemExit, match="no labelled cases"):
            rev.main(["--fixtures", str(fixtures_copy)])

    def test_main_harvest_adds_unlabelled_queries_and_exits(self, tmp_path, capsys):
        """GIVEN an OpenClaw sessions dir with one search transcript
        WHEN main runs with --harvest
        THEN the query lands in the golden set unlabelled."""
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        db = sqlite3.connect(sessions / "a.sqlite")
        db.execute(
            "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, "
            "event_json TEXT, event_zstd BLOB)"
        )
        db.execute(
            "INSERT INTO transcript_events VALUES ('s', 0, ?, NULL)",
            (json.dumps({"url": "http://x/brain/search?q=pricing"}),),
        )
        db.commit()
        db.close()
        golden = tmp_path / "golden.json"
        argv = ["--harvest", str(sessions), "--golden", str(golden)]
        assert rev.main(argv) == 0
        assert "added 1" in capsys.readouterr().out
        assert json.loads(golden.read_text())["cases"][0]["query"] == "pricing"

    def test_main_rejects_write_baseline_in_brain_mode(self, tmp_path):
        """GIVEN --brain together with --write-baseline
        WHEN main runs
        THEN it exits: baselines only apply to committed fixtures."""
        with pytest.raises(SystemExit, match="committed fixtures"):
            rev.main(["--brain", str(tmp_path / "b.db"), "--write-baseline"])

    def test_main_brain_mode_evaluates_a_real_brain(
        self, tmp_path, fake_embeddings, monkeypatch, capsys
    ):
        """GIVEN a brain DB, a labelled golden set and no vector cache
        WHEN main runs in --brain mode with --refresh and --check-truncation
        THEN the cache is created and filled, truncation is checked and the
        report is printed."""
        from brain_db import BrainDB

        brain = tmp_path / "brain.db"
        db = BrainDB(str(brain))
        db.init_db()
        db.upsert_page("n/cats", "Cats", "note", "cats are feline pets")
        db.upsert_page("n/dogs", "Dogs", "note", "dogs are canine pets")
        db._conn.close()
        golden = tmp_path / "golden.json"
        golden.write_text(
            json.dumps({"cases": [{"query": "feline", "relevant": ["n/cats"]}]})
        )
        vectors = tmp_path / "out" / "vectors.json"
        # The default matrix asks for dim 512/1024; the toy cache is 4-d.
        monkeypatch.setattr(
            rev, "DEFAULT_MATRIX", [rev.SearchConfig("production", dim=4)]
        )
        argv = [
            "--brain",
            str(brain),
            "--golden",
            str(golden),
            "--vectors",
            str(vectors),
            "--refresh",
            "--check-truncation",
        ]
        assert rev.main(argv) == 0
        out = capsys.readouterr().out
        assert "embedded" in out and "truncated-vs-native cosine at 512" in out
        assert "2 pages, 1 queries" in out
        assert vectors.exists()
