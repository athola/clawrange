"""Heartbeat interceptor: answer OpenClaw's 10-minute heartbeat in the proxy.

A heartbeat message never reaches a provider. The handler works the next
pending task, or invents a grounded one, and reports through the digest.
Proactive timers, the focus rotation index and the LLM call helpers stay in
`llm_proxy` and are read there at call time, so test resets and patches on
them keep working and this module never touches `llm_proxy` at import time.
"""

import asyncio
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.responses import JSONResponse, StreamingResponse

import digest
import llm_proxy
import research_router
import telegram
import tenant_profile

# ─── Heartbeat Interceptor ───────────────────────────────────────


_HEARTBEAT_MARKER = "read heartbeat.md"


# How often each proactive check can fire (seconds).
_PROACTIVE_INTERVALS = {
    "stale_tasks": 1800,  # 30 min — nudge about tasks stuck pending
    "llm_thinking": 900,  # 15 min — balance responsiveness vs churn
    "pending_reminder": 3600,  # 1 hr — remind the operator of P3+ tasks to review
}

# Path to soul.md inside the container (mounted read-only).
_SOUL_MD_PATH = Path(
    os.environ.get("SOUL_MD_PATH") or Path(__file__).parent / "soul.md"
)


# Task prefixes owned by other consumers. The heartbeat must leave these
# pending: `research:tome:` tasks belong to scripts/tome_bridge.py (keep in
# sync with its _TOME_PATTERNS); `[DRAFT] persona` mirrors a learning the
# operator approves via /persona/*, and `[DRAFT] income:` is claimed by the
# operator. `[DRAFT] Comment draft` tasks stay heartbeat-owned: they ask the
# LLM worker to write the draft that the operator then reviews.
_FOREIGN_TASK = re.compile(
    r"^\s*(research:tome:|\[research:tome\]|research via tome:"
    r"|\[DRAFT\]\s*persona\b|\[DRAFT\]\s*income:)",
    re.IGNORECASE,
)


def _next_heartbeat_task(pending: list[dict]) -> dict | None:
    """First pending task the heartbeat owns, or None."""
    for task in pending:
        if not _FOREIGN_TASK.match(task.get("description") or ""):
            return task
    return None


def _load_soul() -> str:
    """Persona context for self-directed prompts. Empty string if missing.

    Prefers the rendered OpenClaw workspace SOUL.md, which carries approved
    learnings. The bundled soul.md is a read-only bind of the repo template
    and never sees them.
    """
    state = os.environ.get("OPENCLAW_STATE_DIR")
    candidates = [Path(state) / "workspace" / "SOUL.md"] if state else []
    candidates.append(_SOUL_MD_PATH)
    for path in candidates:
        try:
            return path.read_text().strip()
        except OSError:
            continue
    return ""


# Persona sections that hold pure character backstory. When a prompt
# routes to the `:online` search tier, the search runs on the whole prompt,
# so a fictional persona's names pull lore pages to the top of results and
# pollute research output. Which sections to strip, and which citations to
# drop, are persona data: `assistant.search_hygiene` in the profile.
# Operational sections are kept so the model still understands its job.
def _backstory_headings() -> tuple[str, ...]:
    """Persona sections kept out of search-tier prompts, from the profile's
    `assistant.search_hygiene.strip_headings` (persona text is tenant data)."""
    hygiene = tenant_profile.search_hygiene()
    return tuple(h.strip() for h in hygiene.get("strip_headings") or [])


def _sentence_start(text: str) -> str:
    """Upper-case only the first letter ("the operator" -> "The operator",
    "Mary Ann" stays "Mary Ann"); str.capitalize lowercases the rest."""
    return text[:1].upper() + text[1:]


def _operator_label() -> str:
    """Tag for operator-created tasks in listings: the configured name in
    capitals, else YOU."""
    name = tenant_profile.operator_name()
    return "YOU" if name == "the operator" else name.upper()


