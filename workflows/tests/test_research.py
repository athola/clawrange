"""Tests for the multi-source research orchestrator.

Mirrors a subset of `tome`'s synthesis logic (Finding model,
deduplication, ranking with authority bonuses, triangulation
bonus) ported into the workflows service so John-117 can call
it as a single endpoint.
"""

from __future__ import annotations

import pytest

from research import (
    Finding,
    compute_relevance_score,
    compute_triangulation_bonus,
    deduplicate,
    fuzzy_deduplicate,
    merge_findings,
    rank_findings,
)


# ─── Finding model ────────────────────────────────────────────────


class TestFindingModel:
    def test_relevance_clamped_to_unit_interval(self):
        """
        GIVEN a Finding with relevance > 1.0
        WHEN constructed
        THEN relevance is clamped to 1.0.
        """
        f = Finding(
            source="github",
            channel="code",
            title="x",
            url="https://example.com/x",
            relevance=2.0,
            summary="",
        )
        assert f.relevance == 1.0

    def test_negative_relevance_clamped_to_zero(self):
        f = Finding(
            source="reddit",
            channel="discourse",
            title="y",
            url="https://example.com/y",
            relevance=-0.5,
            summary="",
        )
        assert f.relevance == 0.0

    def test_to_dict_round_trip(self):
        f = Finding(
            source="hn",
            channel="discourse",
            title="z",
            url="https://example.com/z",
            relevance=0.4,
            summary="hello",
            metadata={"score": 120},
        )
        d = f.to_dict()
        assert d["source"] == "hn"
        assert d["channel"] == "discourse"
        assert d["metadata"]["score"] == 120


# ─── Deduplication by URL ────────────────────────────────────────


class TestDeduplicateByUrl:
    def test_same_url_keeps_higher_relevance(self):
        a = Finding("reddit", "discourse", "Old", "https://x.com/p", 0.4, "")
        b = Finding("reddit", "discourse", "New", "https://x.com/p", 0.7, "")
        result = deduplicate([a, b])
        assert len(result) == 1
        assert result[0].relevance == 0.7

    def test_empty_url_findings_all_kept(self):
        a = Finding("web", "discourse", "A", "", 0.5, "")
        b = Finding("web", "discourse", "B", "", 0.6, "")
        result = deduplicate([a, b])
        assert len(result) == 2

    def test_distinct_urls_all_kept(self):
        a = Finding("github", "code", "A", "https://g/a", 0.5, "")
        b = Finding("github", "code", "B", "https://g/b", 0.6, "")
        assert len(deduplicate([a, b])) == 2


# ─── Fuzzy deduplication by title ────────────────────────────────


class TestFuzzyDeduplicate:
    def test_same_channel_high_similarity_dedup(self):
        """
        GIVEN two findings on the same channel with near-identical titles
        WHEN fuzzy_deduplicate runs
        THEN only the higher-relevance one survives.
        """
        a = Finding(
            "reddit",
            "discourse",
            "Best Way to Build LLM Agents",
            "https://x/a",
            0.4,
            "",
        )
        b = Finding(
            "reddit", "discourse", "Best Way Build LLM Agents", "https://x/b", 0.7, ""
        )
        result = fuzzy_deduplicate([a, b])
        assert len(result) == 1
        assert result[0].relevance == 0.7

    def test_cross_channel_dedup_optional(self):
        """
        GIVEN two findings on different channels with similar titles
        WHEN fuzzy_deduplicate runs without cross_channel
        THEN both survive.
        """
        a = Finding("reddit", "discourse", "Building AI agents", "https://x/a", 0.5, "")
        b = Finding("github", "code", "Building AI agents", "https://x/b", 0.6, "")
        # default: no cross-channel folding
        assert len(fuzzy_deduplicate([a, b])) == 2
        # with cross-channel: lower-relevance one drops
        merged = fuzzy_deduplicate([a, b], cross_channel=True)
        assert len(merged) == 1
        assert merged[0].relevance == 0.6


# ─── Authority and recency bonuses ───────────────────────────────


