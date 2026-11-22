"""Chat commands answered inside the proxy.

Covers /tier, !help, !task, !persona (alias !learn), /sched, /scan and
/projects. Each returns a synthetic response and never reaches a provider.
Shared helpers and state are read from `llm_proxy` at call time, so this
module never touches `llm_proxy` at import time.

The dispatchers llm_proxy calls (`_dispatch_task_command`,
`_dispatch_marketing_command`, `_dispatch_persona_command`) take `is_stream`
and wrap the reply as SSE exactly once; the handlers below them return
plain text or a synthetic response.
"""

import asyncio
import hashlib
import logging
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

import github_search
import heartbeat
import llm_proxy
import persona_learning
import reddit_search
import research_router
import tenant_profile
from brain_db import TaskStatus, project_list
from errors import UpstreamError

logger = logging.getLogger("clawrange.chat_commands")


def _reply(text: str, is_stream: bool) -> JSONResponse | StreamingResponse:
    """The one place a chat command's text becomes a response."""
    resp = llm_proxy._synthetic_response(text)
    return llm_proxy._wrap_json_as_sse(resp) if is_stream else resp


async def _handle_tier_command() -> JSONResponse:
    """Return current tier status, balance, and circuit breaker state."""
    lines = ["Tier Status\n"]
    for t in llm_proxy.tier_snapshot():
        lines.append(f"  [{t['status']}] {t['name']} — {t['description']}")

    # Refresh the cache (rate-limited inside), then display what is known.
    await llm_proxy._check_openrouter_balance()
    lines.append("")
    lines.append(
        f"OpenRouter balance: {llm_proxy.describe_balance()} "
        f"(floor: ${llm_proxy.OPENROUTER_BALANCE_FLOOR:.2f})"
    )

    if llm_proxy._last_tier_used:
        lines.append(f"Last used: {llm_proxy._last_tier_used}")
    else:
        lines.append("Last used: none yet")

    lines.append("Paid auto-fallback: off (send !paid or !claude to enable)")
    return llm_proxy._synthetic_response("\n".join(lines))


# ─── !help Command ─────────────────────────────────────────────


def _handle_help() -> JSONResponse:
    """Return full command reference — all intercepted commands and brain operations."""
    text = """ClawRange Command Reference

TASK MANAGEMENT
  !task <description>      Create a new task (priority 3)
  !tasks                   Show current task queue
  !task list               Same as !tasks
  !task tail               Show recently finished tasks (newest first)
  !task cancel <id>        Cancel a pending/active task
  !task priority <id> <1-5> Change priority (1=urgent, 5=low)

PERSONA
  !persona <feedback>      Queue a persona enhancement as a [DRAFT] proposal
  !learn <feedback>        Same as !persona <feedback>
  !persona reflect         Review recent activity and queue suggestions

SYSTEM STATUS
  !tier                    Show LLM tier status and balance
  !status                  Same as !tier
  !help                    This command reference

HTTP API (workflows port 5678; every route below needs
  Authorization: Bearer $PROXY_AUTH_TOKEN, which chat does not have)

  Brain slugs use hierarchy: client/acme-corp, incident/wifi-site2, person/bob-smith
  Page types: client, system, incident, decision, note, person, company, project

BRAIN API
  POST /brain/pages              Create or update a page
  GET  /brain/pages/{slug}       Get page with timeline + tags
  DELETE /brain/pages/{slug}     Delete a page
  GET  /brain/pages              List pages (filter: ?page_type=client)
  GET  /brain/search?q=...       Hybrid search (modes: keyword, vector, hybrid)
  POST /brain/pages/{slug}/timeline    Append timeline entry
  GET  /brain/pages/{slug}/timeline    List timeline entries
  POST /brain/pages/{slug}/links       Add link to another page
  GET  /brain/pages/{slug}/links       List links
  GET  /brain/pages/{slug}/graph       Traverse knowledge graph (?depth=2)
  POST /brain/pages/{slug}/tags        Set tags
  GET  /brain/pages/{slug}/versions    Version history
  GET  /brain/pages/{slug}/chunks      Content chunks

TASK API
  POST /task                     Create task
  GET  /task                     List tasks (?status=pending)
  GET  /task/{id}                Get task by ID
  POST /task/{id}/claim          Mark task active
  POST /task/{id}/result         Complete task with result
  DELETE /task/{id}              Cancel task

HEALTH
  GET  /healthz                  Service health + brain status (no token)
  GET  /tier                     Tier status JSON (no token)

MARKETING
  /projects list                 List tracked GitHub projects
  /projects show <slug>          Show project details
  /projects add <slug> <owner>/<repo> [--topics ...] [--subs ...]
  /projects rm <slug>

  /sched list [--paused]         List scheduled jobs (--paused: paused only)
  /sched add <name> cron "<expr>" -- <generator>
  /sched pause|resume|rm|run <id|name>

  /scan reddit <topic> [--subs ...] [--project <slug>]
  /scan github <topic> [--kind repos|issues] [--stars 5]
  /scan github traffic [slug]    Pull repo traffic stats
  /scan web <prompt>             LLM web search

  /marketing                     Queue Reddit + GitHub scan tasks for every
                                 tracked project (runs morning_scan)"""
    return llm_proxy._synthetic_response(text)


