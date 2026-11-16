"""Heartbeat task routing to the marketing scanners and research orchestrator.

Research- and scan-shaped heartbeat tasks are answered here with real,
citable findings instead of going to the chat LLM.
"""

import logging
import re

import github_search
import reddit_search
import tenant_profile
from brain_db import project_list
from errors import UpstreamError

logger = logging.getLogger("clawrange.research_router")

# ─── Marketing Scan Interceptor ──────────────────────────────────

# The generators write "Scan reddit for <slug>: ..." and "traffic snapshot
# for <owner>/<repo>: ...", so a capture must stop at the colon.
_MARKETING_SCAN_PATTERNS = {
    "reddit": re.compile(r"Scan reddit for ([^\s:]+)", re.IGNORECASE),
    "github": re.compile(r"Scan github for ([^\s:]+)", re.IGNORECASE),
    "traffic": re.compile(
        r"(?:Weekly )?traffic snapshot for ([^\s:/]+)/([^\s:]+)", re.IGNORECASE
    ),
    "awesome": re.compile(
        r"Awesome-list watch: ([^\s:]+) not yet listed", re.IGNORECASE
    ),
}

# Results shown per scan; only these are marked seen, so the rest can
# surface on the next run.
_REDDIT_SHOWN = 8
_GITHUB_SHOWN = 5


async def traffic_unavailable(owner: str, repo: str) -> str:
    """Why get_self_traffic returned None: no PAT, or the API call failed."""
    if not await github_search.is_configured():
        return f"{owner}/{repo}: traffic unavailable (needs GITHUB_PAT with repo scope)"
    return f"{owner}/{repo}: traffic unavailable (GitHub API call failed, see logs)"


def _channel_errors(result: dict) -> str:
    errors = result.get("errors") or {}
    return "; ".join(f"{ch}: {msg}" for ch, msg in errors.items())


ANTI_PATTERN_RULES = """
ANTI-PATTERNS (do not produce text that violates these):
- Do NOT lead with "I built this with Claude" or similar AI-builder framing
- Do NOT use superlatives like "fastest", "best", "revolutionary"
- Do NOT fabricate post titles, subreddit names, usernames, or URLs
- Do NOT recommend posting in subreddits where self-promotion is banned

POSITIVE RULES:
- Lead with the user problem solved
- Include real upvote counts, comment counts, and timestamps when available
- Cite source URLs for every claim
- For comment suggestions, anchor in the user's question first; mention the "
project as a relevant tool second
"""


# ─── Research Router ────────────────────────────────────────────────
# The chat personas have no web tooling (the LLM can only stall or ask
# the operator to authorize tools it will never have), but the workflow service
# is web-capable: the research orchestrator fans out to Reddit, GitHub,
# and web search server-side. Research-shaped heartbeat tasks route
# there and come back with real, citable URLs.

_RESEARCH_INTENT = re.compile(
    r"\b(search|research|find|look\s*up|surface|scan|identify)\b", re.IGNORECASE
)
_RESEARCH_TARGET = re.compile(
    r"\b(reddit|subreddit|hacker\s*news|\bhn\b|threads?|posts?|articles?|"
    r"forums?|discussions?|conversations?|papers?|arxiv|github|repos?|"
    r"web|online|internet)\w*\b",
    re.IGNORECASE,
)


async def _try_research_task(description: str) -> str | None:
    """Run a research-shaped task through the research orchestrator.

    Returns condensed findings text when the task matches (intent verb +
    external source target), None when it doesn't so the LLM path handles
    it. When every channel failed the orchestrator raises UpstreamError,
    which propagates so the heartbeat marks the task failed. Any other
    orchestrator error returns None: the LLM attempt is the fallback.
    """
    if not (
        _RESEARCH_INTENT.search(description) and _RESEARCH_TARGET.search(description)
    ):
        return None

    # Content-idea tasks quote research vocabulary in their template
    # ("research on ... surfaced ... Reddit/HN comment") but are drafting
    # jobs for the LLM. Routing them here re-runs research on the whole
    # description instead of drafting the angles, so hand them back.
    if description.strip().lower().startswith("content idea for"):
        return None

    # Function-local on purpose: research imports llm_proxy, which imports
    # heartbeat and this module, so importing research at module scope
    # here would be circular.
    import research

    try:
        result = await research.orchestrate_research(
            description,
            channels=["discourse", "code", "discourse_web"],
        )
    except UpstreamError:
        raise
    except Exception:
        logger.exception("research router: orchestrator failed")
        return None

    findings = result.get("findings", [])
    failed = _channel_errors(result)
    if not findings:
        if failed:
            return f"Research: no findings (failed channels: {failed})."
        return "Research: no findings."

    # No description echo: the digest line already prints it above the
    # result, and re-embedding it here crowds _needs_input's head window.
    lines = [f"Research findings ({len(findings)}):"]
    for i, f in enumerate(findings[:8], 1):
        lines.append(f"{i}. [{f['channel']}] {f['title'][:90]}\n   {f['url']}")
    if failed:
        lines.append(f"Failed channels: {failed}")
    return "\n".join(lines)