class TestRelevanceScore:
    def test_github_high_stars_boost(self):
        f = Finding(
            "github",
            "code",
            "Repo",
            "https://g/r",
            0.5,
            "",
            metadata={"stars": 6000},
        )
        assert compute_relevance_score(f) == pytest.approx(0.7)

    def test_hn_high_score_boost(self):
        f = Finding(
            "hn",
            "discourse",
            "Post",
            "https://hn/i",
            0.5,
            "",
            metadata={"score": 600},
        )
        assert compute_relevance_score(f) == pytest.approx(0.7)

    def test_recency_bonus(self):
        f = Finding(
            "reddit",
            "discourse",
            "Post",
            "https://r/a",
            0.5,
            "",
            metadata={"score": 10, "year": 2026},
        )
        # 0.5 base + 0.05 recency = 0.55
        assert compute_relevance_score(f) == pytest.approx(0.55)

    def test_score_capped_at_one(self):
        f = Finding(
            "github",
            "code",
            "Repo",
            "https://g/r",
            0.95,
            "",
            metadata={"stars": 10000, "year": 2026},
        )
        # would be 0.95 + 0.2 + 0.05 = 1.2, capped at 1.0
        assert compute_relevance_score(f) == 1.0


# ─── Triangulation bonus ─────────────────────────────────────────


class TestTriangulationBonus:
    def test_corroborated_across_channels(self):
        # Identical normalized words across channels -> Jaccard 1.0
        a = Finding(
            "reddit", "discourse", "FastAPI agent platform", "https://r/a", 0.5, ""
        )
        b = Finding("github", "code", "FastAPI agent platform", "https://g/b", 0.5, "")
        # 'a' (discourse) is corroborated by 'b' (code) -> +0.05
        bonus_a = compute_triangulation_bonus(a, [a, b])
        assert bonus_a == pytest.approx(0.05)

    def test_no_corroboration(self):
        a = Finding(
            "reddit", "discourse", "Wholly unique title here", "https://r/a", 0.5, ""
        )
        b = Finding(
            "github", "code", "Different topic entirely", "https://g/b", 0.5, ""
        )
        assert compute_triangulation_bonus(a, [a, b]) == 0.0

    def test_bonus_capped_at_fifteen_percent(self):
        # Five different channels all corroborating -> capped at 0.15
        target = Finding(
            "reddit", "discourse", "FastAPI agent platform", "https://r/a", 0.5, ""
        )
        others = [
            Finding("github", "code", "FastAPI agent platform", "https://g/b", 0.5, ""),
            Finding(
                "arxiv", "academic", "FastAPI agent platform", "https://a/c", 0.5, ""
            ),
            Finding("triz", "triz", "FastAPI agent platform", "https://t/d", 0.5, ""),
            Finding(
                "hn", "discourse", "FastAPI agent platform", "https://hn/e", 0.5, ""
            ),
        ]
        bonus = compute_triangulation_bonus(target, [target] + others)
        assert bonus == pytest.approx(0.15)


# ─── Ranking + merge ─────────────────────────────────────────────


class TestRanking:
    def test_rank_descending(self):
        a = Finding("reddit", "discourse", "A", "https://r/a", 0.3, "")
        b = Finding(
            "github",
            "code",
            "B",
            "https://g/b",
            0.8,
            "",
            metadata={"stars": 10000},
        )
        ranked = rank_findings([a, b])
        assert ranked[0] is b
        assert ranked[1] is a

    def test_merge_dedupes_by_url(self):
        a = Finding("r", "discourse", "A", "https://x/a", 0.4, "")
        b = Finding("r", "discourse", "A", "https://x/a", 0.7, "")
        c = Finding("r", "discourse", "B", "https://x/b", 0.5, "")
        merged = merge_findings([[a, b], [c]])
        assert len(merged) == 2
        # Higher-relevance kept for shared URL
        urls = {f.url: f.relevance for f in merged}
        assert urls["https://x/a"] == 0.7


# ─── Orchestration with mocked source calls ──────────────────────


