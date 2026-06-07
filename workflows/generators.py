"""Schedule generators for marketing orchestrator.

Each generator reads from the projects table and enqueues tasks
into the existing task queue via brain_db.create_task().
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from reddit_search import RedditPost

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


def _is_quiet_hours(
    now_utc: datetime, start_hour: int, end_hour: int, tz_name: str
) -> bool:
    """Return True when `now_utc` falls inside the half-open
    [start_hour, end_hour) window in the named tz.

    DST-aware via zoneinfo: passing ``America/Chicago`` produces the
    correct CST/CDT offset for any date so the operator's 12am-5am
    sleep window stays anchored to local clock time across the
    daylight-saving boundary.

    End hour is exclusive: a window of (0, 5) covers 00:00:00 through
    04:59:59 local, so the */5 cron fire at exactly 05:00 wakes the
    pulse back up. Wrap-around windows (e.g. 22 → 6) are supported.
    """
    local = now_utc.astimezone(ZoneInfo(tz_name))
    hour = local.hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


# Subreddits to include in every morning digest scan, even when no
# tracked project subscribes to them directly. Sourced from Alex's
# brief: AI-coding communities where comment-worthy posts about
# Claude Code, plugins, and self-hosted agent tooling appear.
MORNING_DIGEST_EXTRA_SUBREDDITS = (
    "vibecoding",
    "opensourceai",
    "claudecode",
    "ClaudeAI",
    "codex",
    "sideprojects",
)


async def morning_scan_generator(
    brain_db, project_slugs: list[str] | None = None, **kwargs
) -> None:
    """Generate Reddit + GitHub scan tasks for each tracked project."""
    projects = brain_db.list_projects()
    if project_slugs:
        projects = [p for p in projects if p["slug"] in project_slugs]

    for project in projects:
        topics = json.loads(project.get("topics", "[]"))
        subreddits = json.loads(project.get("subreddits", "[]"))
        search_terms = json.loads(project.get("search_terms", "[]"))
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


_AWESOME_LISTS = [
    ("ComposioHQ", "awesome-claude-plugins"),
    ("hesreallyhim", "awesome-claude-code"),
]


async def awesome_lists_watch_generator(
    brain_db, lists: list[str] | None = None, **kwargs
) -> None:
    """Check awesome-lists for tracked projects, enqueue PR tasks when missing."""
    from github_search import check_awesome_list

    projects = brain_db.list_projects()
    target_lists = _AWESOME_LISTS
    if lists:
        target_lists = [
            (lo, lr)
            for lo, lr in _AWESOME_LISTS
            if f"{lo}/{lr}" in lists or lr in lists
        ]

    for list_owner, list_repo in target_lists:
        target_urls = [f"github.com/{p['owner']}/{p['repo']}" for p in projects]
        found = await check_awesome_list(list_owner, list_repo, target_urls)

        for project in projects:
            url = f"github.com/{project['owner']}/{project['repo']}"
            if not found.get(url, False):
                posture = project.get("posture", "")
                desc = (
                    f"Awesome-list watch: {project['slug']} not yet listed in "
                    f"{list_owner}/{list_repo}. Draft a PR description and "
                    f"submission plan. Posture: {posture}"
                )
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

    The generated task is intended to be picked up by John-117 (or
    Alex) and elaborated into one of:
    - a technical blog post (lead),
    - a Reddit comment (engage),
    - or an X thread (broadcast).

    The generator never auto-posts. It only proposes; Alex approves.
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

    for project in projects:
        slug = project["slug"]
        posture = project.get("posture", "")
        topics = json.loads(project.get("topics", "[]"))
        topic_hint = ", ".join(topics[:3]) if topics else slug

        desc = (
            f"Content idea for {slug}: research on '{top['topic']}' "
            f'surfaced "{top["title"]}" ({top["url"]}). '
            f"Draft three angles - "
            f"(1) technical post tying this to {topic_hint}, "
            f"(2) useful Reddit/HN comment offering specifics, "
            f"(3) short X thread on the lesson. "
            f"Posture: {posture}"
        )
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
      the draft (e.g. mention claude-night-market when the post
      is about plugin discovery).

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
        f"hype. Never auto-post - this draft is for Alex to review and send "
        f"manually.{posture_clause}"
    )
    brain_db.create_task(desc, priority=2, source="schedule")
    logger.info("comment_draft: enqueued draft task for %s", post_url)


def _matched_keywords(
    post: "RedditPost", topics: list[str], terms: list[str]
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


def _comment_angle(post: "RedditPost") -> str:
    """One-line 'why we should comment' framing based on engagement.

    The pulse and the digest both surface the post's URL — this is
    just enough framing for Alex to decide whether to click through.
    No example replies; he writes his own."""
    if post.comments == 0:
        return "fresh thread, no replies yet — first useful answer wins visibility"
    if post.comments < 5:
        return f"{post.comments} replies — early, your comment lands near the top"
    if post.comments < 20:
        return f"{post.comments} replies — active discussion, still on-topic"
    return f"{post.comments} replies — late-stage but high reach"


def _render_pick_lines(
    post: "RedditPost",
    topics: list[str],
    terms: list[str],
    is_bonus: bool,
) -> list[str]:
    """Render one pick as a 3-line block: title-with-link / facts / why.

    The link is the literal post.url so it works as a tap target in
    Telegram clients regardless of Markdown rendering quirks."""
    title = post.title if len(post.title) <= 90 else post.title[:87] + "..."
    matches = _matched_keywords(post, topics, terms)
    if matches:
        match_text = "matched " + " + ".join(f'"{m}"' for m in matches)
    elif is_bonus:
        match_text = "sub-affinity bonus"
    else:
        match_text = "search match"
    star = "★ " if is_bonus else ""
    return [
        f"  • {star}[{title}]({post.url})",
        f"    r/{post.subreddit} · {post.score} pts · "
        f"{post.comments} comments · {match_text}",
        f"    Why: {_comment_angle(post)}",
    ]


def _score_relevance(post: "RedditPost", topics: list[str], terms: list[str]) -> float:
    """Keyword-overlap relevance: title + snippet vs project topics/terms.

    Topics weight 1.0, search_terms weight 1.5 (terms are higher-signal,
    they were curated as queries rather than tags). Multi-word phrases
    count as a single hit when the whole phrase appears.
    """
    haystack = (post.title + " " + (post.snippet or "")).lower()
    score = 0.0
    for t in topics:
        t_norm = t.lower().strip()
        if t_norm and t_norm in haystack:
            score += 1.0
    for t in terms:
        t_norm = t.lower().strip()
        if t_norm and t_norm in haystack:
            score += 1.5
    return score


async def morning_digest_generator(
    brain_db,
    project_slugs: list[str] | None = None,
    extra_subreddits: list[str] | None = None,
    top_per_project: int = 4,
    popularity_multiplier: float = 2.0,
    popular_bonus_cap: int = 2,
    **kwargs,
) -> None:
    """Deliver the 8am Reddit comment-candidate digest to Telegram.

    Two-tier picks per project:
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
    from reddit_search import search_subreddits
    from telegram import notify

    projects = brain_db.list_projects()
    if project_slugs:
        projects = [p for p in projects if p["slug"] in project_slugs]

    if not projects:
        logger.info("morning_digest: no tracked projects, skipping")
        return

    extras = (
        list(extra_subreddits)
        if extra_subreddits is not None
        else list(MORNING_DIGEST_EXTRA_SUBREDDITS)
    )

    # Union of every project's subreddit list plus the user's extras.
    sub_set: set[str] = set(extras)
    for p in projects:
        for s in json.loads(p.get("subreddits", "[]")):
            sub_set.add(s)
    scan_subreddits = sorted(sub_set)

    # Per-project subreddit affinity sets (lowercased for case-insensitive
    # match against post.subreddit).
    project_sub_sets: dict[str, set[str]] = {
        p["slug"]: {s.lower() for s in json.loads(p.get("subreddits", "[]"))}
        for p in projects
    }

    # Track all post scores per subreddit so we can compute adaptive
    # popularity thresholds (popularity_multiplier × median).
    scores_by_sub: dict[str, list[int]] = {}

    # Per-post-id best-fit map: rel + tiny sub-affinity bump for tiebreaks
    # so a sub-affinity-only post lands in the project that subscribes to
    # its subreddit, not one that doesn't.
    best_for_post: dict[str, tuple[dict, "RedditPost", float, bool]] = {}

    # Record one search impression per (sub, project) pair this cycle —
    # gives the stats table a denominator for hit-rate analysis.
    for project in projects:
        slug = project["slug"]
        for sub in scan_subreddits:
            is_curated = sub.lower() in project_sub_sets[slug]
            brain_db.record_subreddit_search(sub, slug, is_curated=is_curated)

    for project in projects:
        slug = project["slug"]
        topics = json.loads(project.get("topics", "[]"))
        terms = json.loads(project.get("search_terms", "[]"))
        # Cap query fan-out: pick top 3 search terms (or fall back to
        # topics) so we don't hammer the Reddit API on every boot.
        queries = terms[:3] if terms else (topics[:3] if topics else [slug])

        for query in queries:
            try:
                posts = await search_subreddits(
                    query,
                    scan_subreddits,
                    since="24h",
                    sort="new",
                    limit_per_sub=15,
                )
            except Exception as exc:
                logger.warning(
                    "morning_digest: search failed for %s/%s: %s", slug, query, exc
                )
                continue

            for post in posts:
                scores_by_sub.setdefault(post.subreddit.lower(), []).append(post.score)
                if brain_db.is_seen("reddit_post", post.id, slug):
                    continue
                rel = _score_relevance(post, topics, terms)
                sub_affinity = post.subreddit.lower() in project_sub_sets[slug]
                # Skip posts with no signal at all (no keyword match and
                # not in this project's subreddit list).
                if rel <= 0 and not sub_affinity:
                    continue
                # Routing priority: rel dominates, sub-affinity is a small
                # tiebreak so e.g. an r/Construction post lands at skrills
                # rather than at clawrange when both have rel == 0.
                priority = rel + (0.1 if sub_affinity else 0)
                prior = best_for_post.get(post.id)
                prior_priority = prior[2] + (0.1 if prior[3] else 0) if prior else -1.0
                if priority > prior_priority:
                    best_for_post[post.id] = (project, post, rel, sub_affinity)

    # Adaptive popularity thresholds per subreddit. Need at least 3
    # samples for a stable median; otherwise no bonus picks from that sub.
    sub_thresholds: dict[str, float] = {
        sub: popularity_multiplier * statistics.median(scores)
        for sub, scores in scores_by_sub.items()
        if len(scores) >= 3
    }

    # Group by project, separate strict (rel > 0) from popular-bonus
    # (rel == 0, sub-affinity, score >= adaptive threshold).
    strict_by_project: dict[str, list[tuple["RedditPost", float]]] = {}
    bonus_by_project: dict[str, list[tuple["RedditPost", float]]] = {}
    for project, post, rel, sub_affinity in best_for_post.values():
        slug = project["slug"]
        if rel > 0:
            strict_by_project.setdefault(slug, []).append((post, rel))
        elif sub_affinity:
            threshold = sub_thresholds.get(post.subreddit.lower())
            if threshold is not None and post.score >= threshold:
                bonus_by_project.setdefault(slug, []).append((post, rel))

    # Final picks per project: top_per_project strict, then up to
    # popular_bonus_cap bonus picks. is_bonus flag drives the ★ render.
    picks_by_project: dict[str, list[tuple["RedditPost", bool]]] = {}
    for project in projects:
        slug = project["slug"]
        strict = strict_by_project.get(slug, [])
        strict.sort(
            key=lambda pr: pr[1] * (1 + math.log(max(pr[0].score, 1))),
            reverse=True,
        )
        bonus = bonus_by_project.get(slug, [])
        bonus.sort(key=lambda pr: pr[0].score, reverse=True)
        chosen = [(p, False) for p, _ in strict[:top_per_project]]
        chosen += [(p, True) for p, _ in bonus[:popular_bonus_cap]]
        if chosen:
            picks_by_project[slug] = chosen

    # Discovery pass: search across all of Reddit (no sub restriction)
    # for each project's first search term. Posts in non-curated subs
    # that match relevance get tagged emerging — surfaced in their own
    # 🆕 section AND recorded as hits in subreddit_stats so they
    # accumulate toward the auto-promote threshold (≥5 hits in 14d).
    emerging_picks_by_project = await _discover_emerging(
        brain_db, projects, project_sub_sets
    )

    # Auto-promote any non-curated subs that have crossed the threshold
    # since the last digest. Mutates project.subreddits + stamps the
    # stats row promoted_at, so the next run treats it as curated.
    newly_promoted = _promote_emerging_subreddits(brain_db, projects)

    # Same heartbeat-status update as hot_pulse: ensure the schedule's
    # last_run reflects cron fires, not just manual /sched/.../run.
    try:
        brain_db.update_schedule_status(
            "morning_digest",
            datetime.now(timezone.utc).isoformat(),
            (
                f"ok ({sum(len(v) for v in picks_by_project.values())} picks, "
                f"{sum(len(v) for v in emerging_picks_by_project.values())} emerging, "
                f"{len(newly_promoted)} promoted)"
            ),
        )
    except Exception as exc:
        logger.warning("morning_digest: could not update schedule status: %s", exc)

    if not picks_by_project and not emerging_picks_by_project and not newly_promoted:
        logger.info("morning_digest: no fresh comment-worthy posts, skipping notify")
        return

    # Compose Markdown digest. Each pick renders as 3 lines:
    # title-with-link / facts / why-comment. No comment-draft text;
    # Alex reads, taps, and writes his own replies.
    lines = ["*Morning digest — Reddit comment candidates (last 24h)*", ""]
    for project in projects:
        slug = project["slug"]
        picks = picks_by_project.get(slug, [])
        emerging = emerging_picks_by_project.get(slug, [])
        if not picks and not emerging:
            continue
        topics = json.loads(project.get("topics", "[]"))
        terms = json.loads(project.get("search_terms", "[]"))
        lines.append(f"*{slug}* ({project['owner']}/{project['repo']})")
        for post, is_bonus in picks:
            lines.extend(_render_pick_lines(post, topics, terms, is_bonus))
        if emerging:
            lines.append("  🆕 Emerging subs:")
            for post in emerging:
                lines.extend(_render_pick_lines(post, topics, terms, is_bonus=False))
        lines.append("")

    # Stored-subs report paragraph: which subs we search, which yield
    # hits, and any newly auto-promoted into curated lists.
    report = _render_subreddit_report(brain_db, projects, newly_promoted)
    if report:
        lines.append(report)

    digest = "\n".join(lines).strip()
    delivered = await notify(digest)
    if not delivered:
        logger.warning("morning_digest: telegram delivery failed; not marking seen")
        return

    # Mark seen + record hits for surfaced posts.
    # No draft tasks are queued — comment-suggestion was removed at
    # the operator's request.
    for slug, picks in picks_by_project.items():
        for post, _is_bonus in picks:
            brain_db.mark_seen("reddit_post", post.id, slug)
            brain_db.record_subreddit_hit(post.subreddit, slug)
    logger.info(
        "morning_digest: delivered %d posts (%d strict + %d bonus + %d emerging) "
        "across %d projects, %d newly promoted",
        sum(len(v) for v in picks_by_project.values())
        + sum(len(v) for v in emerging_picks_by_project.values()),
        sum(1 for v in picks_by_project.values() for _, b in v if not b),
        sum(1 for v in picks_by_project.values() for _, b in v if b),
        sum(len(v) for v in emerging_picks_by_project.values()),
        len(picks_by_project) + len(emerging_picks_by_project),
        len(newly_promoted),
    )


