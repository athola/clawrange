"""Chat commands answered inside the proxy.

Covers /tier, !help, !task, /sched, /scan and /projects. Each returns a
synthetic response and never reaches a provider. The /persona command stays
in `llm_proxy` with its persona helpers. Shared helpers and state are read
from `llm_proxy` at call time, so this module never touches `llm_proxy` at
import time.
"""

import hashlib
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

import github_search
import heartbeat
import llm_proxy
import reddit_search
import tenant_profile
from brain_db import project_list


async def _handle_tier_command() -> JSONResponse:
    """Return current tier status, balance, and circuit breaker state."""
    lines = ["Tier Status\n"]
    for t in llm_proxy.tier_snapshot():
        lines.append(f"  [{t['status']}] {t['name']} — {t['description']}")

    remaining = await llm_proxy._check_openrouter_balance()
    lines.append("")
    if remaining is not None:
        lines.append(
            f"OpenRouter balance: ${remaining:.2f} remaining "
            f"(floor: ${llm_proxy.OPENROUTER_BALANCE_FLOOR:.2f})"
        )
    else:
        lines.append("OpenRouter balance: not configured (set OPENROUTER_API_KEY)")

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
  !task tail               Show recently completed tasks
  !task cancel <id>        Cancel a pending/active task
  !task priority <id> <1-5> Change priority (1=urgent, 5=low)

BRAIN (Knowledge)
  !remember <slug> <info>  Append info to a page's timeline
  !recall <query>          Search the brain for matching pages
  !page <slug>             Show a full page with timeline

  Brain slugs use hierarchy: client/acme-corp, incident/wifi-site2, person/bob-smith
  Page types: client, system, incident, decision, note, person, company, project

PERSONA
  !persona <feedback>      Queue a persona enhancement as a [DRAFT] proposal
  !learn <feedback>        Same as !persona <feedback>
  !persona reflect         Review recent activity and queue suggestions

SYSTEM STATUS
  !tier                    Show LLM tier status and balance
  !status                  Same as !tier
  !help                    This command reference

BRAIN API (via web_fetch)
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

TASK API (via web_fetch)
  POST /task                     Create task
  GET  /task                     List tasks (?status=pending)
  GET  /task/{id}                Get task by ID
  POST /task/{id}/claim          Mark task active
  POST /task/{id}/result         Complete task with result
  DELETE /task/{id}              Cancel task

HEALTH
  GET  /healthz                  Service health + brain status
  GET  /tier                     Tier status JSON

