"""Multi-source research orchestrator.

Ports a focused subset of `tome`'s synthesis logic into the
workflows service so John-117 can call a single endpoint and get
ranked, deduplicated, citation-bearing findings across Reddit,
GitHub, and GLM web search. The shape of `Finding`, the dedup
rules, the authority bonuses, and the triangulation bonus all
mirror the tome plugin so future cross-pollination is cheap.

Channels:
- code: GitHub repos and issues
- discourse: Reddit, HN, blogs (via GLM web search)
- academic: stub for future arXiv/Semantic Scholar work

The orchestrator runs all eligible channel fetchers in parallel,
compresses each channel into Finding lists, merges, ranks, and
returns a single ordered list with confidence flags.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("clawrange.research")

_CURRENT_YEAR = datetime.now(tz=timezone.utc).year

# Same thresholds as tome.synthesis.merger
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_JACCARD_THRESHOLD_SAME_CHANNEL = 0.8
_JACCARD_THRESHOLD_CROSS_CHANNEL = 0.6
_TRIANGULATION_CAP = 0.15
_TRIANGULATION_PER_CHANNEL = 0.05

# ─── Finding model ────────────────────────────────────────────────


@dataclass
class Finding:
    """A single research finding from a channel.

    Mirrors `tome.models.Finding` so we can interop with future
    tome-driven sessions without translation.
    """

    source: str
    channel: str
    title: str
    url: str
    relevance: float
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.relevance = max(0.0, min(1.0, self.relevance))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Finding:
        return cls(
            source=d["source"],
            channel=d["channel"],
            title=d["title"],
            url=d["url"],
            relevance=float(d["relevance"]),
            summary=d.get("summary", ""),
            metadata=dict(d.get("metadata", {})),
        )


# ─── Deduplication ────────────────────────────────────────────────


def deduplicate(findings: list[Finding]) -> list[Finding]:
    """Remove duplicates by URL, keeping the higher-relevance one.

    Findings with empty URLs are kept as-is (no dedup key).
    """
    best: dict[str, Finding] = {}
    no_url: list[Finding] = []
    for f in findings:
        if not f.url:
            no_url.append(f)
            continue
        if f.url not in best or f.relevance > best[f.url].relevance:
            best[f.url] = f
    seen: dict[str, bool] = {}
    out: list[Finding] = []
    for f in findings:
        if not f.url:
            continue
        if f.url not in seen and best[f.url] is f:
            seen[f.url] = True
            out.append(f)
    out.extend(no_url)
    return out


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    stripped = _PUNCTUATION_RE.sub("", title.lower())
    return " ".join(sorted(stripped.split()))


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def fuzzy_deduplicate(
    findings: list[Finding], cross_channel: bool = False
) -> list[Finding]:
    """Drop near-duplicate titles, keeping higher-relevance.

    Within the same channel, similarity >= 0.8 collapses. Across
    channels (when `cross_channel=True`), threshold drops to 0.6.
    """
    if not findings:
        return []
    normals = [_normalize_title(f.title) for f in findings]
    removed: set[int] = set()

    for i in range(len(findings)):
        if i in removed:
            continue
        for j in range(i + 1, len(findings)):
            if j in removed:
                continue
            same = findings[i].channel == findings[j].channel
            if not cross_channel and not same:
                continue
            threshold = (
                _JACCARD_THRESHOLD_SAME_CHANNEL
                if same
                else _JACCARD_THRESHOLD_CROSS_CHANNEL
            )
            if _jaccard(normals[i], normals[j]) >= threshold:
                if findings[i].relevance >= findings[j].relevance:
                    removed.add(j)
                else:
                    removed.add(i)
                    break
    return [f for idx, f in enumerate(findings) if idx not in removed]


def merge_findings(channel_results: list[list[Finding]]) -> list[Finding]:
    """Flatten and dedupe by URL across channels."""
    flat: list[Finding] = []
    for ch in channel_results:
        flat.extend(ch)
    return deduplicate(flat)


# ─── Scoring ─────────────────────────────────────────────────────


def compute_relevance_score(f: Finding) -> float:
    """Composite relevance: base + authority + recency, capped at 1.0.

    Authority bonuses (channel-specific):
    - github: stars > 1000 -> +0.1, > 5000 -> +0.2
    - hn: score > 100 -> +0.1, > 500 -> +0.2
    - arxiv/academic: citations > 50 -> +0.1, > 200 -> +0.2
    - reddit: score > 50 -> +0.05, > 200 -> +0.1

    Recency bonus: metadata "year" within 2 calendar years -> +0.05.
    """
    score = f.relevance
    src = f.source.lower()
    meta = f.metadata or {}

    if src == "github":
        stars = int(meta.get("stars", 0) or 0)
        if stars > 5000:
            score += 0.2
        elif stars > 1000:
            score += 0.1
    elif src == "hn":
        s = int(meta.get("score", 0) or 0)
        if s > 500:
            score += 0.2
        elif s > 100:
            score += 0.1
    elif src in ("arxiv", "academic", "semantic_scholar"):
        c = int(meta.get("citations", 0) or 0)
        if c > 200:
            score += 0.2
        elif c > 50:
            score += 0.1
    elif src == "reddit":
        s = int(meta.get("score", 0) or 0)
        if s > 200:
            score += 0.1
        elif s > 50:
            score += 0.05

    year = meta.get("year")
    if year is not None and (_CURRENT_YEAR - int(year or 0)) <= 2:
        score += 0.05

    return min(score, 1.0)


def compute_triangulation_bonus(finding: Finding, all_findings: list[Finding]) -> float:
    """Bonus for findings corroborated across other channels.

    Each additional corroborating channel adds 0.05, capped at 0.15.
    Corroboration is Jaccard overlap >= 0.6 on normalized titles.
    """
    target = set(_PUNCTUATION_RE.sub("", finding.title.lower()).split())
    if not target:
        return 0.0

    corroborating: set[str] = set()
    for other in all_findings:
        if other is finding:
            continue
        if other.channel == finding.channel:
            continue
        other_words = set(_PUNCTUATION_RE.sub("", other.title.lower()).split())
        if not other_words:
            continue
        if (
            len(target & other_words) / len(target | other_words)
            >= _JACCARD_THRESHOLD_CROSS_CHANNEL
        ):
            corroborating.add(other.channel)

    return min(len(corroborating) * _TRIANGULATION_PER_CHANNEL, _TRIANGULATION_CAP)


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Return findings sorted by composite relevance, descending."""
    return sorted(findings, key=compute_relevance_score, reverse=True)