async def _discover_emerging(
    brain_db,
    projects: list[dict],
    project_sub_sets: dict[str, set[str]],
    cap_per_project: int = 2,
) -> dict[str, list["RedditPost"]]:
    """Search /r/all for each project's first term; return posts in
    non-curated subreddits that pass the project's strict relevance
    filter, capped per project. Records each match as a stat hit so
    sustained emerging subs accumulate toward auto-promotion.
    """
    from reddit_search import search_all

    out: dict[str, list["RedditPost"]] = {}
    for project in projects:
        slug = project["slug"]
        terms = json.loads(project.get("search_terms", "[]"))
        topics = json.loads(project.get("topics", "[]"))
        if not terms and not topics:
            continue
        query = (terms or topics)[0]
        try:
            all_posts = await search_all(query, since="24h", limit=15)
        except Exception as exc:
            logger.warning(
                "morning_digest: discovery search failed for %s/%s: %s",
                slug,
                query,
                exc,
            )
            continue

        curated = project_sub_sets.get(slug, set())
        emerging: list[tuple["RedditPost", float]] = []
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
            brain_db.record_subreddit_search(post.subreddit, slug, is_curated=False)
            brain_db.record_subreddit_hit(post.subreddit, slug)

        if emerging:
            emerging.sort(
                key=lambda pr: pr[1] * (1 + math.log(max(pr[0].score, 1))),
                reverse=True,
            )
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
        for s in json.loads(p.get("subreddits", "[]")):
            all_subs.add(s.lower())
    for s in MORNING_DIGEST_EXTRA_SUBREDDITS:
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
    quiet_hours_tz: str = "America/Chicago",
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
    from reddit_search import search_subreddits
    from telegram import notify

    now = datetime.now(timezone.utc)

    # Quiet-hours gate: skip Reddit + Telegram entirely when the
    # operator is asleep. Default 00:00-05:00 America/Chicago covers
    # the operator's CST/CDT sleep window. Status row is written
    # exactly once on the boundary fire entering quiet hours; later
    # fires inside the window are silent to avoid 60 status writes
    # per night. On the first non-quiet fire after the window the
    # flag clears and the normal flow resumes (and naturally rewrites
    # last_status with "ok"/"throttled" as part of delivery).
    in_quiet = _is_quiet_hours(now, quiet_hours_start, quiet_hours_end, quiet_hours_tz)
    if in_quiet:
        if not _HOT_PULSE_IN_QUIET_HOURS:
            local_now = now.astimezone(ZoneInfo(quiet_hours_tz)).strftime("%H:%M")
            try:
                brain_db.update_schedule_status(
                    "hot_pulse",
                    now.isoformat(),
                    f"quiet hours (entered at {local_now} {quiet_hours_tz}, "
                    f"resume {quiet_hours_end:02d}:00)",
                )
            except Exception as exc:
                logger.warning("hot_pulse: quiet-hours status update failed: %s", exc)
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
            try:
                brain_db.update_schedule_status(
                    "hot_pulse",
                    now.isoformat(),
                    f"throttled ({elapsed_min:.1f}m of "
                    f"{min_delivery_interval_minutes}m)",
                )
            except Exception as exc:
                logger.warning("hot_pulse: schedule status update failed: %s", exc)
            logger.info(
                "hot_pulse: throttled — %.1fm since last delivery (min interval %dm)",
                elapsed_min,
                min_delivery_interval_minutes,
            )
            return

    projects = brain_db.list_projects()
    if project_slugs:
        projects = [p for p in projects if p["slug"] in project_slugs]

    if not projects:
        logger.info("hot_pulse: no tracked projects, skipping")
        return

    extras = (
        list(extra_subreddits)
        if extra_subreddits is not None
        else list(MORNING_DIGEST_EXTRA_SUBREDDITS)
    )
    sub_set: set[str] = set(extras)
    for p in projects:
        for s in json.loads(p.get("subreddits", "[]")):
            sub_set.add(s)
    scan_subreddits = sorted(sub_set)

    project_sub_sets: dict[str, set[str]] = {
        p["slug"]: {s.lower() for s in json.loads(p.get("subreddits", "[]"))}
        for p in projects
    }

    # Record one search impression per (sub, project) pair this cycle.
    for project in projects:
        slug = project["slug"]
        for sub in scan_subreddits:
            is_curated = sub.lower() in project_sub_sets[slug]
            brain_db.record_subreddit_search(sub, slug, is_curated=is_curated)

    # Per-post-id best-fit map. Each entry tracks the project that
    # claimed the post, the post object, the literal-match relevance
    # (>= 1.0 for keyword hits, 0.5 for Reddit-search-only baseline),
    # and the search query that brought the post back. The query
    # becomes the "category bucket" we group by in the render.
    best_for_post: dict[str, tuple[dict, "RedditPost", float, str]] = {}

    for project in projects:
        slug = project["slug"]
        topics = json.loads(project.get("topics", "[]"))
        terms = json.loads(project.get("search_terms", "[]"))
        queries = terms[:3] if terms else (topics[:3] if topics else [slug])

        for query in queries:
            try:
                posts = await search_subreddits(
                    query,
                    scan_subreddits,
                    since=window,
                    sort="new",
                    limit_per_sub=10,
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

    # Group by project, then by category (the search query that
    # brought the post). Each project block in the digest renders one
    # subsection per category so the operator sees which area is
    # currently active.
    by_project_by_category: dict[str, dict[str, list[tuple["RedditPost", float]]]] = {}
    for project, post, rel, query in best_for_post.values():
        slug = project["slug"]
        by_project_by_category.setdefault(slug, {}).setdefault(query, []).append(
            (post, rel)
        )

    by_project: dict[str, list[tuple["RedditPost", float]]] = {}
    for slug, cat_map in by_project_by_category.items():
        for cat_picks in cat_map.values():
            by_project.setdefault(slug, []).extend(cat_picks)

    for slug in list(by_project.keys()):
        by_project[slug].sort(
            key=lambda pr: pr[1] * (1 + math.log(max(pr[0].score, 1))),
            reverse=True,
        )
        by_project[slug] = by_project[slug][:max_per_project]

    # Update the schedule's last_run/last_status so the operator can
    # see the cron is firing even on silent cycles. APScheduler-driven
    # fires bypass run_schedule_now (which is what normally writes
    # those fields), so without this the schedule looks stale and the
    # operator can't distinguish "scheduler dead" from "no fresh hits".
    try:
        brain_db.update_schedule_status(
            "hot_pulse",
            datetime.now(timezone.utc).isoformat(),
            f"ok ({sum(len(v) for v in by_project.values())} picks)",
        )
    except Exception as exc:
        logger.warning("hot_pulse: could not update schedule status: %s", exc)

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

    # Render with category buckets per project. Within each project
    # block, picks subdivide by the search query that brought them
    # back. Each pick is also tagged 📌 (literal phrase match,
    # rel >= 1.0) or ◇ (semantic-only match via Reddit's search
    # ranking, rel == 0.5) so the operator can tell precision tiers
    # at a glance.
    lines = [f"*Hot pulse — fresh posts (last {window})*", ""]
    for project in projects:
        slug = project["slug"]
        cat_map = by_project_by_category.get(slug, {})
        if not cat_map:
            continue
        topics = json.loads(project.get("topics", "[]"))
        terms = json.loads(project.get("search_terms", "[]"))
        lines.append(f"*{slug}* ({project['owner']}/{project['repo']})")
        # Sort categories by total pick count desc (most active first).
        sorted_cats = sorted(cat_map.items(), key=lambda kv: len(kv[1]), reverse=True)
        for category, cat_picks in sorted_cats:
            cat_picks_sorted = sorted(
                cat_picks,
                key=lambda pr: pr[1] * (1 + math.log(max(pr[0].score, 1))),
                reverse=True,
            )
            lines.append(f'  Category: "{category}"')
            for post, rel in cat_picks_sorted:
                tier = "📌" if rel >= 1.0 else "◇"
                pick_lines = _render_pick_lines(post, topics, terms, is_bonus=False)
                # Replace the bullet with our tier marker on line 0,
                # leave the facts/why lines alone.
                pick_lines[0] = pick_lines[0].replace("  • ", f"  • {tier} ", 1)
                lines.extend(pick_lines)
        lines.append("")

    pulse = "\n".join(lines).strip()
    delivered = await notify(pulse)
    if not delivered:
        logger.warning("hot_pulse: telegram delivery failed; not marking seen")
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


# ─── Profile-Driven Seeds ──────────────────────────────────────────
#
# Seeds (tracked projects + baseline schedules) now live in the active
# tenant profile (config/profiles/<name>/profile.yaml), not in code. The
# marketing profile reproduces the original hardcoded set exactly
# (regression-locked by tests/test_profile.py). seed_default_projects /
# seed_default_schedules keep their names as the public entry points so
# existing callers (app lifespan, tests) are unchanged; they load the
# active profile (CLAWRANGE_PROFILE, default "marketing") and seed from it.


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
    return out


def seed_default_schedules(brain_db) -> list[dict]:
    """Seed the active profile's schedules (back-compat entry point)."""
    from tenant_profile import load_profile

    return _seed_schedules_from_profile(brain_db, load_profile())


def seed_default_projects(brain_db) -> list[dict]:
    """Seed the active profile's projects + schedules (back-compat entry point).

    Loads the profile named by CLAWRANGE_PROFILE (default "marketing").
    """
    from tenant_profile import load_profile

    return seed_from_profile(brain_db, load_profile())


# ─── CRM generators (lead-crm profile) ───────────────────────────────


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
    from crm import get_adapter

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
    from connectors import run_connector
    from tenant_profile import load_profile

    profile = profile or load_profile()
    spec = profile.connector(connector)
    now = datetime.now(timezone.utc).isoformat()

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
        counts = run_connector(spec, adapter, http_client=http_client)
    except Exception as exc:
        logger.warning("pipeline: connector %s failed: %s", connector, exc)
        if schedule_id:
            brain_db.update_schedule_status(schedule_id, now, f"error ({exc})")
        return None

    if schedule_id:
        brain_db.update_schedule_status(
            schedule_id, now, f"ok ({counts['written']} written)"
        )

    if counts["written"]:
        from telegram import notify

        await notify(
            f"Lead sync ({connector}): {counts['written']} written "
            f"({counts['fetched']} fetched, {counts['kept']} kept)."
        )
    else:
        logger.info("pipeline: %s wrote 0 rows; no telegram summary", connector)
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
    from tenant_profile import load_profile

    profile = profile or load_profile()
    queries = queries or []
    now = datetime.now(timezone.utc).isoformat()

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

    if schedule_id:
        brain_db.update_schedule_status(
            schedule_id, now, f"ok ({len(queries)} queries)"
        )

    from telegram import notify

    await notify(digest)
    return digest


# ─── Registry ────────────────────────────────────────────────────────

GENERATORS = {
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
}
