"""Schedule generators run by the scheduler (registry: ``GENERATORS``).

Some enqueue tasks via brain_db.create_task() (scans, content ideas,
research pulse, reviews); others build and deliver a report themselves:
the multi-channel outreach digest (Reddit, Hacker News, GitHub, web),
the hot pulse, CRM syncs and digests, and the homelab deal rundown.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import statistics
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import deal_sources
import deals
import github_search
import llm_proxy
import persona_learning as pl
import reddit_search
import rundown
import telegram
import tenant_profile
from brain_db import project_list
from connectors import run_connector
from crm import get_adapter

if TYPE_CHECKING:
    from reddit_search import RedditPost

# NOTE on deferred imports: ``app`` imports this module at module scope, so
# the ``from app import brain_db`` fallback below must stay function-local
# (cycle guard).

logger = logging.getLogger("clawrange.generators")

# In-memory throttle state for the hot_pulse generator. The cron fires
# every 5 minutes (heartbeat), but Telegram delivery is gated to once
# per `min_delivery_interval_minutes` (default 15min) to keep the
# operator's chat from getting spammed. Resets on workflows restart —
# acceptable for a single-uvicorn-worker service where restarts are
# rare and a one-off extra delivery after restart is harmless.
_LAST_HOT_PULSE_DELIVERY_AT: datetime | None = None

# Tracks whether the most recent hot_pulse fire was suppressed by the
# quiet-hours gate. Used so we write the schedule status row exactly
# once per night (on the boundary fire entering quiet hours) instead
# of 60 times — keeps the schedule history readable when the operator
# wakes up. Flag flips back to False on the first non-quiet fire,
# which then naturally writes "ok (N picks)" / "throttled" status as
# part of the normal flow.
_HOT_PULSE_IN_QUIET_HOURS: bool = False


# Quiet-hours check (patchable by name in tests): the shared hour window.
_is_quiet_hours = tenant_profile.hour_in_window


def _record_status(
    brain_db, schedule_id: str, status: str, when: datetime | None = None
) -> None:
    """Write a schedule's last_run/last_status; a failed write only warns.

    APScheduler fires bypass run_schedule_now, so generators record their
    own status; a broken status table must never abort the run.
    """
    try:
        brain_db.update_schedule_status(
            schedule_id, (when or datetime.now(UTC)).isoformat(), status
        )
    except Exception as exc:
        logger.warning("%s: could not update schedule status: %s", schedule_id, exc)


def _delivery_status(sent: int, total: int, counts: str) -> str:
    """Status line saying whether every Telegram message landed."""
    if sent == total:
        return f"delivered {sent}/{total} msgs ({counts})"
    return f"FAILED telegram {sent}/{total} msgs ({counts})"


def _engagement_rank(pick: tuple[RedditPost, float]) -> float:
    """Sort key for (post, relevance): relevance scaled by log engagement."""
    post, rel = pick
    return rel * (1 + math.log(max(post.score, 1)))


def digest_extra_subreddits() -> list[str]:
    """Subreddits added to every digest/pulse scan even when no tracked
    project subscribes to them: the profile's
    `reddit.digest_extra_subreddits` (tenant data, empty by default)."""
    profile = tenant_profile.active_profile()
    reddit = (profile.raw.get("reddit") or {}) if profile else {}
    return [str(s) for s in reddit.get("digest_extra_subreddits") or []]


async def morning_scan_generator(
    brain_db, project_slugs: list[str] | None = None, **kwargs
) -> None:
    """Generate Reddit + GitHub scan tasks for each tracked project."""
    projects = brain_db.list_projects()
    if project_slugs:
        projects = [p for p in projects if p["slug"] in project_slugs]

    for project in projects:
        topics = project_list(project, "topics")
        subreddits = project_list(project, "subreddits")
        search_terms = project_list(project, "search_terms")
        slug = project["slug"]

        topic_str = ", ".join(topics[:3]) if topics else slug
        sub_str = ", ".join(subreddits[:5]) if subreddits else "project defaults"

        desc = (
            f"Scan reddit for {slug}: search '{topic_str}' across {sub_str}. "
            f"Surface posts where commenting would be relevant."
        )
        brain_db.create_task(desc, priority=3, source="schedule")
        logger.info("morning_scan: enqueued reddit task for %s", slug)

        term_str = ", ".join(search_terms[:3]) if search_terms else topic_str
        desc2 = (
            f"Scan github for {slug}: find adjacent repos and recent issues "
            f"asking about {term_str}."
        )
        brain_db.create_task(desc2, priority=3, source="schedule")
        logger.info("morning_scan: enqueued github task for %s", slug)


async def weekly_traffic_generator(
    brain_db, project_slugs: list[str] | None = None, **kwargs
) -> None:
    """Generate traffic snapshot tasks for each tracked project."""
    projects = brain_db.list_projects()
    if project_slugs:
        projects = [p for p in projects if p["slug"] in project_slugs]

    for project in projects:
        owner = project["owner"]
        repo = project["repo"]
        slug = project["slug"]

        desc = (
            f"Weekly traffic snapshot for {owner}/{repo}: pull "
            f"views/clones/uniques and stargazer delta. Compare to previous week."
        )
        brain_db.create_task(desc, priority=4, source="schedule")
        logger.info("weekly_traffic: enqueued task for %s", slug)


def _awesome_lists(lists: list[str] | None) -> list[tuple[str, str]]:
    """ "owner/repo" lists to watch: the profile's `awesome_lists` (tenant
    data; none by default). A schedule's `lists` kwarg overrides it with
    owner/repo entries, or narrows it by bare repo name."""
    profile = tenant_profile.active_profile()
    configured = [
        str(x) for x in ((profile.raw.get("awesome_lists") or []) if profile else [])
    ]
    if lists:
        full = [x for x in lists if "/" in x]
        names = {x for x in lists if "/" not in x}
        lists = full + [x for x in configured if x.partition("/")[2] in names]
    else:
        lists = configured
    out = []
    for entry in lists:
        owner, _, repo = str(entry).partition("/")
        if owner and repo:
            out.append((owner, repo))
        else:
            logger.warning("awesome_lists_watch: %r is not owner/repo", entry)
    return out


async def awesome_lists_watch_generator(
    brain_db, lists: list[str] | None = None, **kwargs
) -> None:
    """Check awesome-lists for tracked projects, enqueue PR tasks when missing."""

    projects = brain_db.list_projects()
    target_lists = _awesome_lists(lists)
    if not target_lists:
        logger.info("awesome_lists_watch: no lists configured, skipping")

    for list_owner, list_repo in target_lists:
        target_urls = [f"github.com/{p['owner']}/{p['repo']}" for p in projects]
        found = await github_search.check_awesome_list(
            list_owner, list_repo, target_urls
        )
        if found is None:
            logger.warning(
                "awesome_lists_watch: %s/%s unreadable, skipping (no tasks queued)",
                list_owner,
                list_repo,
            )
            continue
        pending = {t["description"] for t in brain_db.list_tasks(status="pending")}

        for project in projects:
            url = f"github.com/{project['owner']}/{project['repo']}"
            if not found.get(url, False):
                posture = project.get("posture", "")
                desc = (
                    f"Awesome-list watch: {project['slug']} not yet listed in "
                    f"{list_owner}/{list_repo}. Draft a PR description and "
                    f"submission plan. Posture: {posture}"
                )
                if desc in pending:
                    continue
                brain_db.create_task(desc, priority=3, source="schedule")
                logger.info(
                    "awesome_lists_watch: %s missing from %s/%s",
                    project["slug"],
                    list_owner,
                    list_repo,
                )


async def custom_scan_generator(
    brain_db,
    topic: str = "",
    kind: str = "web",
    project_slug: str | None = None,
    **kwargs,
) -> None:
    """Generic scan generator for user-created schedules."""
    slug_ctx = f" for {project_slug}" if project_slug else ""
    desc = f"Custom scan{slug_ctx}: {topic}"
    brain_db.create_task(desc, priority=3, source="schedule")
    logger.info("custom_scan: enqueued '%s'", desc[:80])


def _task_text(value: object, limit: int = 160) -> str:
    """Single-line, control-free text for embedding untrusted titles in a
    task description (an agent later reads that description as instructions)."""
    text = "".join(" " if ch in "\r\n\t" else ch for ch in str(value or ""))
    text = "".join(ch for ch in text if ch.isprintable())
    text = " ".join(text.split()).replace('"', "'")
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def content_idea_generator(
    brain_db,
    project_slugs: list[str] | None = None,
    sessions_window: int = 5,
    **kwargs,
) -> None:
    """Turn recent research findings into content ideas per project.

    Reads the most recent `sessions_window` research sessions from
    the brain and, for each tracked project, enqueues a single
    content-idea task with the strongest finding from that pool.

    The generated task is intended to be picked up by the assistant (or
    the operator) and elaborated into one of:
    - a technical blog post (lead),
    - a Reddit comment (engage),
    - or an X thread (broadcast).

    The generator never auto-posts. It only proposes; the operator approves.
    """
    projects = brain_db.list_projects()
    if project_slugs:
        projects = [p for p in projects if p["slug"] in project_slugs]
    if not projects:
        return

    sessions = brain_db.list_research_sessions(limit=sessions_window)
    candidate_findings: list[dict] = []
    for s in sessions:
        loaded = brain_db.get_research_session(s["id"])
        if not loaded:
            continue
        for f in loaded.get("findings", []):
            candidate_findings.append({**f, "topic": s["topic"]})

    if not candidate_findings:
        logger.info("content_idea: no recent research findings, skipping idea pass")
        return

    # Highest-relevance first, but still cap one idea per project per run.
    candidate_findings.sort(key=lambda f: f.get("relevance", 0), reverse=True)
    top = candidate_findings[0]
    # Every status: a handled idea for the same finding must not come back
    # tomorrow just because it is no longer pending.
    existing = {t["description"] for t in brain_db.list_tasks()}

    for project in projects:
        slug = project["slug"]
        posture = project.get("posture", "")
        topics = project_list(project, "topics")
        topic_hint = ", ".join(topics[:3]) if topics else slug

        # Some channels (e.g. TRIZ analogies) carry no URL; cite without
        # the parens rather than rendering a dangling "()" in the task.
        citation = f" ({_task_text(top['url'], 300)})" if top.get("url") else ""
        desc = (
            f"Content idea for {slug}: research on '{_task_text(top['topic'])}' "
            f'surfaced "{_task_text(top["title"])}"{citation}. '
            f"Draft three angles - "
            f"(1) technical post tying this to {topic_hint}, "
            f"(2) useful Reddit/HN comment offering specifics, "
            f"(3) short X thread on the lesson. "
            f"Posture: {posture}"
        )
        if desc in existing:
            logger.info("content_idea: %s already has this idea, skipping", slug)
            continue
        brain_db.create_task(desc, priority=3, source="schedule")
        logger.info(
            "content_idea: enqueued idea for %s based on session %s",
            slug,
            top.get("session_id", "unknown"),
        )


async def comment_draft_generator(
    brain_db,
    post_url: str = "",
    post_summary: str = "",
    project_slug: str | None = None,
    **kwargs,
) -> None:
    """Draft a useful, non-promotional comment for a target post.

    The output is a *draft*, never an auto-post. We follow the
    community-first marketing playbook: lead with specific,
    helpful content; mention a product only when it directly
    answers the question the post asks.

    Inputs:
    - post_url: Reddit/HN/blog URL the draft is responding to.
    - post_summary: optional one-line summary of what OP asked.
    - project_slug: optional project whose posture should color
      the draft (e.g. mention a plugin-marketplace project when the
      post is about plugin discovery).

    Emits a single task tagged 'comment_draft' for human review.
    """
    if not post_url or not post_url.strip():
        logger.info("comment_draft: missing post_url, skipping")
        return

    posture = ""
    project_name = project_slug or "(no project context)"
    if project_slug:
        proj = brain_db.get_project(project_slug)
        if proj:
            posture = proj.get("posture", "")
        else:
            logger.info(
                "comment_draft: unknown project slug %s, using defaults",
                project_slug,
            )

    summary_clause = f" OP context: {post_summary}." if post_summary else ""
    posture_clause = f" Posture: {posture}" if posture else ""

    desc = (
        f"[DRAFT] Comment draft for {project_name} on {post_url}.{summary_clause} "
        f"Write a useful 3-5 sentence reply that leads with concrete advice and "
        f"only mentions the product if directly relevant. Honest, specific, no "
        f"hype. Never auto-post - this draft is for "
        f"{tenant_profile.operator_name()} to review and send "
        f"manually.{posture_clause}"
    )
    brain_db.create_task(desc, priority=2, source="schedule")
    logger.info("comment_draft: enqueued draft task for %s", post_url)


def _matched_keywords(
    post: RedditPost, topics: list[str], terms: list[str]
) -> list[str]:
    """Return up to two project keywords/terms that literally appear
    in the post's title or snippet, search_terms first since they
    were curated as queries (higher signal than topic tags)."""
    haystack = (post.title + " " + (post.snippet or "")).lower()
    matches: list[str] = []
    for t in list(terms) + list(topics):
        t_norm = t.lower().strip()
        if t_norm and t_norm in haystack and t_norm not in (m.lower() for m in matches):
            matches.append(t)
            if len(matches) >= 2:
                break
    return matches


def _comment_angle(post: RedditPost) -> str:
    """One-line 'why we should comment' framing based on engagement."""
    if not post.engagement_known:
        return "engagement unknown (feed fallback) — open it to judge"
    return rundown.engagement_angle(post.comments)


def _pick_facts(
    post: RedditPost, topics: list[str], terms: list[str], is_bonus: bool
) -> str:
    """Engagement plus the reason the post matched, on one line."""
    matches = _matched_keywords(post, topics, terms)
    if matches:
        match_text = "matched " + " + ".join(f'"{m}"' for m in matches)
    elif is_bonus:
        match_text = "sub-affinity bonus"
    else:
        match_text = "search match"
    if not post.engagement_known:
        return f"r/{post.subreddit} · engagement unknown · {match_text}"
    return (
        f"r/{post.subreddit} · {post.score} pts · "
        f"{post.comments} comments · {match_text}"
    )


def _render_pick_lines(
    post: RedditPost,
    topics: list[str],
    terms: list[str],
    is_bonus: bool,
) -> list[str]:
    """Render one pick as a 3-line block: title-with-link / facts / why.

    The link is the literal post.url so it works as a tap target in
    Telegram clients regardless of Markdown rendering quirks."""
    # Reddit titles are untrusted: strip Markdown control characters so a
    # title can't break or inject into the `[title](url)` link.
    title = rundown._clean_title(post.title) or "(untitled)"
    star = "★ " if is_bonus else ""
    return [
        f"  • {star}[{title}]({post.url})",
        f"    {_pick_facts(post, topics, terms, is_bonus)}",
        f"    Why: {_comment_angle(post)}",
    ]


def _reddit_evidence(
    project: dict, post: RedditPost, is_bonus: bool, group: str
) -> rundown.Evidence:
    topics = project_list(project, "topics")
    terms = project_list(project, "search_terms")
    return rundown.Evidence(
        source="reddit",
        group=group,
        project=project["slug"],
        title=("★ " if is_bonus else "") + post.title,
        url=post.url,
        facts=_pick_facts(post, topics, terms, is_bonus),
        why=_comment_angle(post),
        score=post.score + post.comments,
    )


def _score_relevance(post: RedditPost, topics: list[str], terms: list[str]) -> float:
    """Keyword-overlap relevance: title + snippet vs project topics/terms."""
    return rundown.relevance(post.title + " " + (post.snippet or ""), topics, terms)


def _outreach_policy(profile_name: str | None = None) -> dict:
    """The profile's optional `outreach` block; {} when absent or broken."""
    try:
        return tenant_profile.load_profile(profile_name).raw.get("outreach") or {}
    except Exception as exc:
        logger.warning("morning_digest: outreach policy unavailable: %s", exc)
        return {}


