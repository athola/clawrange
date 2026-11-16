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
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

import github_search
import llm_proxy
import tenant_profile
from brain_db import project_list
from errors import UpstreamError
from reddit_search import SearchHealth

logger = logging.getLogger("clawrange.rundown")


def report_header(title: str, now: datetime, suffix: str = "") -> str:
    """Bold report title with the local date, then the time (and suffix),
    in the operator's timezone (tenant_profile.operator_timezone)."""
    local = now.astimezone(ZoneInfo(tenant_profile.operator_timezone()))
    return f"*{title} — {local:%a %b %d}* ({local:%H:%M %Z}{suffix})"


HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
PER_CHANNEL_CAP = 3
WEB_TIMEOUT_S = 90.0
HN_TIMEOUT_S = 15.0

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
        project_list(project, "topics"),
        project_list(project, "search_terms"),
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
    async with httpx.AsyncClient(timeout=HN_TIMEOUT_S) as client:
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
            mine = repo.lower().startswith(f"{project['owner'].lower()}/")
            out.append(
                Evidence(
                    source="github",
                    group="GitHub issues" if mine else "GitHub issues (other repos)",
                    project=project["slug"],
                    title=issue.title,
                    url=issue.url,
                    facts=f"issue on {repo}{'' if mine else ' (not yours)'}"
                    f" · open #{issue.number}",
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


DEFAULT_WEB_VENUES = (
    "forum threads, Lobsters, dev.to, GitHub Discussions, newsletters "
    "accepting submissions, awesome-lists, Discord/Discourse communities"
)
DEFAULT_AUDIENCE = "a maintainer promoting these open-source projects"


async def fetch_web(project: dict, policy: dict | None = None) -> list[Evidence]:
    """Web search for venues beyond Reddit/HN where the project fits.

    `outreach.web_venues` in the profile replaces the default venue list.
    """
    topics, terms = _keywords(project)
    venues = (policy or {}).get("web_venues") or DEFAULT_WEB_VENUES
    prompt = (
        f"Find places from the last 7 days where the open-source project "
        f"{project['owner']}/{project['repo']} (topics: {', '.join(topics)}; "
        f"keywords: {', '.join(terms)}) could be usefully mentioned: "
        f"{venues}. Skip Reddit and Hacker News. Return at most 4 lines, "
        f"each exactly: - <title> | <url> | <why it fits, under 15 words>. "
        f"Only include URLs you found in search results."
    )
    text = await asyncio.wait_for(
        llm_proxy._llm_call(prompt, max_tokens=700, web_search=True),
        timeout=WEB_TIMEOUT_S,
    )
    if not text:
        raise UpstreamError("web search tier returned nothing")
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
        return "Web search tier failed — check GET /tier and the LLM provider balance."
    return ""


async def gather_other_channels(
    projects: list[dict], policy: dict | None = None
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
            *(
                fn(p, policy=policy) if fn_name == "fetch_web" else fn(p)
                for p in projects
            ),
            return_exceptions=True,
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
    # Healthy when something was read, or nothing was even attempted.
    # Skips mean Reddit was not searched, which is not a quiet day.
    ok = health.ok > 0 or (health.requests == 0 and not health.skipped)
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
    # Models sometimes cite a bare "E1"; bracket known refs so they link.
    # Unknown tokens (A6000, M4) are left alone.
    # Bracket groups ("[E1, E2, E3]") are left whole for _REF_RE.
    text = re.sub(
        r"\[[^\]]*\]|(?<!\w)([A-Z]\d+)(?!\w)",
        lambda m: (
            f"[{m.group(1)}]" if m.group(1) and m.group(1) in by_ref else m.group(0)
        ),
        text,
    )
    text = _escape_md(text)

    def _link(m: re.Match[str]) -> str:
        refs = [r.strip() for r in m.group(1).split(",")]
        return ", ".join(f"[{r}]({by_ref[r]})" for r in refs if r in by_ref)

    text = _REF_RE.sub(_link, text)
    # "[E1][E2]" would render as one run of link text ("E1E2").
    text = re.sub(r"\)(?=\[[A-Z]\d+\]\()", "), ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# What the operator can do at each kind of venue. Each evidence line in the
# prompt carries its entry, so the model cannot "tag" an issue on a repo the
# operator does not own or move the conversation to a channel nobody cited.
# Defaults for the built-in channels; a profile's `outreach.venue_actions`
# overrides any of them, and `outreach.banned_actions` adds phrases that
# drop an action item (see filter_actions).
VENUE_ACTIONS = {
    "reddit": "reply in the thread",
    "hn": "comment in the HN discussion (no drafted text; HN bans generated "
    "comments and promotion-first posting)",
    "github": "comment on the issue only if you can help; on a repo that is "
    "not yours you cannot label, triage, close or assign it",
    "web": "open and verify the page first, then reply or submit there if it fits",
}
# Actions no evidence item can host: dropped by filter_actions.
_OFF_VENUE_RE = re.compile(
    r"cross-?post|guest post|reach out|cold (?:email|dm)"
    # Triage verbs only when aimed at an issue or a ref: "label plugins"
    # is fine, "tag [E1] help wanted" is not the operator's call.
    r"|\b(?:tag|label|triage)\b[^.]{0,30}?(?:\bissues?\b|\[?\b[A-Z]\d+\b)",
    re.IGNORECASE,
)


def _venue_actions(policy: dict | None) -> dict[str, str]:
    return {**VENUE_ACTIONS, **((policy or {}).get("venue_actions") or {})}


def _banned_re(policy: dict | None) -> re.Pattern[str]:
    extra = [str(p) for p in (policy or {}).get("banned_actions") or [] if str(p)]
    if not extra:
        return _OFF_VENUE_RE
    alts = "|".join(rf"(?<!\w){re.escape(p)}" for p in extra)
    return re.compile(f"{_OFF_VENUE_RE.pattern}|{alts}", re.IGNORECASE)


def _about(project: dict) -> str:
    """The project's own one-line description, or an explicit 'do not
    describe'. Taken from the first sentence of its posture, minus any
    short leading label ("Lead with:", "Voice:")."""
    posture = (project.get("posture") or "").strip()
    text = re.sub(r"^[A-Za-z][\w -]{0,24}:\s*", "", posture)
    text = re.split(r"(?<=\.)\s", text, maxsplit=1)[0].strip()
    return f"about: {text}" if text else "no description: do not describe it"


async def synthesize(
    projects: list[dict], evidence: list[Evidence], policy: dict | None = None
) -> str | None:
    """LLM 'today's read + action items' over numbered evidence.

    Evidence must already carry refs. Returns raw model text (pass it to
    build_report, which links and sanitizes it) or None on failure.

    Grounding (tome research, 2026-11-14): each item carries its owner
    and allowed action, a closed venue rule, a fixed project description
    in place of bare keywords, and count-aware hedging, since summarizers
    overgeneralize from few items (Peters & Chin-Yee 2025).
    """
    if not evidence:
        return None
    venues = _venue_actions(policy)
    lines = [
        f"[{e.ref}] matched for {e.project} via {e.source}: {e.title} — {e.facts}."
        f" Allowed: {venues.get(e.source, 'read only')}"
        for e in evidence
    ]
    n = len(evidence)
    project_lines = [
        f"- {p['slug']} ({p['owner']}/{p['repo']}), {_about(p)}; "
        f"search keywords: {', '.join(_keywords(p)[0])}"
        for p in projects
    ]
    prompt = (
        f"You write a daily outreach brief for "
        f"{(policy or {}).get('audience') or DEFAULT_AUDIENCE}:\n"
        + "\n".join(project_lines)
        + f"\n\nToday's evidence ({n} evidence item{'s' if n != 1 else ''}; "
        "only these exist; 'matched for' is the search that found an item, "
        "not who owns it):\n"
        + "\n".join(lines)
        + "\n\nWrite two plain-text sections, no Markdown, no URLs:\n"
        "TODAY'S READ: 1-4 bullets on demand signals and pain points, and "
        "which project each maps to. Cite evidence as [E#]. A claim backed "
        "by fewer than 3 items from at least 2 sources is a single signal: "
        "write 'one item suggests', never 'the ecosystem is' or 'teams want'. "
        "Describe a project only with its 'about' text; search keywords are "
        "not features.\n"
        "ACTION ITEMS: up to 6 numbered items, highest leverage first. Each "
        "takes place at a cited item ([E#]) using that item's Allowed "
        "action, and says why today. No action off the evidence: no "
        "cross-posting, Slack or Discord, contacting authors, guest posts, "
        "or writing articles and guides. Useful help first; mention a "
        "project only where it directly answers the question. "
        "Do not write reply text for the operator. Ignore evidence that does "
        "not genuinely fit a project; never stretch an unrelated item into "
        "a signal. If little fits, say so in one line rather than padding."
    )
    try:
        draft = await llm_proxy._llm_call(prompt, max_tokens=1500)
    except Exception as exc:
        logger.warning("rundown: synthesis failed: %s", exc)
        return None
    return await check_capabilities(projects, draft) if draft else draft


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# A reply is NONE or a bare list of numbers in any bracket/comma/"and"
# form ("2,3", "[2], [3]", "2 and 3"). Prose is rejected and keeps the draft.
_CHECK_NONE_RE = re.compile(r"\s*none\.?\s*", re.IGNORECASE)
_CHECK_LIST_RE = re.compile(
    r"[\s\[\](),;.]*\d+(?:[\s\[\](),;.]+(?:and\s+)?\d+)*[\s\[\](),;.]*"
)


async def check_capabilities(projects: list[dict], draft: str) -> str:
    """Delete sentences that credit a project with an unstated capability.

    Prompt rules alone did not stop it (live replay: "clawrange handles
    CLAUDE_CODE_PLUGIN_DIRS discovery"). Sentences naming a project are
    numbered and a second call returns only the numbers that overclaim;
    code deletes those, so this pass can remove text but never add any.
    Any unusable reply or failure keeps the draft unchanged.
    """
    names = [p["slug"] for p in projects]
    if not names:
        return draft
    name_re = re.compile(
        r"(?<![\w-])(?:" + "|".join(map(re.escape, names)) + r")(?![\w-])",
        re.IGNORECASE,
    )
    lines = [_SENTENCE_RE.split(line) for line in draft.splitlines()]
    numbered: dict[int, tuple[int, int]] = {}
    for li, sentences in enumerate(lines):
        for si, sentence in enumerate(sentences):
            if name_re.search(sentence):
                numbered[len(numbered) + 1] = (li, si)
    if not numbered:
        return draft

    about = "\n".join(f"- {p['slug']}: {_about(p)}" for p in projects)
    listed = "\n".join(f"[{n}] {lines[li][si]}" for n, (li, si) in numbered.items())
    prompt = (
        "Projects and the only things known about them:\n"
        f"{about}\n\nSentences:\n{listed}\n\n"
        "Which sentences state or imply that a project already does, "
        "supports or handles something its description does not say? "
        "Mapping a demand signal to a project ('this maps to X') is fine. "
        "Reply with the numbers only, comma-separated, or NONE."
    )
    try:
        reply = await llm_proxy._llm_call(prompt, max_tokens=50)
    except Exception as exc:
        logger.warning("rundown: capability check failed: %s", exc)
        return draft
    reply = reply or ""
    if _CHECK_NONE_RE.fullmatch(reply) or not _CHECK_LIST_RE.fullmatch(reply):
        return draft
    drop = {numbered[int(n)] for n in re.findall(r"\d+", reply) if int(n) in numbered}
    if not drop:
        return draft

    out: list[str] = []
    for li, sentences in enumerate(lines):
        kept = [s for si, s in enumerate(sentences) if (li, si) not in drop]
        line = " ".join(kept)
        if len(kept) < len(sentences) and not re.search(r"\w{2,}", line):
            continue  # only a list marker ("•", "3.") was left
        out.append(line)
    logger.info("rundown: capability check removed %d sentence(s)", len(drop))
    return "\n".join(out)


def filter_actions(
    text: str, evidence: list[Evidence], policy: dict | None = None
) -> str:
    """Drop ACTION ITEMS that cite no known [E#] or name an off-venue move.

    A deterministic post-check (claim-to-evidence verification): the prompt
    asks for venue-bound actions, this enforces it. An item is a numbered
    line plus its wrapped continuation lines; unnumbered prose (a 'little
    fits today' line) passes through. Kept items are renumbered.
    """
    known = {e.ref for e in evidence if e.ref}
    banned = _banned_re(policy)
    out: list[str] = []
    in_actions = False
    block: list[str] = []
    kept = dropped = 0

    def flush() -> None:
        nonlocal kept, dropped
        if not block:
            return
        body = " ".join(block)
        refs = set(re.findall(r"\b[A-Z]\d+\b", body)) & known
        if refs and not banned.search(body):
            kept += 1
            out.append(re.sub(r"^(\s*)\d+", rf"\g<1>{kept}", block[0], count=1))
            out.extend(block[1:])
        else:
            dropped += 1
        block.clear()

    for line in text.splitlines():
        if re.match(r"^\s*ACTION ITEMS\b", line, re.IGNORECASE):
            in_actions = True
            out.append(line)
            continue
        if not in_actions:
            out.append(line)
        elif re.match(r"^\s*\d+[.)]\s", line):
            flush()
            block.append(line)
        elif block and line.strip() and not line.strip().isupper():
            block.append(line)
        else:
            flush()
            if line.strip().isupper():
                in_actions = False
            out.append(line)
    flush()
    if dropped:
        out.append(
            f"({dropped} suggested action{'s' if dropped != 1 else ''} dropped: "
            "no cited venue, or not possible there)"
        )
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


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
    policy: dict | None = None,
) -> str:
    """Render the full rundown. Never returns an empty report.

    `policy` is the profile's optional `outreach` block (venue_actions,
    banned_actions)."""
    if not all(e.ref for e in evidence):
        evidence = assign_refs(projects, evidence)
    lines = [
        report_header("Daily outreach rundown", now, ", last 24h"),
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
            link_citations(filter_actions(synthesis, evidence, policy), evidence),
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