MARKETING
  /projects list                 List tracked GitHub projects
  /projects show <slug>          Show project details
  /projects add <slug> <owner>/<repo> [--topics ...] [--subs ...]
  /projects rm <slug>

  /sched list                    List scheduled jobs
  /sched add <name> cron "<expr>" -- <generator>
  /sched pause|resume|rm|run <id|name>

  /scan reddit <topic> [--subs ...] [--project <slug>]
  /scan github <topic> [--kind repos|issues] [--stars 5]
  /scan github traffic [slug]    Pull repo traffic stats
  /scan web <prompt>             LLM web search

  /marketing                     Alias: /sched run morning_scan"""
    return llm_proxy._synthetic_response(text)


# ─── !task Command ──────────────────────────────────────────────


_TASK_ACTIONS = ("cancel", "priority", "list", "tail")

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
    stripped = msg.strip()
    lower = stripped.lower()

    for prefix in _MARKETING_PREFIXES:
        if lower.startswith(prefix):
            rest = stripped[len(prefix) :].strip()
            verb = prefix.lstrip("/!")
            if verb == "marketing":
                return {"verb": "sched", "subcmd": "run", "args": "morning_scan"}
            if not rest:
                return {"verb": verb, "subcmd": "list", "args": ""}
            # Parse subcommand
            parts = rest.split(None, 1)
            subcmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            return {"verb": verb, "subcmd": subcmd, "args": args}

    return None


async def _dispatch_marketing_command(
    cmd: dict, is_stream: bool
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
        text = f"Error: {exc}"

    resp = llm_proxy._synthetic_response(text)
    if is_stream:
        return llm_proxy._wrap_json_as_sse(resp)
    return resp


async def _handle_sched_command(subcmd: str, args: str) -> str:
    """Handle /sched subcommands."""
    from app import brain_db

    if subcmd == "list":
        scheds = brain_db.list_schedules()
        if not scheds:
            return (
                "No schedules configured. Use /sched add <name> cron "
                '"<expr>" -- <generator>'
            )
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

        from generators import GENERATORS

        if kind not in GENERATORS:
            return (
                f"Unknown generator: {kind}. Available: {', '.join(GENERATORS.keys())}"
            )

        from scheduler import add_schedule

        sched_id = hashlib.md5(name.encode()).hexdigest()[:8]

        await add_schedule(
            None,
            brain_db,
            sched_id,
            name,
            kind,
            cron_expr,
            {"topic": " ".join(after_dash[1:])} if after_dash[1:] else {},
        )
        return (
            f"Schedule added: {name} ({sched_id})\n  cron: {cron_expr}\n  kind: {kind}"
        )

    if subcmd in ("pause",):
        sched = brain_db.get_schedule(args.strip())
        if not sched:
            return f"Schedule not found: {args}"
        brain_db.set_schedule_paused(sched["id"], True)
        return f"Paused: {sched['name']} ({sched['id']})"

    if subcmd in ("resume",):
        sched = brain_db.get_schedule(args.strip())
        if not sched:
            return f"Schedule not found: {args}"
        brain_db.set_schedule_paused(sched["id"], False)
        return f"Resumed: {sched['name']} ({sched['id']})"

    if subcmd in ("rm", "remove", "delete"):
        from scheduler import remove_schedule

        removed = await remove_schedule(None, brain_db, args.strip())
        if not removed:
            return f"Schedule not found: {args}"
        return f"Removed: {args}"

    if subcmd == "run":
        from scheduler import run_schedule_now

        try:
            result = await run_schedule_now(None, brain_db, args.strip())
            return (
                f"Ran schedule: {result.get('schedule_id', args)} — "
                f"{result.get('status', 'unknown')}"
            )
        except ValueError as e:
            return str(e)

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

        results: list = await reddit_search.search_subreddits(
            topic, subreddits, since=since, limit_per_sub=10
        )

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

    if subcmd == "github":
        # /scan github <topic> [--kind repos|issues] [--stars 5]
        parts = args.split()
        topic = parts[0] if parts else ""
        kind = "repos"
        min_stars = 0

        for i, p in enumerate(parts):
            if p == "--kind" and i + 1 < len(parts):
                kind = parts[i + 1]
            elif p == "--stars" and i + 1 < len(parts):
                try:
                    min_stars = int(parts[i + 1])
                except ValueError:
                    pass

        if not topic:
            return "Usage: /scan github <topic> [--kind repos|issues] [--stars 5]"

        if kind == "issues":
            results = await github_search.search_issues(topic)
        else:
            results = await github_search.search_repos(topic, min_stars=min_stars)

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
        result = await llm_proxy._llm_call(args, max_tokens=1000, web_search=True)
        return result or "No results from web search."

    if subcmd == "github" and args.lower().startswith("traffic"):
        parts = args.split()
        slug = parts[1] if len(parts) > 1 else ""
        if not slug:
            projects = brain_db.list_projects()
            if not projects:
                return "No projects tracked. Use /projects add first."
            lines = []
            for proj in projects:
                traffic = await github_search.get_self_traffic(
                    proj["owner"], proj["repo"]
                )
                if traffic:
                    lines.append(
                        f"{proj['owner']}/{proj['repo']}\n"
                        f"  Views: {traffic.views_count} "
                        f"({traffic.views_uniques} unique)\n"
                        f"  Clones: {traffic.clones_count} "
                        f"({traffic.clones_uniques} unique)"
                    )
                else:
                    lines.append(
                        f"{proj['owner']}/{proj['repo']}: traffic unavailable "
                        f"(needs GITHUB_PAT)"
                    )
            return "\n\n".join(lines) if lines else "No traffic data available."

        project = brain_db.get_project(slug)
        if not project:
            return f"Project not found: {slug}"

        traffic = await github_search.get_self_traffic(
            project["owner"], project["repo"]
        )
        if not traffic:
            return "Traffic unavailable — needs GITHUB_PAT with repo scope"
        return (
            f"Traffic for {project['owner']}/{project['repo']} (14 days)\n"
            f"  Views: {traffic.views_count} ({traffic.views_uniques} unique)\n"
            f"  Clones: {traffic.clones_count} ({traffic.clones_uniques} unique)"
        )

    return (
        "Usage:\n"
        "  /scan reddit <topic> [--subs sub1,sub2] [--project <slug>]\n"
        "  /scan github <topic> [--kind repos|issues] [--stars 5]\n"
        "  /scan github traffic [project-slug]\n"
        "  /scan web <prompt>"
    )


def _parse_scan_args(args: str) -> tuple[str, list[str], str | None, str]:
    """Parse scan arguments, extracting topic, subreddits, project, since."""
    parts = args.split()
    topic_parts: list[str] = []
    subreddits: list[str] = []
    project_slug = None
    since = "7d"
    i = 0
    while i < len(parts):
        if parts[i] == "--subs" and i + 1 < len(parts):
            subreddits = parts[i + 1].split(",")
            i += 2
        elif parts[i] == "--project" and i + 1 < len(parts):
            project_slug = parts[i + 1]
            i += 2
        elif parts[i] == "--since" and i + 1 < len(parts):
            since = parts[i + 1]
            i += 2
        else:
            topic_parts.append(parts[i])
            i += 1
    return " ".join(topic_parts), subreddits, project_slug, since


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

        topics: list[str] = []
        subs: list[str] = []
        posture = ""
        i = 2
        while i < len(parts):
            if parts[i] == "--topics" and i + 1 < len(parts):
                topics = parts[i + 1].split(",")
                i += 2
            elif parts[i] == "--subs" and i + 1 < len(parts):
                subs = parts[i + 1].split(",")
                i += 2
            elif parts[i] == "--posture" and i + 1 < len(parts):
                posture = parts[i + 1]
                i += 2
            else:
                i += 1

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
            rest_lower = rest.lower()
            # Check for sub-actions
            for action in _TASK_ACTIONS:
                if rest_lower.startswith(action):
                    args = rest[len(action) :].strip()
                    if action == "list":
                        return {"type": "list"}
                    if action == "tail":
                        return {"type": "tail"}
                    return {"type": "action", "action": action, "args": args}
            # Default: create a new task
            if rest:
                return {"type": "create", "description": rest}
            return {"type": "list"}

    return None


async def _dispatch_task_command(
    cmd: dict, is_stream: bool
) -> JSONResponse | StreamingResponse:
    """Route parsed task commands to handlers."""
    if cmd["type"] == "list":
        return await _handle_tasks_list(is_stream)
    elif cmd["type"] == "tail":
        return await _handle_tasks_tail(is_stream)
    elif cmd["type"] == "create":
        return await _handle_task_command(cmd["description"], is_stream)
    elif cmd["type"] == "action":
        return await _handle_task_action(cmd["action"], cmd["args"], is_stream)
    return llm_proxy._synthetic_response(
        "Unknown task command. Try: !task <description> or !tasks"
    )


async def _handle_task_command(
    description: str, is_stream: bool
) -> JSONResponse | StreamingResponse:
    """Enqueue a task via the internal API and return confirmation."""
    from app import TaskCreate as TC
    from app import create_task

    try:
        task = create_task(TC(description=description))
        text = (
            f"Task queued (#{task['id']})\n"
            f"  {task['description']}\n"
            f"  Priority: {task['priority']} | Status: {task['status']}\n"
            f"Max Ops will pick this up on the next heartbeat cycle."
        )
    except Exception as exc:
        text = f"Failed to queue task: {exc}"

    resp = llm_proxy._synthetic_response(text)
    if is_stream:
        return llm_proxy._wrap_json_as_sse(resp)
    return resp


async def _handle_tasks_list(is_stream: bool) -> JSONResponse | StreamingResponse:
    """Show the current task queue."""
    from app import list_tasks

    data = list_tasks()
    tasks = data["tasks"]
    if not tasks:
        text = "Task queue is empty. Send !task <description> to add one."
    else:
        pending = [t for t in tasks if t["status"] == "pending"]
        active = [t for t in tasks if t["status"] == "active"]
        done = [t for t in tasks if t["status"] in ("completed", "failed")]

        lines = [
            f"Task Queue ({len(pending)} pending, {len(active)} active, "
            f"{len(done)} done)\n"
        ]

        if active:
            lines.append("NOW PROCESSING:")
            for t in active:
                src = (
                    heartbeat._operator_label() if t.get("source") == "user" else "SYS"
                )
                lines.append(
                    f"  [{src}] #{t['id']} [P{t['priority']}] {t['description'][:55]}"
                )
            lines.append("")

        if pending:
            lines.append("QUEUED:")
            for t in pending:
                src = (
                    heartbeat._operator_label() if t.get("source") == "user" else "SYS"
                )
                lines.append(
                    f"  [{src}] #{t['id']} [P{t['priority']}] {t['description'][:55]}"
                )
            lines.append("")

        if done:
            lines.append("RECENT:")
            for t in done[-3:]:  # Show last 3
                marker = "✅" if t["status"] == "completed" else "❌"
                lines.append(f"  {marker} #{t['id']} {t['description'][:40]}")
                if t["result"]:
                    lines.append(f"     → {t['result'][:80]}")

        lines.append(
            "\nCommands: !task <desc> | !task cancel <id> | !task priority <id> <1-5>"
        )
        text = "\n".join(lines)

    resp = llm_proxy._synthetic_response(text)
    if is_stream:
        return llm_proxy._wrap_json_as_sse(resp)
    return resp


async def _handle_tasks_tail(is_stream: bool) -> JSONResponse | StreamingResponse:
    """Show recently completed/failed tasks with results."""
    from app import list_tasks

    data = list_tasks()
    done = [t for t in data["tasks"] if t["status"] in ("completed", "failed")]
    if not done:
        text = "No completed tasks yet."
    else:
        lines = [f"Recent Tasks ({len(done)} completed)\n"]
        for t in done[-5:]:  # Last 5
            marker = "✅" if t["status"] == "completed" else "❌"
            lines.append(f"{marker} #{t['id']} — {t['description'][:50]}")
            if t["result"]:
                lines.append(f"   {t['result'][:120]}")
            if t["completed_at"]:
                lines.append(f"   Completed: {t['completed_at'][:19]}")
            lines.append("")
        text = "\n".join(lines)

    resp = llm_proxy._synthetic_response(text)
    if is_stream:
        return llm_proxy._wrap_json_as_sse(resp)
    return resp


async def _handle_task_action(
    action: str, args: str, is_stream: bool
) -> JSONResponse | StreamingResponse:
    """Handle task management subcommands: cancel, priority."""
    from app import brain_db

    parts = args.strip().split(None, 1)
    task_id = parts[0] if parts else ""
    task = brain_db.get_task(task_id)

    if not task:
        text = f"Task #{task_id} not found."
    elif action == "cancel":
        if task["status"] in ("completed", "failed"):
            text = f"Task #{task_id} already {task['status']}."
        else:
            brain_db.cancel_task(task_id)
            text = f"Task #{task_id} cancelled."
    elif action == "priority":
        new_p = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if new_p is None or not 1 <= new_p <= 5:
            text = f"Usage: !task priority {task_id} <1-5>"
        else:
            brain_db.update_priority(task_id, new_p)
            text = f"Task #{task_id} priority set to {new_p}."
    else:
        text = f"Unknown action: {action}. Try: cancel, priority"

    resp = llm_proxy._synthetic_response(text)
    if is_stream:
        return llm_proxy._wrap_json_as_sse(resp)
    return resp