def _scan_plan(
    brain_db, project_slugs, extra_subreddits, tag: str
) -> tuple[list[dict], list[str], dict[str, set[str]]] | None:
    """Shared setup for the Reddit digest generators.

    Returns ``(projects, scan_subreddits, project_sub_sets)`` or None when no
    tracked project matches. ``scan_subreddits`` is the union of every
    project's subreddits plus the extras; ``project_sub_sets`` holds each
    project's lowercased subreddits for case-insensitive affinity checks.
    Records one search impression per (subreddit, project) pair, the
    denominator for the stats table's hit rate.
    """
    projects = brain_db.list_projects()
    if project_slugs:
        projects = [p for p in projects if p["slug"] in project_slugs]
    if not projects:
        logger.info("%s: no tracked projects, skipping", tag)
        return None
    extras = (
        list(extra_subreddits)
        if extra_subreddits is not None
        else digest_extra_subreddits()
    )
    project_sub_sets = {
        p["slug"]: {s.lower() for s in project_list(p, "subreddits")} for p in projects
    }
    scan_subreddits = sorted(
        set(extras) | {s for p in projects for s in project_list(p, "subreddits")}
    )
    brain_db.record_subreddit_searches(
        [
            (sub, slug, sub.lower() in subs)
            for slug, subs in project_sub_sets.items()
            for sub in scan_subreddits
        ]
    )
    return projects, scan_subreddits, project_sub_sets


