"""Heartbeat interceptor: answer OpenClaw's 10-minute heartbeat in the proxy.

A heartbeat message never reaches a provider. The handler works the next
pending task, or invents a grounded one, and reports through the digest.
Heartbeat-only state (proactive timers, the focus rotation index) and the
task-result helpers live here. The LLM call, balance and circuit helpers stay
in `llm_proxy` and are read there at call time, so patches on them keep
working and this module never touches `llm_proxy` at import time.
"""

import asyncio
import logging
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
from errors import UpstreamError

logger = logging.getLogger("clawrange.heartbeat")

# ─── Heartbeat Interceptor ───────────────────────────────────────


# Start of the heartbeat prompt pinned in openclaw/config/openclaw.json
# (agents.entries.*.heartbeat.prompt); OpenClaw sends it verbatim.
_HEARTBEAT_MARKER = "clawrange heartbeat:"


# How often each proactive check can fire (seconds).
_PROACTIVE_INTERVALS = {
    "stale_tasks": 1800,  # 30 min — nudge about tasks stuck pending
    "llm_thinking": 900,  # 15 min — balance responsiveness vs churn
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

# ─── Heartbeat State ─────────────────────────────────────────────
# Reset per test by conftest._reset_proxy_globals.

# Track when proactive checks last fired (survives across heartbeat cycles,
# resets on container restart which is fine — fresh start, fresh initiative).
_proactive_state: dict[str, float] = {}

# Rotates the focus subject so consecutive cycles cover different ground.
_thinking_focus_index = 0


# ─── Keyword Matching ─────────────────────────────────────────────

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "he",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
        "this",
        "but",
        "they",
        "have",
        "had",
        "not",
        "all",
        "can",
        "her",
        "if",
        "our",
        "out",
        "so",
        "up",
        "what",
        "when",
        "which",
        "who",
        "do",
        "does",
        "any",
        "should",
        "ensure",
        "check",
        "review",
        "before",
        "after",
        "current",
    }
)


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful lowercase words (3+ chars, no stop words)."""
    words = re.findall(r"[a-z]+", text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOP_WORDS}


# ─── Task Results Blocked on the Operator ────────────────────────

# A task the model cannot finish without the operator comes back as a long, tidy
# markdown essay. llm_proxy._is_non_answer() cannot see it: that one only fires
# under 300 chars and these run past 1000.
#
# Match the status declaration in the HEADER, not a request for input
# anywhere in the body. Checked against the 846 task results stored when
# this was written:
#
#   "what i need from you"  -> 208 hits, nearly all real deliverables. The
#       work prompt asks for exactly that ("describe what you prepared and
#       what the operator needs to do to finish it"), so it is not a blocked signal.
#   header-anchored below   -> 21 hits (2.5%), including both essays that
#       had reached Telegram.
#
# Anchoring to the header also keeps a finished deliverable whose one
# sub-section is blocked ("Angle 2 (BLOCKED-needs live data)") from being
# binned whole.
_NEEDS_INPUT_HEAD = 400

_NEEDS_INPUT_SIGNALS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"status:\s*\**\s*blocked",
        r"\bblocked\b\s*[^a-z0-9]{0,4}\s*need",
        r"\bhold for now\b",
        r"before\s+[^.]{0,60}\bwe need\b",
    )
]


def _needs_input(text: str) -> bool:
    """True when a task result declares itself blocked on the operator's input."""
    if not text:
        return False
    head = text[:_NEEDS_INPUT_HEAD]
    return any(signal.search(head) for signal in _NEEDS_INPUT_SIGNALS)


def _blocked_digest_line(task: dict) -> str:
    """One compact digest line for a task stalled on the operator's input.

    The full text stays on the task (GET /task/{id}); Telegram gets the
    fact that something stalled and which task it was, not the essay.
    """
    return f"Blocked #{task['id']}: needs your input — {task['description'][:60]}"


# A blocked task asked the operator a question and left the pending queue; dedup
# then stops the LLM re-raising it. Without a re-surface, the ask-once
# design goes mute while waiting on an answer — indistinguishable on the
# phone from a dead assistant.
WAITING_ON_YOU_WINDOW_DAYS = 7
WAITING_ON_YOU_MAX_LINES = 3


