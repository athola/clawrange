"""Reddit search adapter — asyncpraw with a public-JSON fallback.

Preferred path: Reddit's official API via asyncpraw (script-app OAuth
flow). When credentials are missing, falls back to Reddit's
unauthenticated read-only JSON endpoint so the morning_digest still
fires before the operator wires script-app credentials.
"""

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel

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


# ─── Time Filter Parsing ────────────────────────────────────────────


def _parse_since(since: str) -> str:
    """Convert duration string to asyncpraw time_filter value.

    Reddit's API has no sub-hour time_filter, so '5m' and '15m'
    request the 'hour' bucket and the caller post-filters by
    created_utc against the precise cutoff.
    """
    mapping = {
        "5m": "hour",
        "15m": "hour",
        "1h": "hour",
        "24h": "day",
        "7d": "week",
        "30d": "month",
        "365d": "year",
    }
    return mapping.get(since, "week")


def _parse_since_hours(since: str) -> float:
    """Convert duration string to hours for client-side filtering."""
    mapping = {
        "5m": 5 / 60,
        "15m": 15 / 60,
        "1h": 1,
        "24h": 24,
        "7d": 168,
        "30d": 720,
        "365d": 8760,
    }
    return mapping.get(since, 168)


# ─── Search ──────────────────────────────────────────────────────────


async def search_subreddits(
    topic: str,
    subreddits: list[str],
    since: str = "7d",
    sort: str = "new",
    limit_per_sub: int = 25,
    health: SearchHealth | None = None,
) -> list[RedditPost]:
    """Search multiple subreddits for posts matching a topic.

    Returns deduplicated results sorted by score descending. Uses the
    OAuth script-app flow when REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD
    are all set; otherwise falls back to Reddit's public JSON endpoint.
    Network/API errors degrade to an empty list with a warning.
    """
    if not await is_configured():
        logger.info("Reddit OAuth not configured — using public JSON fallback")
        return await _public_search(
            topic, subreddits, since, sort, limit_per_sub, health
        )

    try:
        import asyncpraw
    except ImportError:
        logger.warning("asyncpraw not installed — returning empty results")
        return []

    creds = _get_credentials()
    time_filter = _parse_since(since)
    since_hours = _parse_since_hours(since)
    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)

    seen_ids: set[str] = set()
    results: list[RedditPost] = []
    if health is not None:
        health.mode = "oauth"

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

                    snippet = None
                    if post.selftext:
                        snippet = post.selftext[:200] + (
                            "..." if len(post.selftext) > 200 else ""
                        )

                    results.append(
                        RedditPost(
                            id=post.id,
                            url=f"https://reddit.com/r/{post.subreddit}/comments/{post.id}",
                            title=post.title,
                            subreddit=str(post.subreddit),
                            score=post.score,
                            comments=post.num_comments,
                            created_utc=created.isoformat(),
                            snippet=snippet,
                        )
                    )
                if health is not None:
                    health.record(200)
            except Exception as exc:
                logger.warning("Reddit search failed for r/%s: %s", sub_name, exc)
                if health is not None:
                    health.record(getattr(exc, "status_code", None) or "error")
                continue

        await reddit.close()
    except Exception as exc:
        logger.warning("Reddit API error: %s", exc)

    results.sort(key=lambda p: p.score, reverse=True)
    return results


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
    """
    user_agent = os.getenv("REDDIT_USER_AGENT", "clawrange-marketing-bot/0.1")
    time_filter = _parse_since(since)
    since_hours = _parse_since_hours(since)
    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)

    seen_ids: set[str] = set()
    results: list[RedditPost] = []
    if health is not None:
        health.mode = health.mode or "public"
        if health.tripped:
            health.skipped += 1
            return []

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=15.0,
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
            if resp.status_code != 200:
                logger.warning(
                    "Reddit all-search '%s' -> HTTP %d", topic, resp.status_code
                )
                return []
            payload = resp.json()
        except Exception as exc:
            logger.warning("Reddit all-search '%s' failed: %s", topic, exc)
            if health is not None:
                health.record("error")
            return []

    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {}) or {}
        pid = d.get("id")
        if not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)

        created = datetime.fromtimestamp(d.get("created_utc", 0) or 0, tz=UTC)
        if created < cutoff:
            continue

        selftext = d.get("selftext") or ""
        snippet = selftext[:200] + "..." if len(selftext) > 200 else (selftext or None)
        permalink = d.get("permalink") or ""
        post_url = f"https://reddit.com{permalink}" if permalink else d.get("url", "")

        results.append(
            RedditPost(
                id=pid,
                url=post_url,
                title=d.get("title", ""),
                subreddit=d.get("subreddit", ""),
                score=int(d.get("score", 0) or 0),
                comments=int(d.get("num_comments", 0) or 0),
                created_utc=created.isoformat(),
                snippet=snippet,
            )
        )

    results.sort(key=lambda p: p.score, reverse=True)
    return results


async def _public_search(
    topic: str,
    subreddits: list[str],
    since: str,
    sort: str,
    limit_per_sub: int,
    health: SearchHealth | None = None,
) -> list[RedditPost]:
    """Unauthenticated read-only fallback via Reddit's public JSON API.

    No OAuth required — only a non-default User-Agent (Reddit blocks
    requests using the httpx default UA). Subject to Reddit's stricter
    anonymous rate limit (~30 req/min), so the OAuth path is preferred
    when the operator wires REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD.
    """
    user_agent = os.getenv("REDDIT_USER_AGENT", "clawrange-marketing-bot/0.1")
    time_filter = _parse_since(since)
    since_hours = _parse_since_hours(since)
    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)

    seen_ids: set[str] = set()
    results: list[RedditPost] = []
    if health is not None:
        health.mode = "public"

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=15.0,
    ) as client:
        for sub_name in subreddits:
            if health is not None and health.tripped:
                health.skipped += 1
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
                if health is not None:
                    health.record(resp.status_code)
                if resp.status_code != 200:
                    logger.warning(
                        "Reddit public search r/%s '%s' -> HTTP %d",
                        sub_name,
                        topic,
                        resp.status_code,
                    )
                    continue
                payload = resp.json()
            except Exception as exc:
                logger.warning("Reddit public search r/%s failed: %s", sub_name, exc)
                if health is not None:
                    health.record("error")
                continue

            for child in payload.get("data", {}).get("children", []):
                d = child.get("data", {}) or {}
                pid = d.get("id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)

                created = datetime.fromtimestamp(d.get("created_utc", 0) or 0, tz=UTC)
                if created < cutoff:
                    continue

                selftext = d.get("selftext") or ""
                snippet = (
                    selftext[:200] + "..."
                    if len(selftext) > 200
                    else (selftext or None)
                )
                permalink = d.get("permalink") or ""
                post_url = (
                    f"https://reddit.com{permalink}" if permalink else d.get("url", "")
                )

                results.append(
                    RedditPost(
                        id=pid,
                        url=post_url,
                        title=d.get("title", ""),
                        subreddit=d.get("subreddit", sub_name),
                        score=int(d.get("score", 0) or 0),
                        comments=int(d.get("num_comments", 0) or 0),
                        created_utc=created.isoformat(),
                        snippet=snippet,
                    )
                )

    results.sort(key=lambda p: p.score, reverse=True)
    return results