async def _try_marketing_scan(description: str, brain_db) -> str | None:
    """Detect and execute structured marketing scan tasks.

    Returns result text if the task matches a scan pattern, None otherwise.
    """
    for kind, pattern in _MARKETING_SCAN_PATTERNS.items():
        match = pattern.search(description)
        if not match:
            continue

        if kind == "reddit":
            slug = match.group(1)
            project = brain_db.get_project(slug)
            if not project:
                return None

            topics = project_list(project, "topics")
            subreddits = project_list(project, "subreddits")
            search_terms = project_list(project, "search_terms")

            query = (
                " OR ".join(search_terms[:3]) if search_terms else " ".join(topics[:3])
            )
            results = await reddit_search.search_subreddits(
                query, subreddits or tenant_profile.default_subreddits()
            )

            if not results:
                return f"Reddit scan for {slug}: no new relevant posts found."

            shown = results[:_REDDIT_SHOWN]
            lines = [f"Reddit scan for {slug} ({len(results)} results):\n"]
            for i, r in enumerate(shown, 1):
                lines.append(
                    f"{i}. {r.title}\n"
                    f"   r/{r.subreddit} | {r.score}pts | {r.comments} comments\n"
                    f"   {r.url}"
                )

            for r in shown:
                brain_db.mark_seen("reddit_post", r.id, slug)

            return "\n".join(lines)

        if kind == "github":
            slug = match.group(1)
            project = brain_db.get_project(slug)
            if not project:
                return None

            search_terms = project_list(project, "search_terms")
            topics = project_list(project, "topics")
            query = (
                " OR ".join(search_terms[:3]) if search_terms else " ".join(topics[:3])
            )

            repos = await github_search.search_repos(query, min_stars=5, limit=10)
            issues = await github_search.search_issues(query, limit=10)

            lines = [
                f"GitHub scan for {slug} ({len(repos)} repos, {len(issues)} issues):\n"
            ]

            shown_repos = repos[:_GITHUB_SHOWN]
            if shown_repos:
                lines.append("Adjacent repos:")
                for i, repo in enumerate(shown_repos, 1):
                    lines.append(
                        f"  {i}. {repo.full_name} ({repo.stars}*) — "
                        f"{repo.description or ''}"
                    )

            if issues:
                lines.append("\nRecent issues:")
                for i, iss in enumerate(issues[:_GITHUB_SHOWN], 1):
                    lines.append(f"  {i}. {iss.title} — {iss.repository}#{iss.number}")

            for seen_repo in shown_repos:
                brain_db.mark_seen("github_repo", str(seen_repo.id), slug)

            return "\n".join(lines)

        if kind == "traffic":
            owner, repo_name = match.group(1), match.group(2)

            traffic = await github_search.get_self_traffic(owner, repo_name)
            if not traffic:
                reason = await traffic_unavailable(owner, repo_name)
                if not await github_search.is_configured():
                    return reason
                # The PAT is set, so the API call itself failed: that is a
                # failed task, not an answer.
                raise UpstreamError(reason)

            return (
                f"Traffic for {owner}/{repo_name} (14 days):\n"
                f"  Views: {traffic.views_count} ({traffic.views_uniques} unique)\n"
                f"  Clones: {traffic.clones_count} ({traffic.clones_uniques} unique)"
            )

        if kind == "awesome":
            # These tasks are typically LLM-generated drafts, let them fall through
            return None

    return None
