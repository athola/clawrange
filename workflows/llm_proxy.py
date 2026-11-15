"""LLM proxy with three-tier fallback and Telegram notifications.

Sits between OpenClaw and LLM providers. Tries each tier in order,
notifies via Telegram on fallback transitions, and returns the first
successful response in OpenAI-compatible format (streaming or non-streaming).
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import auth
import embeddings
import github_search
import reddit_search
import telegram
import tenant_profile
from brain_db import project_list

# NOTE on deferred imports: ``app`` imports this module at module scope and
# ``generators``/``scheduler`` import it transitively, so every use of
# ``app``, ``generators``, and ``scheduler`` below stays function-local
# (cycle guard).

logger = logging.getLogger("clawrange.llm_proxy")

router = APIRouter()

# ─── Auth ──────────────────────────────────────────────────────────

PROXY_AUTH_TOKEN = os.getenv("PROXY_AUTH_TOKEN", "")

# ─── Tier Tracking ───────────────────────────────────────────────

_last_tier_used: str | None = None

# ─── Config Loading (once at startup) ─────────────────────────────

MODELS_CONFIG_PATH = Path(__file__).parent / "models.json"


def _load_config() -> dict:
    with open(MODELS_CONFIG_PATH) as f:
        return json.load(f)


CONFIG = _load_config()

# ─── Request Body Allowlist ────────────────────────────────────────

ALLOWED_BODY_FIELDS = {
    "messages",
    "temperature",
    "max_tokens",
    "top_p",
    "stop",
    "frequency_penalty",
    "presence_penalty",
    "stream",
    "tools",
    "tool_choice",
}

# ─── Circuit Breaker State ─────────────────────────────────────────

_circuit_state: dict[str, dict] = {}
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60


def _circuit_open(tier_name: str, probe: bool = True) -> bool:
    """Check if a tier's circuit breaker is open (should be skipped).

    After the cooldown the breaker is half-open: exactly one caller is let
    through as a probe (``probe=True`` claims it). A failure during the probe
    re-trips immediately (the failure count is still at threshold); a success
    closes the breaker. ``probe=False`` is a read-only status check that
    never claims the probe slot.
    """
    state = _circuit_state.get(tier_name)
    if not state:
        return False
    if state["failures"] < CIRCUIT_FAILURE_THRESHOLD:
        return False
    now = time.monotonic()
    if now - state["last_failure"] < CIRCUIT_COOLDOWN_SECONDS:
        return True
    # Half-open: one probe at a time (a probe that never reports back is
    # considered lost after another cooldown).
    probe_at = state.get("probe_at")
    if probe_at is not None and now - probe_at < CIRCUIT_COOLDOWN_SECONDS:
        return True
    if probe:
        state["probe_at"] = now
        return False
    return False


def _record_failure(tier_name: str) -> None:
    state = _circuit_state.setdefault(tier_name, {"failures": 0, "last_failure": 0})
    state["failures"] += 1
    state["last_failure"] = time.monotonic()
    state.pop("probe_at", None)


def _record_success(tier_name: str) -> None:
    _circuit_state.pop(tier_name, None)


def _record_rate_limit(tier_name: str) -> None:
    """Trip circuit breaker immediately — a 429 is an explicit 'stop calling'."""
    _circuit_state[tier_name] = {
        "failures": CIRCUIT_FAILURE_THRESHOLD,
        "last_failure": time.monotonic(),
    }


# ─── Balance Guard (protect $10 free-tier threshold) ─────────────

OPENROUTER_BALANCE_FLOOR = float(os.getenv("OPENROUTER_BALANCE_FLOOR", "10.0"))

_balance_cache: dict[str, float | None] = {"remaining": None, "checked_at": None}
_BALANCE_CHECK_INTERVAL = 300  # re-check at most every 5 minutes
_BALANCE_FAILURE_BACKOFF = 60  # after a failed lookup, wait this long to retry


async def _check_openrouter_balance() -> float | None:
    """Return remaining OpenRouter balance from the credits API, or None.

    GET /api/v1/credits reports {total_credits, total_usage}; the remaining
    balance is the difference, read straight from the API so it stays true
    after deposits and top-ups (no locally-configured constant to drift).
    Cached for _BALANCE_CHECK_INTERVAL; on failure falls back to the last
    known value.
    """
    now = time.monotonic()
    checked_at = _balance_cache.get("checked_at")
    # None (or the legacy 0.0 sentinel) means "never checked": due now, even
    # on a host whose monotonic clock is younger than the interval.
    if (
        checked_at is not None
        and checked_at != 0.0
        and now - checked_at < _BALANCE_CHECK_INTERVAL
    ):
        return _balance_cache.get("remaining")

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return _balance_cache.get("remaining")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/credits",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                remaining = float(data["total_credits"]) - float(data["total_usage"])
                _balance_cache["remaining"] = remaining
                _balance_cache["checked_at"] = now
                return remaining
            logger.warning("openrouter credits lookup: HTTP %s", resp.status_code)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("openrouter credits lookup failed: %s", exc)

    # Back off instead of re-hitting a failing API on every request: stamp
    # the check so the next attempt comes _BALANCE_FAILURE_BACKOFF from now.
    _balance_cache["checked_at"] = (
        now - _BALANCE_CHECK_INTERVAL + _BALANCE_FAILURE_BACKOFF
    )
    return _balance_cache.get("remaining")


# ─── Notification Debouncing ──────────────────────────────────────

_notification_last_sent: dict[str, float] = {}
_NOTIFICATION_DEBOUNCE_SECONDS = 120


def _should_notify(tier_name: str) -> bool:
    """Return True if enough time has passed since last notification for this tier."""
    now = time.monotonic()
    last = _notification_last_sent.get(tier_name)
    if last is not None and now - last < _NOTIFICATION_DEBOUNCE_SECONDS:
        return False
    _notification_last_sent[tier_name] = now
    return True


def _background_notify(tier_name: str, message: str) -> None:
    """Log tier events without sending to Telegram.

    Telegram notifications go to the user's conversation chat and look like
    garbled bot responses. Rate limit events are logged for debugging only.
    Explicit admin alerts (balance guard) use telegram.notify() directly.
    """
    if not _should_notify(tier_name):
        return
    clean = message.replace("*", "").replace("`", "")
    logger.info("[tier] %s", clean)


# ─── Auto-Retry ──────────────────────────────────────────────────

MAX_AUTO_RETRY_WAIT = 30  # seconds — max hold time before returning synthetic response
PROVIDER_TIMEOUT = 45  # per-provider HTTP timeout (seconds) — was 90
REQUEST_DEADLINE = 60  # hard ceiling for the entire chat request (seconds)
# Total ceiling for _llm_call across ALL tiers (seconds). The interactive chat
# path races tiers concurrently under REQUEST_DEADLINE, but the heartbeat path
# (_handle_heartbeat -> _llm_work_task/_llm_suggest_task -> _llm_call) still
# tries tiers sequentially. Without an aggregate cap a hung provider compounds
# to PROVIDER_TIMEOUT * len(tiers), blocking the heartbeat past the upstream
# OpenClaw client timeout and surfacing "LLM request timed out" to Telegram.
LLM_CALL_DEADLINE = 45  # one full provider attempt; fast failover still fits


# ─── Rate Limit Detection ─────────────────────────────────────────

_RATE_LIMIT_KEYWORDS = ("rate limit", "rate_limit", "ratelimit", "too many requests")


def _is_rate_limited(resp: httpx.Response) -> bool:
    """Detect rate limiting from status code or response body keywords."""
    if resp.status_code == 429:
        return True
    if resp.status_code in (400, 403):
        try:
            body = resp.json()
            error = body.get("error", {})
            msg = error.get("message", "") if isinstance(error, dict) else str(error)
            return any(kw in msg.lower() for kw in _RATE_LIMIT_KEYWORDS)
        except Exception:
            pass
    return False


_RESET_HEADERS = ("x-ratelimit-reset-requests", "x-ratelimit-reset", "ratelimit-reset")

# Patterns to extract timing from OpenRouter error messages
_RESET_SECONDS_RE = re.compile(r"(\d+)\s*(?:second|sec|s\b)", re.IGNORECASE)
_RESET_MINUTES_RE = re.compile(r"(\d+)\s*(?:minute|min|m\b)", re.IGNORECASE)
_RESET_HOURS_RE = re.compile(r"(\d+)\s*(?:hour|hr|h\b)", re.IGNORECASE)


def _header_reset_at(resp: httpx.Response) -> datetime | None:
    """First reset timestamp in the rate-limit headers (epoch or ISO), UTC."""
    for header in _RESET_HEADERS:
        value = resp.headers.get(header)
        if not value:
            continue
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError):
            pass
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        return at if at.tzinfo else at.replace(tzinfo=UTC)
    return None


def _error_meta(resp: httpx.Response) -> tuple[dict, str]:
    """(error.metadata, error.message) from an error body; empty when absent."""
    try:
        body = resp.json()
    except Exception:
        return {}, ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return {}, ""
    meta = error.get("metadata")
    return (meta if isinstance(meta, dict) else {}), str(error.get("message") or "")


def _message_reset(msg: str) -> tuple[int, str] | None:
    """(seconds, human text) for the reset timing named in an error message."""
    if m := _RESET_HOURS_RE.search(msg):
        h = int(m.group(1))
        at = datetime.now(UTC) + timedelta(hours=h)
        return h * 3600, f"at ~{at.strftime('%H:%M UTC')} (~{h}h)"
    if m := _RESET_MINUTES_RE.search(msg):
        return int(m.group(1)) * 60, f"in ~{int(m.group(1))}min"
    if m := _RESET_SECONDS_RE.search(msg):
        return int(m.group(1)), f"in {int(m.group(1))}s"
    lower = msg.lower()
    if "day" in lower or "daily" in lower:
        return 86400, "daily limit — resets in up to 24h"
    if "upstream" in lower or "retry shortly" in lower:
        # Transient upstream throttle: no stated time, conservative estimate.
        return 20, "upstream provider throttled — retry in a few minutes"
    return None


def _parse_reset_from_message(msg: str) -> str:
    """Human-readable reset timing from an error message, or ''."""
    found = _message_reset(msg)
    return found[1] if found else ""


def _parse_reset_seconds_from_message(msg: str) -> int | None:
    """Reset timing in seconds from an error message, or None."""
    found = _message_reset(msg)
    return found[0] if found else None


def _get_reset_info(resp: httpx.Response) -> str:
    """Rate limit reset time as a human-readable string, '' if unknown.

    Checks retry-after (seconds), x-ratelimit-reset-* (epoch/ISO), body
    metadata, then error message text.
    """
    retry_after = resp.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        seconds = int(retry_after)
        return f"in {seconds}s" if seconds < 120 else f"in ~{seconds // 60}min"
    at = _header_reset_at(resp)
    if at is not None:
        delta = at - datetime.now(UTC)
        stamp = f"at {at.strftime('%H:%M UTC')}"
        return (
            f"{stamp} (~{int(delta.total_seconds()) // 60}min)"
            if delta > timedelta(0)
            else stamp
        )
    meta, message = _error_meta(resp)
    raw = meta.get("ratelimit_reset") or meta.get("retry_after")
    if raw:
        return f"at {raw}"
    # OpenRouter puts the provider error in metadata.raw.
    for text in (meta.get("raw", ""), message):
        if text and (found := _message_reset(text)):
            return found[1]
    return ""


def _get_reset_seconds(resp: httpx.Response) -> int | None:
    """Rate limit reset time in seconds, or None if unknown."""
    retry_after = resp.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return max(int(retry_after), 1)
    at = _header_reset_at(resp)
    if at is not None:
        delta = at - datetime.now(UTC)
        if delta > timedelta(0):
            return max(int(delta.total_seconds()), 1)
    meta, message = _error_meta(resp)
    raw = meta.get("retry_after")
    if raw and str(raw).isdigit():
        return max(int(raw), 1)
    for text in (meta.get("raw", ""), message):
        if text and (found := _message_reset(text)):
            return found[0]
    return None


def _get_rate_limit_detail(resp: httpx.Response) -> str:
    """Provider name and reason from a rate limit response."""
    meta, _ = _error_meta(resp)
    provider, raw = meta.get("provider_name", ""), meta.get("raw", "")
    if provider and raw:
        return f"({provider}: {raw[:120]})"
    if provider:
        return f"(provider: {provider})"
    return f"({raw[:120]})" if raw else ""


# ─── Provider Calls ───────────────────────────────────────────────

# ─── Response Sanitization ────────────────────────────────────────

# Tool keywords the model commonly hallucinates
_TOOL_KEYWORDS = (
    r"exec|web_search|web_fetch|cron|write|read|edit|search|browse|"
    r"tool_call|function_call|subagents?|web_browse"
)

# Closed blocks: [[keyword ... ]] or [[keyword ... [/keyword]]
_CLOSED_TOOL_BLOCK = re.compile(
    rf"\[\[(?:{_TOOL_KEYWORDS})\b[\s\S]*?(?:\]\]|\[/\w+\]\]?)",
    re.IGNORECASE,
)
# Unclosed blocks: [[keyword ... (runs to end of string)
_UNCLOSED_TOOL_BLOCK = re.compile(
    rf"\[\[(?:{_TOOL_KEYWORDS})\b[\s\S]*$",
    re.IGNORECASE,
)
# Orphaned inner tags: [command]...[/command], [query]...[/query], etc.
_INNER_TAG = re.compile(
    r"\[(?:command|query|filename|content|action)(?:=[^\]]*)?\]"
    r"[\s\S]*?"
    r"\[/(?:command|query|filename|content|action)\]",
    re.IGNORECASE,
)


# XML-style hallucinated tool tags: <exec command="..."></exec>,
# <tool_call>...</tool_call>,
# and colon-suffixed variants like <tool_call:paste>...</tool_call:paste> that free-tier
# models invent when they get OpenAI tool schemas but the upstream doesn't support
# function calling. Alternation keeps the two cases distinct so backref groups stay
# populated — Python's (?P=name) backrefs require the group to have participated.
_XML_TOOL_TAG_NAMES = (
    r"exec|tool_call|function_call|search|browse|cron|write|read|subagents?"
)
_XML_TOOL_TAG = re.compile(
    rf"<(?P<n1>{_XML_TOOL_TAG_NAMES}):(?P<sx>\w+)\b[^>]*>"
    r"[\s\S]*?"
    r"</(?P=n1):(?P=sx)>"
    r"|"
    rf"<(?P<n2>{_XML_TOOL_TAG_NAMES})\b[^>]*>"
    r"[\s\S]*?"
    r"</(?P=n2)>",
    re.IGNORECASE,
)
# Self-closing XML tags: <exec command="..."/> and <tool_call:paste .../> variants
_XML_SELF_CLOSING = re.compile(
    r"<(?:exec|tool_call|function_call|search|browse|cron|write|read|subagents?)"
    r"(?::\w+)?\b[^/]*/\s*>",
    re.IGNORECASE,
)
# Unclosed XML tool tags: <exec ...> or <exec ... (runs to end of string)
_XML_UNCLOSED_TAG = re.compile(
    rf"<(?:{_TOOL_KEYWORDS})\b[\s\S]*$",
    re.IGNORECASE,
)
# Hallucinated metadata tags: <session_status>...</session_status>,
# <system>...</system>, etc.
_XML_META_TAG = re.compile(
    r"<(?:session_status|system_status|thinking|internal_monologue)\b[^>]*>"
    r"[\s\S]*?"
    r"</(?:session_status|system_status|thinking|internal_monologue)>",
    re.IGNORECASE,
)


def _sanitize_response(text: str) -> str:
    """Strip hallucinated tool-call blocks from LLM output."""
    cleaned = _CLOSED_TOOL_BLOCK.sub("", text)
    cleaned = _UNCLOSED_TOOL_BLOCK.sub("", cleaned)
    cleaned = _INNER_TAG.sub("", cleaned)
    cleaned = _XML_TOOL_TAG.sub("", cleaned)
    cleaned = _XML_SELF_CLOSING.sub("", cleaned)
    cleaned = _XML_UNCLOSED_TAG.sub("", cleaned)
    cleaned = _XML_META_TAG.sub("", cleaned)
    # Collapse excessive blank lines left by removals
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _is_garbled(text: str) -> bool:
    """Detect responses with missing spaces (common GLM tokenization issue).

    Normal English has ~15-20% spaces. Below 5% on a 50+ char response is garbled.
    """
    if len(text) < 50:
        return False
    return text.count(" ") / len(text) < 0.05


# ── Non-answer detection ────────────────────────────────────────
#
# Free models hallucinate agent behavior — they promise to "research",
# "read files", "do startup" instead of answering.
#
# Detection counts how many DEFERRAL SIGNALS appear in a short response.
# Each signal is a compiled regex that handles verb conjugation
# (read/reading, load/loading, etc.). 3+ signals = non-answer.

_DEFERRAL_SIGNALS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # Deferral structure
        r"\blet me\b",
        r"\bi'?ll\b",
        r"\bi (?:need|have|want) to\b",
        r"\bfirst\b",
        r"\bthen\b",
        r"\bbefore i\b",
        r"\bhold on\b",
        # Agent actions (with conjugation)
        r"\bstartup\b",
        r"\bstart(?:ing)?\s+(?:up|by|with)\b",
        r"\bresearch",
        r"\bread(?:ing)?\s+(?:my|the|some|required)\b",
        r"\bload(?:ing)?\s+(?:my|the)\b",
        r"\bcatch(?:ing)?\s+up\b",
        r"\binitializ",
        r"\bboot",
        r"\bunderstand(?:ing)?\s+(?:my|the|who|your)\b",
        r"\bprepare?\b",
        r"\bgather(?:ing)?\b",
        # Agent objects
        r"\bcontext\b",
        r"\b(?:config|configuration)\b",
        r"\bidentity\b",
        # Meta qualifiers (padding without substance)
        r"\bproperly\b",
        r"\bthoroughly?\b",
        r"\bcarefully\b",
        r"\b(?:real|right|good|proper)\s+answer\b",
    )
]

# Customer-facing actions exempt a response from non-answer detection
_CUSTOMER_ACTIONS = re.compile(
    r"call you|reach out|connect you|send you|text you|"
    r"schedule|appointment|come by|visit the lot",
    re.IGNORECASE,
)


def _is_non_answer(text: str) -> bool:
    """Detect short responses where the model defers instead of answering.

    Counts deferral signal matches (compiled regexes that handle verb
    conjugation). 3+ signals in a response under 300 chars with no
    customer-facing action = non-answer.
    """
    if len(text) > 300:
        return False
    if _CUSTOMER_ACTIONS.search(text):
        return False
    hits = sum(1 for signal in _DEFERRAL_SIGNALS if signal.search(text))
    return hits >= 3


# A task the model cannot finish without the operator comes back as a long, tidy
# markdown essay. _is_non_answer() cannot see it: that one only fires under
# 300 chars and these run past 1000.
#
# Match the status declaration in the HEADER, not a request for input
# anywhere in the body. Checked against the 846 stored task results:
#
#   "what i need from you"  -> 208 hits, nearly all real deliverables. The
#       work prompt asks for exactly that ("describe what you prepared and
#       what the operator needs to do to finish it"), so it is not a blocked signal.
#   header-anchored below   -> 21 hits (2.5%), including both essays that
#       reached Telegram today.
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
    """Same rule as _has_recent_task: >=2 shared keywords, 30% of the
    smaller keyword set."""
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


PROVIDER_URLS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "zai": "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
    "zai-search": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
}


async def _call_provider(
    provider: str, models: list[str], body: dict, api_key: str
) -> httpx.Response:
    """Call a provider with the given body. Returns the raw response."""
    url = PROVIDER_URLS[provider]
    payload = {**body, "model": models[0]}
    if provider == "openrouter" and len(models) > 1:
        payload["models"] = models

    async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as client:
        return await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )


# ─── Persona Helpers ────────────────────────────────────────────────


async def _post_persona_propose(
    content: str,
    kind: str = "persona",
    target: str = "",
    source: str = "feedback",
):
    """Post a persona proposal to the local /persona API. Returns the created row."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "http://localhost:5678/persona/propose",
            json={"kind": kind, "target": target, "content": content, "source": source},
        )
        r.raise_for_status()
        return r.json()