async def morning_digest_generator(
    brain_db,
    project_slugs: list[str] | None = None,
    extra_subreddits: list[str] | None = None,
    top_per_project: int = 4,
    popularity_multiplier: float = 2.0,
    popular_bonus_cap: int = 2,
    schedule_id: str = "morning_digest",
    **kwargs,
) -> None:
    """Deliver the daily outreach rundown to Telegram.

    The report combines Reddit picks (below) with the other channels
    from `rundown.gather_other_channels` (Hacker News, GitHub issues,
    web search), an LLM synthesis, per-source health, and the subreddit
    coverage report.

    Two-tier Reddit picks per project:
    - Strict tier: posts whose title/snippet literally matches the
      project's topics or search_terms. Capped at top_per_project,
      ranked by relevance × log(score+1).
    - Popular bonus tier: posts in one of the project's own subreddits
      that lack a literal keyword hit but exceed an adaptive popularity
      threshold (popularity_multiplier × that subreddit's median score
      across this scan). Capped at popular_bonus_cap, ranked by raw
      score. Marked with a ★ prefix in the digest so the operator can
      tell them apart from strict picks.

    The adaptive threshold makes the bonus tier fair across busy and
    quiet subs: r/Construction's "popular" bar is far lower than
    r/ClaudeCode's, but each gets the same relative treatment.

    Filters posts already surfaced for that project via scan_cache.
    Marks every delivered post as `seen` so tomorrow's digest won't
    repeat it. Each pick renders as a 3-line block in Telegram:
    title-with-link / facts / why-comment. No comment drafts are
    queued — the operator reads, taps the direct URL, and writes
    their own replies.
    """

    plan = _scan_plan(brain_db, project_slugs, extra_subreddits, "morning_digest")
    if plan is None:
        return
    projects, scan_subreddits, project_sub_sets = plan

    # One tally across every Reddit call this run, so the report can say
    # why Reddit came back empty instead of looking like a quiet day.
    health = reddit_search.SearchHealth()

    best_for_post, scores_by_sub = await _collect_digest_candidates(
        brain_db, projects, scan_subreddits, project_sub_sets, health
    )
    picks_by_project = _tier_digest_picks(
        projects,
        best_for_post,
        scores_by_sub,
        top_per_project,
        popularity_multiplier,
        popular_bonus_cap,
    )

    # Discovery pass: search across all of Reddit (no sub restriction)
    # for each project's first search term. Posts in non-curated subs
    # that match relevance get tagged emerging — surfaced in their own
    # 🆕 section AND, once delivered, recorded as hits in subreddit_stats
    # so they accumulate toward the auto-promote threshold (≥5 hits in 14d).
    emerging_picks_by_project = await _discover_emerging(
        brain_db, projects, project_sub_sets, health=health
    )

    # Auto-promote any non-curated subs that have crossed the threshold
    # since the last digest. Mutates project.subreddits + stamps the
    # stats row promoted_at, so the next run treats it as curated.
    newly_promoted = _promote_emerging_subreddits(brain_db, projects)

    # Evidence from every channel. Reddit picks keep their tiering; the
    # other channels degrade independently so one outage never empties
    # the report.
    evidence: list[rundown.Evidence] = []
    for project in projects:
        slug = project["slug"]
        for post, is_bonus in picks_by_project.get(slug, []):
            evidence.append(_reddit_evidence(project, post, is_bonus, "Reddit"))
        for post in emerging_picks_by_project.get(slug, []):
            evidence.append(_reddit_evidence(project, post, False, "🆕 Emerging subs"))
    policy = _outreach_policy(kwargs.get("profile_name"))
    other, statuses = await rundown.gather_other_channels(projects, policy)
    evidence = rundown.assign_refs(projects, evidence + other)
    statuses.insert(0, rundown.reddit_status(health))
    synthesis = await rundown.synthesize(projects, evidence, policy)

    report = rundown.build_report(
        projects=projects,
        evidence=evidence,
        statuses=statuses,
        synthesis=synthesis,
        coverage=_render_subreddit_report(brain_db, projects, newly_promoted),
        now=datetime.now(UTC),
        policy=policy,
    )
    sent, total = await telegram.notify_long(report)

    # The status row is how "did it arrive?" gets answered later, so it
    # records delivery, not just that the cron fired.
    counts = (
        f"{len(evidence)} items, "
        f"{sum(len(v) for v in picks_by_project.values())} reddit picks, "
        f"{len(newly_promoted)} promoted, synthesis {'ok' if synthesis else 'off'}"
    )
    status = _delivery_status(sent, total, counts)
    _record_status(brain_db, schedule_id, status)

    if sent < total:
        logger.warning("morning_digest: %s; not marking seen", status)
        return

    _mark_digest_delivered(brain_db, picks_by_project, emerging_picks_by_project)
    logger.info("morning_digest: %s", status)


