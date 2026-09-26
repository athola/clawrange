"""Daily outreach rundown: evidence gathering, synthesis, and rendering.

The 8am `morning_digest` generator owns the Reddit scan (picks, emerging
subs, auto-promotion). This module adds the other channels (Hacker News,
GitHub issues, web search), the LLM "today's read + action items" layer,
and the report layout. The report always renders, even with no evidence,
so a silent morning means Telegram failed, not that the scan found nothing.

Citation discipline: the synthesis may cite evidence only as [E#] refs.
`link_citations` turns known refs into links and strips unknown refs and
every raw URL, so an invented source cannot reach the operator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

import github_search
import llm_proxy
from reddit_search import SearchHealth

logger = logging.getLogger("clawrange.rundown")

REPORT_TZ = "America/Chicago"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
PER_CHANNEL_CAP = 3
WEB_TIMEOUT_S = 90.0

_URL_RE = re.compile(r"https?://[^\s)\]>]+")
_REF_RE = re.compile(r"\\\[([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\]")


@dataclass
class Evidence:
    """One linkable item the operator could act on."""

    source: str  # reddit | hn | github | web
    group: str  # section label within the project block
    project: str
    title: str
    url: str
    facts: str  # one line: engagement and context
    why: str = ""
    score: int = 0  # engagement, used to rank fallback action items
    ref: str = ""  # "E#", assigned by build_report


@dataclass
class SourceStatus:
    name: str
    ok: bool
    detail: str
    fix: str = ""


# ─── Shared scoring ──────────────────────────────────────────────────


def relevance(text: str, topics: list[str], terms: list[str]) -> float:
    """Keyword overlap: topics weigh 1.0, curated search terms 1.5."""
    haystack = text.lower()
    score = 0.0
    for t in topics:
        if t.lower().strip() and t.lower().strip() in haystack:
            score += 1.0
    for t in terms:
        if t.lower().strip() and t.lower().strip() in haystack:
            score += 1.5
    return score


def engagement_angle(comments: int) -> str:
    """One-line framing of why a thread is worth a reply today."""
    if comments == 0:
        return "fresh thread, no replies yet — first useful answer wins visibility"
    if comments < 5:
        return f"{comments} replies — early, your comment lands near the top"
    if comments < 20:
        return f"{comments} replies — active discussion, still on-topic"
    return f"{comments} replies — late-stage but high reach"


def _keywords(project: dict) -> tuple[list[str], list[str]]:
    return (
        json.loads(project.get("topics", "[]")),
        json.loads(project.get("search_terms", "[]")),
    )


def _queries(project: dict, n: int) -> list[str]:
    topics, terms = _keywords(project)
    return (terms or topics or [project["slug"]])[:n]


# ─── Channels ────────────────────────────────────────────────────────


def parse_hn_hits(payload: dict, project: dict, query: str = "") -> list[Evidence]:
    """Relevant HN stories, linked to the discussion (where you reply).

    A story counts when it contains a topic/term phrase, or every word of
    the query that found it (so "Claude Code ... plugin" matches the term
    "claude code plugin" without the exact phrase).
    """
    topics, terms = _keywords(project)
    words = [w for w in query.lower().split() if len(w) > 2]
    out: list[Evidence] = []
    for hit in payload.get("hits", []):
        title = hit.get("title") or ""
        haystack = f"{title} {hit.get('url') or ''} {hit.get('story_text') or ''}"
        all_words = bool(words) and all(w in haystack.lower() for w in words)
        if relevance(haystack, topics, terms) <= 0 and not all_words:
            continue
        points = int(hit.get("points") or 0)
        comments = int(hit.get("num_comments") or 0)
        facts = f"HN · {points} pts · {comments} comments"
        if hit.get("url"):
            facts += f" · links to {urlparse(hit['url']).netloc}"
        out.append(
            Evidence(
                source="hn",
                group="Hacker News",
                project=project["slug"],
                title=title,
                url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
                facts=facts,
                why=engagement_angle(comments),
                score=points + comments,
            )
        )
    return out


async def fetch_hn(project: dict, since_hours: int = 24) -> list[Evidence]:
    """Keyless Algolia search over the last day of HN stories."""
    cutoff = int((datetime.now(UTC) - timedelta(hours=since_hours)).timestamp())
    found: dict[str, Evidence] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for query in _queries(project, 2):
            resp = await client.get(
                HN_SEARCH_URL,
                params={
                    "query": query,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{cutoff}",
                    "hitsPerPage": "20",
                },
            )
            resp.raise_for_status()
            for ev in parse_hn_hits(resp.json(), project, query):
                found.setdefault(ev.url, ev)
    return sorted(found.values(), key=lambda e: e.score, reverse=True)[:PER_CHANNEL_CAP]


async def fetch_github(project: dict, since_hours: int = 72) -> list[Evidence]:
    """Open issues elsewhere on GitHub that ask about the project's area.

    One query per project keeps the keyless 10 req/min search budget
    intact. Issues on the project's own repo are support, not outreach.
    """
    since = (datetime.now(UTC) - timedelta(hours=since_hours)).date().isoformat()
    own = f"github.com/{project['owner']}/{project['repo']}/".lower()
    out: list[Evidence] = []
    for query in _queries(project, 1):
        issues = await github_search.search_issues(
            f'"{query}" in:title is:issue is:open created:>={since}', limit=10
        )
        for issue in issues:
            if own in issue.url.lower():
                continue
            repo = "/".join(urlparse(issue.url).path.strip("/").split("/")[:2])
            out.append(
                Evidence(
                    source="github",
                    group="GitHub issues",
                    project=project["slug"],
                    title=issue.title,
                    url=issue.url,
                    facts=f"{repo} · open issue #{issue.number}",
                    why=f'asks about "{query}"',
                )
            )
    return out[:PER_CHANNEL_CAP]


def parse_web_lines(text: str, project: dict) -> list[Evidence]:
    """Parse `- title | url | why` lines; anything else is ignored."""
    out: list[Evidence] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.strip().lstrip("-•* ").split("|")]
        if len(parts) < 2 or not _URL_RE.fullmatch(parts[1]):
            continue
        title = re.sub(r"^\[(.+?)\]\(.*?\)$", r"\1", parts[0])
        out.append(
            Evidence(
                source="web",
                group="Web (single source — verify before acting)",
                project=project["slug"],
                title=title,
                url=parts[1],
                facts=urlparse(parts[1]).netloc,
                why=parts[2] if len(parts) > 2 else "",
            )
        )
    return out[:PER_CHANNEL_CAP]


async def fetch_web(project: dict) -> list[Evidence]:
    """Web search for venues beyond Reddit/HN where the project fits."""
    topics, terms = _keywords(project)
    prompt = (
        f"Find places from the last 7 days where the open-source project "
        f"{project['owner']}/{project['repo']} (topics: {', '.join(topics)}; "
        f"keywords: {', '.join(terms)}) could be usefully mentioned: forum "
        f"threads, Lobsters, dev.to, GitHub Discussions, newsletters "
        f"accepting submissions, awesome-lists, Discord/Discourse "
        f"communities. Skip Reddit and Hacker News. Return at most 4 lines, "
        f"each exactly: - <title> | <url> | <why it fits, under 15 words>. "
        f"Only include URLs you found in search results."
    )
    text = await asyncio.wait_for(
        llm_proxy._llm_call(prompt, max_tokens=700, web_search=True),
        timeout=WEB_TIMEOUT_S,
    )
    if not text:
        raise RuntimeError("web search tier returned nothing")
    return parse_web_lines(text, project)


_CHANNELS = (
    ("Hacker News", "fetch_hn"),
    ("GitHub issues", "fetch_github"),
    ("Web search", "fetch_web"),
)


async def _channel_fix(name: str, ok: bool) -> str:
    if name == "GitHub issues" and not await github_search.is_configured():
        return "Set GITHUB_PAT in .env to lift keyless GitHub search off 10 req/min."
    if name == "Web search" and not ok:
        return "Web search tier failed — check GET /tier and the OpenRouter balance."
    return ""


async def gather_other_channels(
    projects: list[dict],
) -> tuple[list[Evidence], list[SourceStatus]]:
    """Run every non-Reddit channel for every project, isolating failures.

    A channel is healthy when at least one project's call succeeded.
    """
    evidence: list[Evidence] = []
    statuses: list[SourceStatus] = []
    seen: set[str] = set()
    for name, fn_name in _CHANNELS:
        fn = globals()[fn_name]  # looked up at call time so tests can patch
        results = await asyncio.gather(
            *(fn(p) for p in projects), return_exceptions=True
        )
        failed = 0
        count = 0
        for r in results:
            if isinstance(r, BaseException):
                failed += 1
                logger.warning("rundown: %s channel failed: %r", name, r)
                continue
            for ev in r:
                if ev.url not in seen:
                    seen.add(ev.url)
                    evidence.append(ev)
                    count += 1
        ok = failed < len(results) or not results
        detail = f"{count} items"
        if failed:
            detail += f" ({failed}/{len(results)} projects failed)"
        statuses.append(SourceStatus(name, ok, detail, await _channel_fix(name, ok)))
    return evidence, statuses


def reddit_status(health: SearchHealth) -> SourceStatus:
    ok = health.ok > 0 or health.requests == 0
    fix = ""
    if health.mode != "oauth":
        fix = (
            "Reddit blocks keyless requests. Create a 'script' app at "
            "https://www.reddit.com/prefs/apps, set REDDIT_CLIENT_ID, "
            "REDDIT_CLIENT_SECRET, REDDIT_USERNAME and REDDIT_PASSWORD in "
            ".env, then restart workflows."
        )
    return SourceStatus("Reddit", ok, health.summary(), fix)


# ─── Synthesis ───────────────────────────────────────────────────────


def _escape_md(text: str) -> str:
    """Escape Telegram legacy-Markdown control characters."""
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def _clean_title(title: str) -> str:
    title = re.sub(r"[\[\]*_`]", "", title).strip()
    return title if len(title) <= 90 else title[:87] + "..."


def link_citations(text: str, evidence: list[Evidence]) -> str:
    """Link known [E#] refs; drop unknown refs and every raw URL."""
    return link_refs(text, {e.ref: e.url for e in evidence if e.ref})


def link_refs(text: str, by_ref: dict[str, str]) -> str:
    """Link known [X#] refs (any capital-letter prefix); drop unknown refs
    and every raw URL, so model output can only point at gathered items."""
    text = _URL_RE.sub("", text.replace("**", ""))
    text = _escape_md(text)

    def _link(m: re.Match[str]) -> str:
        refs = [r.strip() for r in m.group(1).split(",")]
        return ", ".join(f"[{r}]({by_ref[r]})" for r in refs if r in by_ref)

    text = _REF_RE.sub(_link, text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


async def synthesize(projects: list[dict], evidence: list[Evidence]) -> str | None:
    """LLM 'today's read + action items' over numbered evidence.

    Evidence must already carry refs. Returns raw model text (pass it to
    build_report, which links and sanitizes it) or None on failure.
    """
    if not evidence:
        return None
    lines = [
        f"[{e.ref}] ({e.project}, {e.source}) {e.title} — {e.facts}" for e in evidence
    ]
    project_lines = [
        f"- {p['slug']} ({p['owner']}/{p['repo']}): {', '.join(_keywords(p)[0])}"
        for p in projects
    ]
    prompt = (
        "You write a daily outreach brief for a solo maintainer promoting "
        "these open-source projects:\n"
        + "\n".join(project_lines)
        + "\n\nToday's evidence (only these exist):\n"
        + "\n".join(lines)
        + "\n\nWrite two plain-text sections, no Markdown, no URLs:\n"
        "TODAY'S READ: 3-5 bullets on demand signals, recurring pain points, "
        "and which project each maps to. Cite evidence as [E#].\n"
        "ACTION ITEMS: up to 6 numbered items, highest leverage first. Each "
        "says what to do, where ([E#]), and why today. Useful help first; "
        "mention a project only where it directly answers the question. "
        "Do not write reply text for the operator. Ignore evidence that does "
        "not genuinely fit a project; never stretch an unrelated item into "
        "a signal. If little fits, say so in one line rather than padding."
    )
    try:
        return await llm_proxy._llm_call(prompt, max_tokens=1500)
    except Exception as exc:
        logger.warning("rundown: synthesis failed: %s", exc)
        return None


# ─── Rendering ───────────────────────────────────────────────────────


def assign_refs(projects: list[dict], evidence: list[Evidence]) -> list[Evidence]:
    """Number evidence E1..En in the order the report will list it."""
    ordered: list[Evidence] = []
    for p in projects:
        ordered.extend(e for e in evidence if e.project == p["slug"])
    for i, e in enumerate(ordered, 1):
        e.ref = f"E{i}"
    return ordered


def _fallback_actions(evidence: list[Evidence]) -> list[str]:
    ranked = sorted(
        (e for e in evidence if e.source != "web"),
        key=lambda e: e.score,
        reverse=True,
    )[:5]
    return [
        f"{i}. {e.ref}: open [{_clean_title(e.title)}]({e.url}) and reply if you "
        f"can add something useful ({e.project}) — {_escape_md(e.why or e.facts)}"
        for i, e in enumerate(ranked, 1)
    ]


def build_report(
    projects: list[dict],
    evidence: list[Evidence],
    statuses: list[SourceStatus],
    synthesis: str | None,
    coverage: str,
    now: datetime,
) -> str:
    """Render the full rundown. Never returns an empty report."""
    if not all(e.ref for e in evidence):
        evidence = assign_refs(projects, evidence)
    local = now.astimezone(ZoneInfo(REPORT_TZ))
    lines = [
        f"*Daily outreach rundown — {local:%a %b %d}* ({local:%H:%M %Z}, last 24h)",
        "Sources: "
        + " · ".join(
            f"{s.name} {'✓' if s.ok else '✗'} {_escape_md(s.detail)}" for s in statuses
        ),
        "",
    ]

    if not evidence:
        lines += [
            "No fresh evidence today. Nothing new matched the tracked "
            "projects across the working sources.",
            "",
        ]
    elif synthesis:
        lines += [
            "*Today's read and action items*",
            link_citations(synthesis, evidence),
            "",
        ]
    else:
        lines += ["*Action items* (synthesis unavailable, ranked by engagement)"]
        lines += _fallback_actions(evidence)
        lines.append("")

    for p in projects:
        mine = [e for e in evidence if e.project == p["slug"]]
        if not mine:
            continue
        lines.append(f"*{p['slug']}* ({p['owner']}/{p['repo']})")
        for group in dict.fromkeys(e.group for e in mine):
            lines.append(f"  {group}:")
            for e in (x for x in mine if x.group == group):
                lines.append(f"  • {e.ref} [{_clean_title(e.title)}]({e.url})")
                lines.append(f"    {_escape_md(e.facts)}")
                if e.why:
                    lines.append(f"    Why: {_escape_md(e.why)}")
        lines.append("")

    fixes = [s.fix for s in statuses if s.fix]
    if fixes:
        lines.append("*Setup needed*")
        lines += [f"{i}. {_escape_md(f)}" for i, f in enumerate(fixes, 1)]
        lines.append("")

    if coverage:
        lines.append(coverage)
    return "\n".join(lines).strip()