# ─── !task Command ──────────────────────────────────────────────


_TASK_ACTIONS = ("cancel", "priority", "list", "tail")

_DONE_MARKERS = {"completed": "✅", "failed": "❌", "blocked": "❓"}


def _first_token(text: str) -> tuple[str, str]:
    """Split off the first whitespace-delimited word: (word lowercased, rest).

    Commands match on the whole first word, so "tailor" is not "tail" and
    "/schedule" is not "/sched"."""
    parts = text.split(None, 1)
    if not parts:
        return "", ""
    return parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""


def _parse_flags(
    parts: list[str], flags: tuple[str, ...]
) -> tuple[list[str], dict[str, str]]:
    """Pull `--flag value` pairs out of a token list.

    Returns the remaining positional tokens and {flag: value}. A flag with
    no value after it is kept as a positional token."""
    positional: list[str] = []
    values: dict[str, str] = {}
    i = 0
    while i < len(parts):
        if parts[i] in flags and i + 1 < len(parts):
            values[parts[i]] = parts[i + 1]
            i += 2
        else:
            positional.append(parts[i])
            i += 1
    return positional, values


def _ops_name() -> str:
    """Who works the queue: the profile's `assistant.ops.name`, else a
    neutral label (no operator's agent name in core code)."""
    profile = tenant_profile.active_profile()
    ops = ((getattr(profile, "assistant", None) or {}).get("ops")) or {}
    return str(ops.get("name") or "").strip() or "The ops agent"


def _live_scheduler():
    """The running APScheduler (None outside the app lifespan)."""
    from app import app

    return getattr(app.state, "scheduler", None)


# ─── Marketing Commands (/sched, /scan, /projects) ──────────────

_MARKETING_PREFIXES = (
    "/sched",
    "!sched",
    "/scan",
    "!scan",
    "/projects",
    "!projects",
    "/marketing",
    "!marketing",
)


def _extract_marketing_command(msg: str) -> dict | None:
    """Parse marketing commands. Returns dict with verb, subcmd, args or None."""
    prefix, rest = _first_token(msg.strip())
    if prefix not in _MARKETING_PREFIXES:
        return None
    verb = prefix.lstrip("/!")
    if verb == "marketing":
        return {"verb": "sched", "subcmd": "run", "args": "morning_scan"}
    if not rest:
        return {"verb": verb, "subcmd": "list", "args": ""}
    subcmd, args = _first_token(rest)
    return {"verb": verb, "subcmd": subcmd, "args": args}


async def _dispatch_marketing_command(
    cmd: dict, is_stream: bool = False
) -> JSONResponse | StreamingResponse:
    """Route parsed marketing commands to handlers."""
    verb = cmd["verb"]
    subcmd = cmd["subcmd"]
    args = cmd["args"]

    try:
        if verb == "sched":
            text = await _handle_sched_command(subcmd, args)
        elif verb == "scan":
            text = await _handle_scan_command(subcmd, args)
        elif verb == "projects":
            text = await _handle_projects_command(subcmd, args)
        else:
            text = f"Unknown command: {verb}"
    except Exception as exc:
        logger.exception("chat command /%s %s failed", verb, subcmd)
        text = f"Error: {exc}"

    return _reply(text, is_stream)


