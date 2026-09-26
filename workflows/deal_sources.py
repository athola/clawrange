"""Homelab deal sources: turn feeds and APIs into `deals.Listing`s.

Verified 2026-09-26: Slickdeals search RSS, the ServeTheHome "Great Deals"
RSS and Reddit's www.reddit.com `/new/.rss` Atom feeds answer without
auth (Reddit rate-limits hard, so each subreddit gets one retry on 429).
The eBay Browse API needs free developer keys (EBAY_CLIENT_ID/SECRET);
without them the source reports itself as not configured.

Skipped for now: reseller Shopify JSON (Cloudflare 403), PCPartPicker
(403), Apple refurb (HTML only), LabGopher (unreachable), eBay search RSS
(retired).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from urllib.parse import quote_plus

import httpx

import reddit_search
from deals import TOO_GOOD_OF_TARGET, Listing, WatchTarget, parse_price
from reddit_search import SearchHealth
from rundown import SourceStatus, reddit_status

logger = logging.getLogger("clawrange.deal_sources")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) clawrange-deals/0.1"
SLICKDEALS_RSS = (
    "https://slickdeals.net/newsearch.php?q={q}&searcharea=deals&searchin=first&rss=1"
)
STH_GREAT_DEALS_RSS = (
    "https://forums.servethehome.com/index.php?forums/great-deals.8/index.rss"
)
REDDIT_NEW_RSS = "https://www.reddit.com/r/{sub}/new/.rss?limit=100"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"
# New, open-box, refurbished grades, used. Excludes 7000 (for parts).
EBAY_CONDITIONS = "1000|1500|2000|2010|2020|2030|2500|2750|3000"
# Search up to this multiple of the great price so history sees the market.
EBAY_PRICE_CEILING = 1.3

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
_TAG_RE = re.compile(r"<[^>]+>")
_ALL_PRICES_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?")

Sleep = Callable[[float], Awaitable[None]]


class NotConfigured(RuntimeError):
    """A source that needs credentials the operator has not set."""


# ─── Feeds ───────────────────────────────────────────────────────────


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", html.unescape(text or ""))).strip()


def _price_from(title: str, body: str) -> float | None:
    """Title price first; a body price only when the body names exactly one.

    Sale posts often list several items with prices in the body, and the
    first amount may belong to a different item than the one we matched.
    """
    price = parse_price(title)
    if price is not None:
        return price
    amounts = {m.group(0).replace(" ", "") for m in _ALL_PRICES_RE.finditer(body)}
    return parse_price(body) if len(amounts) == 1 else None


def parse_feed(xml_text: str, source: str) -> list[Listing]:
    """Parse RSS 2.0 items or Atom entries into listings.

    Feeds are untrusted input. Legitimate RSS/Atom never needs a DTD, so any
    DOCTYPE or ENTITY declaration is refused before parsing, which rules out
    entity-expansion attacks without an extra dependency.
    """
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text, re.IGNORECASE):
        raise ValueError("feed declares a DTD; refusing to parse")
    root = ET.fromstring(xml_text)  # noqa: S314 - DTDs rejected above
    out: list[Listing] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        body = _strip_html(
            item.findtext("content:encoded", namespaces=_NS)
            or item.findtext("description")
            or ""
        )
        link = (item.findtext("link") or "").strip()
        out.append(
            Listing(
                source=source,
                listing_id=(item.findtext("guid") or link).strip(),
                title=title,
                url=link,
                price=_price_from(title, body),
            )
        )
    for entry in root.iter(f"{{{_NS['atom']}}}entry"):
        title = (entry.findtext("atom:title", namespaces=_NS) or "").strip()
        body = _strip_html(entry.findtext("atom:content", namespaces=_NS) or "")
        link_el = entry.find("atom:link", _NS)
        link = link_el.get("href", "") if link_el is not None else ""
        out.append(
            Listing(
                source=source,
                listing_id=(entry.findtext("atom:id", namespaces=_NS) or link).strip(),
                title=title,
                url=link,
                price=_price_from(title, body),
            )
        )
    return out


async def _get_feed(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


async def fetch_slickdeals(
    client: httpx.AsyncClient,
    targets: list[WatchTarget],
    pace: float = 1.0,
    _sleep: Sleep | None = None,
) -> list[Listing]:
    """One search feed per target; fails only if every query fails."""
    sleep = _sleep or asyncio.sleep
    out: list[Listing] = []
    failures = 0
    for i, t in enumerate(targets):
        if i and pace:
            await sleep(pace)
        try:
            text = await _get_feed(client, SLICKDEALS_RSS.format(q=quote_plus(t.query)))
            out.extend(parse_feed(text, "Slickdeals"))
        except Exception as exc:
            failures += 1
            logger.warning("deals: slickdeals '%s' failed: %s", t.query, exc)
    if targets and failures == len(targets):
        raise RuntimeError(f"all {failures} Slickdeals queries failed")
    return out


async def fetch_servethehome(
    client: httpx.AsyncClient, targets: list[WatchTarget], **_: object
) -> list[Listing]:
    return parse_feed(await _get_feed(client, STH_GREAT_DEALS_RSS), "ServeTheHome")


# ─── Reddit ──────────────────────────────────────────────────────────


async def _reddit_oauth_new(
    subreddits: list[str], health: SearchHealth
) -> list[Listing]:
    import asyncpraw

    creds = reddit_search._get_credentials()
    health.mode = "oauth"
    out: list[Listing] = []
    reddit = asyncpraw.Reddit(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        username=creds["username"],
        password=creds["password"],
        user_agent=creds["user_agent"],
    )
    try:
        for sub in subreddits:
            try:
                subreddit = await reddit.subreddit(sub)
                async for post in subreddit.new(limit=100):
                    body = post.selftext or ""
                    out.append(
                        Listing(
                            source=f"r/{sub}",
                            listing_id=post.id,
                            title=post.title,
                            url=f"https://www.reddit.com{post.permalink}",
                            price=_price_from(post.title, body),
                        )
                    )
                health.record(200)
            except Exception as exc:
                logger.warning("deals: reddit r/%s failed: %s", sub, exc)
                health.record(getattr(exc, "status_code", None) or "error")
    finally:
        await reddit.close()
    return out


async def fetch_reddit(
    client: httpx.AsyncClient,
    subreddits: list[str],
    pace: float = 6.0,
    _sleep: Sleep | None = None,
) -> tuple[list[Listing], SourceStatus]:
    """Newest posts from sale subreddits: OAuth when configured, else Atom.

    Returns its own status because Reddit health (403/429 counts and the
    OAuth fix) is richer than "worked / raised".
    """
    sleep = _sleep or asyncio.sleep
    health = SearchHealth()
    if await reddit_search.is_configured():
        return await _reddit_oauth_new(subreddits, health), reddit_status(health)

    health.mode = "rss"
    out: list[Listing] = []
    for i, sub in enumerate(subreddits):
        if i and pace:
            await sleep(pace)
        url = REDDIT_NEW_RSS.format(sub=sub)
        for attempt in range(2):
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            health.record(resp.status_code)
            if resp.status_code == 200:
                out.extend(parse_feed(resp.text, f"r/{sub}"))
                break
            if resp.status_code != 429 or attempt:
                break
            await sleep(pace * 2)
    return out, reddit_status(health)


# ─── eBay Browse API ─────────────────────────────────────────────────


async def _ebay_token(client: httpx.AsyncClient) -> str:
    cid = os.getenv("EBAY_CLIENT_ID", "")
    secret = os.getenv("EBAY_CLIENT_SECRET", "")
    if not (cid and secret):
        raise NotConfigured("EBAY_CLIENT_ID/EBAY_CLIENT_SECRET not set")
    resp = await client.post(
        EBAY_TOKEN_URL,
        auth=(cid, secret),
        data={"grant_type": "client_credentials", "scope": EBAY_SCOPE},
    )
    if resp.status_code in (400, 401, 403):
        raise NotConfigured(f"eBay token request rejected (HTTP {resp.status_code})")
    resp.raise_for_status()
    return resp.json()["access_token"]


def _ebay_listing(item: dict) -> Listing | None:
    try:
        price = float(item["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None
    shipping = None
    opts = item.get("shippingOptions") or []
    if opts and (opts[0].get("shippingCost") or {}).get("value") is not None:
        shipping = float(opts[0]["shippingCost"]["value"])
    return Listing(
        source="eBay",
        listing_id=item.get("itemId", ""),
        title=item.get("title", ""),
        url=item.get("itemWebUrl", ""),
        price=price,
        shipping=shipping,
        condition=item.get("condition", ""),
        seller_feedback=(item.get("seller") or {}).get("feedbackScore"),
        location=(item.get("itemLocation") or {}).get("country", ""),
    )


async def fetch_ebay(
    client: httpx.AsyncClient,
    targets: list[WatchTarget],
    pace: float = 1.0,
    _sleep: Sleep | None = None,
) -> list[Listing]:
    """Fixed-price and auction listings per target, excluding for-parts.

    The price window runs from the too-good-to-be-true floor to 130% of the
    great price, so the history median sees the normal market, not only
    bargains.
    """
    sleep = _sleep or asyncio.sleep
    token = await _ebay_token(client)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    out: list[Listing] = []
    for i, t in enumerate(targets):
        if i and pace:
            await sleep(pace)
        lo = TOO_GOOD_OF_TARGET * t.great_price
        hi = EBAY_PRICE_CEILING * t.great_price
        resp = await client.get(
            EBAY_SEARCH_URL,
            headers=headers,
            params={
                "q": t.query,
                "limit": "50",
                "filter": (
                    f"price:[{lo:.0f}..{hi:.0f}],priceCurrency:USD,"
                    f"conditionIds:{{{EBAY_CONDITIONS}}},deliveryCountry:US"
                ),
            },
        )
        if resp.status_code in (401, 403):
            raise NotConfigured(f"eBay search rejected (HTTP {resp.status_code})")
        if resp.status_code != 200:
            logger.warning("deals: ebay '%s' -> HTTP %d", t.query, resp.status_code)
            continue
        for item in resp.json().get("itemSummaries") or []:
            listing = _ebay_listing(item)
            if listing is not None:
                out.append(listing)
    return out


# ─── Orchestration ───────────────────────────────────────────────────

EBAY_FIX = (
    "Create a free eBay developer keyset (Production) at "
    "https://developer.ebay.com/my/keys, set EBAY_CLIENT_ID and "
    "EBAY_CLIENT_SECRET in .env, then restart workflows."
)

# (config key, display name, fetcher name). Fetchers are looked up at call
# time so tests can patch them.
_SIMPLE_SOURCES = (
    ("ebay", "eBay", "fetch_ebay"),
    ("slickdeals", "Slickdeals", "fetch_slickdeals"),
    ("servethehome", "ServeTheHome", "fetch_servethehome"),
)


async def gather(
    targets: list[WatchTarget], sources: dict, pace: float = 1.0
) -> tuple[list[Listing], list[SourceStatus]]:
    """Poll every configured source, isolating failures per source."""
    listings: list[Listing] = []
    statuses: list[SourceStatus] = []
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for key, name, fn_name in _SIMPLE_SOURCES:
            if key not in sources:
                continue
            fn = globals()[fn_name]
            try:
                found = await fn(client, targets, pace=pace)
                listings.extend(found)
                statuses.append(SourceStatus(name, True, f"{len(found)} listings"))
            except NotConfigured as exc:
                statuses.append(SourceStatus(name, False, str(exc), EBAY_FIX))
            except Exception as exc:
                logger.warning("deals: %s failed: %r", name, exc)
                statuses.append(SourceStatus(name, False, f"failed: {exc}"[:120]))
        if "reddit" in sources:
            subs = list((sources.get("reddit") or {}).get("subreddits") or [])
            try:
                found, status = await fetch_reddit(client, subs, pace=pace * 6)
                listings.extend(found)
                status.detail += f" · {len(found)} posts"
                statuses.append(status)
            except Exception as exc:
                logger.warning("deals: reddit failed: %r", exc)
                statuses.append(SourceStatus("Reddit", False, f"failed: {exc}"[:120]))

    unique: dict[tuple[str, str], Listing] = {}
    for listing in listings:
        unique.setdefault((listing.source, listing.listing_id), listing)
    return list(unique.values()), statuses
