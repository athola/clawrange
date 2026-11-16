"""Reddit search adapter — asyncpraw with a public-JSON fallback.

Preferred path: Reddit's official API via asyncpraw (script-app OAuth
flow). When credentials are missing, falls back to Reddit's
unauthenticated read-only JSON endpoint so the morning_digest still
fires before the operator wires script-app credentials. When Reddit
refuses that too (HTTP 403 for keyless JSON from many hosts), each
subreddit's Atom feed is read once per run and topics are matched
locally; feeds carry no score or comment counts.
"""

import asyncio
import html
import logging
import os
import re
import xml.etree.ElementTree as ET  # noqa: S405 - DTDs refused before parsing
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from errors import UpstreamError

logger = logging.getLogger("clawrange.reddit")

# ─── Configuration ──────────────────────────────────────────────────


def _get_credentials() -> dict[str, str]:
    return {
        "client_id": os.getenv("REDDIT_CLIENT_ID", ""),
        "client_secret": os.getenv("REDDIT_CLIENT_SECRET", ""),
        "username": os.getenv("REDDIT_USERNAME", ""),
        "password": os.getenv("REDDIT_PASSWORD", ""),
        "user_agent": os.getenv("REDDIT_USER_AGENT", "clawrange-marketing-bot/0.1"),
    }


async def is_configured() -> bool:
    creds = _get_credentials()
    return bool(
        creds["client_id"]
        and creds["client_secret"]
        and creds["username"]
        and creds["password"]
    )


# ─── Source health ──────────────────────────────────────────────────


@dataclass
class SearchHealth:
    """Per-run tally of Reddit request outcomes.

    Callers pass one instance through a run's searches so the report can
    say *why* it found nothing. After BLOCK_LIMIT consecutive 403/429
    responses the run stops calling Reddit (`tripped`) instead of spending
    minutes on requests that will be refused.
    """

    BLOCK_LIMIT = 3

    mode: str = ""
    # Set once keyless JSON is refused: the rest of the run reads feeds,
    # cached here per subreddit so each feed costs one request per run
    # (None: the feed could not be read this run).
    feed_mode: bool = False
    feeds: dict[str, list[Any] | None] = field(default_factory=dict)
    requests: int = 0
    ok: int = 0
    skipped: int = 0
    failures: Counter[str] = field(default_factory=Counter)
    _block_streak: int = 0

    def record(self, status: int | str) -> None:
        self.requests += 1
        if status == 200:
            self.ok += 1
            self._block_streak = 0
            return
        self.failures[str(status)] += 1
        if status in (403, 429):
            self._block_streak += 1

    @property
    def tripped(self) -> bool:
        return self._block_streak >= self.BLOCK_LIMIT

    def summary(self) -> str:
        parts = [f"{self.ok}/{self.requests} requests ok"]
        if self.failures:
            parts.append(
                ", ".join(f"{n}× HTTP {code}" for code, n in self.failures.items())
            )
        if self.skipped:
            parts.append(f"{self.skipped} skipped after repeated blocks")
        return f"{self.mode or 'reddit'}: " + "; ".join(parts)


async def get_retrying_429(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    health: SearchHealth,
    sleep: Callable[[float], Awaitable[Any]],
    backoff: float,
) -> httpx.Response:
    """GET `url`, retried once after `backoff` seconds when Reddit answers
    429. Every status lands on `health`; transport errors propagate."""
    resp = await client.get(url, headers=headers)
    health.record(resp.status_code)
    if resp.status_code == 429:
        await sleep(backoff)
        resp = await client.get(url, headers=headers)
        health.record(resp.status_code)
    return resp


# ─── Models ──────────────────────────────────────────────────────────


class RedditPost(BaseModel):
    id: str
    url: str
    title: str
    subreddit: str
    score: int
    comments: int
    created_utc: str
    snippet: str | None = None
    # False for feed posts: score/comments are unknown, not zero.
    engagement_known: bool = True