async def _handle_sched_command(subcmd: str, args: str) -> str:
    """Handle /sched subcommands.

    Changes go through the scheduler helpers with the live APScheduler, as
    the HTTP routes do, so a pause/resume/add/rm takes effect immediately
    instead of at the next restart."""
    from app import brain_db

    if subcmd == "list":
        scheds = brain_db.list_schedules()
        if not scheds:
            return (
                "No schedules configured. Use /sched add <name> cron "
                '"<expr>" -- <generator>'
            )
        if "--paused" in args.split():
            scheds = [s for s in scheds if s.get("paused")]
            if not scheds:
                return "No paused schedules."
        lines = ["Scheduled Jobs:\n"]
        for s in scheds:
            status = "PAUSED" if s.get("paused") else "ACTIVE"
            last = s.get("last_status", "never")
            lines.append(
                f"  [{status}] {s['name']} ({s['id']})\n"
                f"    cron: {s['cron']} | kind: {s['kind']}\n"
                f"    last: {last or 'never'}"
            )
        lines.append("\n/sched show|pause|resume|rm|run <id|name>")
        return "\n".join(lines)

    if subcmd in ("show", "info"):
        sched = brain_db.get_schedule(args.strip())
        if not sched:
            return f"Schedule not found: {args}"
        return (
            f"Schedule: {sched['name']} ({sched['id']})\n"
            f"  Kind: {sched['kind']}\n"
            f"  Cron: {sched['cron']}\n"
            f"  Kwargs: {sched.get('kwargs', '{}')}\n"
            f"  Paused: {bool(sched.get('paused'))}\n"
            f"  Last run: {sched.get('last_run', 'never')}\n"
            f"  Last status: {sched.get('last_status', 'never')}"
        )

    if subcmd == "add":
        import shlex

        try:
            parts = shlex.split(args, posix=True)
        except ValueError:
            return 'Parse error. Usage: /sched add <name> cron "<expr>" -- <generator>'

        if "--" in parts:
            split_idx = parts.index("--")
            before_dash = parts[:split_idx]
            after_dash = parts[1 + split_idx :]
        else:
            before_dash = parts
            after_dash = []

        if len(before_dash) < 3:
            return 'Usage: /sched add <name> cron "<expr>" -- <generator>'

        name = before_dash[0]
        cron_expr = " ".join(before_dash[2:])
        kind = after_dash[0] if after_dash else "custom_scan"
        kwargs = {"topic": " ".join(after_dash[1:])} if after_dash[1:] else {}

        from scheduler import add_schedule, validate_schedule

        try:
            validate_schedule(kind, cron_expr, kwargs)
        except ValueError as exc:
            return f"Schedule not added: {exc}"

        sched_id = hashlib.md5(name.encode()).hexdigest()[:8]

        await add_schedule(
            _live_scheduler(), brain_db, sched_id, name, kind, cron_expr, kwargs
        )
        return (
            f"Schedule added: {name} ({sched_id})\n  cron: {cron_expr}\n  kind: {kind}"
        )

    if subcmd in ("pause", "resume", "rm", "remove", "delete"):
        from scheduler import pause_schedule, remove_schedule, resume_schedule

        # Resolve <id|name> first: the scheduler helpers and job ids are
        # keyed by id only.
        sched = brain_db.get_schedule(args.strip())
        if not sched:
            return f"Schedule not found: {args}"
        label = f"{sched['name']} ({sched['id']})"
        live = _live_scheduler()
        if subcmd == "pause":
            await pause_schedule(live, brain_db, sched["id"])
            return f"Paused: {label}"
        if subcmd == "resume":
            await resume_schedule(live, brain_db, sched["id"])
            return f"Resumed: {label}"
        await remove_schedule(live, brain_db, sched["id"])
        return f"Removed: {label}"

    if subcmd == "run":
        from generators import GENERATORS
        from scheduler import run_schedule_now

        target = args.strip()
        if not brain_db.get_schedule(target) and target in GENERATORS:
            # /marketing names the morning_scan generator, which no shipped
            # profile seeds as a schedule: run the generator itself once.
            await GENERATORS[target](brain_db=brain_db)
            return f"Ran generator {target} once (no schedule by that name)."
        try:
            result = await run_schedule_now(_live_scheduler(), brain_db, target)
        except ValueError as e:
            return str(e)
        status = result.get("status", "unknown")
        if result.get("error"):
            status = f"{status}: {result['error']}"
        return f"Ran schedule: {result.get('schedule_id', args)} — {status}"

    return (
        "Usage:\n"
        "  /sched list [--paused]\n"
        "  /sched show <id|name>\n"
        '  /sched add <name> cron "<expr>" -- <generator>\n'
        "  /sched pause <id|name>\n"
        "  /sched resume <id|name>\n"
        "  /sched rm <id|name>\n"
        "  /sched run <id|name>"
    )


