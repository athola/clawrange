"""Tests for reddit_search source-health reporting on the public fallback."""

import httpx
import pytest

import reddit_search
from reddit_search import SearchHealth


def _patch_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(reddit_search.httpx, "AsyncClient", factory)
    for var in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)


class TestSearchHealth:
    """GIVEN Reddit blocks the unauthenticated endpoint
    WHEN the digest searches several subreddits
    THEN the failures are counted and the run stops hammering Reddit."""

    @pytest.mark.asyncio
    async def test_counts_blocked_responses_and_trips(self, monkeypatch):
        calls: list[str] = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(403)

        _patch_transport(monkeypatch, handler)
        health = SearchHealth()
        subs = [f"sub{i}" for i in range(10)]
        posts = await reddit_search.search_subreddits(
            "claude", subs, since="24h", health=health
        )

        assert posts == []
        assert health.mode == "public"
        assert health.failures == {"403": SearchHealth.BLOCK_LIMIT}
        assert health.tripped
        assert health.skipped == 10 - SearchHealth.BLOCK_LIMIT
        assert len(calls) == SearchHealth.BLOCK_LIMIT

    @pytest.mark.asyncio
    async def test_success_resets_block_streak(self, monkeypatch):
        statuses = iter([429, 200, 429, 200])

        def handler(request):
            return httpx.Response(next(statuses), json={"data": {"children": []}})

        _patch_transport(monkeypatch, handler)
        health = SearchHealth()
        await reddit_search.search_subreddits(
            "claude", ["a", "b", "c", "d"], since="24h", health=health
        )
        assert health.ok == 2
        assert health.failures == {"429": 2}
        assert not health.tripped

    @pytest.mark.asyncio
    async def test_search_all_skips_when_tripped(self, monkeypatch):
        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("tripped health must skip the request")

        _patch_transport(monkeypatch, handler)
        health = SearchHealth()
        for _ in range(SearchHealth.BLOCK_LIMIT):
            health.record(403)
        assert await reddit_search.search_all("claude", health=health) == []
        assert health.skipped == 1

    def test_summary_names_the_fix_when_blocked(self):
        health = SearchHealth(mode="public")
        for _ in range(3):
            health.record(403)
        assert "403" in health.summary()
        assert "0/3" in health.summary()