def flag_confidence(findings: list[Finding]) -> list[dict[str, Any]]:
    """Annotate findings with a confidence flag.

    - high: corroborated by 2+ other channels
    - medium: corroborated by 1 other channel
    - low: single-source (research best practice flags these as
      "needs verification")
    """
    out: list[dict[str, Any]] = []
    for f in findings:
        bonus = compute_triangulation_bonus(f, findings)
        if bonus >= 2 * _TRIANGULATION_PER_CHANNEL:
            confidence = "high"
        elif bonus >= _TRIANGULATION_PER_CHANNEL:
            confidence = "medium"
        else:
            confidence = "low"
        d = f.to_dict()
        d["confidence"] = confidence
        out.append(d)
    return out


# ─── Channel health ──────────────────────────────────────────────


def channel_health() -> dict[str, dict[str, Any]]:
    """Report per-channel configuration status.

    The /research endpoint returns empty findings when a channel
    isn't configured; this report tells operators *why* (e.g.
    GITHUB_PAT not set) so they can fix the actual gap rather than
    chase phantom logic bugs.
    """
    import os

    report: dict[str, dict[str, Any]] = {}

    # discourse / Reddit
    reddit_configured = bool(
        os.getenv("REDDIT_CLIENT_ID")
        and os.getenv("REDDIT_CLIENT_SECRET")
        and os.getenv("REDDIT_USERNAME")
        and os.getenv("REDDIT_PASSWORD")
    )
    report["discourse"] = {
        "source": "reddit",
        "configured": reddit_configured,
    }
    if not reddit_configured:
        report["discourse"]["reason"] = (
            "REDDIT_CLIENT_ID/SECRET/USERNAME/PASSWORD not set"
        )

    # code / GitHub
    github_configured = bool(os.getenv("GITHUB_PAT"))
    report["code"] = {
        "source": "github",
        "configured": github_configured,
    }
    if not github_configured:
        report["code"]["reason"] = "GITHUB_PAT not set"

    # discourse_web / GLM via proxy
    web_configured = bool(os.getenv("ZAI_API_KEY") or os.getenv("OPENROUTER_API_KEY"))
    report["discourse_web"] = {
        "source": "glm-web",
        "configured": web_configured,
    }
    if not web_configured:
        report["discourse_web"]["reason"] = "ZAI_API_KEY or OPENROUTER_API_KEY not set"

    return report


# ─── Channel fetchers (thin wrappers around existing modules) ─────