def _strip_persona_backstory(soul: str, headings: tuple[str, ...] | None = None) -> str:
    """Remove persona-backstory sections from soul.md text.

    A section runs from its `## Heading` line to (but not including) the
    next `## ` heading or end-of-file. Only the sections named in
    `headings` (default: the profile's search_hygiene.strip_headings) get
    stripped; the rest is returned intact.

    Empty or whitespace-only input returns "" so callers can use the
    result directly in prompt templates without an extra falsy check.
    """
    if not soul or not soul.strip():
        return ""
    strip = set(headings if headings is not None else _backstory_headings())
    lines = soul.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("## "):
            skipping = line.strip() in strip
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    return "\n".join(out).strip()


# Self-invented work is grounded in subjects that exist -- tracked projects
# and homelab deal targets -- and must be research the worker can run
# (_try_research_task). Free-form categories ("client outreach", "financial
# review", "wellness") produced ~90 tasks by 2026-09 that needed a CRM,
# billing data, a calendar or internal docs, and all dead-ended.


def _thinking_subjects(projects: list[dict], deals_block: dict | None) -> list[str]:
    """Project slugs, then deal target names without their parenthetical."""
    subjects = [p["slug"] for p in projects if p.get("slug")]
    for t in (deals_block or {}).get("targets") or []:
        name = re.sub(r"\s*\(.*?\)", "", t.get("name", "")).strip()
        if name and name not in subjects:
            subjects.append(name)
    return subjects


def _deals_block() -> dict | None:
    from app import app

    profile = getattr(app.state, "profile", None)
    return profile.raw.get("homelab_deals") if profile else None


def _current_thinking_subjects() -> list[str]:
    from app import brain_db

    try:
        return _thinking_subjects(brain_db.list_projects(), _deals_block())
    except Exception:
        llm_proxy.logger.exception("thinking: could not load subjects")
        return []


def _thinking_context() -> str:
    """Stack gaps from the deals profile, so hardware research has a why."""
    gaps = (_deals_block() or {}).get("stack_gaps") or {}
    if not gaps:
        return ""
    return "Stack gaps: " + "; ".join(f"{k}: {v}" for k, v in gaps.items())


def _squash_words(text: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split()) + " "


def _is_grounded_suggestion(text: str, subjects: list[str]) -> bool:
    """Runnable research (the research router would take it) about a subject
    that exists. Both halves matter: "query the docs" names a real project
    but no worker can run it; "search the web for inactive clients" is
    runnable but about nothing real."""
    if not (
        research_router._RESEARCH_INTENT.search(text)
        and research_router._RESEARCH_TARGET.search(text)
    ):
        return False
    words = _squash_words(text)
    return any(_squash_words(s) in words for s in subjects)


def _build_thinking_prompt(subjects: list[str], context: str = "") -> str:
    """Ask for one research task about a listed subject."""
    soul = _load_soul()
    recent_descriptions = []
    try:
        from app import brain_db

        cutoff = datetime.now(UTC) - timedelta(hours=24)
        for t in brain_db.list_tasks():
            created = llm_proxy._parse_task_time(t.get("created_at"))
            if created and created > cutoff:
                recent_descriptions.append(t["description"])
    except Exception:
        pass
    recent_block = ""
    if recent_descriptions:
        recent_list = "\n".join(f"  - {d}" for d in recent_descriptions[-10:])
        recent_block = (
            "\nTasks already created in the last 24 hours "
            f"(DO NOT repeat or suggest anything similar):\n{recent_list}\n"
        )
    focus = subjects[llm_proxy._thinking_focus_index % len(subjects)]
    llm_proxy._thinking_focus_index += 1
    return (
        (f"{soul}\n\n---\n" if soul else "")
        + "Suggest ONE task for yourself. The only work you can finish is "
        "searching Reddit, GitHub, Hacker News, arXiv or the web and "
        "summarizing what you find. You cannot read internal docs, billing, "
        "calendars, a CRM or private data.\n\n"
        "Real subjects (use one, spelled exactly as listed):\n"
        + "\n".join(f"  - {s}" for s in subjects)
        + "\n"
        + (f"{context}\n" if context else "")
        + f"Prefer this cycle's focus: {focus}\n"
        + recent_block
        + "\nRULES:\n"
        "- Start with Research, Search, Find or Scan, name one subject from "
        "the list, and name where to look (Reddit, GitHub, web, arXiv).\n"
        f"- Never suggest a task that needs information only "
        f"{tenant_profile.operator_name()} can supply "
        "-- their clients, priorities, or private history.\n"
        "- Do NOT invent clients, people, events, or subjects not listed.\n\n"
        "Respond with ONLY the task description (one sentence, no quotes)."
    )