async def _collect_digest_candidates(
    brain_db,
    projects: list[dict],
    scan_subreddits: list[str],
    project_sub_sets: dict[str, set[str]],
    health: reddit_search.SearchHealth,
) -> tuple[dict[str, tuple[dict, RedditPost, float, bool]], dict[str, list[int]]]:
    """Search each project's queries and route every unseen post to the
    project it fits best. Returns that best-fit map (post id to project,
    post, relevance, sub affinity) and the known scores per subreddit."""
    # Track all post scores per subreddit so we can compute adaptive
    # popularity thresholds (popularity_multiplier × median).
    scores_by_sub: dict[str, list[int]] = {}

    # Per-post-id best-fit map: rel + tiny sub-affinity bump for tiebreaks
    # so a sub-affinity-only post lands in the project that subscribes to
    # its subreddit, not one that doesn't.
    best_for_post: dict[str, tuple[dict, RedditPost, float, bool]] = {}

    for project in projects:
        slug = project["slug"]
        topics = project_list(project, "topics")
        terms = project_list(project, "search_terms")
        # Cap query fan-out: pick top 3 search terms (or fall back to
        # topics) so we don't hammer the Reddit API on every boot.
        queries = (terms or topics or [slug])[:3]

        for query in queries:
            try:
                posts = await reddit_search.search_subreddits(
                    query,
                    scan_subreddits,
                    since="24h",
                    sort="new",
                    limit_per_sub=15,
                    health=health,
                    # One health spans the run, so feeds are fetched once
                    # per subreddit and paced when JSON is refused.
                    feed_fallback=True,
                )
            except Exception as exc:
                logger.warning(
                    "morning_digest: search failed for %s/%s: %s", slug, query, exc
                )
                continue

            for post in posts:
                # Feed posts have no score; a median of zeros would make
                # every one of them a "popular" bonus pick.
                if post.engagement_known:
                    scores_by_sub.setdefault(post.subreddit.lower(), []).append(
                        post.score
                    )
                if brain_db.is_seen("reddit_post", post.id, slug):
                    continue
                rel = _score_relevance(post, topics, terms)
                sub_affinity = post.subreddit.lower() in project_sub_sets[slug]
                # Skip posts with no signal at all (no keyword match and
                # not in this project's subreddit list).
                if rel <= 0 and not sub_affinity:
                    continue
                # Routing priority: rel dominates, sub-affinity is a small
                # tiebreak so e.g. an r/Construction post lands at the
                # project subscribed to r/Construction, not at another one
                # with the same rel == 0.
                priority = rel + (0.1 if sub_affinity else 0)
                prior = best_for_post.get(post.id)
                prior_priority = prior[2] + (0.1 if prior[3] else 0) if prior else -1.0
                if priority > prior_priority:
                    best_for_post[post.id] = (project, post, rel, sub_affinity)
    return best_for_post, scores_by_sub


def _tier_digest_picks(
    projects: list[dict],
    best_for_post: dict[str, tuple[dict, RedditPost, float, bool]],
    scores_by_sub: dict[str, list[int]],
    top_per_project: int,
    popularity_multiplier: float,
    popular_bonus_cap: int,
) -> dict[str, list[tuple[RedditPost, bool]]]:
    """Each project's strict picks then its popular-bonus picks, as
    (post, is_bonus) pairs; is_bonus drives the ★ render."""
    # Adaptive popularity thresholds per subreddit. Need at least 3
    # samples for a stable median; otherwise no bonus picks from that sub.
    sub_thresholds: dict[str, float] = {
        sub: popularity_multiplier * statistics.median(scores)
        for sub, scores in scores_by_sub.items()
        if len(scores) >= 3
    }

    # Group by project, separate strict (rel > 0) from popular-bonus
    # (rel == 0, sub-affinity, score >= adaptive threshold).
    strict_by_project: dict[str, list[tuple[RedditPost, float]]] = {}
    bonus_by_project: dict[str, list[tuple[RedditPost, float]]] = {}
    for project, post, rel, sub_affinity in best_for_post.values():
        slug = project["slug"]
        if rel > 0:
            strict_by_project.setdefault(slug, []).append((post, rel))
        elif sub_affinity:
            threshold = sub_thresholds.get(post.subreddit.lower())
            if threshold is not None and post.score >= threshold:
                bonus_by_project.setdefault(slug, []).append((post, rel))

    # Final picks per project: top_per_project strict, then up to
    # popular_bonus_cap bonus picks.
    picks_by_project: dict[str, list[tuple[RedditPost, bool]]] = {}
    for project in projects:
        slug = project["slug"]
        strict = strict_by_project.get(slug, [])
        strict.sort(key=_engagement_rank, reverse=True)
        bonus = bonus_by_project.get(slug, [])
        bonus.sort(key=lambda pr: pr[0].score, reverse=True)
        chosen = [(p, False) for p, _ in strict[:top_per_project]]
        chosen += [(p, True) for p, _ in bonus[:popular_bonus_cap]]
        if chosen:
            picks_by_project[slug] = chosen
    return picks_by_project


def _mark_digest_delivered(
    brain_db,
    picks_by_project: dict[str, list[tuple[RedditPost, bool]]],
    emerging_picks_by_project: dict[str, list[RedditPost]],
) -> None:
    """After a full delivery: never resurface these posts, and count their
    subreddits' hits toward curation and auto-promotion."""
    for slug, picks in picks_by_project.items():
        for post, _is_bonus in picks:
            brain_db.mark_seen("reddit_post", post.id, slug)
            brain_db.record_subreddit_hit(post.subreddit, slug)
    for slug, emerging_posts in emerging_picks_by_project.items():
        for post in emerging_posts:
            brain_db.mark_seen("reddit_post", post.id, slug)
            brain_db.record_subreddit_search(post.subreddit, slug, is_curated=False)
            brain_db.record_subreddit_hit(post.subreddit, slug)


async def _discover_emerging(
    brain_db,
    projects: list[dict],
    project_sub_sets: dict[str, set[str]],
    cap_per_project: int = 2,
    health: reddit_search.SearchHealth | None = None,
) -> dict[str, list[RedditPost]]:
    """Search /r/all for each project's first term; return posts in
    non-curated subreddits that pass the project's strict relevance
    filter, capped per project. Records nothing: the caller records the
    stat hits and marks the picks seen only after delivery, so a re-run
    never re-counts a post toward auto-promotion.
    """

    out: dict[str, list[RedditPost]] = {}
    for project in projects:
        slug = project["slug"]
        terms = project_list(project, "search_terms")
        topics = project_list(project, "topics")
        if not terms and not topics:
            continue
        query = (terms or topics)[0]
        try:
            all_posts = await reddit_search.search_all(
                query, since="24h", limit=15, health=health
            )
        except Exception as exc:
            logger.warning(
                "morning_digest: discovery search failed for %s/%s: %s",
                slug,
                query,
                exc,
            )
            continue

        curated = project_sub_sets.get(slug, set())
        emerging: list[tuple[RedditPost, float]] = []
        for post in all_posts:
            sub_lower = post.subreddit.lower()
            if not sub_lower or sub_lower in curated:
                continue
            rel = _score_relevance(post, topics, terms)
            if rel <= 0:
                continue
            # Skip posts already surfaced in earlier digests.
            if brain_db.is_seen("reddit_post", post.id, slug):
                continue
            emerging.append((post, rel))

        if emerging:
            emerging.sort(key=_engagement_rank, reverse=True)
            out[slug] = [p for p, _ in emerging[:cap_per_project]]
    return out


def _promote_emerging_subreddits(
    brain_db,
    projects: list[dict],
    min_hits: int = 5,
    window_days: int = 14,
) -> list[tuple[str, str]]:
    """Auto-promote non-curated subs that crossed the (≥min_hits in
    window_days) threshold. Mutates project.subreddits via
    add_project_subreddit and stamps the stats row promoted_at.
    Returns list of (project_slug, subreddit) pairs newly promoted."""
    promoted: list[tuple[str, str]] = []
    for project in projects:
        slug = project["slug"]
        candidates = brain_db.find_promotion_candidates(
            slug, min_hits=min_hits, window_days=window_days
        )
        for c in candidates:
            sub = c["subreddit"]
            if brain_db.add_project_subreddit(slug, sub):
                brain_db.mark_subreddit_promoted(sub, slug)
                promoted.append((slug, sub))
                logger.info(
                    "morning_digest: auto-promoted r/%s to %s subreddits "
                    "(%d hits since %s)",
                    sub,
                    slug,
                    c["hits"],
                    c.get("first_hit_at", "?"),
                )
    return promoted