# ─── Atom feed fallback ─────────────────────────────────────────────

FEED_URL = "https://www.reddit.com/r/{sub}/new/.rss?limit=100"
# Reddit serves feeds to browser-like agents it refuses JSON to.
# REDDIT_FEED_USER_AGENT overrides it.
FEED_USER_AGENT = (
    os.getenv("REDDIT_FEED_USER_AGENT", "").strip()
    or "Mozilla/5.0 (compatible; clawrange/0.1)"
)
FEED_PACE_S = 6.0
# Per-request timeout for the JSON search and feed clients.
HTTP_TIMEOUT_S = 15.0
_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_sleep = asyncio.sleep  # patched in tests


def parse_feed_posts(xml_text: str, cutoff: datetime) -> list[RedditPost]:
    """Atom entries newer than `cutoff`, as posts with unknown engagement.

    Feeds are untrusted: any DOCTYPE/ENTITY declaration is refused, which
    rules out entity-expansion attacks without an extra dependency.
    """
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text, re.IGNORECASE):
        logger.warning("Reddit feed declares a DTD; refusing to parse")
        return []
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - DTDs refused above
    except ET.ParseError as exc:
        logger.warning("Reddit feed unparseable: %s", exc)
        return []
    out: list[RedditPost] = []
    for entry in root.iter(f"{{{_ATOM['a']}}}entry"):
        raw_id = (entry.findtext("a:id", namespaces=_ATOM) or "").strip()
        when = entry.findtext("a:published", namespaces=_ATOM) or entry.findtext(
            "a:updated", namespaces=_ATOM
        )
        try:
            created = datetime.fromisoformat((when or "").strip())
        except ValueError:
            continue
        if not raw_id or created < cutoff:
            continue
        link = entry.find("a:link", _ATOM)
        category = entry.find("a:category", _ATOM)
        text = html.unescape(entry.findtext("a:content", namespaces=_ATOM) or "")
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        out.append(
            RedditPost(
                id=raw_id.removeprefix("t3_"),
                url=link.get("href", "") if link is not None else "",
                title=(entry.findtext("a:title", namespaces=_ATOM) or "").strip(),
                subreddit=category.get("term", "") if category is not None else "",
                score=0,
                comments=0,
                created_utc=created.astimezone(UTC).isoformat(),
                snippet=_snippet(body),
                engagement_known=False,
            )
        )
    return out


def _topic_matches(post: RedditPost, topic: str) -> bool:
    words = [w for w in topic.lower().split() if len(w) > 2]
    haystack = f"{post.title} {post.snippet or ''}".lower()
    return bool(words) and all(w in haystack for w in words)


async def _feed_search(
    client: httpx.AsyncClient,
    sub_name: str,
    topic: str,
    cutoff: datetime,
    health: "SearchHealth",
) -> list[RedditPost] | None:
    """Posts in one subreddit's feed that match the topic (cached per run).

    None when the feed could not be read (refused, failed, or skipped
    after repeated blocks), so the caller can tell it from an empty feed.
    """
    key = sub_name.lower()
    if key not in health.feeds:
        if health.tripped:
            health.skipped += 1
            return None
        if health.feeds:
            await _sleep(FEED_PACE_S)
        posts: list[RedditPost] | None = None
        try:
            resp = await get_retrying_429(
                client,
                FEED_URL.format(sub=sub_name),
                {"User-Agent": FEED_USER_AGENT},
                health,
                _sleep,
                FEED_PACE_S * 2,
            )
            if resp.status_code == 200:
                # Cache the whole feed; each call applies its own window.
                posts = parse_feed_posts(resp.text, datetime.min.replace(tzinfo=UTC))
            else:
                logger.warning(
                    "Reddit feed r/%s -> HTTP %d", sub_name, resp.status_code
                )
        except Exception as exc:
            logger.warning("Reddit feed r/%s failed: %s", sub_name, exc)
            health.record("error")
        health.feeds[key] = posts
    cached = health.feeds[key]
    if cached is None:
        return None
    return [
        p
        for p in cached
        if datetime.fromisoformat(p.created_utc) >= cutoff and _topic_matches(p, topic)
    ]