async def _handle_persona_command(args: str) -> JSONResponse:
    args = args.strip()
    if args.startswith("reflect"):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post("http://localhost:5678/persona/reflect")
                r.raise_for_status()
        except httpx.HTTPError as exc:
            return _synthetic_response(
                f"Persona API unavailable — reflection not queued ({exc})."
            )
        return _synthetic_response(
            "Persona reflection queued — review drafts with !tasks."
        )
    if not args:
        return _synthetic_response("Usage: !persona <feedback> | !persona reflect")
    try:
        row = await _post_persona_propose(args)
    except httpx.HTTPError as exc:
        return _synthetic_response(
            f"Persona API unavailable — feedback not queued ({exc})."
        )
    return _synthetic_response(
        f"Queued persona enhancement as [DRAFT] (id {row.get('id')}). "
        "Approve via /persona/proposals/<id>/approve."
    )


# ─── Tier Routing Hints ────────────────────────────────────────────

TIER_HINTS = {
    "z.ai": "zai-direct",
    "zai": "zai-direct",
    "zhipu": "zai-direct",
    "glm": "zai-direct",
    "bigmodel": "zai-direct",
    "paid": "openrouter-paid",
    "claude": "openrouter-paid",
}


def _detect_tier_hint(messages: list[dict]) -> str | None:
    """Check the last user message for an explicit ``!keyword`` routing command.

    Only matches keywords prefixed with ``!`` (e.g. ``!paid``, ``!claude``,
    ``!zai``).  The sender metadata block is stripped first so that
    incidental mentions of provider names in conversation context never
    trigger routing.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            text = msg.get("content", "")
            if isinstance(text, list):
                text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
            # Strip sender metadata blocks to avoid false matches
            text = re.sub(
                r"sender \(untrusted metadata\):.*?```.*?```",
                "",
                text,
                flags=re.DOTALL,
            )
            text_lower = text.lower()
            for keyword, tier in TIER_HINTS.items():
                if re.search(rf"(?<!\S)!{re.escape(keyword)}\b", text_lower):
                    return tier
            break
    return None


# ─── Synthetic Response Helper ────────────────────────────────────


def _synthetic_response(text: str) -> JSONResponse:
    """Return a synthetic OpenAI-compatible chat response (no LLM call)."""
    return JSONResponse(
        content={
            "id": "clawrange-synthetic",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }
    )


def _wrap_json_as_sse(json_response: JSONResponse) -> StreamingResponse:
    """Wrap a non-streaming JSONResponse as SSE for clients that requested streaming.

    Converts the full JSON chat completion into token-by-token SSE chunks
    so OpenAI-compatible clients (like OpenClaw) can parse it correctly.
    """
    data = json.loads(bytes(json_response.body))
    completion_id = data.get("id", "clawrange-fallback")
    model = data.get("model", "unknown")
    first_choice = (data.get("choices") or [{}])[0]
    message = first_choice.get("message") or {}
    # Tool-call replies carry "content": null.
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []
    tier = data.get("_clawrange_tier", "")

    def _make_chunk(delta: dict, finish_reason: str | None = None) -> str:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if tier:
            chunk["_clawrange_tier"] = tier
        return f"data: {json.dumps(chunk)}\n\n"

    async def _generate():
        # Role chunk (no content)
        yield _make_chunk({"role": "assistant"})
        # Token-by-token content — split on space boundaries, preserve spaces
        if content:
            for i, word in enumerate(content.split(" ")):
                token = f" {word}" if i > 0 else word
                yield _make_chunk({"content": token})
        if tool_calls:
            # OpenAI streaming format: every tool call carries its index.
            yield _make_chunk(
                {"tool_calls": [{"index": i, **tc} for i, tc in enumerate(tool_calls)]}
            )
        # Final chunk
        yield _make_chunk({}, finish_reason="tool_calls" if tool_calls else "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Regex to strip OpenClaw's metadata prefix from user messages.
# Format: "<label> (untrusted metadata):\n```json\n{...}\n```\n<actual message>"
_OPENCLAW_META_RE = re.compile(
    r"^[^\n]*\(untrusted metadata\)\s*:\s*```json\s*\{.*?\}\s*```\s*",
    re.DOTALL | re.IGNORECASE,
)


def _get_last_user_message(messages: list[dict]) -> str:
    """Extract the text of the last user message, stripping OpenClaw metadata."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            if not isinstance(content, str):
                return ""
            # Strip OpenClaw conversation metadata prefix
            content = _OPENCLAW_META_RE.sub("", content)
            return content.strip()
    return ""