def _render_subreddit_report(
    brain_db,
    projects: list[dict],
    newly_promoted: list[tuple[str, str]],
) -> str:
    """One-paragraph report on subreddit coverage: how many subs the
    digest searches across all projects, how many produced hits this
    cycle, top yields, and any newly auto-promoted (project, sub)
    pairs. Renders at the bottom of the morning_digest message."""
    all_subs: set[str] = set()
    for p in projects:
        for s in project_list(p, "subreddits"):
            all_subs.add(s.lower())
    for s in digest_extra_subreddits():
        all_subs.add(s.lower())

    # Top yields across all projects (limit 5)
    top_rows: list[dict] = []
    for p in projects:
        rows = brain_db.list_subreddit_stats(project_slug=p["slug"], limit=20)
        for r in rows:
            if r["hits"] > 0:
                top_rows.append({**r, "project_slug": p["slug"]})
    top_rows.sort(key=lambda r: r["hits"], reverse=True)
    top_yields = top_rows[:5]

    parts = [
        "*Subreddit coverage report*",
        f"Tracking {len(all_subs)} subreddits across {len(projects)} projects.",
    ]
    if top_yields:
        yields_str = ", ".join(
            f"r/{r['subreddit']} → {r['project_slug']} ({r['hits']} hits)"
            for r in top_yields
        )
        parts.append(f"Top yield: {yields_str}.")
    if newly_promoted:
        promoted_str = ", ".join(f"r/{sub} → {slug}" for slug, sub in newly_promoted)
        parts.append(
            f"🆕 Newly promoted to curated: {promoted_str}. "
            f"These subs hit ≥5 relevant posts in the last 14 days "
            f"and are now part of the project's standing scan list."
        )
    else:
        parts.append(
            "No new subs promoted this cycle (auto-promote bar: "
            "≥5 relevant hits in 14d)."
        )
    return " ".join(parts)


async def hot_pulse_generator(
    brain_db,
    project_slugs: list[str] | None = None,
    extra_subreddits: list[str] | None = None,
    max_per_project: int = 25,
    window: str = "15m",
    min_relevance: float = 1.0,
    min_delivery_interval_minutes: int = 15,
    quiet_hours_start: int = 0,
    quiet_hours_end: int = 5,
    quiet_hours_tz: str | None = None,
    schedule_id: str = "hot_pulse",
    **kwargs,
) -> None:
    """Reddit pulse for brand-new comment candidates.

    Scheduled `*/5 * * * *` for cron heartbeat, but Telegram delivery
    is throttled to once per `min_delivery_interval_minutes` (default
    15min). On throttled fires the function skips Reddit + Telegram
    entirely, just updates the schedule's last_run/last_status so the
    operator can verify the cron is alive. Cuts Reddit API load by 3x
    and reduces chat noise to one message per 15min.

    Per project, searches the union of that project's subreddits +
    the AI-coding extras for posts created in the last `window`
    (default 15m). High-precision filter: a post must literally match
    a project topic (rel 1.0) or search_term (rel 1.5) in title or
    snippet — `min_relevance` (default 1.0) is the floor. The 15min
    upvote sample is too small to fall back on popularity, so the
    operator's stated rule applies: surface only the highly relevant,
    let the 24h morning_digest's popular-bonus tier handle the
    less-relevant-but-popular case.

    `max_per_project` defaults to 25 — effectively uncapped for any
    realistic window. The operator wants every relevant post
    surfaced, not a top-3 truncation; on the rare cycle where many
    posts match, the cap prevents a runaway message.

    Dedup is via scan_cache `kind="reddit_pulse"` — a separate
    namespace from the morning_digest's `kind="reddit_post"`. That
    means a post seen by the pulse will still be eligible for the
    next morning_digest. Within the pulse's own stream, dedup also
    means a post caught at fire N (still 5min old) won't re-report
    at fires N+1 (10min old) and N+2 (15min old) — each post lands
    in Telegram exactly once.

    The Telegram message uses the same 3-line per-pick rendering as
    the morning_digest. No comment-draft tasks are queued; the
    direct Reddit URL is enough for the operator to click through
    and write their own reply.

    Skips Telegram delivery silently when no fresh picks exist
    (logged as a heartbeat WARNING so the cron is still observable).
    """
    global _LAST_HOT_PULSE_DELIVERY_AT, _HOT_PULSE_IN_QUIET_HOURS

    now = datetime.now(UTC)

    # Quiet-hours gate: skip Reddit + Telegram entirely when the
    # operator is asleep: default 00:00-05:00 in the operator timezone
    # (assistant.timezone / SCHEDULER_TZ) unless the schedule passes
    # quiet_hours_tz. Status row is written
    # exactly once on the boundary fire entering quiet hours; later
    # fires inside the window are silent to avoid 60 status writes
    # per night. On the first non-quiet fire after the window the
    # flag clears and the normal flow resumes (and naturally rewrites
    # last_status with "ok"/"throttled" as part of delivery).
    quiet_hours_tz = quiet_hours_tz or tenant_profile.operator_timezone()
    in_quiet = _is_quiet_hours(now, quiet_hours_start, quiet_hours_end, quiet_hours_tz)
    if in_quiet:
        if not _HOT_PULSE_IN_QUIET_HOURS:
            local_now = now.astimezone(ZoneInfo(quiet_hours_tz)).strftime("%H:%M")
            _record_status(
                brain_db,
                schedule_id,
                f"quiet hours (entered at {local_now} {quiet_hours_tz}, "
                f"resume {quiet_hours_end:02d}:00)",
                when=now,
            )
            _HOT_PULSE_IN_QUIET_HOURS = True
            logger.info(
                "hot_pulse: entering quiet hours (%02d:00-%02d:00 %s), "
                "suppressing delivery until window ends",
                quiet_hours_start,
                quiet_hours_end,
                quiet_hours_tz,
            )
        return
    if _HOT_PULSE_IN_QUIET_HOURS:
        logger.info("hot_pulse: exiting quiet hours, resuming normal delivery")
        _HOT_PULSE_IN_QUIET_HOURS = False

    # Delivery throttle: the cron fires every 5min for heartbeat, but
    # Telegram delivery is gated to once per min_delivery_interval_minutes.
    # On throttled fires we skip Reddit + Telegram and just update
    # schedule status so the operator sees the cron is still alive.
    if _LAST_HOT_PULSE_DELIVERY_AT is not None:
        elapsed_min = (now - _LAST_HOT_PULSE_DELIVERY_AT).total_seconds() / 60.0
        if elapsed_min < min_delivery_interval_minutes:
            _record_status(
                brain_db,
                schedule_id,
                f"throttled ({elapsed_min:.1f}m of {min_delivery_interval_minutes}m)",
                when=now,
            )
            logger.info(
                "hot_pulse: throttled — %.1fm since last delivery (min interval %dm)",
                elapsed_min,
                min_delivery_interval_minutes,
            )
            return

    plan = _scan_plan(brain_db, project_slugs, extra_subreddits, "hot_pulse")
    if plan is None:
        return
    projects, scan_subreddits, project_sub_sets = plan

    # One health spans the fire so an outage is visible in the status row
    # instead of reading as a quiet "0 picks" cycle.
    health = reddit_search.SearchHealth()
    best_for_post = await _collect_pulse_candidates(
        brain_db, projects, scan_subreddits, health, window, min_relevance
    )
    by_project, by_project_by_category = _cap_pulse_picks(
        best_for_post, max_per_project
    )

    n_picks = sum(len(v) for v in by_project.values())
    outage = health.requests > 0 and health.ok == 0
    health_note = health.summary() if health.requests else "no reddit requests"

    # Update the schedule's last_run/last_status so the operator can
    # see the cron is firing even on silent cycles. APScheduler-driven
    # fires bypass run_schedule_now (which is what normally writes
    # those fields), so without this the schedule looks stale and the
    # operator can't distinguish "scheduler dead" from "no fresh hits".
    _record_status(
        brain_db,
        schedule_id,
        f"reddit outage, 0 picks ({health_note})"
        if outage
        else f"ok ({n_picks} picks; {health_note})",
    )

    if not any(by_project.values()):
        # Quiet pulse — no Telegram noise. Logged at WARNING level
        # (rather than INFO) so it's visible without changing the
        # default root logger config; this is the operator's only
        # visible signal that the */5 cron actually fired on cycles
        # that produced no picks.
        logger.warning(
            "hot_pulse: heartbeat — no fresh comment-worthy posts in "
            "last %s (scanned %d subs across %d projects)",
            window,
            len(scan_subreddits),
            len(projects),
        )
        return

    pulse = _render_pulse(projects, by_project_by_category, window)
    # Split at the 4096-char limit: an oversized message is rejected
    # whole by Telegram and every pick in it would be lost.
    sent, total = await telegram.notify_long(pulse)
    if sent < total:
        logger.warning(
            "hot_pulse: telegram delivered %d/%d msgs; not marking seen", sent, total
        )
        _record_status(
            brain_db, schedule_id, _delivery_status(sent, total, f"{n_picks} picks")
        )
        return

    # Cross-project mark_seen: a delivered post is marked seen for ALL
    # tracked projects, not just the one that owned it this fire. Plugs
    # the cross-fire dedup leak where the same post otherwise pingpongs
    # between projects on consecutive fires (loser project's
    # is_seen(post, slug) check returns False because only the owner
    # was marked).
    all_slugs = [p["slug"] for p in projects]
    for slug, picks in by_project.items():
        for post, _rel in picks:
            for s in all_slugs:
                brain_db.mark_seen("reddit_pulse", post.id, s)
            brain_db.record_subreddit_hit(post.subreddit, slug)

    _LAST_HOT_PULSE_DELIVERY_AT = now
    logger.info(
        "hot_pulse: delivered %d fresh posts across %d projects",
        sum(len(v) for v in by_project.values()),
        sum(1 for v in by_project.values() if v),
    )