async def _handle_scan_command(subcmd: str, args: str) -> str:
    """Handle /scan subcommands."""
    from app import brain_db

    if subcmd == "reddit":
        # /scan reddit <topic> [--subs ...] [--project <slug>] [--since 7d]
        topic, subreddits, project_slug, since = _parse_scan_args(args)
        if not topic:
            return "Usage: /scan reddit <topic> [--subs sub1,sub2] [--project <slug>]"

        subreddits = tenant_profile.resolve_subreddits(
            brain_db, subreddits, project_slug
        )

        try:
            results: list = await reddit_search.search_subreddits(
                topic, subreddits, since=since, limit_per_sub=10
            )
        except UpstreamError as exc:
            return f"Reddit search unavailable: {exc}"

        if not results:
            return f"No Reddit results for '{topic}' in {subreddits}"

        lines = [f"Reddit scan: {topic} ({len(results)} results)\n"]
        for i, r in enumerate(results[:10], 1):
            lines.append(
                f"{i}. {r.title}\n"
                f"   r/{r.subreddit} | {r.score}pts | {r.comments} comments\n"
                f"   {r.url}"
            )
            if r.snippet:
                lines.append(f"   {r.snippet[:120]}")

        if len(results) > 10:
            lines.append(f"\n... and {len(results) - 10} more")

        return "\n".join(lines)

    word, rest = _first_token(args)
    if subcmd == "github" and word == "traffic":
        return await _github_traffic(rest.split()[0] if rest else "")

    if subcmd == "github":
        # /scan github <topic> [--kind repos|issues] [--stars 5]
        positional, flags = _parse_flags(args.split(), ("--kind", "--stars"))
        topic = " ".join(positional)
        kind = flags.get("--kind", "repos")
        try:
            min_stars = int(flags.get("--stars", "0"))
        except ValueError:
            min_stars = 0

        if not topic:
            return "Usage: /scan github <topic> [--kind repos|issues] [--stars 5]"

        try:
            if kind == "issues":
                results = await github_search.search_issues(topic)
            else:
                results = await github_search.search_repos(topic, min_stars=min_stars)
        except UpstreamError as exc:
            return f"GitHub search unavailable: {exc}"

        if not results:
            return f"No GitHub results for '{topic}'"

        lines = [f"GitHub scan ({kind}): {topic} ({len(results)} results)\n"]
        for i, r in enumerate(results[:10], 1):
            # search_repos returns pydantic models with two possible shapes
            # (repo vs web/code result); narrow at runtime via hasattr and
            # access through Any since the fields aren't statically declared.
            item: Any = r
            if hasattr(item, "full_name"):
                lines.append(
                    f"{i}. {item.full_name} ({item.stars}*)\n"
                    f"   {item.description or 'No description'}\n"
                    f"   {item.url}"
                )
            else:
                lines.append(f"{i}. {item.title}\n   {item.url}")

        return "\n".join(lines)

    if subcmd == "web":
        if not args:
            return "Usage: /scan web <prompt>"
        try:
            result = await llm_proxy._llm_call(args, max_tokens=1000, web_search=True)
        except UpstreamError as exc:
            return f"Web search unavailable: {exc}"
        # None means every search tier failed, not "nothing found".
        return result or "Web search unavailable: no search tier answered."

    return (
        "Usage:\n"
        "  /scan reddit <topic> [--subs sub1,sub2] [--project <slug>]\n"
        "  /scan github <topic> [--kind repos|issues] [--stars 5]\n"
        "  /scan github traffic [project-slug]\n"
        "  /scan web <prompt>"
    )