# ─── /tier Command ───────────────────────────────────────────────


def tier_snapshot() -> list[dict[str, str]]:
    """Read-only status of every tier: TRIPPED, ACTIVE (last used) or ready.

    Never claims the half-open probe (probe=False): status displays are
    polled, and claiming the probe there kept a tripped tier from ever
    getting the real request that would close its breaker.
    """
    out = []
    for tier in CONFIG["tiers"]:
        name = tier["name"]
        if _circuit_open(name, probe=False):
            marker = "TRIPPED"
        elif name == _last_tier_used:
            marker = "ACTIVE"
        else:
            marker = "ready"
        out.append({"name": name, "status": marker, "description": tier["description"]})
    return out


async def _handle_tier_command() -> JSONResponse:
    """Return current tier status, balance, and circuit breaker state."""
    lines = ["Tier Status\n"]
    for t in tier_snapshot():
        lines.append(f"  [{t['status']}] {t['name']} — {t['description']}")

    remaining = await _check_openrouter_balance()
    lines.append("")
    if remaining is not None:
        lines.append(
            f"OpenRouter balance: ${remaining:.2f} remaining "
            f"(floor: ${OPENROUTER_BALANCE_FLOOR:.2f})"
        )
    else:
        lines.append("OpenRouter balance: not configured (set OPENROUTER_API_KEY)")

    if _last_tier_used:
        lines.append(f"Last used: {_last_tier_used}")
    else:
        lines.append("Last used: none yet")

    lines.append("Paid auto-fallback: off (send !paid or !claude to enable)")
    return _synthetic_response("\n".join(lines))


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
    return _synthetic_response(text)


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

    resp = _synthetic_response(text)
    if is_stream:
        return _wrap_json_as_sse(resp)
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
        result = await _llm_call(args, max_tokens=1000, web_search=True)
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
    return _synthetic_response(
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

    resp = _synthetic_response(text)
    if is_stream:
        return _wrap_json_as_sse(resp)
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
                src = _operator_label() if t.get("source") == "user" else "SYS"
                lines.append(
                    f"  [{src}] #{t['id']} [P{t['priority']}] {t['description'][:55]}"
                )
            lines.append("")

        if pending:
            lines.append("QUEUED:")
            for t in pending:
                src = _operator_label() if t.get("source") == "user" else "SYS"
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

    resp = _synthetic_response(text)
    if is_stream:
        return _wrap_json_as_sse(resp)
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

    resp = _synthetic_response(text)
    if is_stream:
        return _wrap_json_as_sse(resp)
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

    resp = _synthetic_response(text)
    if is_stream:
        return _wrap_json_as_sse(resp)
    return resp


# ─── Heartbeat Interceptor ───────────────────────────────────────


_HEARTBEAT_MARKER = "read heartbeat.md"

# Track when proactive checks last fired (survives across heartbeat cycles,
# resets on container restart which is fine — fresh start, fresh initiative).
_proactive_state: dict[str, float] = {}

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

# Rotates the focus subject so consecutive cycles cover different ground.
_thinking_focus_index = 0


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
    if not (_RESEARCH_INTENT.search(text) and _RESEARCH_TARGET.search(text)):
        return False
    words = _squash_words(text)
    return any(_squash_words(s) in words for s in subjects)


def _build_thinking_prompt(subjects: list[str], context: str = "") -> str:
    """Ask for one research task about a listed subject."""
    global _thinking_focus_index
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
        pass
    recent_block = ""
    if recent_descriptions:
        recent_list = "\n".join(f"  - {d}" for d in recent_descriptions[-10:])
        recent_block = (
            "\nTasks already created in the last 24 hours "
            f"(DO NOT repeat or suggest anything similar):\n{recent_list}\n"
        )
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
    """Detect heartbeat messages from max-ops agent."""
    return msg.lower().strip().startswith(_HEARTBEAT_MARKER)


def _proactive_ready(check_name: str) -> bool:
    """Return True if enough time has passed since the last run of this check."""
    interval = _PROACTIVE_INTERVALS.get(check_name, 3600)
    last_run = _proactive_state.get(check_name)
    return last_run is None or (time.monotonic() - last_run) >= interval


def _proactive_mark(check_name: str) -> None:
    """Record that a proactive check just ran."""
    _proactive_state[check_name] = time.monotonic()


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


def _has_recent_task(queue: list[dict], keyword: str, hours: int = 24) -> bool:
    """Check if a semantically similar task exists in the last N hours.

    Uses keyword overlap: if >=2 meaningful words overlap between the new
    suggestion and an existing task, it's considered a duplicate.  The
    threshold scales with the smaller keyword set (at least 30% overlap)
    so short descriptions aren't unfairly penalised.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    new_keywords = _extract_keywords(keyword)
    if not new_keywords:
        return False
    for t in queue:
        try:
            created = datetime.fromisoformat(t["created_at"])
            if created <= cutoff:
                continue
        except (ValueError, KeyError):
            continue
        existing_keywords = _extract_keywords(t["description"])
        overlap = new_keywords & existing_keywords
        smaller = min(len(new_keywords), len(existing_keywords)) or 1
        if len(overlap) >= max(2, int(smaller * 0.3)):
            return True
    return False


async def _llm_call(
    prompt: str, max_tokens: int = 100, web_search: bool = False
) -> str | None:
    """Send a focused prompt to the first available LLM tier.

    Returns the response text, or None on failure. Used for both
    task suggestions (thinking) and task execution (working).

    When web_search=True, routes through CONFIG["search"] — a dedicated
    tier (OpenRouter `:online`) that performs server-side web search.
    Z.AI's web_search MCP tool is paywalled to a separate resource
    package the GLM Coding Plan does not unlock, so it's bypassed.
    """
    if web_search:
        try:
            return await asyncio.wait_for(
                _llm_call_search(prompt, max_tokens), timeout=LLM_CALL_DEADLINE
            )
        except TimeoutError:
            logger.warning("Search call exceeded %ss deadline", LLM_CALL_DEADLINE)
            return None

    async def _try_tiers() -> str | None:
        for tier in CONFIG["tiers"]:
            tier_name = tier["name"]
            # Background work never spends paid credit: the paid tier is
            # opt-in per chat message (!paid / !claude), never automatic.
            if tier_name == "openrouter-paid":
                continue
            provider = tier["provider"]
            if provider not in PROVIDER_URLS:
                logger.warning(
                    "tier %s: provider %r has no URL configured — skipping",
                    tier_name,
                    provider,
                )
                continue

            if provider == "openrouter":
                remaining = await _check_openrouter_balance()
                # Same rule as the racing path: a zero/negative balance
                # 402s on every OpenRouter model — go straight to the next
                # tier instead of burning a doomed provider attempt.
                if remaining is not None and remaining <= 0:
                    continue

            provider_config = CONFIG["providers"][provider]
            api_key = os.getenv(provider_config["env_key"], "")
            if not api_key:
                continue
            # Last gate, so a claimed half-open probe is always really used.
            if _circuit_open(tier_name):
                continue

            body: dict = {
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }

            try:
                resp = await _call_provider(provider, tier["models"], body, api_key)
                if resp.status_code == 200:
                    data = resp.json()
                    msg = data.get("choices", [{}])[0].get("message", {})
                    text = (msg.get("content") or "").strip()
                    if text:
                        _record_success(tier_name)
                        return text
                    _record_failure(tier_name)
                elif _is_rate_limited(resp):
                    _record_rate_limit(tier_name)
                else:
                    _record_failure(tier_name)
            except Exception as exc:
                logger.warning(
                    "background LLM call failed on tier %s: %s: %s",
                    tier_name,
                    type(exc).__name__,
                    exc,
                )
                _record_failure(tier_name)
        return None

    # Cap total time so a hung provider can't compound across tiers and block
    # the heartbeat past the upstream client timeout. On timeout, return None —
    # callers (_llm_work_task / _llm_suggest_task) degrade gracefully.
    try:
        return await asyncio.wait_for(_try_tiers(), timeout=LLM_CALL_DEADLINE)
    except TimeoutError:
        logger.warning("LLM call exceeded %ss deadline across tiers", LLM_CALL_DEADLINE)
        return None


async def _llm_call_search(prompt: str, max_tokens: int) -> str | None:
    """Route web-search tasks to the dedicated search tier (OpenRouter :online).

    The model name carries the `:online` suffix, which tells OpenRouter to run
    Exa-powered web search before generation and attach url_citation
    annotations to the response. We extract those citations and append them
    to the answer text so Telegram messages include real source links.
    """
    search_cfg = CONFIG.get("search")
    if not search_cfg:
        return None

    provider = search_cfg["provider"]
    models = search_cfg["models"]
    provider_config = CONFIG["providers"][provider]
    api_key = os.getenv(provider_config["env_key"], "")
    if not api_key:
        return None

    body = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    try:
        resp = await _call_provider(provider, models, body, api_key)
        if resp.status_code == 200:
            data = resp.json()
            msg = data.get("choices", [{}])[0].get("message", {})
            text = (msg.get("content") or "").strip()
            citations = _extract_openrouter_citations(msg.get("annotations") or [])
            if citations:
                text = f"{text}\n\n{citations}" if text else citations
            if text:
                return text
        else:
            print(
                f"[PROXY] search tier {provider} returned {resp.status_code}: "
                f"{resp.text[:200]}",
                flush=True,
            )
    except Exception as exc:
        print(f"[PROXY] search tier exception: {type(exc).__name__}: {exc}", flush=True)
    return None


def _extract_web_citations(tool_calls: list[dict]) -> str:
    """Pull URLs and titles from GLM web_search tool call results.

    Retained for backwards compatibility with any direct GLM web_search
    integration. The active search path now uses _extract_openrouter_citations.
    """
    refs: list[str] = []
    for tc in tool_calls:
        if tc.get("type") == "web_search":
            results = tc.get("web_search", {}).get("search_result", [])
            for i, hit in enumerate(results, 1):
                title = hit.get("title", "")
                link = hit.get("link", "")
                content = hit.get("content", "")[:120]
                if link:
                    refs.append(f"{i}. [{title}]({link})\n   {content}")
    return "\n".join(refs)


def _blocked_citation_patterns() -> list[re.Pattern[str]]:
    """Compiled `assistant.search_hygiene.blocked_citations` regexes."""
    patterns = tenant_profile.search_hygiene().get("blocked_citations") or []
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _is_persona_citation(url: str, title: str) -> bool:
    """Return True when a citation URL/title matches the persona's blocked
    citation patterns (e.g. lore pages for a fictional persona).

    Belt-and-suspenders for _strip_persona_backstory: even if a soul.md
    edit leaks persona names past the stripper, matching search results
    never reach the operator's message.
    """
    haystack = f"{url} {title}"
    return any(p.search(haystack) for p in _blocked_citation_patterns())


def _extract_openrouter_citations(annotations: list[dict]) -> str:
    """Pull URLs and titles from OpenRouter `:online` url_citation annotations.

    OpenRouter's `:online` plugin returns citations on the message via:
        message.annotations[].type == "url_citation"
        message.annotations[].url_citation = {url, title, content, "
        start_index, end_index}

    Citations matching the persona's blocked patterns are filtered out —
    see _is_persona_citation. Format mirrors
    _extract_web_citations so Telegram output stays consistent regardless
    of which search backend produced the citations.
    """
    refs: list[str] = []
    idx = 0
    for ann in annotations:
        if ann.get("type") != "url_citation":
            continue
        c = ann.get("url_citation", {})
        url = c.get("url", "")
        title = c.get("title", "")
        if not url:
            continue
        if _is_persona_citation(url, title):
            continue
        idx += 1
        snippet = (c.get("content") or "")[:120]
        refs.append(f"{idx}. [{title}]({url})\n   {snippet}")
    return "\n".join(refs)


async def _llm_suggest_task(subjects: list[str], context: str = "") -> str | None:
    """Ask the LLM for one proactive task suggestion."""
    text = await _llm_call(_build_thinking_prompt(subjects, context), max_tokens=100)
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
    for tier in CONFIG["tiers"]:
        name = tier["name"]
        status = (
            "TRIPPED"
            if _circuit_open(name, probe=False)
            else ("ACTIVE" if name == _last_tier_used else "ready")
        )
        tier_lines.append(f"  {name}: {status} — {tier['description']}")

    # Balance
    remaining = await _check_openrouter_balance()
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
        f"OpenRouter balance: {balance} (floor: ${OPENROUTER_BALANCE_FLOOR:.2f}). "
        "zai-direct is a separate provider with its own quota and is not "
        "affected by this balance.\n"
        f"Last tier used: {_last_tier_used or 'none'}\n"
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
        marketing_context = ANTI_PATTERN_RULES + "\n"

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
    result = await _llm_call(prompt, max_tokens=max_tok, web_search=needs_web)
    if result:
        print(f"[PROXY] LLM worked task: {result[:100]!r}", flush=True)
        return result
    return (
        f"Could not reach LLM to work this task. "
        f"{_sentence_start(tenant_profile.operator_name())} should handle manually: "
        f"{description}"
    )


# ─── Marketing Scan Interceptor ──────────────────────────────────

_MARKETING_SCAN_PATTERNS = {
    "reddit": re.compile(r"Scan reddit for (\S+)", re.IGNORECASE),
    "github": re.compile(r"Scan github for (\S+)", re.IGNORECASE),
    "traffic": re.compile(
        r"(?:Weekly )?traffic snapshot for (\S+)/(\S+)", re.IGNORECASE
    ),
    "awesome": re.compile(r"Awesome-list watch: (\S+) not yet listed", re.IGNORECASE),
}

ANTI_PATTERN_RULES = """
ANTI-PATTERNS (do not produce text that violates these):
- Do NOT lead with "I built this with Claude" or similar AI-builder framing
- Do NOT use superlatives like "fastest", "best", "revolutionary"
- Do NOT fabricate post titles, subreddit names, usernames, or URLs
- Do NOT recommend posting in subreddits where self-promotion is banned

POSITIVE RULES:
- Lead with the user problem solved
- Include real upvote counts, comment counts, and timestamps when available
- Cite source URLs for every claim
- For comment suggestions, anchor in the user's question first; mention the "
project as a relevant tool second
"""


# ─── Research Router ────────────────────────────────────────────────
# The chat personas have no web tooling (the LLM can only stall or ask
# the operator to authorize tools it will never have), but the workflow service
# is web-capable: the research orchestrator fans out to Reddit, GitHub,
# and web search server-side. Research-shaped heartbeat tasks route
# there and come back with real, citable URLs.

_RESEARCH_INTENT = re.compile(
    r"\b(search|research|find|look\s*up|surface|scan|identify)\b", re.IGNORECASE
)
_RESEARCH_TARGET = re.compile(
    r"\b(reddit|subreddit|hacker\s*news|\bhn\b|threads?|posts?|articles?|"
    r"forums?|discussions?|conversations?|papers?|arxiv|github|repos?|"
    r"web|online|internet)\w*\b",
    re.IGNORECASE,
)


async def _try_research_task(description: str) -> str | None:
    """Run a research-shaped task through the research orchestrator.

    Returns condensed findings text when the task matches (intent verb +
    external source target), None when it doesn't so the LLM path handles
    it. A failure inside the orchestrator also returns None — the LLM
    attempt is a better fallback than a hard error.
    """
    if not (
        _RESEARCH_INTENT.search(description) and _RESEARCH_TARGET.search(description)
    ):
        return None

    # Content-idea tasks quote research vocabulary in their template
    # ("research on ... surfaced ... Reddit/HN comment") but are drafting
    # jobs for the LLM. Routing them here re-runs research on the whole
    # description instead of drafting the angles, so hand them back.
    if description.strip().lower().startswith("content idea for"):
        return None

    # Function-local on purpose (cycle guard): research imports this
    # module at module scope for _llm_call, so a top-level import here
    # would be circular.
    import research

    try:
        result = await research.orchestrate_research(
            description,
            channels=["discourse", "code", "discourse_web"],
        )
    except Exception:
        logger.exception("research router: orchestrator failed")
        return None

    findings = result.get("findings", [])
    if not findings:
        return "Research: no findings."

    # No description echo: the digest line already prints it above the
    # result, and re-embedding it here crowds _needs_input's head window.
    lines = [f"Research findings ({len(findings)}):"]
    for i, f in enumerate(findings[:8], 1):
        lines.append(f"{i}. [{f['channel']}] {f['title'][:90]}\n   {f['url']}")
    return "\n".join(lines)


async def _try_marketing_scan(description: str, brain_db) -> str | None:
    """Detect and execute structured marketing scan tasks.

    Returns result text if the task matches a scan pattern, None otherwise.
    """
    for kind, pattern in _MARKETING_SCAN_PATTERNS.items():
        match = pattern.search(description)
        if not match:
            continue

        if kind == "reddit":
            slug = match.group(1)
            project = brain_db.get_project(slug)
            if not project:
                return None

            topics = project_list(project, "topics")
            subreddits = project_list(project, "subreddits")
            search_terms = project_list(project, "search_terms")

            query = (
                " OR ".join(search_terms[:3]) if search_terms else " ".join(topics[:3])
            )
            results = await reddit_search.search_subreddits(
                query, subreddits or tenant_profile.default_subreddits()
            )

            if not results:
                return f"Reddit scan for {slug}: no new relevant posts found."

            lines = [f"Reddit scan for {slug} ({len(results)} results):\n"]
            for i, r in enumerate(results[:8], 1):
                lines.append(
                    f"{i}. {r.title}\n"
                    f"   r/{r.subreddit} | {r.score}pts | {r.comments} comments\n"
                    f"   {r.url}"
                )

            # Mark results in scan cache
            for r in results:
                brain_db.mark_seen("reddit_post", r.id, slug)

            return "\n".join(lines)

        if kind == "github":
            slug = match.group(1)
            project = brain_db.get_project(slug)
            if not project:
                return None

            search_terms = project_list(project, "search_terms")
            topics = project_list(project, "topics")
            query = (
                " OR ".join(search_terms[:3]) if search_terms else " ".join(topics[:3])
            )

            repos = await github_search.search_repos(query, min_stars=5, limit=10)
            issues = await github_search.search_issues(query, limit=10)

            lines = [
                f"GitHub scan for {slug} ({len(repos)} repos, {len(issues)} issues):\n"
            ]

            if repos:
                lines.append("Adjacent repos:")
                for i, repo in enumerate(repos[:5], 1):
                    lines.append(
                        f"  {i}. {repo.full_name} ({repo.stars}*) — "
                        f"{repo.description or ''}"
                    )

            if issues:
                lines.append("\nRecent issues:")
                for i, iss in enumerate(issues[:5], 1):
                    lines.append(f"  {i}. {iss.title} — {iss.repository}#{iss.number}")

            # Mark in cache
            for seen_repo in repos:
                brain_db.mark_seen("github_repo", str(seen_repo.id), slug)

            return "\n".join(lines)

        if kind == "traffic":
            owner, repo_name = match.group(1), match.group(2)

            traffic = await github_search.get_self_traffic(owner, repo_name)
            if not traffic:
                return (
                    f"Traffic for {owner}/{repo_name}: unavailable "
                    f"(needs GITHUB_PAT with repo scope)"
                )

            return (
                f"Traffic for {owner}/{repo_name} (14 days):\n"
                f"  Views: {traffic.views_count} ({traffic.views_uniques} unique)\n"
                f"  Clones: {traffic.clones_count} ({traffic.clones_uniques} unique)"
            )

        if kind == "awesome":
            # These tasks are typically LLM-generated drafts, let them fall through
            return None

    return None


# ─── Heartbeat Digest ──────────────────────────────────────────────
# The heartbeat runs every 10 minutes, but Telegram should hear from it
# at most hourly. Completed work buffers here and is delivered as the
# heartbeat response (which OpenClaw relays to Telegram) once the hour
# elapses — every other cycle stays silent.

HEARTBEAT_DIGEST_INTERVAL = float(
    os.getenv("HEARTBEAT_DIGEST_INTERVAL", "3600")
)  # seconds between Telegram digests; independent of heartbeat cadence

# Telegram rejects a sendMessage body over 4096 chars with a 400 and the
# whole digest is lost. Items go out as one direct message each (the
# OpenClaw relay is structurally a single message), so the limit governs
# per-message chunking, not how many items survive the hour.

HEARTBEAT_DIGEST_MAX_MESSAGES = int(
    os.getenv("HEARTBEAT_DIGEST_MAX_MESSAGES", "20")
)  # direct sends per flush; Telegram rate-limits bots to ~20 msgs/min/chat

DIGEST_RESULT_LIMIT = int(
    os.getenv("HEARTBEAT_DIGEST_RESULT_LIMIT", "3500")
)  # chars of a task result buffered per digest item (room for one message)


def _digest_state_path() -> str:
    return os.getenv("HEARTBEAT_DIGEST_STATE", "/data/heartbeat_digest.json")


# ``last_flush`` stays None until loaded from disk. It is wall-clock
# (``time.time()``), not ``time.monotonic()``: monotonic's zero point is
# per-process, so it cannot survive the restart this state exists to survive.
_digest_state: dict = {"lines": [], "last_flush": None}


def record_heartbeat_seen(now: float | None = None) -> None:
    """Stamp the last heartbeat arrival for the watchdog (watchdog.py).

    The digest only flushes when a heartbeat prompt arrives, so this
    stamp is how the scheduler-side watchdog tells a quiet hour from a
    dead OpenClaw. Written every heartbeat (~10 min), so a plain file
    is cheap and restart-safe.
    """
    path = os.getenv("HEARTBEAT_SEEN_PATH", "/data/heartbeat_seen.json")
    try:
        with open(path, "w") as fh:
            json.dump({"ts": now if now is not None else time.time()}, fh)
    except OSError as exc:
        # The stamp is an observability aid; a heartbeat must never fail
        # because it could not be recorded.
        print(f"[PROXY] heartbeat-seen stamp failed: {exc}", flush=True)


def _digest_load() -> None:
    """Load the buffer and flush clock from disk, once per process.

    The digest is the only path from the heartbeat to Telegram. Holding its
    clock in memory meant every container recreate and every ``uvicorn
    --reload`` reset it to zero and dropped the buffer, so a process cycling
    faster than HEARTBEAT_DIGEST_INTERVAL never reported at all.

    A missing file leaves ``last_flush`` at 0.0 so the first heartbeat after
    a fresh deploy is due immediately instead of costing a silent hour. A
    corrupt file falls back to the same defaults — the heartbeat must never
    fail on unreadable state.
    """
    if _digest_state["last_flush"] is not None:
        return
    _digest_state["lines"] = []
    _digest_state["last_flush"] = 0.0
    try:
        with open(_digest_state_path()) as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            lines = saved.get("lines")
            if isinstance(lines, list):
                _digest_state["lines"] = [str(x) for x in lines]
            _digest_state["last_flush"] = float(saved.get("last_flush") or 0.0)
    except (OSError, ValueError, TypeError):
        pass


def _digest_save() -> None:
    """Persist the buffer and clock atomically (tmp + ``os.replace``)."""
    path = _digest_state_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(
                {
                    "lines": _digest_state["lines"],
                    "last_flush": _digest_state["last_flush"],
                },
                fh,
            )
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[PROXY] digest state save failed: {exc}", flush=True)


def _digest_record(line: str) -> None:
    """Buffer one digest line for the next hourly flush."""
    _digest_load()
    _digest_state["lines"].append(line)
    _digest_save()


def _digest_due(now: float | None = None) -> bool:
    _digest_load()
    now = now if now is not None else time.time()
    return now - _digest_state["last_flush"] >= HEARTBEAT_DIGEST_INTERVAL


def _digest_take_ex(
    now: float,
    remaining: float | None,
    tripped: list[str],
    waiting: list[str] | None = None,
) -> tuple[list[str], str, list[int | None], list[str]]:
    """Plan a digest flush WITHOUT clearing the buffer.

    Returns ``(messages, summary, owners, lines)``. The clock advances now
    (a concurrent heartbeat must not flush the same hour twice) but the
    buffered lines are only released by ``_digest_commit`` once the sends
    have been attempted, so a failed send never loses the hour.

    Each message is one digest item (sent straight to Telegram by the
    caller); the summary rides the heartbeat response, which OpenClaw
    relays, keeping an independent channel for the actionable footer
    (open questions, tripped tiers, balance).

    A due interval with an empty buffer stays silent, unless unanswered
    questions are waiting on the operator: that silence is
    indistinguishable from a dead assistant, so the questions surface.
    """
    waiting = waiting or []
    _digest_load()
    lines = list(_digest_state["lines"])
    _digest_state["last_flush"] = now
    _digest_save()
    footer = list(waiting)
    if tripped:
        footer.append(f"Tiers: {', '.join(tripped)} TRIPPED")
    if remaining is not None:
        footer.append(f"OpenRouter balance: ${remaining:.2f}")
    if not lines:
        if not footer:
            return [], "", [], lines
        summary = "\n".join(["Heartbeat digest: nothing new this hour.", *footer])
        return [], summary, [], lines
    messages, owners, _kept = _digest_plan(lines)
    summary = "\n".join(
        [
            f"Heartbeat digest: {len(lines)} item(s) this hour — "
            f"sent as {len(messages)} message(s) above.",
            *footer,
        ]
    )
    return messages, summary, owners, lines


def _digest_commit(
    lines: list[str], owners: list[int | None], failed: set[int]
) -> None:
    """Release the flushed lines, keeping what was not delivered.

    Re-buffered: lines with any failed message, and lines deferred past the
    per-digest message cap. Lines recorded while the sends were in flight
    (anything after the taken prefix) are preserved.
    """
    _digest_load()
    kept = max((o for o in owners if o is not None), default=-1) + 1
    failed_lines = {owners[i] for i in failed if owners[i] is not None}
    carry = [ln for idx, ln in enumerate(lines[:kept]) if idx in failed_lines]
    carry += lines[kept:]
    _digest_state["lines"] = carry + _digest_state["lines"][len(lines) :]
    _digest_save()


def _clip_result(text: str, limit: int = 500) -> str:
    """Shorten a task result for one digest line without cutting mid-word.

    Cuts after the last sentence terminator inside the limit (word boundary
    as fallback) and marks the cut with an ellipsis; the full result stays
    on the task (GET /task/{id}).
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("."), head.rfind("!"), head.rfind("?"))
    if cut < limit // 2:
        # The last sentence end is too far back to waste the budget on;
        # a word boundary keeps more of the result.
        cut = head.rfind(" ")
    if cut <= 0:
        return head.rstrip() + "…"
    return text[: cut + 1].rstrip() + "…"