async def _collect_pulse_candidates(
    brain_db,
    projects: list[dict],
    scan_subreddits: list[str],
    health: reddit_search.SearchHealth,
    window: str,
    min_relevance: float,
) -> dict[str, tuple[dict, RedditPost, float, str]]:
    """Unseen posts in the window that clear `min_relevance`, each routed
    to its best-matching project.

    Each entry tracks the project that claimed the post, the post object,
    the literal-match relevance (>= 1.0 for keyword hits, 0.5 for
    Reddit-search-only baseline), and the search query that brought the
    post back. The query becomes the "category bucket" the render groups by.
    """
    best_for_post: dict[str, tuple[dict, RedditPost, float, str]] = {}
    for project in projects:
        slug = project["slug"]
        topics = project_list(project, "topics")
        terms = project_list(project, "search_terms")
        queries = (terms or topics or [slug])[:3]

        for query in queries:
            try:
                posts = await reddit_search.search_subreddits(
                    query,
                    scan_subreddits,
                    since=window,
                    sort="new",
                    limit_per_sub=10,
                    health=health,
                    feed_fallback=True,
                )
            except Exception as exc:
                logger.warning(
                    "hot_pulse: search failed for %s/%s: %s", slug, query, exc
                )
                continue

            for post in posts:
                if brain_db.is_seen("reddit_pulse", post.id, slug):
                    continue
                rel = _score_relevance(post, topics, terms)
                if rel < min_relevance:
                    continue
                prior = best_for_post.get(post.id)
                if prior is None or rel > prior[2]:
                    best_for_post[post.id] = (project, post, rel, query)
    return best_for_post


def _cap_pulse_picks(
    best_for_post: dict[str, tuple[dict, RedditPost, float, str]],
    max_per_project: int,
) -> tuple[
    dict[str, list[tuple[RedditPost, float]]],
    dict[str, dict[str, list[tuple[RedditPost, float]]]],
]:
    """The capped picks per project, and the same picks grouped by category.

    Cap first, then render and mark seen from the same capped picks: a
    post that was cut must stay unseen so a later fire can surface it.
    """
    by_project: dict[str, list[tuple[RedditPost, float]]] = {}
    for project, post, rel, query in best_for_post.values():
        by_project.setdefault(project["slug"], []).append((post, rel))
    for slug in list(by_project.keys()):
        by_project[slug].sort(key=_engagement_rank, reverse=True)
        by_project[slug] = by_project[slug][:max_per_project]

    # Group by project, then by category (the search query that
    # brought the post). Each project block in the digest renders one
    # subsection per category so the operator sees which area is
    # currently active.
    kept_ids = {
        slug: {post.id for post, _ in picks} for slug, picks in by_project.items()
    }
    by_project_by_category: dict[str, dict[str, list[tuple[RedditPost, float]]]] = {}
    for project, post, rel, query in best_for_post.values():
        if post.id in kept_ids.get(project["slug"], set()):
            by_project_by_category.setdefault(project["slug"], {}).setdefault(
                query, []
            ).append((post, rel))
    return by_project, by_project_by_category


def _render_pulse(
    projects: list[dict],
    by_project_by_category: dict[str, dict[str, list[tuple[RedditPost, float]]]],
    window: str,
) -> str:
    """The pulse message, with category buckets per project.

    Within each project block, picks subdivide by the search query that
    brought them back. Each pick is also tagged 📌 (literal phrase match,
    rel >= 1.0) or ◇ (semantic-only match via Reddit's search ranking,
    rel == 0.5) so the operator can tell precision tiers at a glance.
    """
    lines = [f"*Hot pulse — fresh posts (last {window})*", ""]
    for project in projects:
        slug = project["slug"]
        cat_map = by_project_by_category.get(slug, {})
        if not cat_map:
            continue
        topics = project_list(project, "topics")
        terms = project_list(project, "search_terms")
        lines.append(f"*{slug}* ({project['owner']}/{project['repo']})")
        # Sort categories by total pick count desc (most active first).
        sorted_cats = sorted(cat_map.items(), key=lambda kv: len(kv[1]), reverse=True)
        for category, cat_picks in sorted_cats:
            cat_picks_sorted = sorted(cat_picks, key=_engagement_rank, reverse=True)
            lines.append(f'  Category: "{category}"')
            for post, rel in cat_picks_sorted:
                tier = "📌" if rel >= 1.0 else "◇"
                pick_lines = _render_pick_lines(post, topics, terms, is_bonus=False)
                # Replace the bullet with our tier marker on line 0,
                # leave the facts/why lines alone.
                pick_lines[0] = pick_lines[0].replace("  • ", f"  • {tier} ", 1)
                lines.extend(pick_lines)
        lines.append("")
    return "\n".join(lines).strip()


# ─── Profile-Driven Seeds ──────────────────────────────────────────
#
# Seeds (tracked projects + baseline schedules) now live in the active
# tenant profile (config/profiles/<name>/profile.yaml), not in code. The
# marketing profile reproduces the original hardcoded set exactly
# (regression-locked by tests/test_profile.py). seed_from_profile is the
# entry point; the app lifespan passes the active profile
# (CLAWRANGE_PROFILE, default "starter", which seeds nothing).


def _seed_schedules_from_profile(brain_db, profile) -> list[dict]:
    """Idempotently register the profile's baseline schedules.

    Only inserts when the schedule_id is missing so hand-edits to cron,
    kwargs, or paused state via the /sched API survive reboots.
    """
    out: list[dict] = []
    for spec in profile.schedules:
        existing = brain_db.get_schedule(spec["id"])
        if existing is None:
            row = brain_db.upsert_schedule(
                spec["id"],
                spec["name"],
                spec["kind"],
                spec["cron"],
                spec.get("kwargs"),
            )
            out.append(row)
            logger.info("seed_schedules: created %s", spec["id"])
        else:
            out.append(existing)
    return out


