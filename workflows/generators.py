"""Schedule generators for marketing orchestrator.

Each generator reads from the projects table and enqueues tasks
into the existing task queue via brain_db.create_task().
"""

import json
import logging

logger = logging.getLogger("clawrange.generators")


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


def seed_default_projects(brain_db) -> list[dict]:
    """Idempotently insert the default project tracking set.

    Returns the list of resulting project rows. Safe to call on every
    boot; `upsert_project` uses ON CONFLICT to preserve hand-edits to
    topics/subreddits/posture.
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
    return out


# ─── Registry ────────────────────────────────────────────────────────

GENERATORS = {
    "morning_scan": morning_scan_generator,
    "weekly_traffic": weekly_traffic_generator,
    "awesome_lists_watch": awesome_lists_watch_generator,
    "custom_scan": custom_scan_generator,
    "content_idea": content_idea_generator,
    "comment_draft": comment_draft_generator,
}