def _parse_task_time(value: object) -> datetime | None:
    """Parse a task's created_at/completed_at, or None if unusable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _descriptions_overlap(a: dict, b: dict) -> bool:
    """The one keyword-overlap dedup rule: >=2 shared keywords, 30% of the
    smaller keyword set. _has_recent_task calls it too."""
    ka = _extract_keywords(a["description"])
    kb = _extract_keywords(b["description"])
    if not ka or not kb:
        return False
    smaller = min(len(ka), len(kb))
    return len(ka & kb) >= max(2, int(smaller * 0.3))


def _waiting_on_you_lines(
    all_tasks: list[dict], now: datetime | None = None
) -> list[str]:
    """Digest footer lines for tasks blocked on the operator's input.

    A question drops off when it ages past the window, when a later
    overlapping task completes successfully (the answer landed), or when
    a newer duplicate question supersedes it. Capped: the footer is a
    nudge, not a backlog view — more than a handful of open questions
    is a different problem.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=WAITING_ON_YOU_WINDOW_DAYS)

    answered = [
        t
        for t in all_tasks
        if t.get("status") == "completed"
        and not _needs_input(t.get("result") or "")
        and (_parse_task_time(t.get("completed_at")) or now) > cutoff
    ]

    waiting = []
    for t in all_tasks:
        completed = _parse_task_time(t.get("completed_at"))
        if completed is None or completed < cutoff:
            continue
        if t.get("status") != "blocked" and not _needs_input(t.get("result") or ""):
            continue
        if any(
            _descriptions_overlap(t, a)
            and (_parse_task_time(a.get("completed_at")) or now) > completed
            for a in answered
        ):
            continue
        waiting.append(t)

    # Newest first so a newer duplicate question replaces the older one.
    waiting.sort(
        key=lambda t: _parse_task_time(t.get("completed_at")) or cutoff,
        reverse=True,
    )
    kept: list[dict] = []
    for t in waiting:
        if any(_descriptions_overlap(t, k) for k in kept):
            continue
        kept.append(t)
        if len(kept) == WAITING_ON_YOU_MAX_LINES:
            break
    kept.reverse()  # oldest first reads better in the digest
    return [f"Waiting on you: #{t['id']} {t['description'][:60]}" for t in kept]


def _next_heartbeat_task(pending: list[dict]) -> dict | None:
    """First pending task the heartbeat owns, or None."""
    for task in pending:
        if not _FOREIGN_TASK.match(task.get("description") or ""):
            return task
    return None


def _load_soul() -> str:
    """Persona context for self-directed prompts. Empty string if missing.

    Prefers the OpenClaw workspace SOUL.md, re-rendered on every learning
    approval. The fallback SOUL_MD_PATH is a read-only bind of the rendered
    openclaw/soul.md, current only as of the last `make persona`.
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
        logger.exception("thinking: could not load subjects")
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
            created = _parse_task_time(t.get("created_at"))
            if created and created > cutoff:
                recent_descriptions.append(t["description"])
    except Exception:
        # The prompt still works without the dedup hint; say why it is gone.
        logger.exception("thinking prompt: could not list recent tasks")
    recent_block = ""
    if recent_descriptions:
        recent_list = "\n".join(f"  - {d}" for d in recent_descriptions[-10:])
        recent_block = (
            "\nTasks already created in the last 24 hours "
            f"(DO NOT repeat or suggest anything similar):\n{recent_list}\n"
        )
    global _thinking_focus_index
    focus = subjects[_thinking_focus_index % len(subjects)]
    _thinking_focus_index += 1
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
    """Detect heartbeat messages from the ops agent."""
    return msg.lower().strip().startswith(_HEARTBEAT_MARKER)


def _proactive_ready(check_name: str) -> bool:
    """Return True if enough time has passed since the last run of this check."""
    interval = _PROACTIVE_INTERVALS.get(check_name, 3600)
    last_run = _proactive_state.get(check_name)
    return last_run is None or (time.monotonic() - last_run) >= interval


def _proactive_mark(check_name: str) -> None:
    """Record that a proactive check just ran."""
    _proactive_state[check_name] = time.monotonic()


def _has_recent_task(queue: list[dict], keyword: str, hours: int = 24) -> bool:
    """Check if a semantically similar task exists in the last N hours.

    Similarity is the shared keyword-overlap rule in `_descriptions_overlap`
    (>=2 shared keywords, at least 30% of the smaller keyword set), the same
    one the "Waiting on you" footer uses, so the two never disagree.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    candidate = {"description": keyword}
    for t in queue:
        try:
            created = datetime.fromisoformat(t["created_at"])
            if created <= cutoff:
                continue
        except (ValueError, KeyError):
            continue
        if _descriptions_overlap(candidate, t):
            return True
    return False