class TestOrchestrateResearch:
    @pytest.mark.asyncio
    async def test_returns_ranked_findings(self, monkeypatch):
        """
        GIVEN mocked Reddit + GitHub + web sources
        WHEN orchestrate_research runs
        THEN it returns a single ranked, deduped list.
        """
        from research import orchestrate_research

        async def fake_reddit(topic, **kw):
            return [
                Finding(
                    "reddit",
                    "discourse",
                    "Reddit hit on " + topic,
                    "https://r/1",
                    0.6,
                    "summary",
                    metadata={"score": 80},
                ),
            ]

        async def fake_github(topic, **kw):
            return [
                Finding(
                    "github",
                    "code",
                    "GitHub hit on " + topic,
                    "https://g/1",
                    0.7,
                    "summary",
                    metadata={"stars": 1500},
                ),
            ]

        async def fake_web(topic, **kw):
            return [
                Finding(
                    "web",
                    "discourse",
                    "Web hit on " + topic,
                    "https://w/1",
                    0.5,
                    "summary",
                ),
            ]

        monkeypatch.setattr("research._fetch_reddit", fake_reddit)
        monkeypatch.setattr("research._fetch_github", fake_github)
        monkeypatch.setattr("research._fetch_web", fake_web)

        result = await orchestrate_research("agent platforms")
        assert "findings" in result
        assert "topic" in result
        assert len(result["findings"]) == 3
        # Highest score (GitHub with stars boost) should be first
        assert result["findings"][0]["source"] == "github"

    @pytest.mark.asyncio
    async def test_handles_partial_source_failures(self, monkeypatch):
        from research import orchestrate_research

        async def reddit_fails(topic, **kw):
            raise RuntimeError("reddit down")

        async def fake_github(topic, **kw):
            return [
                Finding(
                    "github",
                    "code",
                    "GitHub hit",
                    "https://g/1",
                    0.5,
                    "",
                ),
            ]

        async def fake_web(topic, **kw):
            return []

        monkeypatch.setattr("research._fetch_reddit", reddit_fails)
        monkeypatch.setattr("research._fetch_github", fake_github)
        monkeypatch.setattr("research._fetch_web", fake_web)

        result = await orchestrate_research("anything")
        assert len(result["findings"]) == 1
        # Errors are keyed by short channel name (discourse_web -> discourse)
        assert "discourse" in result["errors"]

    @pytest.mark.asyncio
    async def test_empty_topic_raises(self):
        from research import orchestrate_research

        with pytest.raises(ValueError):
            await orchestrate_research("")

    @pytest.mark.asyncio
    async def test_channels_filter(self, monkeypatch):
        """
        GIVEN channels=['code']
        WHEN orchestrate_research runs
        THEN only the github source is queried.
        """
        from research import orchestrate_research

        called: list[str] = []

        async def fake_reddit(topic, **kw):
            called.append("reddit")
            return []

        async def fake_github(topic, **kw):
            called.append("github")
            return []

        async def fake_web(topic, **kw):
            called.append("web")
            return []

        monkeypatch.setattr("research._fetch_reddit", fake_reddit)
        monkeypatch.setattr("research._fetch_github", fake_github)
        monkeypatch.setattr("research._fetch_web", fake_web)

        await orchestrate_research("topic", channels=["code"])
        assert called == ["github"]


# ─── Academic + TRIZ channels ────────────────────────────────────


class TestAcademicChannel:
    """`_fetch_academic` queries arXiv (Atom) and converts entries to
    Findings with citation/year metadata.
    """

    @pytest.mark.asyncio
    async def test_parses_arxiv_atom_feed(self, monkeypatch):
        from research import _fetch_academic

        atom = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <title>Multi-Agent Research Systems</title>
    <summary>An exploration of orchestrator-worker patterns.</summary>
    <published>2026-01-15T00:00:00Z</published>
    <link href="http://arxiv.org/abs/2401.12345v1"/>
  </entry>