def _traffic_lines(owner: str, repo: str, traffic) -> str:
    return (
        f"Traffic for {owner}/{repo} (14 days)\n"
        f"  Views: {traffic.views_count} ({traffic.views_uniques} unique)\n"
        f"  Clones: {traffic.clones_count} ({traffic.clones_uniques} unique)"
    )


async def _github_traffic(slug: str) -> str:
    """/scan github traffic [slug]: one project, or every tracked one."""
    from app import brain_db

    if slug:
        project = brain_db.get_project(slug)
        if not project:
            return f"Project not found: {slug}"
        projects = [project]
    else:
        projects = brain_db.list_projects()
        if not projects:
            return "No projects tracked. Use /projects add first."

    blocks = []
    for proj in projects:
        owner, repo = proj["owner"], proj["repo"]
        traffic = await github_search.get_self_traffic(owner, repo)
        if traffic:
            blocks.append(_traffic_lines(owner, repo, traffic))
        else:
            blocks.append(await research_router.traffic_unavailable(owner, repo))
    return "\n\n".join(blocks)


def _parse_scan_args(args: str) -> tuple[str, list[str], str | None, str]:
    """Parse scan arguments, extracting topic, subreddits, project, since."""
    positional, flags = _parse_flags(args.split(), ("--subs", "--project", "--since"))
    subs = flags.get("--subs")
    return (
        " ".join(positional),
        subs.split(",") if subs else [],
        flags.get("--project"),
        flags.get("--since", "7d"),
    )


async def _handle_projects_command(subcmd: str, args: str) -> str:
    """Handle /projects subcommands."""
    from app import brain_db

    if subcmd in ("list", ""):
        projects = brain_db.list_projects()
        if not projects:
            return "No projects tracked. Use /projects add <slug> <owner>/<repo>"
        lines = ["Tracked Projects:\n"]
        for p in projects:
            sub_list = project_list(p, "subreddits")
            lines.append(
                f"  {p['slug']} — {p['owner']}/{p['repo']}\n"
                f"    subs: {', '.join(sub_list[:5]) or 'none'}\n"
                f"    posture: {p.get('posture', 'none') or 'none'}"
            )
        return "\n".join(lines)

    if subcmd == "show":
        slug = args.strip()
        project = brain_db.get_project(slug)
        if not project:
            return f"Project not found: {slug}"

        return (
            f"Project: {project['slug']}\n"
            f"  Repo: {project['owner']}/{project['repo']}\n"
            f"  Topics: {project_list(project, 'topics')}\n"
            f"  Subreddits: {project_list(project, 'subreddits')}\n"
            f"  Search terms: {project_list(project, 'search_terms')}\n"
            f"  Posture: {project.get('posture', 'none')}"
        )

    if subcmd == "add":
        import shlex

        try:
            parts = shlex.split(args, posix=True)
        except ValueError:
            return (
                "Parse error. Usage: /projects add <slug> <owner>/<repo> "
                "[--topics ...] [--subs ...]"
            )

        if len(parts) < 2:
            return (
                "Usage: /projects add <slug> <owner>/<repo> "
                '[--topics ...] [--subs ...] [--posture "..."]'
            )

        slug = parts[0]
        owner_repo = parts[1]
        if "/" not in owner_repo:
            return "Format: owner/repo"
        owner, repo = owner_repo.split("/", 1)

        _, flags = _parse_flags(parts[2:], ("--topics", "--subs", "--posture"))
        topics = flags["--topics"].split(",") if "--topics" in flags else []
        subs = flags["--subs"].split(",") if "--subs" in flags else []
        posture = flags.get("--posture", "")

        brain_db.upsert_project(slug, owner, repo, topics, subs, posture=posture)
        return f"Project added: {slug} ({owner}/{repo})"

    if subcmd in ("rm", "remove"):
        slug = args.strip()
        if brain_db.delete_project(slug):
            return f"Removed project: {slug}"
        return f"Project not found: {slug}"

    return (
        "Usage:\n"
        "  /projects list\n"
        "  /projects show <slug>\n"
        "  /projects add <slug> <owner>/<repo> "
        '[--topics ...] [--subs ...] [--posture "..."]\n'
        "  /projects rm <slug>"
    )