async def _fetch_reddit(topic: str, **kwargs: Any) -> list[Finding]:
    """Wrap reddit_search.search_subreddits and emit Findings."""
    from reddit_search import search_subreddits

    subreddits = kwargs.get("subreddits", ["ClaudeAI", "LocalLLaMA", "SideProject"])
    posts = await search_subreddits(
        topic,
        subreddits,
        since=kwargs.get("since", "30d"),
        limit_per_sub=kwargs.get("limit", 10),
    )
    return [
        Finding(
            source="reddit",
            channel="discourse",
            title=p.title,
            url=p.url,
            relevance=0.5,
            summary=getattr(p, "summary", "") or getattr(p, "selftext", "")[:300],
            metadata={"score": getattr(p, "score", 0)},
        )
        for p in posts
    ]


async def _fetch_github(topic: str, **kwargs: Any) -> list[Finding]:
    """Wrap github_search.search_repos and emit Findings."""
    from github_search import search_repos

    repos = await search_repos(
        topic,
        min_stars=kwargs.get("min_stars", 50),
        language=kwargs.get("language"),
        limit=kwargs.get("limit", 10),
    )
    return [
        Finding(
            source="github",
            channel="code",
            title=r.full_name,
            url=r.url,
            relevance=0.5,
            summary=r.description or "",
            metadata={"stars": r.stars},
        )
        for r in repos
    ]


async def _fetch_web(topic: str, **kwargs: Any) -> list[Finding]:
    """Use the GLM web-search proxy for general discourse coverage.

    Returns at most one synthesized Finding per call — GLM responses
    are already a synthesis. The caller should treat this as a
    summary, not a list of pages.
    """
    from llm_proxy import _llm_call

    prompt = (
        f"Web research request: {topic}\n\n"
        "Return a tight summary with the top 3 sources. For each, "
        "include the URL."
    )
    text = await _llm_call(prompt, max_tokens=800, web_search=True)
    if not text:
        return []
    return [
        Finding(
            source="web",
            channel="discourse",
            title=f"Web summary: {topic}"[:200],
            url="",
            relevance=0.4,
            summary=text[:1500],
            metadata={"raw_response_chars": len(text)},
        )
    ]


# ─── Orchestration ───────────────────────────────────────────────


_DEFAULT_CHANNELS = ("discourse", "code", "discourse_web")

# Channel-to-fetcher-name lookup. Resolved dynamically at call time
# via module globals so monkeypatch can swap fetchers in tests.
_CHANNEL_FETCHER_NAMES = {
    "discourse": "_fetch_reddit",
    "code": "_fetch_github",
    "discourse_web": "_fetch_web",
}


async def orchestrate_research(
    topic: str,
    channels: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a multi-source research session and return ranked findings.

    Channels (default: all):
    - 'discourse'      -> Reddit subreddits via `_fetch_reddit`
    - 'code'           -> GitHub repos via `_fetch_github`
    - 'discourse_web'  -> GLM web search summary via `_fetch_web`
      (also accepted as the shorthand 'web')

    Returns:
        {
          "topic": str,
          "channels": list[str],
          "findings": list[dict],   # ranked, with confidence flags
          "errors": dict[str, str], # channel -> error message
          "total": int,
        }
    """
    if not topic or not topic.strip():
        raise ValueError("topic is required")

    selected = channels or list(_DEFAULT_CHANNELS)
    selected = ["discourse_web" if c == "web" else c for c in selected]

    import sys

    mod = sys.modules[__name__]

    fetchers = []
    used_channels: list[str] = []
    for ch in selected:
        fname = _CHANNEL_FETCHER_NAMES.get(ch)
        if fname is None:
            logger.warning("research: skipping unknown channel %s", ch)
            continue
        fn = getattr(mod, fname)
        fetchers.append(fn(topic, **kwargs))
        used_channels.append(ch)

    raw = await asyncio.gather(*fetchers, return_exceptions=True)

    errors: dict[str, str] = {}
    channel_findings: list[list[Finding]] = []
    for ch, result in zip(used_channels, raw):
        if isinstance(result, BaseException):
            short = ch.split("_")[0]
            errors[short] = str(result)
            channel_findings.append([])
            continue
        channel_findings.append(result)

    merged = merge_findings(channel_findings)
    ranked = rank_findings(merged)
    flagged = flag_confidence(ranked)

    return {
        "topic": topic,
        "channels": used_channels,
        "findings": flagged,
        "errors": errors,
        "total": len(flagged),
    }
