"""One failing fetch must not abort the whole deal source."""

import httpx
import pytest

import deal_sources
from deals import WatchTarget
from tests.test_deal_sources import ATOM

T1 = WatchTarget(name="A", query="rtx 3090", category="gpu", great_price=750.0)
T2 = WatchTarget(name="B", query="r740", category="server", great_price=500.0)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_reddit_rss_one_sub_raising_keeps_the_others(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)

    def handler(request):
        if "badsub" in str(request.url):
            raise httpx.ConnectTimeout("slow")
        return httpx.Response(200, text=ATOM)

    async with _client(handler) as client:
        listings, status = await deal_sources.fetch_reddit(
            client, ["badsub", "homelabsales"], pace=0
        )
    assert len(listings) == 1
    assert "error" in status.detail


def _ebay_env(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")


def _item(ship="5.00"):
    return {
        "itemId": "1",
        "title": "RTX 3090",
        "price": {"value": "600"},
        "shippingOptions": [{"shippingCost": {"value": ship}}],
        "itemWebUrl": "https://e/1",
    }


@pytest.mark.asyncio
async def test_ebay_failing_target_is_counted_and_skipped(monkeypatch):
    _ebay_env(monkeypatch)

    def handler(request):
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t"})
        if request.url.params["q"] == "rtx 3090":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"itemSummaries": [_item()]})

    failed: list[str] = []
    async with _client(handler) as client:
        out = await deal_sources.fetch_ebay(client, [T1, T2], pace=0, failed=failed)
    assert len(out) == 1 and failed == ["rtx 3090"]


@pytest.mark.asyncio
async def test_ebay_bad_shipping_value_keeps_listing(monkeypatch):
    _ebay_env(monkeypatch)

    def handler(request):
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, json={"itemSummaries": [_item("n/a")]})

    async with _client(handler) as client:
        [item] = await deal_sources.fetch_ebay(client, [T1], pace=0)
    assert item.shipping is None and item.price == 600.0


@pytest.mark.asyncio
async def test_gather_reports_partial_ebay_failures(monkeypatch):
    async def partial(client, targets, pace=0, config=None, failed=None):
        failed.append("rtx 3090")
        return [
            deal_sources.Listing(
                source="eBay", listing_id="1", title="x", url="u", price=1.0
            )
        ]

    monkeypatch.setattr(deal_sources, "fetch_ebay", partial)
    listings, statuses = await deal_sources.gather([T1, T2], {"ebay": {}}, pace=0)
    assert len(listings) == 1
    assert statuses[0].ok and "1/2 targets failed" in statuses[0].detail
