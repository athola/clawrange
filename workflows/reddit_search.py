"""Reddit search adapter — asyncpraw with a public-JSON fallback.

Preferred path: Reddit's official API via asyncpraw (script-app OAuth
flow). When credentials are missing, falls back to Reddit's
unauthenticated read-only JSON endpoint so the morning_digest still
fires before the operator wires script-app credentials.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

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
    """Convert duration string to asyncpraw time_filter value."""
    mapping = {
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
) -> list[RedditPost]:
    """Search multiple subreddits for posts matching a topic.

    Returns deduplicated results sorted by score descending. Uses the
    OAuth script-app flow when REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD
    are all set; otherwise falls back to Reddit's public JSON endpoint.
    Network/API errors degrade to an empty list with a warning.
    """
    if not await is_configured():
        logger.info("Reddit OAuth not configured — using public JSON fallback")
        return await _public_search(topic, subreddits, since, sort, limit_per_sub)

    try:
        import asyncpraw
    except ImportError:
        logger.warning("asyncpraw not installed — returning empty results")
        return []

    creds = _get_credentials()
    time_filter = _parse_since(since)
    since_hours = _parse_since_hours(since)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    seen_ids: set[str] = set()
    results: list[RedditPost] = []

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

                    created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
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
            except Exception as exc:
                logger.warning("Reddit search failed for r/%s: %s", sub_name, exc)
                continue

        await reddit.close()
    except Exception as exc:
        logger.warning("Reddit API error: %s", exc)

    results.sort(key=lambda p: p.score, reverse=True)
    return results


async def _public_search(
    topic: str,
    subreddits: list[str],
    since: str,
    sort: str,
    limit_per_sub: int,
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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    seen_ids: set[str] = set()
    results: list[RedditPost] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=15.0,
    ) as client:
        for sub_name in subreddits:
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
                continue

            for child in payload.get("data", {}).get("children", []):
                d = child.get("data", {}) or {}
                pid = d.get("id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)

                created = datetime.fromtimestamp(
                    d.get("created_utc", 0) or 0, tz=timezone.utc
                )
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