def _is_heartbeat(msg: str) -> bool:
    """Detect heartbeat messages from max-ops agent."""
    return msg.lower().strip().startswith(_HEARTBEAT_MARKER)


def _proactive_ready(check_name: str) -> bool:
    """Return True if enough time has passed since the last run of this check."""
    interval = _PROACTIVE_INTERVALS.get(check_name, 3600)
    last_run = llm_proxy._proactive_state.get(check_name)
    return last_run is None or (time.monotonic() - last_run) >= interval


def _proactive_mark(check_name: str) -> None:
    """Record that a proactive check just ran."""
    llm_proxy._proactive_state[check_name] = time.monotonic()


def _has_recent_task(queue: list[dict], keyword: str, hours: int = 24) -> bool:
    """Check if a semantically similar task exists in the last N hours.

    Uses keyword overlap: if >=2 meaningful words overlap between the new
    suggestion and an existing task, it's considered a duplicate.  The
    threshold scales with the smaller keyword set (at least 30% overlap)
    so short descriptions aren't unfairly penalised.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    new_keywords = llm_proxy._extract_keywords(keyword)
    if not new_keywords:
        return False
    for t in queue:
        try:
            created = datetime.fromisoformat(t["created_at"])
            if created <= cutoff:
                continue
        except (ValueError, KeyError):
            continue
        existing_keywords = llm_proxy._extract_keywords(t["description"])
        overlap = new_keywords & existing_keywords
        smaller = min(len(new_keywords), len(existing_keywords)) or 1
        if len(overlap) >= max(2, int(smaller * 0.3)):
            return True
    return False


async def _llm_suggest_task(subjects: list[str], context: str = "") -> str | None:
    """Ask the LLM for one proactive task suggestion."""
    text = await llm_proxy._llm_call(
        _build_thinking_prompt(subjects, context), max_tokens=100
    )
    if text:
        text = text.strip('"')
        if 10 <= len(text) <= 200 and "\n" not in text:
            print(f"[PROXY] LLM suggested task: {text!r}", flush=True)
            return text
    return None


async def _gather_system_state() -> str:
    """Collect current system state for task context."""
    from app import brain_db

    # Tier status
    tier_lines = []
    for tier in llm_proxy.CONFIG["tiers"]:
        name = tier["name"]
        status = (
            "TRIPPED"
            if llm_proxy._circuit_open(name, probe=False)
            else ("ACTIVE" if name == llm_proxy._last_tier_used else "ready")
        )
        tier_lines.append(f"  {name}: {status} — {tier['description']}")

    # Balance
    remaining = await llm_proxy._check_openrouter_balance()
    balance = f"${remaining:.2f}" if remaining is not None else "not configured"

    # Task queue summary
    all_tasks = brain_db.list_tasks()
    pending = sum(1 for t in all_tasks if t["status"] == "pending")
    active = sum(1 for t in all_tasks if t["status"] == "active")
    done = sum(
        1 for t in all_tasks if t["status"] in ("completed", "failed", "blocked")
    )

    return (
        "CURRENT SYSTEM STATE:\n"
        "Tiers:\n" + "\n".join(tier_lines) + "\n"
        f"OpenRouter balance: {balance} "
        f"(floor: ${llm_proxy.OPENROUTER_BALANCE_FLOOR:.2f}). "
        "zai-direct is a separate provider with its own quota and is not "
        "affected by this balance.\n"
        f"Last tier used: {llm_proxy._last_tier_used or 'none'}\n"
        f"Task queue: {pending} pending, {active} active, {done} done"
    )


async def _build_work_prompt(task_description: str, web_search: bool = False) -> str:
    """Build a prompt for the LLM to work on a specific task, with live system data.

    When `web_search=True`, the prompt routes through OpenRouter `:online`
    which runs server-side search over the full prompt body. Persona
    backstory pulls lore pages to the top of results, so the profile's
    search_hygiene.strip_headings sections are removed for search-tier
    routes; non-search routes keep the full persona so the assistant stays
    in character for direct chat replies.
    """
    soul = _load_soul()
    if web_search:
        soul = _strip_persona_backstory(soul)
    state = await _gather_system_state()
    context = f"{soul}\n\n---\n" if soul else ""

    # Pull brain pages for grounding (what actually exists)
    brain_context = ""
    try:
        from app import brain_db

        pages = brain_db.list_pages(limit=10)
        if pages:
            page_list = ", ".join(f"{p['slug']} ({p['page_type']})" for p in pages)
            brain_context = f"\nKnown entities in brain: {page_list}\n"
        else:
            brain_context = (
                "\nBrain is empty — no clients, people, or incidents recorded yet.\n"
            )
    except Exception:
        pass

    format_rules = (
        (
            "OUTPUT FORMAT (for Telegram delivery):\n"
            "- Include full URLs to posts/threads you find.\n"
            "- For each result: link, why it's relevant, which project it maps to, "
            f"and a suggested comment {tenant_profile.operator_name()} can post.\n"
            "- Use numbered items, not bullet nesting.\n"
            "- Be specific — real post titles, real subreddits, real URLs.\n"
        )
        if web_search
        else ""
    )

    # Detect marketing tasks and inject anti-pattern rules
    marketing_context = ""
    desc_lower = task_description.lower()
    marketing_keywords = {
        "marketing",
        "awesome-list",
        "reddit",
        "subreddit",
        "comment",
        "launch",
        "post",
        "draft",
    }
    if any(kw in desc_lower for kw in marketing_keywords):
        marketing_context = research_router.ANTI_PATTERN_RULES + "\n"

    return (
        f"{context}"
        f"{state}\n"
        f"{brain_context}\n"
        f"---\n"
        f"You have been assigned this task:\n"
        f'"{task_description}"\n\n'
        f"{marketing_context}"
        "RULES:\n"
        "- Reference real data from the system state, brain, and web search results.\n"
        "- Do NOT invent URLs, post titles, or usernames. Only cite what you found.\n"
        "- If you have web search, use it to find live data before answering.\n"
        "- If the task requires an action you can't perform (sending email, "
        "making calls), "
        f"describe what you prepared and what {tenant_profile.operator_name()} "
        "needs to do to finish it.\n"
        "- Be honest. 'I found nothing relevant today' is better than fabricating.\n\n"
        f"{format_rules}"
        "Provide the result. Include specific links or references when available."
    )


_WEB_SEARCH_KEYWORDS = {
    "reddit",
    "search",
    "rundown",
    "news",
    "trending",
    "posts",
    "online",
    "web",
    "latest",
    "recent",
    "scan",
    "find online",
    "hacker news",
    "hn",
    "lobsters",
    "twitter",
    "x.com",
}


def _task_needs_web(description: str) -> bool:
    """Return True if the task description suggests it needs live web data."""
    desc_lower = description.lower()
    return any(kw in desc_lower for kw in _WEB_SEARCH_KEYWORDS)


async def _llm_work_task(description: str) -> str:
    """Ask the LLM to actually work on a task. Returns the result text."""
    needs_web = _task_needs_web(description)
    prompt = await _build_work_prompt(description, web_search=needs_web)
    if needs_web:
        print(f"[PROXY] task needs web search: {description[:80]!r}", flush=True)
    max_tok = 1500 if needs_web else 500
    result = await llm_proxy._llm_call(prompt, max_tokens=max_tok, web_search=needs_web)
    if result:
        print(f"[PROXY] LLM worked task: {result[:100]!r}", flush=True)
        return result
    return (
        f"Could not reach LLM to work this task. "
        f"{_sentence_start(tenant_profile.operator_name())} should handle manually: "
        f"{description}"
    )


_ALERT_TASK_PREFIXES = ("low balance alert:", "investigate tier recovery:")


def _is_status_alert(description: str) -> bool:
    """True for tasks the heartbeat's proactive scan created to flag state.

    These close with live facts below — sending them through the LLM
    dramatizes them into alarmist reports and can assert stale numbers
    (the value in the description is minutes old, or a phantom from a
    superseded balance source).
    """
    return description.strip().lower().startswith(_ALERT_TASK_PREFIXES)


def _status_alert_result(description: str, remaining: float | None) -> str:
    """Factual closing note for a status-alert task, from live state."""
    balance = f"${remaining:.2f}" if remaining is not None else "unknown"
    if remaining is None:
        routing = "balance unknown (credits API unreachable)"
    elif remaining <= 0:
        routing = "openrouter skipped (depleted), zai-direct carrying traffic"
    else:
        routing = "openrouter active"
    lowered = description.lower()
    if lowered.startswith("investigate tier recovery:"):
        name = description.split(":", 1)[1].strip()
        if llm_proxy._circuit_open(name, probe=False):
            state = "TRIPPED (circuit open)"
        else:
            state = "recovered (circuit closed)"
        return f"Tier {name}: {state}. OpenRouter balance: {balance}."
    floor = f"${llm_proxy.OPENROUTER_BALANCE_FLOOR:.0f}"
    return f"Live status: OpenRouter balance {balance} (floor {floor}); {routing}."


# Total ceiling for one heartbeat task's work. OpenClaw's client times out the
# heartbeat request (see LLM_CALL_DEADLINE above), so stay well under it.
HEARTBEAT_WORK_DEADLINE_DEFAULT = 90.0


def _heartbeat_work_deadline() -> float:
    """Seconds allowed for one heartbeat task (env HEARTBEAT_WORK_DEADLINE)."""
    raw = os.environ.get("HEARTBEAT_WORK_DEADLINE", "")
    try:
        value = float(raw)
    except ValueError:
        return HEARTBEAT_WORK_DEADLINE_DEFAULT
    return value if value > 0 else HEARTBEAT_WORK_DEADLINE_DEFAULT


async def _run_heartbeat_task(task: dict, brain_db, remaining: float | None) -> str:
    """Do one claimed task's work and return its result text.

    Structured interception, in order: self-created status alerts (closed
    with live facts, never the LLM), then marketing scans, then
    research-shaped tasks (web-capable orchestrator), then the LLM.
    """
    description = task["description"]
    if _is_status_alert(description):
        return _status_alert_result(description, remaining)
    scan_result = await research_router._try_marketing_scan(description, brain_db)
    if scan_result is not None:
        return scan_result
    research_result = await research_router._try_research_task(description)
    if research_result is not None:
        return research_result
    return await _llm_work_task(description)


async def _handle_heartbeat(is_stream: bool) -> JSONResponse | StreamingResponse:
    """Run heartbeat checks in Python instead of relying on the LLM.

    Three layers of initiative:
    1. Infrastructure monitoring — tripped tiers, low balance (every cycle)
    2. Task queue awareness — stale task nudges (every 30 min)
    3. LLM-powered thinking — self-directed task suggestions (every 1 hr)

    Everything worth reporting buffers into the hourly digest instead of
    messaging per event. At most once per HEARTBEAT_DIGEST_INTERVAL the
    flush sends each item straight to Telegram (one message per item) and
    returns a short closing summary as this response, which OpenClaw
    relays — so long results arrive whole instead of clipped to fit the
    single relayed message.
    """
    from app import brain_db

    digest.record_heartbeat_seen()

    tasks_created: list[dict] = []

    # ── 1. Tier status ──────────────────────────────────────────
    tripped = [
        tier["name"]
        for tier in llm_proxy.CONFIG["tiers"]
        if llm_proxy._circuit_open(tier["name"], probe=False)
    ]

    remaining = await llm_proxy._check_openrouter_balance()

    all_tasks = brain_db.list_tasks()

    # ── 2. Check for pending tasks ──────────────────────────────
    # Process one task per cycle — both system-generated and user-created.
    pending = brain_db.list_tasks(status="pending")
    task = _next_heartbeat_task(pending)
    skipped = False
    label = "SYSTEM"

    if task:
        source = task.get("source", "system")
        label = _operator_label() if source == "user" else "SYSTEM"
        try:
            claimed = brain_db.claim_task(task["id"])
        except ValueError as exc:
            # Another consumer won the race (or the task was cancelled):
            # leave it alone this cycle, never mark it failed.
            print(f"[PROXY] claim skipped for #{task['id']}: {exc}", flush=True)
            claimed = None
        if not claimed:
            # Lost the race / not pending: skip this cycle entirely.
            task = None
            skipped = True

    if task:
        deadline = _heartbeat_work_deadline()
        try:
            result = await asyncio.wait_for(
                _run_heartbeat_task(task, brain_db, remaining), timeout=deadline
            )
            failure: str | None = None
        except TimeoutError:
            result = ""
            failure = f"timed out after {deadline:g}s"
        except Exception as exc:
            llm_proxy.logger.exception("heartbeat task #%s failed", task["id"])
            result = ""
            failure = f"{type(exc).__name__}: {str(exc)[:200]}"

        try:
            if failure is not None:
                llm_proxy.logger.warning(
                    "heartbeat task #%s failed: %s", task["id"], failure
                )
                brain_db.complete_task(task["id"], f"Failed: {failure}", "failed")
                digest._digest_record(
                    f"[{label}] #{task['id']}: {task['description']}\nFailed: {failure}"
                )
            elif llm_proxy._needs_input(result):
                # The model asked the operator a question instead of finishing.
                # Close it as blocked so it leaves the pending queue -- left
                # pending it is reworked every cycle -- and spend one digest
                # line on it.
                brain_db.complete_task(task["id"], result, "blocked")
                digest._digest_record(llm_proxy._blocked_digest_line(task))
            else:
                brain_db.complete_task(task["id"], result, "completed")
                # One digest item per task; the result gets most of a Telegram
                # message now that items flush as separate messages. The full
                # result still lives on the task (!tasks / GET /task/{id}).
                clipped = digest._clip_result(result, limit=digest.DIGEST_RESULT_LIMIT)
                digest._digest_record(
                    f"[{label}] #{task['id']}: {task['description']}\nResult: {clipped}"
                )
        except ValueError as exc:
            # Task was cancelled or finished elsewhere meanwhile.
            llm_proxy.logger.warning("could not complete task #%s: %s", task["id"], exc)

        # No direct Telegram notification: the digest line above carries
        # the completion, at most once per hour.

    elif not skipped:
        # ── PROACTIVE SCAN ──────────────────────────────────────
        # (all_tasks is fetched before the pending branch: the digest
        # footer also needs it to re-surface blocked questions.)

        # Layer 1: Infrastructure — every cycle
        for name in tripped:
            desc = f"Investigate tier recovery: {name}"
            # 24h dedup regardless of status — pending-only dedup had the
            # same recreate-after-complete loop as the balance alert.
            if not _has_recent_task(all_tasks, desc, hours=24):
                t = brain_db.create_task(desc, priority=2)
                tasks_created.append(t)

        if remaining is not None and remaining < llm_proxy.OPENROUTER_BALANCE_FLOOR:
            desc = f"Low balance alert: ${remaining:.2f} remaining"
            # Any alert in the last 24h counts, completed or not. The old
            # pending-only dedup re-created the alert every cycle once the
            # previous one completed, spamming Telegram with duplicates.
            if not _has_recent_task(all_tasks, "Low balance alert", hours=24):
                t = brain_db.create_task(desc, priority=1)
                tasks_created.append(t)

        # Layer 2: Stale task awareness — every 30 min
        if _proactive_ready("stale_tasks"):
            _proactive_mark("stale_tasks")
            stale_cutoff = datetime.now(UTC) - timedelta(hours=4)
            stale = [
                t
                for t in all_tasks
                if t["status"] == "pending"
                and datetime.fromisoformat(t["created_at"]) < stale_cutoff
            ]
            if stale and not _has_recent_task(all_tasks, "stale tasks pending"):
                desc = f"{len(stale)} task(s) pending > 4 hours — review queue"
                t = brain_db.create_task(desc, priority=2)
                tasks_created.append(t)

        # Layer 3: LLM self-directed thinking, grounded in real subjects.
        # HEARTBEAT_LLM_SUGGEST=0 turns it off.
        subjects = (
            _current_thinking_subjects()
            if os.environ.get("HEARTBEAT_LLM_SUGGEST", "1") != "0"
            and not tasks_created
            and _proactive_ready("llm_thinking")
            else []
        )
        if subjects:
            _proactive_mark("llm_thinking")
            suggestion = await _llm_suggest_task(subjects, _thinking_context())
            if suggestion and not _is_grounded_suggestion(suggestion, subjects):
                # Prompt rules alone never held this line; drop it in code.
                print(f"[PROXY] dropped ungrounded task: {suggestion!r}", flush=True)
                suggestion = None
            if suggestion and not _has_recent_task(all_tasks, suggestion):
                t = brain_db.create_task(suggestion, priority=3)
                tasks_created.append(t)

    # ── 3. Build response ───────────────────────────────────────
    # Created tasks buffer into the digest alongside completions. The
    # response is non-empty only when the hourly digest is due, so the
    # 10-minute heartbeat stays silent on Telegram except once per hour
    # (OpenClaw relays non-empty heartbeat responses, not empty ones).
    for t in tasks_created:
        digest._digest_record(
            f"Created #{t['id']} [P{t['priority']}] {t['description'][:60]}"
        )

    if digest._digest_due():
        messages, text, owners, taken = digest._digest_take_ex(
            time.time(),
            remaining,
            tripped,
            waiting=llm_proxy._waiting_on_you_lines(all_tasks),
        )
        # Items go straight to Telegram — one message each — before the
        # response returns, so they land ahead of the relayed summary.
        # Sequential sends keep the burst in order (header, oldest first).
        failed_idx: set[int] = set()
        try:
            for i, msg in enumerate(messages):
                if not await telegram.notify(msg):
                    failed_idx.add(i)
        except BaseException:
            # Interrupted mid-burst: nothing unsent may be lost.
            failed_idx.update(range(len(messages)))
            digest._digest_commit(taken, owners, failed_idx)
            raise
        digest._digest_commit(taken, owners, failed_idx)
        failed = len(failed_idx)
        if failed:
            text += (
                f"\n{failed} digest message(s) failed to send — "
                "they stay buffered for the next digest; full results remain "
                "in the task queue (GET /task)."
            )
        # OpenClaw logs only Telegram *failures*, so a silent digest and a
        # dropped one look identical downstream. Log the handoff to give the
        # two logs a correlation point.
        print(
            f"[PROXY] digest flushed: {len(messages)} message(s), {failed} failed",
            flush=True,
        )
    else:
        text = ""
    resp = llm_proxy._synthetic_response(text)

    if is_stream:
        return llm_proxy._wrap_json_as_sse(resp)
    return resp
