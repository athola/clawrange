"""Reddit search adapter — asyncpraw wrapper for marketing research.

Calls Reddit's official API via asyncpraw (script-app OAuth flow).
Returns structured RedditPost results with real URLs, scores, and snippets.
Gracefully degrades when credentials are missing.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

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

    Returns deduplicated results sorted by score descending.
    On missing credentials or API errors, returns empty list with warning.
    """
    if not await is_configured():
        logger.warning("Reddit credentials not configured — returning empty results")
        return []

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