def _digest_plan(lines: list[str]) -> tuple[list[str], list[int | None], int]:
    """Plan the direct sends for the buffered items, oldest first.

    Returns ``(messages, owners, kept)``: ``owners[i]`` is the index of the
    line message ``i`` belongs to (None for the overflow pointer) and
    ``lines[kept:]`` are the items deferred to the next digest.

    The hour's header rides the first message; an item longer than the send
    limit chunks across messages instead of being clipped. A runaway hour
    (scheduler bug, burst of created-task lines) stops at
    HEARTBEAT_DIGEST_MAX_MESSAGES direct sends — Telegram rate-limits bots —
    and names what was deferred; deferred items stay buffered for the next digest.
    """
    item_word = "item" if len(lines) == 1 else "items"
    header = f"Heartbeat digest ({len(lines)} {item_word} this hour):"
    # Leave headroom on every part for the header or the "…" continuation.
    budget = telegram.MAX_CHARS - 64
    kept = lines[:HEARTBEAT_DIGEST_MAX_MESSAGES]
    deferred = len(lines) - len(kept)
    if deferred:
        # The pointer line is itself a direct send: it takes the last slot.
        kept = kept[:-1]
        deferred += 1
    messages: list[str] = []
    owners: list[int | None] = []
    for idx, line in enumerate(kept):
        parts = telegram.split_sentences(line, limit=budget)
        for part_idx, part in enumerate(parts):
            if idx == 0 and part_idx == 0:
                messages.append(f"{header}\n\n{part}")
            else:
                # Continuation parts already carry their one "… " marker.
                messages.append(part)
            owners.append(idx)
    if deferred > 0:
        more_word = "item" if deferred == 1 else "items"
        messages.append(
            f"… {deferred} more {more_word} follow in the next digest "
            "(full results: GET /task)"
        )
        owners.append(None)
    return messages, owners, len(kept)


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
        if _circuit_open(name, probe=False):
            state = "TRIPPED (circuit open)"
        else:
            state = "recovered (circuit closed)"
        return f"Tier {name}: {state}. OpenRouter balance: {balance}."
    floor = f"${OPENROUTER_BALANCE_FLOOR:.0f}"
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
    scan_result = await _try_marketing_scan(description, brain_db)
    if scan_result is not None:
        return scan_result
    research_result = await _try_research_task(description)
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

    record_heartbeat_seen()

    tasks_created: list[dict] = []

    # ── 1. Tier status ──────────────────────────────────────────
    tripped = [
        tier["name"]
        for tier in CONFIG["tiers"]
        if _circuit_open(tier["name"], probe=False)
    ]

    remaining = await _check_openrouter_balance()

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
            logger.exception("heartbeat task #%s failed", task["id"])
            result = ""
            failure = f"{type(exc).__name__}: {str(exc)[:200]}"

        try:
            if failure is not None:
                logger.warning("heartbeat task #%s failed: %s", task["id"], failure)
                brain_db.complete_task(task["id"], f"Failed: {failure}", "failed")
                _digest_record(
                    f"[{label}] #{task['id']}: {task['description']}\nFailed: {failure}"
                )
            elif _needs_input(result):
                # The model asked the operator a question instead of finishing.
                # Close it as blocked so it leaves the pending queue -- left
                # pending it is reworked every cycle -- and spend one digest
                # line on it.
                brain_db.complete_task(task["id"], result, "blocked")
                _digest_record(_blocked_digest_line(task))
            else:
                brain_db.complete_task(task["id"], result, "completed")
                # One digest item per task; the result gets most of a Telegram
                # message now that items flush as separate messages. The full
                # result still lives on the task (!tasks / GET /task/{id}).
                _digest_record(
                    f"[{label}] #{task['id']}: {task['description']}\n"
                    f"Result: {_clip_result(result, limit=DIGEST_RESULT_LIMIT)}"
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
            # same recreate-after-complete loop as the balance alert.
            if not _has_recent_task(all_tasks, desc, hours=24):
                t = brain_db.create_task(desc, priority=2)
                tasks_created.append(t)

        if remaining is not None and remaining < OPENROUTER_BALANCE_FLOOR:
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
        _digest_record(f"Created #{t['id']} [P{t['priority']}] {t['description'][:60]}")

    if _digest_due():
        messages, text, owners, taken = _digest_take_ex(
            time.time(),
            remaining,
            tripped,
            waiting=_waiting_on_you_lines(all_tasks),
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
            _digest_commit(taken, owners, failed_idx)
            raise
        _digest_commit(taken, owners, failed_idx)
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
    resp = _synthetic_response(text)

    if is_stream:
        return _wrap_json_as_sse(resp)
    return resp


# ─── Tier Helpers ─────────────────────────────────────────────────


async def _notify_tier_change(tier_name: str) -> None:
    """Notify via Telegram when the serving tier changes."""
    global _last_tier_used
    if _last_tier_used is not None and _last_tier_used != tier_name:
        _background_notify(
            f"tier-change:{_last_tier_used}:{tier_name}",
            f"*Tier switch*: `{_last_tier_used}` -> `{tier_name}`",
        )
    _last_tier_used = tier_name


# ─── Fire-and-forget tasks ───────────────────────────────────────

# asyncio keeps only weak references to tasks; an un-held fire-and-forget
# task can be garbage collected mid-flight. Hold them until they finish.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ─── Single-Tier Attempt ─────────────────────────────────────────


async def _try_single_tier(
    tier: dict, body: dict, status_msg_id: int | None = None
) -> tuple[JSONResponse | None, tuple[str, int | None] | None]:
    """Attempt one provider tier. Returns (response, rate_limit_info).

    On success: (JSONResponse, None)
    On rate limit: (None, (tier_name, reset_seconds))
    On other failure: (None, None)
    """
    tier_name = tier["name"]
    provider = tier["provider"]
    models = tier["models"]
    provider_config = CONFIG["providers"][provider]
    api_key = os.getenv(provider_config["env_key"], "")

    if status_msg_id:
        _spawn_background(telegram.send_typing())
        _spawn_background(
            telegram.edit_status(status_msg_id, f"\u23f3 {tier['description']}")
        )

    try:
        non_stream_body = {**body, "stream": False}
        resp = await _call_provider(provider, models, non_stream_body, api_key)

        if resp.status_code == 200:
            data = resp.json()
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                if not msg.get("content") and msg.get("reasoning_content"):
                    msg["content"] = msg["reasoning_content"]
                if "content" in msg and isinstance(msg["content"], str):
                    msg["content"] = _sanitize_response(msg["content"])
                msg.pop("reasoning", None)
                msg.pop("reasoning_content", None)
                msg.pop("reasoning_details", None)
                msg.pop("refusal", None)
                choice.pop("native_finish_reason", None)

            first_msg = data.get("choices", [{}])[0].get("message", {})
            first_content = first_msg.get("content") or ""
            has_tool_calls = bool(first_msg.get("tool_calls"))

            # Tool-call responses have null content — pass them through
            if has_tool_calls:
                _record_success(tier_name)
                data["_clawrange_tier"] = tier_name
                tool_names = [
                    tc.get("function", {}).get("name", "?")
                    for tc in first_msg["tool_calls"]
                ]
                print(
                    f"[PROXY] tool_call response via {tier_name} "
                    f"({len(first_msg['tool_calls'])} calls): {tool_names}",
                    flush=True,
                )
                return JSONResponse(content=data), None

            if _is_garbled(first_content):
                print(
                    f"[PROXY] garbled response from {tier_name} — skipping", flush=True
                )
                _record_failure(tier_name)
                return None, None

            if not first_content.strip():
                print(
                    f"[PROXY] empty response from {tier_name} after "
                    f"sanitization — skipping",
                    flush=True,
                )
                _record_failure(tier_name)
                return None, None

            if _is_non_answer(first_content):
                print(
                    f"[PROXY] non-answer from {tier_name}: {first_content[:80]!r} "
                    f"— skipping",
                    flush=True,
                )
                # Don't _record_failure — the provider worked fine, the content
                # was just bad. Tripping the circuit breaker would lock out a
                # working provider and force expensive paid-tier fallback.
                return None, None

            _record_success(tier_name)
            data["_clawrange_tier"] = tier_name
            print(
                f"[PROXY] response via {tier_name} ({len(first_content)} chars)",
                flush=True,
            )
            return JSONResponse(content=data), None

        if _is_rate_limited(resp):
            _record_rate_limit(tier_name)
            try:
                print(
                    f"[PROXY] 429 on {tier_name} — headers: {dict(resp.headers)} "
                    f"body: {resp.text[:500]}",
                    flush=True,
                )
            except Exception:
                pass
            reset = _get_reset_info(resp)
            detail = _get_rate_limit_detail(resp)
            _background_notify(
                tier_name,
                f"*Rate limited* on `{tier_name}`"
                + (f"\nResets {reset}" if reset else "")
                + (f"\n{detail}" if detail else ""),
            )
            return None, (tier_name, _get_reset_seconds(resp))

        _record_failure(tier_name)
        _background_notify(
            tier_name,
            f"*Error* on `{tier_name}` (HTTP {resp.status_code})",
        )
        return None, None

    except httpx.HTTPError as exc:
        _record_failure(tier_name)
        _background_notify(tier_name, f"*Network error* on `{tier_name}`: {exc}")
        return None, None
    except (ValueError, IndexError, KeyError, TypeError, AttributeError) as exc:
        # A 200 with a non-JSON body, an empty/odd `choices`, or a non-dict
        # payload is a broken provider: count it against the breaker.
        logger.warning(
            "tier %s returned a malformed response: %s: %s",
            tier_name,
            type(exc).__name__,
            exc,
        )
        _record_failure(tier_name)
        return None, None


# ─── Proxy Endpoint ───────────────────────────────────────────────


async def _json_object(request: Request) -> dict:
    """Parse the request body as a JSON object or raise 400."""
    try:
        raw = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return raw


@router.post("/v1/embeddings")
async def create_embeddings(
    request: Request,
    authorization: str | None = Header(None),
):
    """OpenAI-compatible embeddings for OpenClaw memory, via OpenRouter.

    Always uses the configured model (embeddings.config) so every vector
    OpenClaw stores is comparable; a GLM Coding Plan Z.AI key can't embed.
    """
    # Auth check — same PROXY_AUTH_TOKEN gate as chat completions
    if not auth.bearer_ok(authorization, PROXY_AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw_body = await _json_object(request)
    inputs = raw_body.get("input")
    if not inputs:
        raise HTTPException(status_code=400, detail="input is required")

    cfg = embeddings.config()
    if not cfg.api_key:
        raise HTTPException(
            status_code=503,
            detail="embeddings provider not configured (set OPENROUTER_API_KEY)",
        )
    batch = inputs if isinstance(inputs, list) else [inputs]
    try:
        resp = await embeddings.request(batch, cfg)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"embeddings upstream error: {exc}")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"embeddings upstream {resp.status_code}: {resp.text[:300]}",
        )
    return JSONResponse(content=resp.json())


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(None),
):
    # Auth check — fails closed when PROXY_AUTH_TOKEN is unset
    if not auth.bearer_ok(authorization, PROXY_AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw_body = await _json_object(request)
    messages = raw_body.get("messages", [])
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise HTTPException(
            status_code=400, detail="messages must be a list of objects"
        )
    is_stream = raw_body.get("stream", False)

    # Intercept status commands before they hit the LLM
    last_user_msg = _get_last_user_message(messages)
    msg_lower = last_user_msg.lower().strip()
    print(f"[PROXY] extracted msg: {msg_lower[:200]!r} stream={is_stream}", flush=True)
    # Intercept !help — return full command reference
    help_commands = ("!help", "/help", "help", "help?", "!commands", "/commands")
    last_line = msg_lower.rstrip().rsplit("\n", 1)[-1].strip()
    if msg_lower in help_commands or last_line in help_commands:
        resp = _handle_help()
        if is_stream:
            return _wrap_json_as_sse(resp)
        return resp

    tier_commands = (
        "/tier",
        "!tier",
        "tier",
        "tier?",
        "tier status",
        "/status",
        "!status",
        "status",
        "status?",
        "claw status",
        "clawstatus",
        "proxy status",
        "proxy?",
        "which tier",
        "what tier",
        "what's the status",
        "whats the status",
        "what is the status",
        "what's the status?",
        "whats the status?",
        "what is the status?",
        "whats your status",
        "what's your status",
        "whats your status?",
        "what's your status?",
        "your status",
        "your status?",
    )
    if msg_lower in tier_commands or last_line in tier_commands:
        resp = await _handle_tier_command()
        if is_stream:
            return _wrap_json_as_sse(resp)
        return resp

    # Intercept heartbeat messages — run checks in Python instead of LLM
    if _is_heartbeat(last_user_msg):
        print("[PROXY] heartbeat intercepted — running checks in Python", flush=True)
        return await _handle_heartbeat(is_stream)

    # Intercept !task commands — enqueue, list, cancel, reprioritize
    # Check both full message and last line (fallback when metadata stripping fails)
    raw_last_line = last_user_msg.rstrip().rsplit("\n", 1)[-1].strip()
    task_action = _extract_task_command(last_user_msg) or _extract_task_command(
        raw_last_line
    )
    if task_action is not None:
        return await _dispatch_task_command(task_action, is_stream)

    # Intercept marketing commands (/sched, /scan, /projects, /marketing)
    marketing_cmd = _extract_marketing_command(last_user_msg)
    if marketing_cmd is not None:
        return await _dispatch_marketing_command(marketing_cmd, is_stream)

    # Intercept !persona / !learn (spec §6 alias) — explicit feedback and
    # on-demand reflection
    if msg_lower.startswith(("!persona", "!learn")):
        prefix = "!persona" if msg_lower.startswith("!persona") else "!learn"
        args = last_user_msg.strip()[len(prefix) :]
        resp = await _handle_persona_command(args)
        return _wrap_json_as_sse(resp) if is_stream else resp

    # Strip poisoned assistant messages from conversation history.
    # Uses both static markers (synthetic responses, tool hallucinations)
    # AND the deferral detector — any assistant message that would fail
    # _is_non_answer() gets stripped so the model doesn't see its own
    # previous failures and generate apologies or continued deferrals.
    _POISON_MARKERS = (
        "temporarily on pause",
        "took too long to respond",
        "<exec",
        "[[exec",
        "<tool_call",
    )
    messages = [
        msg
        for msg in messages
        if not (
            msg.get("role") == "assistant"
            and isinstance(msg.get("content"), str)
            and (
                any(marker in msg["content"] for marker in _POISON_MARKERS)
                or _is_non_answer(msg["content"])
            )
        )
    ]

    # Allowlist body fields
    body = {k: v for k, v in raw_body.items() if k in ALLOWED_BODY_FIELDS}
    body["messages"] = messages

    # Inject anti-hallucination reinforcement as a TRAILING system message.
    # LLMs weight instructions near the end of context most heavily.
    # Placing it at the start (where soul.md lives) gets lost — placing it
    # after the last user message means the model sees it right before generating.
    #
    # Always inject, even when tools are present. Free-tier upstream models
    # (the racing default) often get OpenAI tool schemas but don't actually
    # support function calling — they invent fake <tool_call:paste>...XML
    # that the proxy then strips, leaving the user with empty replies. The
    # adapted wording below tells the model to either use the OpenAI
    # tool-call format properly or answer in plain text — never improvise XML.
    has_tools = bool(body.get("tools"))
    if has_tools:
        _ANTI_HALLUCINATION = (
            "[SYSTEM] If you need a tool, emit a real OpenAI tool_call object — "
            "never XML tags, bracket syntax, or <tool_call:NAME> blocks. "
            "Hallucinated tool syntax is stripped and the response is retried "
            "on another tier. If no tool fits, answer in plain text from your "
            "knowledge and conversation context."
        )
    else:
        _ANTI_HALLUCINATION = (
            "[SYSTEM] Respond now in plain text. "
            "No startup sequences. No initialization steps. "
            "Never output XML tags, bracket syntax, or tool_call blocks — "
            "those are not real tools and will be stripped. "
            "Answer directly using your knowledge and conversation context."
        )
    messages.append({"role": "system", "content": _ANTI_HALLUCINATION})

    # Tier routing — explicit !keyword overrides, otherwise race all free tiers
    forced_tier = _detect_tier_hint(messages)
    if forced_tier:
        print(f"[PROXY] tier hint detected: {forced_tier}", flush=True)
    else:
        print("[PROXY] no tier hint — racing all free tiers", flush=True)

    # Show typing indicator and send a transient status message
    _spawn_background(telegram.send_typing())
    status_msg_id = await telegram.send_status("\u23f3")

    # Always use non-streaming internally for full sanitization + garbled
    # detection, then re-wrap as SSE if the client requested streaming.
    # Hard deadline prevents the proxy from blocking OpenClaw indefinitely.
    try:
        result = await asyncio.wait_for(
            _handle_non_streaming(body, forced_tier, status_msg_id=status_msg_id),
            timeout=REQUEST_DEADLINE,
        )
    except TimeoutError:
        logger.warning("Request exceeded %ds deadline", REQUEST_DEADLINE)
        result = _synthetic_response(
            "I took too long to respond — the LLM providers might be slow. "
            "Please try again in a moment."
        )
    finally:
        # Remove the status message — OpenClaw delivers the real response.
        # In a finally so an error never strands the "⏳" message.
        if status_msg_id:
            try:
                await asyncio.wait_for(telegram.delete_message(status_msg_id), 5)
            except Exception as exc:
                logger.warning("status message delete failed: %s", exc)

    if is_stream and isinstance(result, JSONResponse):
        return _wrap_json_as_sse(result)
    return result


async def _handle_non_streaming(
    body: dict,
    forced_tier: str | None = None,
    _attempt: int = 0,
    status_msg_id: int | None = None,
):
    """Handle non-streaming requests — race available tiers concurrently.

    Instead of trying tiers sequentially (where each 45s timeout compounds),
    all available free tiers are dispatched simultaneously. The first
    successful response wins and the remaining requests are cancelled.
    """
    # Determine which tiers are available right now
    available: list[dict] = []
    for tier in CONFIG["tiers"]:
        tier_name = tier["name"]
        if forced_tier and tier_name != forced_tier:
            continue
        provider = tier["provider"]
        provider_config = CONFIG["providers"][provider]
        api_key = os.getenv(provider_config["env_key"], "")
        if not api_key:
            continue
        if provider not in PROVIDER_URLS:
            continue
        if provider == "openrouter":
            remaining = await _check_openrouter_balance()
            # A zero/negative balance blocks every OpenRouter model —
            # free-tier included — with 402 payment-required errors. Skip
            # the whole provider so zai-direct carries traffic instead of
            # every request stalling through failed OpenRouter attempts.
            if remaining is not None and remaining <= 0:
                if _should_notify("balance-guard"):
                    await telegram.notify(
                        f"*Balance guard* — OpenRouter balance ${remaining:.2f}.\n"
                        "Routing around OpenRouter until it's topped up."
                    )
                continue
        if tier_name == "openrouter-paid" and not forced_tier:
            continue
        if tier_name == "openrouter-paid":
            remaining = await _check_openrouter_balance()
            if remaining is not None and remaining <= OPENROUTER_BALANCE_FLOOR:
                continue
        # Last gate: claiming the half-open probe slot must mean the tier
        # really is going to be called.
        if _circuit_open(tier_name):
            continue
        available.append(tier)

    if not available:
        print("[PROXY] no tiers available", flush=True)
        return _synthetic_response(
            "I'm temporarily on pause — no LLM tiers are available right now. "
            "Try again later."
        )

    # Race all available tiers concurrently — first success wins
    tasks = {
        asyncio.create_task(
            _try_single_tier(tier, body, status_msg_id),
            name=f"tier:{tier['name']}",
        ): tier["name"]
        for tier in available
    }
    print(f"[PROXY] racing {len(tasks)} tiers: {list(tasks.values())}", flush=True)

    rate_limit_resets: list[tuple[str, int | None]] = []

    try:
        pending = set(tasks.keys())
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    response, rl_info = task.result()
                except Exception:
                    logger.exception("Unexpected error in tier %s", tasks[task])
                    continue
                if response is not None:
                    winner = tasks[task]
                    await _notify_tier_change(winner)
                    return response
                if rl_info is not None:
                    rate_limit_resets.append(rl_info)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    # All tiers failed — auto-retry if a short reset window exists
    if _attempt == 0 and rate_limit_resets:
        known = [secs for _, secs in rate_limit_resets if secs is not None]
        if known:
            wait = max(min(known), 2)  # at least 2s to avoid hammering
            if wait <= MAX_AUTO_RETRY_WAIT:
                print(f"[PROXY] auto-retrying in {wait}s", flush=True)
                _background_notify(
                    "auto-retry",
                    f"All tiers rate-limited — auto-retrying in {wait}s",
                )
                if status_msg_id:
                    _spawn_background(
                        telegram.edit_status(
                            status_msg_id, f"\u23f3 Rate limited — retrying in {wait}s"
                        )
                    )
                await asyncio.sleep(wait)
                # Only tiers whose reset window has elapsed by now; a tier
                # with a longer (or unknown) reset stays tripped.
                for name, secs in rate_limit_resets:
                    if secs is not None and secs <= wait:
                        _circuit_state.pop(name, None)
                return await _handle_non_streaming(
                    body,
                    forced_tier,
                    _attempt=1,
                    status_msg_id=status_msg_id,
                )

    print("[PROXY] all tiers exhausted — returning synthetic response", flush=True)
    return _synthetic_response(
        "I'm temporarily on pause — the free API tiers are rate-limited right now. "
        "Send !paid or !claude to use the paid tier, or try again later."
    )