# ─── Time Filter Parsing ────────────────────────────────────────────


# Duration -> (Reddit time_filter bucket, hours for the precise client-side
# cutoff). Reddit has no sub-hour time_filter, so 5m/15m request the 'hour'
# bucket and the caller post-filters by created_utc.
_SINCE: dict[str, tuple[str, float]] = {
    "5m": ("hour", 5 / 60),
    "15m": ("hour", 15 / 60),
    "1h": ("hour", 1),
    "24h": ("day", 24),
    "7d": ("week", 168),
    "30d": ("month", 720),
    "365d": ("year", 8760),
}
_SINCE_DEFAULT = _SINCE["7d"]


def _parse_since(since: str) -> str:
    """Duration string -> asyncpraw/Reddit time_filter value."""
    return _SINCE.get(since, _SINCE_DEFAULT)[0]


def _parse_since_hours(since: str) -> float:
    """Duration string -> hours for client-side filtering."""
    return _SINCE.get(since, _SINCE_DEFAULT)[1]


def _cutoff(since: str) -> datetime:
    return datetime.now(UTC) - timedelta(hours=_parse_since_hours(since))


def _snippet(text: str | None) -> str | None:
    """First 200 chars of a post body, '...' when truncated, None if empty."""
    text = text or ""
    return text[:200] + "..." if len(text) > 200 else (text or None)


def _post_from_json(
    d: dict, cutoff: datetime, seen: set[str], default_sub: str = ""
) -> RedditPost | None:
    """A search.json child as a RedditPost; None if seen, idless or too old."""
    pid = d.get("id")
    if not pid or pid in seen:
        return None
    seen.add(pid)
    created = datetime.fromtimestamp(d.get("created_utc", 0) or 0, tz=UTC)
    if created < cutoff:
        return None
    permalink = d.get("permalink") or ""
    return RedditPost(
        id=pid,
        url=f"https://reddit.com{permalink}" if permalink else d.get("url", ""),
        title=d.get("title", ""),
        subreddit=d.get("subreddit", default_sub),
        score=int(d.get("score", 0) or 0),
        comments=int(d.get("num_comments", 0) or 0),
        created_utc=created.isoformat(),
        snippet=_snippet(d.get("selftext")),
    )


def _add_unique(results: list[RedditPost], seen: set[str], posts) -> None:
    for post in posts:
        if post.id not in seen:
            seen.add(post.id)
            results.append(post)


# ─── Search ──────────────────────────────────────────────────────────


