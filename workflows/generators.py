"""Schedule generators for marketing orchestrator.

Each generator reads from the projects table and enqueues tasks
into the existing task queue via brain_db.create_task().
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reddit_search import RedditPost

logger = logging.getLogger("clawrange.generators")

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
    queue_drafts: bool = True,
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
    repeat it. When queue_drafts is True, enqueues a `[DRAFT]`
    comment-draft task per post for human review. Never auto-posts.
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

    if not picks_by_project:
        logger.info("morning_digest: no fresh comment-worthy posts, skipping notify")
        return

    # Compose Markdown digest.
    lines = ["*Morning digest — Reddit comment candidates (last 24h)*", ""]
    for project in projects:
        slug = project["slug"]
        picks = picks_by_project.get(slug, [])
        if not picks:
            continue
        lines.append(f"*{slug}* ({project['owner']}/{project['repo']})")
        for post, is_bonus in picks:
            title = post.title if len(post.title) <= 90 else post.title[:87] + "..."
            star = "★ " if is_bonus else ""
            lines.append(
                f"  • {star}r/{post.subreddit} — [{title}]({post.url}) "
                f"[id:{post.id}] · {post.score} pts · {post.comments} comments"
            )
        lines.append("")

    digest = "\n".join(lines).strip()
    delivered = await notify(digest)
    if not delivered:
        logger.warning("morning_digest: telegram delivery failed; not marking seen")
        return

    # Mark seen + enqueue draft tasks only after successful delivery.
    for slug, picks in picks_by_project.items():
        proj = brain_db.get_project(slug) or {}
        posture = proj.get("posture", "")
        for post, is_bonus in picks:
            brain_db.mark_seen("reddit_post", post.id, slug)
            if queue_drafts:
                bonus_note = (
                    " (popular sub-affinity pick — verify topical fit)"
                    if is_bonus
                    else ""
                )
                desc = (
                    f"[DRAFT] Comment draft for {slug} on {post.url} "
                    f"[id:{post.id}]. OP context: r/{post.subreddit} — "
                    f'"{post.title}".{bonus_note} Write a 3-5 sentence reply '
                    f"that leads with concrete advice; only mention {slug} if "
                    f"directly relevant. Never auto-post — Alex reviews. "
                    f"Posture: {posture}"
                )
                brain_db.create_task(desc, priority=2, source="morning_digest")
    logger.info(
        "morning_digest: delivered %d posts (%d strict + %d bonus) across %d projects",
        sum(len(v) for v in picks_by_project.values()),
        sum(1 for v in picks_by_project.values() for _, b in v if not b),
        sum(1 for v in picks_by_project.values() for _, b in v if b),
        len(picks_by_project),
    )


# ─── Default Project Seeds ──────────────────────────────────────────


_DEFAULT_PROJECTS = (
    {
        "slug": "claude-night-market",
        "owner": "athola",
        "repo": "claude-night-market",
        "topics": ["claude-code", "plugins", "agent-tooling"],
        "subreddits": ["ClaudeAI", "LocalLLaMA", "SideProject"],
        "search_terms": [
            "claude code plugin",
            "claude code marketplace",
            "claude skill",
        ],
        "posture": (
            "Lead with: a curated marketplace for Claude Code plugins. "
            "Useful comments first - link only when the question is about "
            "discovering or composing plugins."
        ),
    },
    {
        "slug": "skrills",
        "owner": "athola",
        "repo": "skrills",
        "topics": ["chrome-extension", "trades", "knowledge-capture"],
        "subreddits": ["Construction", "ITCareerQuestions", "SideProject"],
        "search_terms": [
            "trade skill capture",
            "field knowledge chrome extension",
        ],
        "posture": (
            "Lead with: capture trade-skill knowledge from the field. "
            "Comment on the workflow first, mention skrills only when the "
            "thread is explicitly about capture or onboarding."
        ),
    },
    {
        "slug": "simple-resume",
        "owner": "athola",
        "repo": "simple-resume",
        "topics": ["resume", "yaml-to-pdf", "static-site"],
        "subreddits": ["resumes", "cscareerquestions", "SideProject"],
        "search_terms": [
            "yaml resume",
            "static resume site",
            "resume generator",
        ],
        "posture": (
            "Lead with: a YAML-driven resume that builds PDF and HTML. "
            "Comment with concrete examples; mention only when the post is "
            "about resume tooling specifically."
        ),
    },
    {
        "slug": "clawrange",
        "owner": "athola",
        "repo": "clawrange",
        "topics": [
            "personal ai ops",
            "fastapi workflow service",
            "claude code",
            "telegram bot",
            "llm proxy",
            "openrouter",
            "apscheduler",
            "self-hosted ai",
        ],
        "subreddits": [
            "ClaudeAI",
            "claudecode",
            "vibecoding",
            "opensourceai",
            "codex",
            "sideprojects",
            "LocalLLaMA",
            "selfhosted",
        ],
        "search_terms": [
            "personal ai ops stack",
            "claude code workflow",
            "fastapi llm proxy",
            "openrouter telegram",
            "agent orchestration self-hosted",
        ],
        "posture": (
            "Lead with: a single-host AI ops stack (FastAPI workflows + "
            "Claude Code via OpenClaw) for tier-routed LLM access, "
            "scheduled marketing scans, and Telegram delivery. Comment "
            "with first-hand operational details (single uvicorn worker, "
            "APScheduler job persistence, balance-aware tier routing). "
            "Mention clawrange only when the post is explicitly about "
            "self-hosted Claude tooling or LLM-proxy gateways."
        ),
    },
    {
        "slug": "personal-brand",
        "owner": "athola",
        "repo": "athola",  # GitHub profile repo
        "topics": [
            "ai-systems",
            "agent-platforms",
            "plugin-ecosystems",
            "msp-tooling",
            "fastapi",
            "ai-engineering",
        ],
        "subreddits": [
            "ClaudeAI",
            "LocalLLaMA",
            "MachineLearning",
            "ExperiencedDevs",
            "ProgrammerHumor",
        ],
        "search_terms": [
            "ai systems engineering",
            "agent orchestration",
            "claude code plugins",
            "personal ai ops stack",
            "msp automation",
        ],
        "posture": (
            "Voice: AI systems engineer at webAI in Austin, building a "
            "personal AI ops stack. Share first-hand experience, not pitches. "
            "Always link to source code or running infra. The tone: terse, "
            "specific, opinionated. Never auto-post; queue drafts for review."
        ),
    },
)


_DEFAULT_SCHEDULES = (
    {
        "id": "morning_digest",
        "name": "Morning Reddit comment-candidate digest",
        "kind": "morning_digest",
        "cron": "0 8 * * *",
        "kwargs": {},
    },
)


def _seed_default_schedules(brain_db) -> list[dict]:
    """Idempotently register baseline marketing schedules.

    Only inserts when the schedule_id is missing so that hand-edits to
    cron, kwargs, or paused state via the /sched API survive reboots.
    """
    out: list[dict] = []
    for spec in _DEFAULT_SCHEDULES:
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
            logger.info("seed_default_schedules: created %s", spec["id"])
        else:
            out.append(existing)
    return out


def seed_default_projects(brain_db) -> list[dict]:
    """Idempotently insert the default project tracking set.

    Returns the list of resulting project rows. Safe to call on every
    boot; `upsert_project` uses ON CONFLICT to preserve hand-edits to
    topics/subreddits/posture. Also registers the baseline marketing
    schedules (e.g. the 8am morning_digest) so a fresh deploy gets the
    morning rundown without manual /sched setup.
    """
    out = []
    for spec in _DEFAULT_PROJECTS:
        existing = brain_db.get_project(spec["slug"])
        if existing is None:
            row = brain_db.upsert_project(
                spec["slug"],
                spec["owner"],
                spec["repo"],
                topics=spec["topics"],
                subreddits=spec["subreddits"],
                search_terms=spec["search_terms"],
                posture=spec["posture"],
            )
            out.append(row)
            logger.info("seed_default_projects: created %s", spec["slug"])
        else:
            out.append(existing)
    _seed_default_schedules(brain_db)
    return out


# ─── Registry ────────────────────────────────────────────────────────

GENERATORS = {
    "morning_scan": morning_scan_generator,
    "morning_digest": morning_digest_generator,
    "weekly_traffic": weekly_traffic_generator,
    "awesome_lists_watch": awesome_lists_watch_generator,
    "custom_scan": custom_scan_generator,
    "content_idea": content_idea_generator,
    "comment_draft": comment_draft_generator,
}
