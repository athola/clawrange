"""Failure modes of the deal sources: each must degrade, never go silent.

A source that is down has to show up as a failed status, and one bad
query, subreddit or item must not take the rest of the source with it.
"""

import sys
import types

import httpx
import pytest

import deal_sources
from deals import WatchTarget
from tests.test_deal_sources import ATOM

T1 = WatchTarget(name="A", query="rtx 3090", category="gpu", great_price=750.0)
T2 = WatchTarget(name="B", query="r740", category="server", great_price=500.0)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ebay_env(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")


def _is_token(request: httpx.Request) -> bool:
    return request.url.path.endswith("/oauth2/token")


def _item(item_id="1", **extra):
    base = {
        "itemId": item_id,
        "title": "RTX 3090",
        "price": {"value": "600"},
        "itemWebUrl": f"https://e/{item_id}",
    }
    return {**base, **extra}


class TestSlickdeals:
    @pytest.mark.asyncio
    async def test_one_failing_query_keeps_the_other(self):
        """GIVEN two targets WHEN one query errors THEN the other's listings stay."""

        def handler(request):
            if "r740" in str(request.url):
                return httpx.Response(503)
            return httpx.Response(200, text=ATOM)

        async with _client(handler) as client:
            out = await deal_sources.fetch_slickdeals(client, [T1, T2], pace=0)
        assert [x.source for x in out] == ["Slickdeals"]

    @pytest.mark.asyncio
    async def test_every_query_failing_raises(self):
        """GIVEN all queries fail WHEN fetched THEN it raises, not returns []."""

        def handler(request):
            raise httpx.ConnectError("down")

        async with _client(handler) as client:
            with pytest.raises(RuntimeError, match="all 2 Slickdeals queries"):
                await deal_sources.fetch_slickdeals(client, [T1, T2], pace=0)

    @pytest.mark.asyncio
    async def test_no_targets_is_not_a_failure(self):
        """GIVEN no targets WHEN fetched THEN empty result, no error."""
        async with _client(lambda r: httpx.Response(500)) as client:
            assert await deal_sources.fetch_slickdeals(client, [], pace=0) == []

    @pytest.mark.asyncio
    async def test_paces_between_queries_only(self):
        """GIVEN three targets WHEN fetched THEN it sleeps twice, never first."""
        slept: list[float] = []

        async def sleep(s):
            slept.append(s)

        async with _client(lambda r: httpx.Response(200, text=ATOM)) as client:
            await deal_sources.fetch_slickdeals(
                client, [T1, T2, T1], pace=1.5, _sleep=sleep
            )
        assert slept == [1.5, 1.5]


class TestServeTheHome:
    @pytest.mark.asyncio
    async def test_parses_the_great_deals_feed(self):
        """GIVEN a feed WHEN fetched THEN listings carry the STH source."""
        async with _client(lambda r: httpx.Response(200, text=ATOM)) as client:
            [listing] = await deal_sources.fetch_servethehome(client, [T1], pace=0)
        assert listing.source == "ServeTheHome"
        assert listing.price == 680.0

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        """GIVEN STH is down WHEN fetched THEN the error reaches gather."""
        async with _client(lambda r: httpx.Response(502)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await deal_sources.fetch_servethehome(client, [T1])


class _Post:
    def __init__(self, pid, title, body=""):
        self.id = pid
        self.title = title
        self.selftext = body
        self.permalink = f"/r/x/comments/{pid}/"


class _RateLimited(Exception):
    status_code = 429


def _fake_asyncpraw(monkeypatch, posts_by_sub):
    """Install a stub asyncpraw; returns the list of created clients."""
    created: list = []

    class Sub:
        def __init__(self, name):
            self.name = name

        async def new(self, limit):
            for p in posts_by_sub[self.name]:
                yield p

    class Reddit:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        async def subreddit(self, name):
            if isinstance(posts_by_sub.get(name), Exception):
                raise posts_by_sub[name]
            return Sub(name)

        async def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "asyncpraw", types.SimpleNamespace(Reddit=Reddit))
    for var in (
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
    ):
        monkeypatch.setenv(var, "x")
    return created


class TestRedditOAuth:
    @pytest.mark.asyncio
    async def test_oauth_reads_new_posts_and_closes_the_client(self, monkeypatch):
        """GIVEN credentials WHEN fetched THEN posts parse and the client closes."""
        created = _fake_asyncpraw(
            monkeypatch,
            {"homelabsales": [_Post("p1", "[FS] RTX 3090", "asking $650")]},
        )
        async with _client(lambda r: httpx.Response(500)) as client:
            listings, status = await deal_sources.fetch_reddit(
                client, ["homelabsales"], pace=0
            )
        [listing] = listings
        assert listing.source == "r/homelabsales"
        assert listing.url == "https://www.reddit.com/r/x/comments/p1/"
        assert listing.price == 650.0
        assert status.ok is True
        assert status.fix == ""  # OAuth mode carries no keyless-request fix
        assert created and created[0].closed

    @pytest.mark.asyncio
    async def test_one_failing_sub_keeps_the_others(self, monkeypatch):
        """GIVEN one sub rate-limited WHEN fetched THEN the rest still read."""
        created = _fake_asyncpraw(
            monkeypatch,
            {
                "badsub": _RateLimited("slow down"),
                "homelabsales": [_Post("p1", "[FS] R740 $400")],
            },
        )
        async with _client(lambda r: httpx.Response(500)) as client:
            listings, status = await deal_sources.fetch_reddit(
                client, ["badsub", "homelabsales"], pace=0
            )
        assert [x.listing_id for x in listings] == ["p1"]
        assert "429" in status.detail
        assert created[0].closed


class TestEbayCredentials:
    @pytest.mark.asyncio
    async def test_missing_keys_is_not_configured(self, monkeypatch):
        """GIVEN no eBay keys WHEN fetched THEN NotConfigured, no request sent."""
        monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
        monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
        calls: list = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200)

        async with _client(handler) as client:
            with pytest.raises(deal_sources.NotConfigured):
                await deal_sources.fetch_ebay(client, [T1], pace=0)
        assert calls == []

    @pytest.mark.parametrize("code", [400, 401, 403])
    @pytest.mark.asyncio
    async def test_rejected_token_is_not_configured(self, monkeypatch, code):
        """GIVEN eBay rejects the keyset THEN NotConfigured names the code."""
        _ebay_env(monkeypatch)
        async with _client(lambda r: httpx.Response(code)) as client:
            with pytest.raises(deal_sources.NotConfigured, match=str(code)):
                await deal_sources.fetch_ebay(client, [T1], pace=0)

    @pytest.mark.asyncio
    async def test_token_server_error_is_a_plain_failure(self, monkeypatch):
        """GIVEN the token endpoint 500s THEN it is an outage, not bad keys."""
        _ebay_env(monkeypatch)
        async with _client(lambda r: httpx.Response(500)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await deal_sources.fetch_ebay(client, [T1], pace=0)


class TestEbaySearch:
    @pytest.mark.parametrize("code", [401, 403])
    @pytest.mark.asyncio
    async def test_search_auth_rejection_aborts_the_source(self, monkeypatch, code):
        """GIVEN search returns 401/403 THEN the whole source aborts."""
        _ebay_env(monkeypatch)

        def handler(request):
            if _is_token(request):
                return httpx.Response(200, json={"access_token": "t"})
            return httpx.Response(code)

        failed: list[str] = []
        async with _client(handler) as client:
            with pytest.raises(deal_sources.NotConfigured):
                await deal_sources.fetch_ebay(client, [T1, T2], pace=0, failed=failed)
        assert failed == []  # an auth failure is not a per-target failure

    @pytest.mark.asyncio
    async def test_non_200_target_is_recorded_and_skipped(self, monkeypatch):
        """GIVEN one target 500s THEN it is listed in failed, the other kept."""
        _ebay_env(monkeypatch)

        def handler(request):
            if _is_token(request):
                return httpx.Response(200, json={"access_token": "t"})
            if request.url.params["q"] == "rtx 3090":
                return httpx.Response(500)
            return httpx.Response(200, json={"itemSummaries": [_item()]})

        failed: list[str] = []
        async with _client(handler) as client:
            out = await deal_sources.fetch_ebay(client, [T1, T2], pace=0, failed=failed)
        assert len(out) == 1 and failed == ["rtx 3090"]

    @pytest.mark.asyncio
    async def test_malformed_and_variation_items_are_dropped(self, monkeypatch):
        """GIVEN odd items WHEN parsed THEN only the sane one survives."""
        _ebay_env(monkeypatch)
        items = [
            _item("bad-ship", shippingOptions=[None]),  # raises inside parsing
            _item("group", itemGroupType="SELLER_DEFINED_VARIATIONS"),
            _item("no-price", price={}),
            _item("good"),
        ]

        def handler(request):
            if _is_token(request):
                return httpx.Response(200, json={"access_token": "t"})
            return httpx.Response(200, json={"itemSummaries": items})

        async with _client(handler) as client:
            out = await deal_sources.fetch_ebay(client, [T1], pace=0)
        assert [x.listing_id for x in out] == ["good"]

    @pytest.mark.asyncio
    async def test_marketplace_and_category_reach_the_request(self, monkeypatch):
        """GIVEN profile config WHEN searching THEN headers and params carry it."""
        _ebay_env(monkeypatch)
        seen: list[httpx.Request] = []
        target = WatchTarget(
            name="A",
            query="rtx 3090",
            category="gpu",
            great_price=750.0,
            ebay_category="27386",
        )

        def handler(request):
            if _is_token(request):
                return httpx.Response(200, json={"access_token": "t"})
            seen.append(request)
            return httpx.Response(200, json={"itemSummaries": []})

        async with _client(handler) as client:
            await deal_sources.fetch_ebay(
                client,
                [target],
                pace=0,
                config={
                    "marketplace": "EBAY_GB",
                    "currency": "GBP",
                    "delivery_country": "GB",
                },
            )
        [req] = seen
        assert req.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"
        assert req.headers["Authorization"] == "Bearer t"
        assert req.url.params["category_ids"] == "27386"
        filt = req.url.params["filter"]
        assert "priceCurrency:GBP" in filt and "deliveryCountry:GB" in filt


async def _value(v):
    return v


class TestGatherStatuses:
    @pytest.mark.asyncio
    async def test_unconfigured_ebay_reports_the_fix(self, monkeypatch):
        """GIVEN eBay lacks keys WHEN gathered THEN its status names the fix."""

        async def not_configured(*a, **k):
            raise deal_sources.NotConfigured("EBAY_CLIENT_ID not set")

        monkeypatch.setattr(deal_sources, "fetch_ebay", not_configured)
        _, statuses = await deal_sources.gather([T1], {"ebay": {}}, pace=0)
        [status] = statuses
        assert status.ok is False
        assert status.fix == deal_sources.EBAY_FIX

    @pytest.mark.asyncio
    async def test_every_ebay_target_failing_marks_the_source_failed(self, monkeypatch):
        """GIVEN all targets failed WHEN gathered THEN eBay is not ok."""

        async def all_failed(client, targets, pace, config, failed):
            failed.extend(t.query for t in targets)
            return []

        monkeypatch.setattr(deal_sources, "fetch_ebay", all_failed)
        _, [status] = await deal_sources.gather([T1, T2], {"ebay": {}}, pace=0)
        assert status.ok is False
        assert "all 2 target searches failed" in status.detail

    @pytest.mark.asyncio
    async def test_partial_ebay_failure_is_ok_but_says_so(self, monkeypatch):
        """GIVEN one of two targets failed THEN ok, with the count in detail."""

        async def half(client, targets, pace, config, failed):
            failed.append(targets[0].query)
            return []

        monkeypatch.setattr(deal_sources, "fetch_ebay", half)
        _, [status] = await deal_sources.gather([T1, T2], {"ebay": {}}, pace=0)
        assert status.ok is True
        assert "(1/2 targets failed)" in status.detail

    @pytest.mark.asyncio
    async def test_reddit_crash_becomes_a_failed_status(self, monkeypatch):
        """GIVEN fetch_reddit raises WHEN gathered THEN Reddit shows failed."""

        async def boom(*a, **k):
            raise RuntimeError("praw exploded")

        monkeypatch.setattr(deal_sources, "fetch_reddit", boom)
        _, [status] = await deal_sources.gather(
            [T1], {"reddit": {"subreddits": ["x"]}}, pace=0
        )
        assert status.name == "Reddit" and status.ok is False
        assert "praw exploded" in status.detail

    @pytest.mark.asyncio
    async def test_unlisted_sources_are_not_polled(self, monkeypatch):
        """GIVEN a profile with no sources WHEN gathered THEN nothing runs."""

        async def must_not_run(*a, **k):
            raise AssertionError("polled a source the profile did not list")

        for name in (
            "fetch_ebay",
            "fetch_slickdeals",
            "fetch_servethehome",
            "fetch_reddit",
        ):
            monkeypatch.setattr(deal_sources, name, must_not_run)
        assert await deal_sources.gather([T1], {}, pace=0) == ([], [])

    @pytest.mark.asyncio
    async def test_reddit_status_detail_gets_the_post_count(self, monkeypatch):
        """GIVEN reddit found posts WHEN gathered THEN detail counts them."""
        post = deal_sources.Listing(
            source="r/x", listing_id="1", title="t", url="u", price=1.0
        )
        monkeypatch.setattr(
            deal_sources,
            "fetch_reddit",
            lambda *a, **k: _value(
                ([post], deal_sources.SourceStatus("Reddit", True, "1 ok"))
            ),
        )
        listings, [status] = await deal_sources.gather(
            [T1], {"reddit": {"subreddits": ["x"]}}, pace=0
        )
        assert listings == [post]
        assert status.detail == "1 ok · 1 posts"