async def search_subreddits(
    topic: str,
    subreddits: list[str],
    since: str = "7d",
    sort: str = "new",
    limit_per_sub: int = 25,
    health: SearchHealth | None = None,
    feed_fallback: bool = False,
) -> list[RedditPost]:
    """Search multiple subreddits for posts matching a topic.

    `feed_fallback` lets a keyless run switch to subreddit Atom feeds when
    search JSON is refused. Opt-in, and only useful with one `health`
    shared across a run's searches: the feed cache and pacing live there,
    so a caller without one would re-probe and re-fetch on every call.

    Returns deduplicated results sorted by score descending. Uses the
    OAuth script-app flow when REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD
    are all set; otherwise falls back to Reddit's public JSON endpoint.

    A subreddit that fails is logged and tallied in `health`; the call
    still returns what the others found. Raises UpstreamError when no
    subreddit could be searched at all, so an outage never reads as a
    quiet day. Zero matches from a successful search is a plain [].
    """
    if not await is_configured():
        logger.info("Reddit OAuth not configured — using public JSON fallback")
        return await _public_search(
            topic, subreddits, since, sort, limit_per_sub, health, feed_fallback
        )

    health = health if health is not None else SearchHealth()
    health.mode = "oauth"
    try:
        import asyncpraw
    except ImportError as exc:
        logger.warning("asyncpraw not installed; Reddit OAuth search unavailable")
        health.record("error")
        msg = "Reddit search unavailable: asyncpraw not installed"
        raise UpstreamError(msg) from exc

    creds = _get_credentials()
    time_filter = _parse_since(since)
    cutoff = _cutoff(since)

    seen_ids: set[str] = set()
    results: list[RedditPost] = []
    looked = failed = 0

    try:
        reddit = asyncpraw.Reddit(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            username=creds["username"],
            password=creds["password"],
            user_agent=creds["user_agent"],
        )

        for sub_name in subreddits:
            try:
                subreddit = await reddit.subreddit(sub_name)
                async for post in subreddit.search(
                    topic, sort=sort, time_filter=time_filter, limit=limit_per_sub
                ):
                    if post.id in seen_ids:
                        continue
                    seen_ids.add(post.id)

                    created = datetime.fromtimestamp(post.created_utc, tz=UTC)
                    if created < cutoff:
                        continue

                    results.append(
                        RedditPost(
                            id=post.id,
                            url=f"https://reddit.com/r/{post.subreddit}/comments/{post.id}",
                            title=post.title,
                            subreddit=str(post.subreddit),
                            score=post.score,
                            comments=post.num_comments,
                            created_utc=created.isoformat(),
                            snippet=_snippet(post.selftext),
                        )
                    )
                health.record(200)
                looked += 1
            except Exception as exc:
                logger.warning("Reddit search failed for r/%s: %s", sub_name, exc)
                health.record(getattr(exc, "status_code", None) or "error")
                failed += 1
                continue
    except Exception as exc:
        logger.warning("Reddit API error: %s", exc)
        health.record("error")
        raise UpstreamError(f"Reddit API error: {exc}") from exc
    try:
        await reddit.close()
    except Exception as exc:  # the searches already ran; keep their results
        logger.warning("Reddit client close failed: %s", exc)

    _raise_if_blind(looked, failed, health)
    results.sort(key=lambda p: p.score, reverse=True)
    return results


def _raise_if_blind(looked: int, failed: int, health: SearchHealth) -> None:
    """Raise when a call tried subreddits and could read none of them."""
    if failed and not looked:
        raise UpstreamError(f"Reddit search failed: {health.summary()}")


