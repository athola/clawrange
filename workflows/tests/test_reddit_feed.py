"""Keyless Reddit fallback: when search.json is refused, read each
subreddit's Atom feed once per run and match topics locally."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

import reddit_search
from errors import UpstreamError
from reddit_search import SearchHealth

_REAL_CLIENT = httpx.AsyncClient


def _atom(entries: list[tuple[str, str, datetime]]) -> str:
    body = "".join(
        f"<entry><id>t3_{pid}</id><title>{title}</title>"
        f'<link href="https://www.reddit.com/r/ClaudeAI/comments/{pid}/x/"/>'
        f"<published>{when.isoformat()}</published>"
        '<category term="ClaudeAI" label="r/ClaudeAI"/>'
        f'<content type="html">&lt;p&gt;body of {pid}&lt;/p&gt;</content></entry>'
        for pid, title, when in entries
    )
    return (
        f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'
    )


@pytest.fixture
def reddit(monkeypatch):
    """Route reddit_search's HTTP through a handler; record calls."""
    calls: list[str] = []
    now = datetime.now(UTC)
    feed = _atom(
        [
            ("a1", "Best Claude Code plugin for reviews?", now - timedelta(hours=2)),
            ("a2", "Unrelated cooking question", now - timedelta(hours=3)),
            ("a3", "Old claude code plugin thread", now - timedelta(days=3)),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("search.json"):
            return httpx.Response(403)
        if request.url.path.endswith("/new/.rss"):
            return httpx.Response(200, text=feed)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        reddit_search.httpx,
        "AsyncClient",
        lambda **kw: _REAL_CLIENT(transport=transport, **kw),
    )
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(reddit_search, "_sleep", fake_sleep)
    return calls, slept


class TestFeedFallback:
    @pytest.mark.asyncio
    async def test_blocked_json_falls_back_to_the_feed(self, reddit):
        health = SearchHealth()
        posts = await reddit_search.search_subreddits(
            "claude code plugin",
            ["ClaudeAI"],
            since="24h",
            health=health,
            feed_fallback=True,
        )
        assert [p.id for p in posts] == ["a1"]  # topic matched, window kept
        assert posts[0].engagement_known is False
        assert posts[0].url.startswith("https://www.reddit.com/r/ClaudeAI/")
        assert health.mode == "rss"
        assert health.ok == 1

    @pytest.mark.asyncio
    async def test_feed_is_fetched_once_per_sub_per_run(self, reddit):
        calls, slept = reddit
        health = SearchHealth()
        for topic in ("claude code plugin", "plugin", "cooking"):
            await reddit_search.search_subreddits(
                topic,
                ["ClaudeAI", "LocalLLaMA"],
                since="24h",
                health=health,
                feed_fallback=True,
            )
        feeds = [c for c in calls if c.endswith(".rss")]
        assert len(feeds) == 2  # one per subreddit, not per topic
        assert sum(c.endswith("search.json") for c in calls) == 1  # stop probing
        assert slept  # paced between feed requests
        assert not health.tripped

    @pytest.mark.asyncio
    async def test_all_reddit_search_is_skipped_in_feed_mode(self, reddit):
        health = SearchHealth()
        await reddit_search.search_subreddits(
            "plugin",
            ["ClaudeAI"],
            since="24h",
            health=health,
            feed_fallback=True,
        )
        assert await reddit_search.search_all("plugin", health=health) == []
        assert health.skipped == 1

    def test_feed_with_a_dtd_is_refused(self):
        evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed/>'
        assert reddit_search.parse_feed_posts(evil, datetime.now(UTC)) == []


class TestUnknownEngagementRendering:
    def test_facts_and_angle_say_engagement_is_unknown(self):
        import generators

        post = reddit_search.RedditPost(
            id="a1",
            url="https://www.reddit.com/r/ClaudeAI/comments/a1/x/",
            title="Best Claude Code plugin?",
            subreddit="ClaudeAI",
            score=0,
            comments=0,
            created_utc=datetime.now(UTC).isoformat(),
            engagement_known=False,
        )
        facts = generators._pick_facts(post, ["plugin"], [], False)
        assert "0 pts" not in facts and "engagement unknown" in facts
        assert "no replies" not in generators._comment_angle(post)


class TestFeedRetry:
    @pytest.mark.asyncio
    async def test_one_429_is_retried_after_a_backoff(self, monkeypatch):
        now = datetime.now(UTC)
        feed = _atom([("b1", "claude code plugin help", now - timedelta(hours=1))])
        statuses = iter([429, 200])

        def handler(request):
            if request.url.path.endswith("search.json"):
                return httpx.Response(403)
            return httpx.Response(next(statuses), text=feed)

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            reddit_search.httpx,
            "AsyncClient",
            lambda **kw: _REAL_CLIENT(transport=transport, **kw),
        )
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(reddit_search, "_sleep", fake_sleep)
        health = SearchHealth()
        posts = await reddit_search.search_subreddits(
            "claude code plugin",
            ["ClaudeAI"],
            since="24h",
            health=health,
            feed_fallback=True,
        )
        assert [p.id for p in posts] == ["b1"]
        assert slept == [reddit_search.FEED_PACE_S * 2]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("statuses", "final", "slept_expected"),
        [([200], 200, []), ([429, 200], 200, [7.0]), ([429, 429], 429, [7.0])],
    )
    async def test_shared_429_retry(self, statuses, final, slept_expected):
        """#14: the one 429 retry both the scanner and deal feeds use."""
        replies = iter(statuses)
        seen: list[str] = []

        def handler(request):
            seen.append(request.headers["user-agent"])
            return httpx.Response(next(replies))

        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)

        health = SearchHealth()
        async with _REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
            resp = await reddit_search.get_retrying_429(
                client,
                "https://r.example/x",
                {"User-Agent": "ua"},
                health,
                fake_sleep,
                7.0,
            )
        assert resp.status_code == final
        assert slept == slept_expected
        assert seen == ["ua"] * len(statuses)
        assert health.requests == len(statuses)


class TestFallbackIsOptIn:
    @pytest.mark.asyncio
    async def test_default_callers_never_fetch_feeds(self, reddit):
        calls, _ = reddit
        # JSON is refused and feeds are off, so the call raises (#26).
        with pytest.raises(UpstreamError):
            await reddit_search.search_subreddits("plugin", ["ClaudeAI"], since="24h")
        assert not any(c.endswith(".rss") for c in calls)