def _extract_task_command(msg: str) -> dict | None:
    """Parse task commands. Returns dict with 'type' and relevant fields, or None."""
    stripped = msg.strip()
    lower = stripped.lower()

    # !tasks or /tasks → list
    if lower in ("!tasks", "/tasks", "!task", "/task", "!task list", "/task list"):
        return {"type": "list"}

    # !task <action> <args>
    for prefix in ("!task ", "/task "):
        if lower.startswith(prefix):
            rest = stripped[len(prefix) :].strip()
            # Whole first word only: "tailor the resume" is a new task.
            action, args = _first_token(rest)
            if action in ("list", "tail"):
                return {"type": action}
            if action in _TASK_ACTIONS:
                return {"type": "action", "action": action, "args": args}
            # Default: create a new task
            if rest:
                return {"type": "create", "description": rest}
            return {"type": "list"}

    return None


async def _dispatch_task_command(
    cmd: dict, is_stream: bool = False
) -> JSONResponse | StreamingResponse:
    """Route parsed task commands to handlers."""
    if cmd["type"] == "list":
        text = _handle_tasks_list()
    elif cmd["type"] == "tail":
        text = _handle_tasks_tail()
    elif cmd["type"] == "create":
        text = _handle_task_command(cmd["description"])
    elif cmd["type"] == "action":
        text = _handle_task_action(cmd["action"], cmd["args"])
    else:
        text = "Unknown task command. Try: !task <description> or !tasks"
    return _reply(text, is_stream)


def _handle_task_command(description: str) -> str:
    """Enqueue a task via the internal API and return confirmation."""
    from app import TaskCreate as TC
    from app import create_task

    try:
        task = create_task(TC(description=description))
    except Exception as exc:
        logger.exception("chat !task: could not queue task")
        return f"Failed to queue task: {exc}"
    return (
        f"Task queued (#{task['id']})\n"
        f"  {task['description']}\n"
        f"  Priority: {task['priority']} | Status: {task['status']}\n"
        f"{_ops_name()} will pick this up on the next heartbeat cycle."
    )


def _recently_closed(tasks: list[dict], n: int) -> list[dict]:
    """The n tasks closed most recently (completed, failed or blocked),
    newest first. list_tasks orders by priority, which is the wrong order
    for "what just happened"."""
    closed = [t for t in tasks if t["status"] in TaskStatus.RESULTS]
    closed.sort(key=lambda t: t.get("completed_at") or "", reverse=True)
    return closed[:n]


def _queue_section(title: str, tasks: list[dict]) -> list[str]:
    if not tasks:
        return []
    lines = [title]
    for t in tasks:
        src = heartbeat._operator_label() if t.get("source") == "user" else "SYS"
        lines.append(f"  [{src}] #{t['id']} [P{t['priority']}] {t['description'][:55]}")
    lines.append("")
    return lines


def _handle_tasks_list() -> str:
    """Show the current task queue."""
    from app import list_tasks

    tasks = list_tasks()["tasks"]
    if not tasks:
        return "Task queue is empty. Send !task <description> to add one."

    pending = [t for t in tasks if t["status"] == TaskStatus.PENDING]
    active = [t for t in tasks if t["status"] == TaskStatus.ACTIVE]
    done = sum(1 for t in tasks if t["status"] in TaskStatus.RESULTS)

    lines = [
        f"Task Queue ({len(pending)} pending, {len(active)} active, {done} done)\n"
    ]
    lines += _queue_section("NOW PROCESSING:", active)
    lines += _queue_section("QUEUED:", pending)

    recent = _recently_closed(tasks, 3)
    if recent:
        lines.append("RECENT:")
        for t in recent:
            marker = _DONE_MARKERS[t["status"]]
            lines.append(f"  {marker} #{t['id']} {t['description'][:40]}")
            if t["result"]:
                lines.append(f"     → {t['result'][:80]}")

    lines.append(
        "\nCommands: !task <desc> | !task cancel <id> | !task priority <id> <1-5>"
    )
    return "\n".join(lines)