async def search_all(
    topic: str,
    since: str = "24h",
    sort: str = "new",
    limit: int = 25,
    health: SearchHealth | None = None,
) -> list[RedditPost]:
    """Search across all of Reddit (no subreddit restriction).

    Used for tangential/emerging-sub discovery: find posts matching a
    topic regardless of which subreddit they appear in. Combined with
    the project's `subreddits` curated list, this surfaces posts in
    non-curated subs that the curated scan would have missed.

    Public-API-only for now (the OAuth path uses asyncpraw's
    Subreddit.search which requires a subreddit; using `all` works
    but adds complexity). Subject to the same ~30 req/min anonymous
    rate limit as `_public_search`.

    Raises UpstreamError when the request fails or the run's `health` has
    tripped. In feed mode it returns [] by design (counted as skipped):
    there is no feed for all of Reddit.
    """
    user_agent = os.getenv("REDDIT_USER_AGENT", "clawrange-marketing-bot/0.1")
    time_filter = _parse_since(since)
    cutoff = _cutoff(since)

    seen_ids: set[str] = set()
    results: list[RedditPost] = []
    if health is not None:
        health.mode = health.mode or "public"
        if health.tripped:
            health.skipped += 1
            raise UpstreamError(f"Reddit all-search skipped: {health.summary()}")
        # Feed mode has no all-of-Reddit equivalent short of one request
        # per topic, which the anonymous rate limit cannot afford.
        if health.feed_mode:
            health.skipped += 1
            return []

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=HTTP_TIMEOUT_S,
    ) as client:
        try:
            resp = await client.get(
                "https://www.reddit.com/search.json",
                params={
                    "q": topic,
                    "sort": sort,
                    "t": time_filter,
                    "limit": str(limit),
                },
            )
            if health is not None:
                health.record(resp.status_code)
        except Exception as exc:
            logger.warning("Reddit all-search '%s' failed: %s", topic, exc)
            if health is not None:
                health.record("error")
            raise UpstreamError(f"Reddit all-search failed: {exc}") from exc
        if resp.status_code != 200:
            logger.warning("Reddit all-search '%s' -> HTTP %d", topic, resp.status_code)
            raise UpstreamError(f"Reddit all-search: HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("Reddit all-search '%s': bad JSON: %s", topic, exc)
            raise UpstreamError(f"Reddit all-search: bad JSON: {exc}") from exc

    for child in payload.get("data", {}).get("children", []):
        post = _post_from_json(child.get("data", {}) or {}, cutoff, seen_ids)
        if post is not None:
            results.append(post)

    results.sort(key=lambda p: p.score, reverse=True)
    return results


async def _public_search(
    topic: str,
    subreddits: list[str],
    since: str,
    sort: str,
    limit_per_sub: int,
    health: SearchHealth | None = None,
    feed_fallback: bool = False,
) -> list[RedditPost]:
    """Unauthenticated read-only fallback via Reddit's public JSON API.

    No OAuth required — only a non-default User-Agent (Reddit blocks
    requests using the httpx default UA). Subject to Reddit's stricter
    anonymous rate limit (~30 req/min), so the OAuth path is preferred
    when the operator wires REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD.
    """
    user_agent = os.getenv("REDDIT_USER_AGENT", "clawrange-marketing-bot/0.1")
    time_filter = _parse_since(since)
    cutoff = _cutoff(since)

    seen_ids: set[str] = set()
    results: list[RedditPost] = []
    health = health if health is not None else SearchHealth()
    if not health.feed_mode:
        health.mode = "public"
    # Per call, not per run: one health spans many calls.
    looked = failed = 0

    async def from_feed(sub_name: str) -> None:
        nonlocal looked, failed
        feed = await _feed_search(client, sub_name, topic, cutoff, health)
        if feed is None:
            failed += 1
        else:
            looked += 1
            _add_unique(results, seen_ids, feed)

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=HTTP_TIMEOUT_S,
    ) as client:
        for sub_name in subreddits:
            if health.feed_mode:
                await from_feed(sub_name)
                continue
            if health.tripped:
                health.skipped += 1
                failed += 1
                continue
            url = f"https://www.reddit.com/r/{sub_name}/search.json"
            params = {
                "q": topic,
                "restrict_sr": "1",
                "sort": sort,
                "t": time_filter,
                "limit": str(limit_per_sub),
            }
            try:
                resp = await client.get(url, params=params)
                health.record(resp.status_code)
                if resp.status_code == 403 and feed_fallback:
                    # Keyless JSON refused: switch the run to feeds and
                    # stop probing JSON, which only feeds the block streak.
                    logger.info("Reddit JSON refused; switching to feeds")
                    health.feed_mode, health.mode = True, "rss"
                    health._block_streak = 0
                    await from_feed(sub_name)
                    continue
                if resp.status_code != 200:
                    logger.warning(
                        "Reddit public search r/%s '%s' -> HTTP %d",
                        sub_name,
                        topic,
                        resp.status_code,
                    )
                    failed += 1
                    continue
                payload = resp.json()
            except Exception as exc:
                logger.warning("Reddit public search r/%s failed: %s", sub_name, exc)
                health.record("error")
                failed += 1
                continue

            looked += 1
            for child in payload.get("data", {}).get("children", []):
                post = _post_from_json(
                    child.get("data", {}) or {}, cutoff, seen_ids, sub_name
                )
                if post is not None:
                    results.append(post)

    _raise_if_blind(looked, failed, health)
    results.sort(key=lambda p: p.score, reverse=True)
    return results