def seed_from_profile(brain_db, profile) -> list[dict]:
    """Idempotently seed projects + schedules from a tenant profile.

    Safe to call on every boot; upsert_project uses ON CONFLICT to preserve
    hand-edits to topics/subreddits/posture. Also registers the profile's
    baseline schedules so a fresh deploy gets its standing jobs without
    manual /sched setup.
    """
    out: list[dict] = []
    for spec in profile.projects:
        existing = brain_db.get_project(spec["slug"])
        if existing is None:
            row = brain_db.upsert_project(
                spec["slug"],
                spec["owner"],
                spec["repo"],
                topics=spec.get("topics", []),
                subreddits=spec.get("subreddits", []),
                search_terms=spec.get("search_terms", []),
                posture=spec.get("posture", ""),
            )
            out.append(row)
            logger.info("seed_from_profile: created project %s", spec["slug"])
        else:
            out.append(existing)
    _seed_schedules_from_profile(brain_db, profile)

    # Seed approved persona learnings from the profile's learned.yaml overlay
    # (the git-portable projection of the brain's approved meta-learnings).
    try:
        pl.seed_overlay(brain_db, profile.name, pl.load_overlay(profile.name))
    except Exception as exc:  # never crash boot on a bad overlay
        logger.warning("seed_from_profile: learned overlay skipped: %s", exc)

    return out


# ─── CRM generators (lead-crm profile) ───────────────────────────────


_STATUS_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _redact_urls(text: str) -> str:
    """Drop URLs from text bound for a stored schedule status.

    httpx errors embed the full request URL, and connector URLs can carry
    tokens in the query string; /sched readers must not see them.
    """
    return _STATUS_URL_RE.sub("<url>", text)


def _crm_for(profile, crm):
    """Resolve a CRM adapter: prefer the injected one, else build from profile.

    Returns ``None`` (graceful) when the profile defines no CRM, mirroring
    the marketing scanners — a missing backend disables the feature rather
    than crashing the heartbeat.
    """
    if crm is not None:
        return crm
    if not (profile and profile.crm):
        logger.warning("crm generator: profile has no crm configured; skipping")
        return None

    adapter = get_adapter(profile.crm)
    adapter.init()
    return adapter


async def pipeline_generator(
    brain_db,
    connector: str,
    profile=None,
    crm=None,
    schedule_id: str | None = None,
    http_client=None,
    **kwargs,
):
    """Run a profile connector (source->transform->sink) into the CRM (FR-6.1).

    Loads the named connector from the active profile, runs it against the
    CRM adapter, records the schedule status with counts, and (when rows
    were written) posts a one-line Telegram summary. Never auto-crashes the
    heartbeat: unknown connectors / fetch errors degrade to a logged status.
    """

    profile = profile or tenant_profile.load_profile()
    spec = profile.connector(connector)
    now = datetime.now(UTC).isoformat()

    if spec is None:
        logger.warning("pipeline: connector %r not defined in profile", connector)
        if schedule_id:
            brain_db.update_schedule_status(
                schedule_id, now, f"error (unknown connector {connector})"
            )
        return None

    adapter = _crm_for(profile, crm)
    if adapter is None:
        if schedule_id:
            brain_db.update_schedule_status(schedule_id, now, "error (no crm)")
        return None

    try:
        # Sync HTTP + SQLite work: keep it off the event loop.
        counts = await asyncio.to_thread(
            run_connector, spec, adapter, http_client=http_client
        )
    except Exception as exc:
        logger.warning("pipeline: connector %s failed: %s", connector, exc)
        if schedule_id:
            brain_db.update_schedule_status(
                schedule_id, now, f"error ({_redact_urls(str(exc))})"
            )
        return None

    delivery = ""
    if counts["written"]:
        sent, total = await telegram.notify_long(
            f"Lead sync ({connector}): {counts['written']} written "
            f"({counts['fetched']} fetched, {counts['kept']} kept)."
        )
        if sent < total:
            delivery = f"; telegram FAILED {sent}/{total} msgs"
            logger.warning("pipeline: telegram summary delivered %d/%d", sent, total)
    else:
        logger.info("pipeline: %s wrote 0 rows; no telegram summary", connector)

    # Recorded after delivery so the status says whether the summary landed.
    if schedule_id:
        brain_db.update_schedule_status(
            schedule_id, now, f"ok ({counts['written']} written{delivery})"
        )
    return counts


async def crm_digest_generator(
    brain_db,
    queries: list[str] | None = None,
    profile=None,
    crm=None,
    schedule_id: str | None = None,
    **kwargs,
):
    """Run named query templates and deliver a Telegram digest (FR-6.2).

    Each entry in ``queries`` names a ``crm.query_templates`` template; the
    digest renders each result with the same deterministic formatter the
    NL ``answer`` path uses. Unknown templates and per-query errors are
    reported inline rather than raising.
    """
    from crm.query import _format_rows, find_template, run_query

    profile = profile or tenant_profile.load_profile()
    queries = queries or []
    now = datetime.now(UTC).isoformat()

    adapter = _crm_for(profile, crm)
    if adapter is None:
        if schedule_id:
            brain_db.update_schedule_status(schedule_id, now, "error (no crm)")
        return None

    templates = profile.query_templates()
    lines = ["*CRM digest*", ""]
    for qname in queries:
        template = find_template(templates, qname)
        if template is None:
            lines.append(f"{qname}: (unknown template)")
            continue
        try:
            rows = run_query(adapter, template, {})
            lines.append(_format_rows(template, rows))
        except Exception as exc:
            logger.warning("crm_digest: query %s failed: %s", qname, exc)
            lines.append(f"{qname}: error ({exc})")

    digest = "\n".join(lines).strip()

    sent, total = await telegram.notify_long(digest)
    if schedule_id:
        status = _delivery_status(sent, total, f"{len(queries)} queries")
        brain_db.update_schedule_status(schedule_id, now, status)
    return digest


async def persona_reflect_generator(brain_db, profile_name=None, **kwargs) -> None:
    """Review recent activity and queue persona-enhancement proposals.

    Routes the reflection prompt through the LLM proxy and posts any
    suggestion as a [DRAFT] persona proposal under the active profile.
    Only the reply's first non-empty line is used (models often add a
    justification line). Degrades to a no-op if the proxy is unavailable
    or the suggestion fails validation.
    """

    if not profile_name:
        active = tenant_profile.active_profile()
        # Same name active_profile() resolves, even when its file won't load.
        profile_name = (
            active.name if active else os.environ.get("CLAWRANGE_PROFILE") or "starter"
        )
    prompt = (
        "From recent operator interactions, suggest at most ONE concrete "
        "persona adjustment (tone/format/priority). Reply with a single "
        "imperative sentence, or 'none'."
    )
    reply = await llm_proxy._llm_call(prompt, max_tokens=80)
    lines = [ln.strip() for ln in (reply or "").splitlines() if ln.strip()]
    suggestion = lines[0] if lines else ""
    if not suggestion or suggestion.lower().startswith("none"):
        return
    if brain_db is None:
        from app import brain_db as _bd

        brain_db = _bd
    try:
        pl.propose(
            brain_db, profile_name, "persona", "Reflection", suggestion, "reflect"
        )
    except ValueError as exc:
        logger.warning("persona_reflect: suggestion skipped: %s", exc)


def _hours_since(iso_ts: str | None) -> float:
    """Hours elapsed since an ISO-8601 timestamp (``inf`` if missing/bad)."""
    if not iso_ts:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