def _handle_tasks_tail() -> str:
    """Show the most recently closed tasks with results, newest first."""
    from app import list_tasks

    recent = _recently_closed(list_tasks()["tasks"], 5)
    if not recent:
        return "No finished tasks yet."
    lines = [f"Recent Tasks (last {len(recent)} finished)\n"]
    for t in recent:
        lines.append(
            f"{_DONE_MARKERS[t['status']]} #{t['id']} — {t['description'][:50]}"
        )
        if t["result"]:
            lines.append(f"   {t['result'][:120]}")
        if t["completed_at"]:
            lines.append(f"   {t['status'].capitalize()}: {t['completed_at'][:19]}")
        lines.append("")
    return "\n".join(lines)


def _handle_task_action(action: str, args: str) -> str:
    """Handle task management subcommands: cancel, priority."""
    from app import brain_db

    parts = args.strip().split(None, 1)
    task_id = parts[0] if parts else ""
    task = brain_db.get_task(task_id)

    if not task:
        return f"Task #{task_id} not found."
    if action == "cancel":
        if task["status"] in TaskStatus.DONE:
            return f"Task #{task_id} already {task['status']}."
        brain_db.cancel_task(task_id)
        return f"Task #{task_id} cancelled."
    if action == "priority":
        new_p = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if new_p is None or not 1 <= new_p <= 5:
            return f"Usage: !task priority {task_id} <1-5>"
        brain_db.update_priority(task_id, new_p)
        return f"Task #{task_id} priority set to {new_p}."
    return f"Unknown action: {action}. Try: cancel, priority"


# ─── !persona Command ──────────────────────────────────────────────
# The chat commands call the persona pipeline in-process, the same functions
# the /persona routes use. A loopback HTTP call would need the API bearer
# token and could only fail in ways the user can't act on.

# !learn is an alias for !persona (docs/tradeoffs.md TR-013).
_PERSONA_COMMANDS = frozenset({"!persona", "!learn"})


def _extract_persona_command(msg: str) -> str | None:
    """The arguments of a !persona / !learn message, or None.

    Whole first word only, so "!personality" is not the command. The
    arguments keep the original casing.
    """
    stripped = msg.strip()
    first_word = (stripped.lower().split(None, 1) or [""])[0]
    if first_word not in _PERSONA_COMMANDS:
        return None
    return stripped[len(first_word) :]


def _persona_context():
    """(brain_db, profile name) the /persona routes use (function-local: cycle)."""
    import app as app_module

    return app_module.brain_db, app_module._current_profile().name


async def _handle_persona_command(args: str) -> JSONResponse:
    # Function-local: persona_api does `from generators import ...`, and
    # generators imports llm_proxy, which imports this module, so a
    # module-level import fails when generators is imported first.
    import persona_api

    args = args.strip()
    if not args:
        return llm_proxy._synthetic_response(
            "Usage: !persona <feedback> | !persona reflect"
        )
    try:
        brain_db, profile_name = _persona_context()
    except HTTPException as exc:
        return llm_proxy._synthetic_response(f"Persona unavailable: {exc.detail}.")
    if args.lower() == "reflect":
        try:
            queued = await asyncio.to_thread(
                persona_api.reflect_now, brain_db, profile_name
            )
        except Exception as exc:
            logger.exception("!persona reflect failed")
            return llm_proxy._synthetic_response(
                f"Persona reflection failed: {type(exc).__name__}: {exc}"
            )
        return llm_proxy._synthetic_response(
            f"Persona reflection done: {queued} proposal(s) pending. "
            "Review drafts with !tasks."
        )
    try:
        row = await asyncio.to_thread(
            persona_learning.propose,
            brain_db,
            profile_name,
            "persona",
            persona_api.Proposal.model_fields["target"].default,
            args,
            "feedback",
        )
    except ValueError as exc:
        return llm_proxy._synthetic_response(f"Feedback not queued: {exc}.")
    except Exception as exc:
        logger.exception("!persona propose failed")
        return llm_proxy._synthetic_response(
            f"Feedback not queued: {type(exc).__name__}: {exc}"
        )
    return llm_proxy._synthetic_response(
        f"Queued persona enhancement as [DRAFT] (id {row.get('id')}). "
        "Approve via /persona/proposals/<id>/approve."
    )


async def _dispatch_persona_command(
    args: str, is_stream: bool = False
) -> JSONResponse | StreamingResponse:
    """Run a !persona / !learn command and wrap the reply as SSE once."""
    resp = await _handle_persona_command(args)
    return llm_proxy._wrap_json_as_sse(resp) if is_stream else resp