</feed>
"""

        class FakeResp:
            status_code = 200
            text = atom

            def raise_for_status(self):
                pass

        async def fake_get(self, url, **kw):
            return FakeResp()

        import httpx

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        results = await _fetch_academic("multi-agent research")
        assert len(results) >= 1
        first = next((f for f in results if f.source == "arxiv"), None)
        assert first is not None
        assert "Multi-Agent Research Systems" in first.title
        assert "arxiv.org/abs/2401.12345" in first.url
        assert first.metadata.get("year") == 2026

    @pytest.mark.asyncio
    async def test_handles_arxiv_error_gracefully(self, monkeypatch):
        from research import _fetch_academic
        import httpx

        async def fail_get(self, url, **kw):
            raise httpx.HTTPError("network down")

        monkeypatch.setattr(httpx.AsyncClient, "get", fail_get)

        # Should not raise; both arxiv and semantic-scholar fail -> []
        results = await _fetch_academic("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_orchestrator_includes_academic_when_requested(self, monkeypatch):
        from research import Finding, orchestrate_research

        async def fake_academic(topic, **kw):
            return [
                Finding(
                    "arxiv",
                    "academic",
                    "Test paper",
                    "https://arxiv.org/abs/x",
                    0.7,
                    "",
                    metadata={"citations": 80},
                )
            ]

        async def empty(topic, **kw):
            return []

        monkeypatch.setattr("research._fetch_academic", fake_academic)
        monkeypatch.setattr("research._fetch_reddit", empty)
        monkeypatch.setattr("research._fetch_github", empty)
        monkeypatch.setattr("research._fetch_web", empty)

        result = await orchestrate_research("topic", channels=["academic"])
        assert result["total"] == 1
        assert result["findings"][0]["source"] == "arxiv"


class TestTrizChannel:
    """`_fetch_triz` does cross-domain analogical reasoning by
    asking GLM web-search for solutions in adjacent fields.
    """

    @pytest.mark.asyncio
    async def test_returns_at_least_one_triz_finding(self, monkeypatch):
        from research import _fetch_triz

        async def fake_llm(prompt, max_tokens=100, web_search=False):
            return (
                "1. From queueing theory: bounded buffer pattern.\n"
                "2. From cell biology: ATP throttle.\n"
            )

        monkeypatch.setattr("llm_proxy._llm_call", fake_llm)
        results = await _fetch_triz("rate limiting LLM API calls")
        assert len(results) >= 1
        first = results[0]
        assert first.channel == "triz"

    @pytest.mark.asyncio
    async def test_returns_empty_when_llm_returns_none(self, monkeypatch):
        from research import _fetch_triz

        async def fake_llm(prompt, max_tokens=100, web_search=False):
            return None

        monkeypatch.setattr("llm_proxy._llm_call", fake_llm)
        results = await _fetch_triz("anything")
        assert results == []


# ─── Confidence flagging (single-source warning) ─────────────────


class TestChannelHealth:
    """`channel_health()` reports per-channel readiness for the
    operator so an empty `/research` response can be diagnosed
    without log spelunking.
    """

    def test_reports_each_channel_with_source(self):
        from research import channel_health

        report = channel_health()
        assert set(report.keys()) >= {"discourse", "code", "discourse_web"}
        for ch, info in report.items():
            assert "configured" in info
            assert "source" in info

    def test_unconfigured_channel_includes_reason(self, monkeypatch):
        # Force github 'not configured' branch
        monkeypatch.setenv("GITHUB_PAT", "")
        # Re-import to pick up env change
        import importlib

        import github_search

        importlib.reload(github_search)

        from research import channel_health

        report = channel_health()
        code = report["code"]
        assert code["configured"] is False
        assert "reason" in code
        assert "GITHUB_PAT" in code["reason"]


class TestConfidenceFlags:
    def test_single_source_findings_flagged(self):
        from research import flag_confidence

        findings = [
            Finding(
                "reddit",
                "discourse",
                "Unique claim",
                "https://r/1",
                0.6,
                "",
            ),
            Finding(
                "github",
                "code",
                "Cross-source claim",
                "https://g/1",
                0.6,
                "",
            ),
            Finding(
                "hn",
                "discourse",
                "Cross-source claim",
                "https://hn/1",
                0.5,
                "",
            ),
        ]
        flagged = flag_confidence(findings)
        # Unique claim has only one corroborator (itself) -> flagged
        assert flagged[0]["confidence"] == "low"
        # Cross-source claim appears on two channels -> not flagged
        assert flagged[1]["confidence"] in ("medium", "high")
