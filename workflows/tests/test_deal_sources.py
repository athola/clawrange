"""Tests for homelab deal source adapters (feeds, Reddit, eBay Browse)."""

import httpx
import pytest

import deal_sources
from deals import WatchTarget

RTX3090 = WatchTarget(
    name="RTX 3090 24GB", query="rtx 3090", category="gpu", great_price=750.0
)

RSS = """<?xml version="1.0"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
<item>
  <title>EVGA RTX 3090 FTW3 24GB $649.99 + Free Shipping</title>
  <link>https://slickdeals.net/f/1</link>
  <guid>sd-1</guid>
  <description>&lt;p&gt;Great card&lt;/p&gt;</description>
</item>
<item>
  <title>Dell R740 barebones</title>
  <link>https://forums.servethehome.com/t/2</link>
  <guid>sth-2</guid>
  <content:encoded>&lt;p&gt;Asking &lt;b&gt;$450&lt;/b&gt; shipped
  &lt;/p&gt;</content:encoded>
</item>
<item>
  <title>Mixed lot</title>
  <link>https://forums.servethehome.com/t/3</link>
  <guid>sth-3</guid>
  <description>RTX 3090 $700, RTX 4090 $1500</description>
</item>
</channel>
</rss>"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <id>t3_abc123</id>
  <title>[FS][US-TX] RTX 3090 Founders Edition</title>
  <link href="https://www.reddit.com/r/homelabsales/comments/abc123/fs/"/>
  <content type="html">&lt;p&gt;Price: $680 shipped&lt;/p&gt;</content>
</entry>
</feed>"""


class TestParseFeed:
    def test_rss_title_price_and_ids(self):
        items = deal_sources.parse_feed(RSS, "slickdeals")
        assert items[0].price == 649.99
        assert items[0].listing_id == "sd-1"
        assert items[0].url == "https://slickdeals.net/f/1"

    def test_body_price_used_when_title_has_none(self):
        items = deal_sources.parse_feed(RSS, "servethehome")
        assert items[1].price == 450.0

    def test_ambiguous_body_prices_leave_price_unset(self):
        items = deal_sources.parse_feed(RSS, "servethehome")
        assert items[2].price is None

    def test_atom_entries(self):
        [item] = deal_sources.parse_feed(ATOM, "r/homelabsales")
        assert item.price == 680.0
        assert item.listing_id == "t3_abc123"
        assert item.url.startswith("https://www.reddit.com/r/homelabsales/")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestRedditFeeds:
    @pytest.mark.asyncio
    async def test_retries_once_on_429_then_reports_health(self, monkeypatch):
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429)
            return httpx.Response(200, text=ATOM)

        async with _client(handler) as client:
            listings, status = await deal_sources.fetch_reddit(
                client, ["homelabsales"], pace=0
            )
        assert len(listings) == 1
        assert status.ok
        assert "429" in status.detail
        assert "prefs/apps" in status.fix


class TestEbay:
    @pytest.mark.asyncio
    async def test_not_configured_raises_with_signup_fix(self, monkeypatch):
        monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
        monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
        async with _client(lambda r: httpx.Response(500)) as client:
            with pytest.raises(deal_sources.NotConfigured):
                await deal_sources.fetch_ebay(client, [RTX3090], pace=0)

    @pytest.mark.asyncio
    async def test_token_then_search_maps_items(self, monkeypatch):
        monkeypatch.setenv("EBAY_CLIENT_ID", "id")
        monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
        seen: dict = {}

        def handler(request):
            if request.url.path.endswith("/oauth2/token"):
                assert request.headers["Authorization"].startswith("Basic ")
                return httpx.Response(200, json={"access_token": "tok"})
            seen["params"] = dict(request.url.params)
            seen["auth"] = request.headers["Authorization"]
            seen["mkt"] = request.headers["X-EBAY-C-MARKETPLACE-ID"]
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "itemId": "v1|123|0",
                            "title": "NVIDIA RTX 3090 24GB",
                            "price": {"value": "640.00", "currency": "USD"},
                            "shippingOptions": [{"shippingCost": {"value": "25.00"}}],
                            "condition": "Used",
                            "seller": {"feedbackScore": 812},
                            "itemWebUrl": "https://www.ebay.com/itm/123",
                            "itemLocation": {"country": "US"},
                        }
                    ]
                },
            )

        async with _client(handler) as client:
            [item] = await deal_sources.fetch_ebay(client, [RTX3090], pace=0)

        assert seen["auth"] == "Bearer tok"
        assert seen["mkt"] == "EBAY_US"
        assert seen["params"]["q"] == "rtx 3090"
        assert "price:[" in seen["params"]["filter"]
        assert "conditionIds" in seen["params"]["filter"]
        assert (item.price, item.shipping, item.seller_feedback) == (640.0, 25.0, 812)
        assert item.source == "eBay"


class TestGather:
    @pytest.mark.asyncio
    async def test_one_broken_source_does_not_sink_the_rest(self, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("down")

        async def ok(*a, **k):
            return [
                deal_sources.Listing(
                    source="slickdeals",
                    listing_id="1",
                    title="RTX 3090",
                    url="https://s.test/1",
                    price=600.0,
                )
            ]

        monkeypatch.setattr(deal_sources, "fetch_ebay", boom)
        monkeypatch.setattr(deal_sources, "fetch_slickdeals", ok)
        monkeypatch.setattr(deal_sources, "fetch_servethehome", ok)
        monkeypatch.setattr(
            deal_sources,
            "fetch_reddit",
            lambda *a, **k: _async_value(
                ([], deal_sources.SourceStatus("Reddit", True, "0"))
            ),
        )
        cfg = {
            "ebay": {},
            "slickdeals": {},
            "servethehome": {},
            "reddit": {"subreddits": []},
        }
        listings, statuses = await deal_sources.gather([RTX3090], cfg, pace=0)
        assert len(listings) == 1  # the two "ok" feeds share a listing id
        by_name = {s.name: s for s in statuses}
        assert by_name["eBay"].ok is False
        assert by_name["Slickdeals"].ok is True


async def _async_value(v):
    return v


class TestUntrustedXml:
    def test_documents_with_a_dtd_are_rejected(self):
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]>'
            "<rss><channel/></rss>"
        )
        with pytest.raises(ValueError):
            deal_sources.parse_feed(bomb, "x")