async def research_pulse_generator(
    brain_db,
    topics: list[str] | None = None,
    stale_hours: int = 24,
    **kwargs,
) -> None:
    """Queue a heavy-research task when research has gone stale.

    Research only runs via ``POST /research`` or the tome bridge; the
    restricted LLM personas have no tool that can issue a POST, so a
    research task queued for the agent hits a dead end. This generator
    closes that gap from the scheduler's Python process (no LLM needed):
    when no research session has run within ``stale_hours`` it enqueues a
    ``research:tome: <topic>`` task. ``scripts/tome_bridge.py`` already
    matches that prefix, runs it through the local ``/tome:research``
    session, and posts results back -- no agent POST capability required.

    Topic selection: an explicit ``topics`` list (from the schedule's
    kwargs) wins; otherwise the tracked projects' topic hints are used.
    Topics with a pending or active task are skipped; of the rest, the one
    queued least recently (any status, never-queued first) is chosen, so
    the whole list rotates and no topic is double-queued.
    Never auto-posts -- it only enqueues a research task for review.
    """
    recent = brain_db.list_research_sessions(limit=1)
    if recent and _hours_since(recent[0].get("created_at")) < stale_hours:
        logger.info("research_pulse: research is fresh, skipping")
        return

    candidates: list[str] = [t.strip() for t in (topics or []) if t and t.strip()]
    if not candidates:
        for project in brain_db.list_projects():
            for topic in project_list(project, "topics"):
                if topic and topic.strip():
                    candidates.append(topic.strip())
    if not candidates:
        logger.info("research_pulse: no topic configured, skipping")
        return

    # Every status counts for "when was it last queued"; only pending and
    # active tasks block a topic. Deduping against pending alone requeued
    # topic 1 as soon as the bridge claimed it.
    open_descs: set[str] = set()
    last_queued: dict[str, str] = {}
    for task in brain_db.list_tasks():
        desc = task["description"]
        if task["status"] in ("pending", "active"):
            open_descs.add(desc)
        last_queued[desc] = max(last_queued.get(desc, ""), task["created_at"] or "")

    descs = [f"research:tome: {t}" for t in dict.fromkeys(candidates)]
    eligible = [d for d in descs if d not in open_descs]
    if not eligible:
        logger.info("research_pulse: all candidate topics already queued, skipping")
        return
    # Never-queued topics ("") first, then least recently queued; ties keep
    # the configured order (min is stable).
    desc = min(eligible, key=lambda d: last_queued.get(d, ""))
    brain_db.create_task(desc, priority=3, source="schedule")
    logger.info("research_pulse: enqueued '%s'", desc)


INCOME_REVIEW_DESC = "[DRAFT] income: weekly strategy review"

# Neutral default; a tenant supplies its own via the profile's
# `income_review.checklist` block.
_INCOME_REVIEW_CHECKLIST = (
    "Cover: balances and P&L against the benchmarks the strategy doc "
    "defines, decisions taken this week and their results, fees paid, and "
    "proposed adjustments with cited sources (single-source claims "
    "flagged). This task is a draft for operator approval: never execute "
    "trades."
)


def _income_review_checklist(profile_name: str | None = None) -> str:
    """The profile's `income_review.checklist`, else the generic default."""
    try:
        block = tenant_profile.load_profile(profile_name).raw.get("income_review")
    except Exception as exc:
        logger.warning("income_review: profile checklist unavailable: %s", exc)
        return _INCOME_REVIEW_CHECKLIST
    text = (block or {}).get("checklist")
    return (
        text.strip()
        if isinstance(text, str) and text.strip()
        else (_INCOME_REVIEW_CHECKLIST)
    )


async def income_review_generator(
    brain_db, profile_name: str | None = None, **kwargs
) -> None:
    """Enqueue the weekly strategy review as a [DRAFT] task.

    Mirrors research_pulse: idempotent (skips while a review is still
    pending), enqueue-first so a Telegram outage never loses the task,
    and never acts on its own — the review is a draft the operator
    approves on Telegram. The protocol is the profile's own doc, e.g.
    config/profiles/marketing/income-strategy.md.
    """

    pending = {t["description"] for t in brain_db.list_tasks(status="pending")}
    if any(d.startswith(INCOME_REVIEW_DESC) for d in pending):
        logger.info("income_review: review already queued, skipping")
        return

    brain_db.create_task(
        f"{INCOME_REVIEW_DESC} — {_income_review_checklist(profile_name)}",
        priority=2,
        source="schedule",
    )
    logger.info("income_review: enqueued weekly review")
    if not await telegram.notify(
        "📋 Weekly income review queued — claim the [DRAFT] task to run it."
    ):
        logger.warning("income_review: telegram notify failed (task kept)")


# ─── Homelab deals ───────────────────────────────────────────────────


async def homelab_deals_generator(
    brain_db,
    profile_name: str | None = None,
    schedule_id: str = "homelab_deals",
    **kwargs,
) -> None:
    """Daily homelab hardware deal rundown, delivered to Telegram.

    Reads targets and sources from the profile's `homelab_deals` block.
    Every matched, priced listing feeds the price history; deals are judged
    against history as it stood *before* today's batch, so a flood of
    listings cannot drag its own reference price. Like the outreach
    rundown it always sends, and records delivery for the boot catch-up.
    """
    block = tenant_profile.load_profile(profile_name).raw.get("homelab_deals") or {}
    targets = deals.load_targets(block)
    if not targets:
        logger.info("homelab_deals: profile has no targets, skipping")
        return

    listings, statuses = await deal_sources.gather(targets, block.get("sources") or {})
    # Medians skip observations today's rules reject (see history_stats).
    history = deals.history_stats(brain_db.deal_observations(), targets)

    found: list[deals.Deal] = []
    misses: list[tuple[deals.Listing, deals.WatchTarget]] = []
    leads: list[tuple[deals.Listing, deals.WatchTarget]] = []
    for listing in listings:
        target = deals.match_target(listing, targets)
        if target is None:
            continue
        key = f"{listing.source}:{listing.listing_id}"
        total = listing.total
        if total is None:
            if not brain_db.is_seen("deal", key):
                leads.append((listing, target))
            continue
        brain_db.record_deal_observation(
            listing.source, listing.listing_id, target.name, total, title=listing.title
        )
        deal = deals.classify(listing, target, history[target.name])
        if deal is None:
            misses.append((listing, target))
        elif not brain_db.is_seen("deal", key):
            found.append(deal)
    misses.sort(key=lambda lt: (lt[0].total or 0) / lt[1].great_price)

    clean, flagged = deals.order_and_number(
        found, block.get("max_per_target") or deals.MAX_PER_TARGET
    )
    shown = clean + flagged
    synthesis = await deals.synthesize(
        shown,
        block.get("current_setup", ""),
        block.get("stack_gaps"),
        block.get("advisor"),
    )
    report = deals.render_report(
        shown,
        misses,
        statuses,
        synthesis,
        datetime.now(UTC),
        leads=leads,
        more=deals.overflow_counts(found, shown),
        goal=block.get("goal", ""),
        title=block.get("title") or "Deal rundown",
    )
    sent, total_msgs = await telegram.notify_long(report)

    counts = (
        f"{len(listings)} listings, {len(clean)} deals, {len(flagged)} flagged, "
        f"{len(leads)} leads, synthesis {'ok' if synthesis else 'off'}"
    )
    status = _delivery_status(sent, total_msgs, counts)
    _record_status(brain_db, schedule_id, status)
    if sent < total_msgs:
        logger.warning("homelab_deals: %s; not marking seen", status)
        return
    for d in shown:
        brain_db.mark_seen("deal", f"{d.listing.source}:{d.listing.listing_id}")
    for listing, _t in leads[: deals.MAX_LEADS]:
        brain_db.mark_seen("deal", f"{listing.source}:{listing.listing_id}")
    logger.info("homelab_deals: %s", status)


# ─── Registry ────────────────────────────────────────────────────────

GENERATORS: dict[str, Callable[..., Any]] = {
    "morning_scan": morning_scan_generator,
    "morning_digest": morning_digest_generator,
    "hot_pulse": hot_pulse_generator,
    "weekly_traffic": weekly_traffic_generator,
    "awesome_lists_watch": awesome_lists_watch_generator,
    "custom_scan": custom_scan_generator,
    "content_idea": content_idea_generator,
    "comment_draft": comment_draft_generator,
    "pipeline": pipeline_generator,
    "crm_digest": crm_digest_generator,
    "persona_reflect": persona_reflect_generator,
    "research_pulse": research_pulse_generator,
    "income_review": income_review_generator,
    "homelab_deals": homelab_deals_generator,
}