def _has_recent_prefix(
    queue: list[dict], prefix: str, hours: int = 24, exact: bool = False
) -> bool:
    """True if a task created in the last N hours (any status) starts with
    `prefix` (or equals it, with exact=True), ignoring case.

    For descriptions the heartbeat writes itself. Keyword overlap merged
    distinct alerts ("tier recovery: zai-direct" matched the openrouter
    one) and missed the stale nudge, whose text starts with a count.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    want = prefix.strip().lower()
    for t in queue:
        created = _parse_task_time(t.get("created_at"))
        if created is None or created <= cutoff:
            continue
        desc = (t.get("description") or "").strip().lower()
        if desc == want or (not exact and desc.startswith(want)):
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
            logger.info("LLM suggested task: %r", text)
            return text
    return None


async def _gather_system_state() -> str:
    """Collect current system state for task context."""
    from app import brain_db

    tier_lines = [
        f"  {t['name']}: {t['status']} — {t['description']}"
        for t in llm_proxy.tier_snapshot()
    ]

    # Refresh the cache, then describe it (unknown is never shown as a number).
    await llm_proxy._check_openrouter_balance()
    balance = llm_proxy.describe_balance()

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
        logger.exception("work prompt: could not list brain pages")

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
    # Whole-word matching no longer finds "search" inside these; a task
    # like "Research competitor pricing" names no research_router target,
    # so it reaches the LLM and still needs live data.
    "searching",
    "research",
    "researching",
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


# Whole words only: as substrings, "hn" matched "John" and "technology" and
# sent them to the paid search tier. Inflections that should still mean
# "look it up" ("searching", "research") are listed explicitly above.
_WEB_SEARCH_RE = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(kw) for kw in sorted(_WEB_SEARCH_KEYWORDS, key=len, reverse=True)
    )
    + r")\b"
)


def _task_needs_web(description: str) -> bool:
    """Return True if the task description suggests it needs live web data."""
    return _WEB_SEARCH_RE.search(description.lower()) is not None


class LLMUnavailable(UpstreamError):
    """Every LLM tier failed, so the task was not worked (not a result)."""


async def _llm_work_task(description: str) -> str:
    """Ask the LLM to actually work on a task. Returns the result text.

    Raises LLMUnavailable when no tier answered, so the caller can re-queue
    the task instead of closing it with an apology as its result.
    """
    needs_web = _task_needs_web(description)
    prompt = await _build_work_prompt(description, web_search=needs_web)
    if needs_web:
        logger.info("task needs web search: %r", description[:80])
    max_tok = 1500 if needs_web else 500
    result = await llm_proxy._llm_call(prompt, max_tokens=max_tok, web_search=needs_web)
    if not result:
        raise LLMUnavailable("no LLM tier answered")
    logger.info("LLM worked task: %r", result[:100])
    return result


_BALANCE_UNKNOWN_PREFIX = "Balance unknown alert:"
_ALERT_TASK_PREFIXES = (
    "low balance alert:",
    "investigate tier recovery:",
    _BALANCE_UNKNOWN_PREFIX.lower(),
)

# How long the balance must stay unreadable before the alert: one failed
# lookup (a network blip at startup) is not worth a task.
BALANCE_UNKNOWN_ALERT_SECS = 1800.0
_balance_unknown_since: float | None = None


def _balance_unknown_for(remaining: float | None) -> float:
    """Seconds the OpenRouter balance has been unreadable (0 if known, if
    no key is configured, or if no lookup has failed yet)."""
    global _balance_unknown_since
    failing = (
        remaining is None
        and bool(os.getenv("OPENROUTER_API_KEY"))
        and bool(llm_proxy._balance_status.get("error"))
    )
    if not failing:
        _balance_unknown_since = None
        return 0.0
    now = time.monotonic()
    if _balance_unknown_since is None:
        _balance_unknown_since = now
    return now - _balance_unknown_since


# Fixed start of the stale-queue nudge, so dedup matches what is written.
# Not a status alert: it goes through the worker like any system task.
STALE_NUDGE_PREFIX = "Stale queue:"


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
    balance = (
        f"${remaining:.2f}" if remaining is not None else llm_proxy.describe_balance()
    )
    if remaining is None:
        routing = "floor check skipped until the balance can be read"
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
# heartbeat request (see llm_proxy.LLM_CALL_DEADLINE), so stay well under it.
HEARTBEAT_WORK_DEADLINE_DEFAULT = 90.0


def _heartbeat_work_deadline() -> float:
    """Seconds allowed for one heartbeat task (env HEARTBEAT_WORK_DEADLINE)."""
    raw = os.environ.get("HEARTBEAT_WORK_DEADLINE", "")
    try:
        value = float(raw)
    except ValueError:
        return HEARTBEAT_WORK_DEADLINE_DEFAULT
    return value if value > 0 else HEARTBEAT_WORK_DEADLINE_DEFAULT


# Heartbeats a task may be re-queued for an LLM outage before it fails
# (6 x 10 min = an hour); bounds starvation of the tasks queued behind it.
LLM_RETRY_LIMIT = 6
_llm_retry_counts: dict[str, int] = {}

# A task claimed longer ago than this has lost its worker (env
# HEARTBEAT_ACTIVE_REAP_HOURS). Tome research runs take minutes, not hours.
ACTIVE_REAP_HOURS_DEFAULT = 6.0


def _active_reap_hours() -> float:
    raw = os.environ.get("HEARTBEAT_ACTIVE_REAP_HOURS", "")
    try:
        value = float(raw)
    except ValueError:
        return ACTIVE_REAP_HOURS_DEFAULT
    return value if value > 0 else ACTIVE_REAP_HOURS_DEFAULT


def _reap_stale_active(brain_db) -> None:
    """Fail tasks stuck in `active` and report each in the digest."""
    hours = _active_reap_hours()
    for t in brain_db.fail_stale_active(hours):
        label = _operator_label() if t.get("source") == "user" else "SYSTEM"
        logger.warning("reaped task #%s: active over %gh", t["id"], hours)
        digest._digest_record(
            f"[{label}] #{t['id']}: {t['description']}\n"
            f"Failed: still active after {hours:g}h; its worker never "
            "finished. Re-create the task to retry."
        )


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
    1. Infrastructure monitoring: tripped tiers, low or unreadable balance,
       tasks stuck active (every cycle)
    2. Task queue awareness: stale task nudges (every 30 min)
    3. LLM-powered thinking: self-directed task suggestions (every 15 min)

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
    unknown_for = _balance_unknown_for(remaining)

    _reap_stale_active(brain_db)
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
            logger.warning("claim skipped for #%s: %s", task["id"], exc)
            claimed = None
        if not claimed:
            # Lost the race / not pending: skip this cycle entirely.
            task = None
            skipped = True

    if task:
        deadline = _heartbeat_work_deadline()
        llm_down = False
        try:
            result = await asyncio.wait_for(
                _run_heartbeat_task(task, brain_db, remaining), timeout=deadline
            )
            failure: str | None = None
        except TimeoutError:
            result = ""
            failure = f"timed out after {deadline:g}s"
        except LLMUnavailable:
            result = ""
            failure = None
            llm_down = True
        except Exception as exc:
            logger.exception("heartbeat task #%s failed", task["id"])
            result = ""
            failure = f"{type(exc).__name__}: {str(exc)[:200]}"

        attempts = _llm_retry_counts.pop(task["id"], 0) + (1 if llm_down else 0)
        if llm_down and attempts >= LLM_RETRY_LIMIT:
            llm_down = False
            failure = (
                f"could not reach the LLM in {attempts} attempts; "
                f"{_sentence_start(tenant_profile.operator_name())} "
                "should handle it manually"
            )

        try:
            if llm_down:
                # An outage is not a result: put the task back for the next
                # cycle. One digest line per task per outage, not per cycle.
                brain_db.release_task(task["id"])
                _llm_retry_counts[task["id"]] = attempts
                logger.warning(
                    "heartbeat task #%s re-queued: LLM unreachable (attempt %d/%d)",
                    task["id"],
                    attempts,
                    LLM_RETRY_LIMIT,
                )
                if attempts == 1:
                    digest._digest_record(
                        f"[{label}] #{task['id']}: {task['description']}\n"
                        "LLM unreachable; re-queued, retrying each heartbeat."
                    )
            elif failure is not None:
                logger.warning("heartbeat task #%s failed: %s", task["id"], failure)
                brain_db.complete_task(task["id"], f"Failed: {failure}", "failed")
                digest._digest_record(
                    f"[{label}] #{task['id']}: {task['description']}\nFailed: {failure}"
                )
            elif _needs_input(result):
                # The model asked the operator a question instead of finishing.
                # Close it as blocked so it leaves the pending queue -- left
                # pending it is reworked every cycle -- and spend one digest
                # line on it.
                brain_db.complete_task(task["id"], result, "blocked")
                digest._digest_record(_blocked_digest_line(task))
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
            logger.warning("could not complete task #%s: %s", task["id"], exc)

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
            # same recreate-after-complete loop as the balance alert. Exact
            # match: each tier gets its own alert.
            if not _has_recent_prefix(all_tasks, desc, exact=True):
                t = brain_db.create_task(desc, priority=2)
                tasks_created.append(t)

        if remaining is not None and remaining < llm_proxy.OPENROUTER_BALANCE_FLOOR:
            desc = f"Low balance alert: ${remaining:.2f} remaining"
            # Any alert in the last 24h counts, completed or not. The old
            # pending-only dedup re-created the alert every cycle once the
            # previous one completed, spamming Telegram with duplicates.
            if not _has_recent_prefix(all_tasks, "Low balance alert:"):
                t = brain_db.create_task(desc, priority=1)
                tasks_created.append(t)

        if unknown_for >= BALANCE_UNKNOWN_ALERT_SECS:
            # A balance that cannot be read is not a healthy balance: say
            # so once a day instead of silently skipping the floor check.
            lookup_error = llm_proxy._balance_status.get("error")
            if not _has_recent_prefix(all_tasks, _BALANCE_UNKNOWN_PREFIX):
                desc = (
                    f"{_BALANCE_UNKNOWN_PREFIX} OpenRouter credits lookup "
                    f"failing ({lookup_error})"
                )
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
                and not t["description"].startswith(STALE_NUDGE_PREFIX)
            ]
            if stale and not _has_recent_prefix(all_tasks, STALE_NUDGE_PREFIX):
                desc = (
                    f"{STALE_NUDGE_PREFIX} {len(stale)} task(s) pending over "
                    "4 hours; review the queue"
                )
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
                logger.info("dropped ungrounded task: %r", suggestion)
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
            waiting=_waiting_on_you_lines(all_tasks),
            balance=llm_proxy.describe_balance(),
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
        logger.info("digest flushed: %d message(s), %d failed", len(messages), failed)
    else:
        text = ""
    resp = llm_proxy._synthetic_response(text)
    # Reached only when the cycle ran to the end; the watchdog alerts when
    # heartbeats keep arriving without this stamp moving.
    digest.record_heartbeat_done()

    if is_stream:
        return llm_proxy._wrap_json_as_sse(resp)
    return resp
