"""GitHub search adapter — githubkit async wrapper for marketing research.

Calls GitHub's Search API for repos and issues, and the Traffic API
for self-repo analytics. Gracefully degrades when credentials are missing.
"""

import logging
import os
from datetime import datetime, timezone

from pydantic import BaseModel

logger = logging.getLogger("clawrange.github")


# ─── Configuration ──────────────────────────────────────────────────

GITHUB_PAT = os.getenv("GITHUB_PAT", "")


async def is_configured() -> bool:
    return bool(GITHUB_PAT)


# ─── Models ──────────────────────────────────────────────────────────


class GitHubRepo(BaseModel):
    id: int
    full_name: str
    url: str
    description: str | None = None
    stars: int
    language: str | None = None
    topics: list[str] = []


class GitHubIssue(BaseModel):
    id: int
    number: int
    title: str
    url: str
    state: str
    repository: str
    labels: list[str] = []


class GitHubTrafficSnapshot(BaseModel):
    owner: str
    repo: str
    views_count: int
    views_uniques: int
    clones_count: int
    clones_uniques: int
    fetched_at: str


# ─── Client Factory ──────────────────────────────────────────────────


def _get_client():
    """Get a githubkit GitHub client (sync, uses httpx internally)."""
    try:
        from githubkit import GitHub
    except ImportError:
        return None
    if GITHUB_PAT:
        return GitHub(GITHUB_PAT)
    return GitHub()


# ─── Search Functions ────────────────────────────────────────────────


async def search_repos(
    query: str,
    min_stars: int = 0,
    language: str | None = None,
    sort: str = "stars",
    limit: int = 25,
) -> list[GitHubRepo]:
    """Search GitHub repositories."""
    client = _get_client()
    if not client:
        logger.warning("githubkit not installed — returning empty results")
        return []

    parts = [query]
    if min_stars > 0:
        parts.append(f"stars:>={min_stars}")
    if language:
        parts.append(f"language:{language}")
    full_query = " ".join(parts)

    try:
        resp = client.rest.search.search_repos(
            q=full_query, sort=sort, order="desc", per_page=limit
        )
        return [
            GitHubRepo(
                id=item.id,
                full_name=item.full_name,
                url=item.html_url,
                description=item.description,
                stars=item.stargazers_count,
                language=item.language,
                topics=item.topics or [],
            )
            for item in resp.parsed_data.items
        ]
    except Exception as exc:
        logger.warning("GitHub repo search failed: %s", exc)
        return []


async def search_issues(
    query: str,
    limit: int = 25,
) -> list[GitHubIssue]:
    """Search GitHub issues and PRs."""
    client = _get_client()
    if not client:
        return []

    try:
        resp = client.rest.search.search_issues_and_pull_requests(
            q=query, sort="updated", order="desc", per_page=limit
        )
        return [
            GitHubIssue(
                id=item.id,
                number=item.number,
                title=item.title,
                url=item.html_url,
                state=item.state,
                repository=item.repository_url.split("/")[-1],
                labels=[lb.name for lb in (item.labels or [])],
            )
            for item in resp.parsed_data.items
        ]
    except Exception as exc:
        logger.warning("GitHub issue search failed: %s", exc)
        return []


async def get_self_traffic(owner: str, repo: str) -> GitHubTrafficSnapshot | None:
    """Get 14-day traffic stats for a repo. Requires PAT with repo scope."""
    client = _get_client()
    if not client or not GITHUB_PAT:
        logger.warning("GitHub PAT not configured — traffic unavailable")
        return None

    try:
        views_resp = client.rest.repos.get_views(owner=owner, repo=repo)
        clones_resp = client.rest.repos.get_clones(owner=owner, repo=repo)

        views_data = views_resp.parsed_data
        clones_data = clones_resp.parsed_data

        return GitHubTrafficSnapshot(
            owner=owner,
            repo=repo,
            views_count=views_data.count or 0,
            views_uniques=views_data.uniques or 0,
            clones_count=clones_data.count or 0,
            clones_uniques=clones_data.uniques or 0,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        logger.warning("GitHub traffic fetch failed for %s/%s: %s", owner, repo, exc)
        return None


async def check_awesome_list(
    list_owner: str, list_repo: str, target_urls: list[str]
) -> dict[str, bool]:
    """Check whether target URLs appear in an awesome-list README.

    Returns dict mapping each target URL to whether it was found.
    """
    client = _get_client()
    if not client:
        return {url: False for url in target_urls}

    try:
        resp = client.rest.repos.get_readme(owner=list_owner, repo=list_repo)
        import base64

        content = base64.b64decode(resp.parsed_data.content).decode()
        return {url: url in content for url in target_urls}
    except Exception as exc:
        logger.warning("Failed to fetch %s/%s README: %s", list_owner, list_repo, exc)
        return {url: False for url in target_urls}
