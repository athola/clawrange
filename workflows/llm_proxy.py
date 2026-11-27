"""OpenAI-compatible LLM proxy with tiered routing (models.json).

Sits between OpenClaw and the LLM providers. Chat requests race every
available free tier concurrently and return the first good reply
(streaming or not); a paid tier is used only when the message asks for it
(!paid / !claude) and the OpenRouter balance is above the floor.
Background calls (_llm_call) try the free tiers in order. Tier events
are logged, not sent to Telegram.
"""

import asyncio
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
import chat_commands
import embeddings
import heartbeat
import telegram
import tenant_profile

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


def _validate_config(cfg: dict, hints: dict[str, str]) -> None:
    """Fail at import, not at first request, on a broken models.json.

    Checks: tier names are unique, every tier names a configured provider
    with models, every provider has a base_url and env_key, the search
    block names a real provider, and every routing hint targets a tier.
    """
    providers = cfg.get("providers") or {}
    for name, p in providers.items():
        for key in ("base_url", "env_key"):
            if not p.get(key):
                raise ValueError(f"models.json: provider {name!r} has no {key}")
    names: set[str] = set()
    for tier in cfg.get("tiers") or []:
        name = tier.get("name")
        if not name or name in names:
            raise ValueError(f"models.json: duplicate or empty tier name {name!r}")
        names.add(name)
        if tier.get("provider") not in providers:
            raise ValueError(
                f"models.json: tier {name!r} uses unknown provider "
                f"{tier.get('provider')!r}"
            )
        if not tier.get("models"):
            raise ValueError(f"models.json: tier {name!r} lists no models")
    search = cfg.get("search")
    if search and (search.get("provider") not in providers or not search.get("models")):
        raise ValueError("models.json: search block needs a known provider and models")
    for keyword, target in hints.items():
        if target not in names:
            raise ValueError(f"tier hint !{keyword} targets unknown tier {target!r}")


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
_BALANCE_CHECK_TIMEOUT = 10  # seconds for the /credits lookup
# Outcome of the most recent lookup, for describe_balance(): monotonic time of
# the last success, and the last failure reason (cleared on success).
_balance_status: dict[str, Any] = {"ok_at": None, "error": None}


def describe_balance() -> str:
    """Human-readable OpenRouter balance for status lines and alerts.

    Never renders an unknown balance as a number: a failing credits lookup
    says so, and a cached value served after a failure carries its age.
    """
    if not os.getenv("OPENROUTER_API_KEY", ""):
        return "not configured"
    remaining = _balance_cache.get("remaining")
    error = _balance_status.get("error")
    if remaining is None:
        if error:
            return f"unknown (lookup failed: {error})"
        return "unknown (not checked yet)"
    amount = f"${remaining:.2f}"
    if not error:
        return amount
    ok_at = _balance_status.get("ok_at")
    age = (
        "age unknown"
        if ok_at is None
        else f"{int((time.monotonic() - ok_at) // 60)} min old"
    )
    return f"{amount} ({age}; lookup failing: {error})"


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
    # None means "never checked": due now, even on a host whose monotonic
    # clock is younger than the interval.
    if checked_at is not None and now - checked_at < _BALANCE_CHECK_INTERVAL:
        return _balance_cache.get("remaining")

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return _balance_cache.get("remaining")

    try:
        async with httpx.AsyncClient(timeout=_BALANCE_CHECK_TIMEOUT) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/credits",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                remaining = float(data["total_credits"]) - float(data["total_usage"])
                _balance_cache["remaining"] = remaining
                _balance_cache["checked_at"] = now
                _balance_status["ok_at"] = now
                _balance_status["error"] = None
                return remaining
            logger.warning("openrouter credits lookup: HTTP %s", resp.status_code)
            _balance_status["error"] = f"HTTP {resp.status_code}"
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("openrouter credits lookup failed: %s", exc)
        _balance_status["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"

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
    """Log a tier event (rate limit, error, switch). Never sends to Telegram.

    Telegram notifications go to the user's conversation chat and look like
    garbled bot responses, so these are log lines only, and every event is
    logged (no debounce: a dropped line hides the failure being debugged).
    Explicit admin alerts (balance guard) use telegram.notify() directly.
    """
    clean = message.replace("*", "").replace("`", "")
    logger.warning("[tier] %s: %s", tier_name, clean)


# ─── Auto-Retry ──────────────────────────────────────────────────

MAX_AUTO_RETRY_WAIT = 30  # seconds — max hold time before returning synthetic response
PROVIDER_TIMEOUT = 45  # per-provider attempt timeout on the chat path (seconds)
REQUEST_DEADLINE = 60  # hard ceiling for the entire chat request (seconds)
# Total ceiling for _llm_call across ALL tiers (seconds). The interactive chat
# path races tiers concurrently under REQUEST_DEADLINE, but the heartbeat path
# (_handle_heartbeat -> _llm_work_task/_llm_suggest_task -> _llm_call) still
# tries tiers sequentially. Without an aggregate cap a hung provider compounds
# to PROVIDER_TIMEOUT * len(tiers), blocking the heartbeat past the upstream
# OpenClaw client timeout and surfacing "LLM request timed out" to Telegram.
LLM_CALL_DEADLINE = 45
# One background attempt. Kept well below LLM_CALL_DEADLINE so a hung first
# tier times out (and counts against its breaker) with time left to try the
# next free tier.
BACKGROUND_ATTEMPT_TIMEOUT = 20
# Breaker key for the background web-search tier (CONFIG["search"]).
SEARCH_TIER = "search"


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


# Out of credit, as each provider says it: OpenRouter 402 "Insufficient
# credits", OpenAI-style insufficient_quota, Z.AI code 1113 ("余额不足" =
# insufficient balance, sent as HTTP 429). Retrying cannot fix these, so
# naming them sends the operator to top up rather than wait.
_NO_CREDIT_CODES = {"1113"}
_NO_CREDIT_KEYWORDS = (
    "insufficient credit",
    "insufficient balance",
    "insufficient_quota",
    "insufficient quota",
    "out of credit",
    "余额不足",
)


def _classify_failure(resp: httpx.Response) -> str:
    """Short operator-facing cause for a non-200 provider reply."""
    code = msg = ""
    try:
        error = resp.json().get("error", {})
        if isinstance(error, dict):
            code = str(error.get("code", ""))
            msg = str(error.get("message", ""))
        else:
            msg = str(error)
    except Exception:
        msg = resp.text[:200]
    lowered = msg.lower()
    if (
        resp.status_code == 402
        or code in _NO_CREDIT_CODES
        or any(kw in lowered for kw in _NO_CREDIT_KEYWORDS)
    ):
        return "out of credit"
    if _is_rate_limited(resp):
        return "rate-limited"
    if resp.status_code in (401, 403):
        return "API key rejected"
    return f"HTTP {resp.status_code}"


# Why each tier last failed (kept until it succeeds), so a skip behind an
# open breaker can still say what tripped it.
_tier_failure_reason: dict[str, str] = {}
# Per-tier outcome of the most recent background _llm_call that failed;
# empty after one that answered. Read by describe_last_failure().
_last_call_failures: dict[str, str] = {}


def _note_failure(tier_name: str, reason: str) -> None:
    _tier_failure_reason[tier_name] = reason
    _last_call_failures[tier_name] = reason


def describe_last_failure() -> str:
    """'tier: cause; ...' for the last background call that got no answer."""
    return "; ".join(f"{t}: {r}" for t, r in _last_call_failures.items())


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


# One source of truth: the provider URLs come from models.json (a missing
# base_url is reported by _validate_config at import).
PROVIDER_URLS = {name: p.get("base_url", "") for name, p in CONFIG["providers"].items()}


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


# ─── Tier Routing Hints ────────────────────────────────────────────

TIER_HINTS = {
    "sub": "claude-sub",
    "subscription": "claude-sub",
    "paid": "openrouter-paid",
    "claude": "openrouter-paid",
}

_validate_config(CONFIG, TIER_HINTS)


def _detect_tier_hint(messages: list[dict]) -> str | None:
    """Check the last user message for an explicit ``!keyword`` routing command.

    Only matches keywords prefixed with ``!`` (e.g. ``!paid``, ``!claude``,
    ``!sub``).  The sender metadata block is stripped first so that
    incidental mentions of provider names in conversation context never
    trigger routing.
    """
    for text in _user_texts(messages):
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


# OpenClaw 2026.8+ appends a separate user message carrying runtime context
# (temporal context, interrupted inputs) after the real one, and prefixes
# the real text with a "[Sun 2026-11-15 22:21 UTC] " envelope.
_RUNTIME_CONTEXT_PREFIX = "openclaw runtime context:"
_TIMESTAMP_ENVELOPE_RE = re.compile(
    r"^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d{2}-\d{2} \d{2}:\d{2}"
    r"(?::\d{2})? [A-Za-z0-9_+/:-]+\]\s*"
)
# OpenClaw 2026.11.5+ automation jobs (the heartbeat among them) prefix the
# job message with "[cron:<job-id> <job-name>] ". Without stripping it the
# heartbeat marker never leads and heartbeats reach the model unintercepted.
_AUTOMATION_TAG_RE = re.compile(r"^\[cron:[^\s\]]+(?: [^\]\n]*)?\]\s*")
# First turn of a fresh session: "\n\nRuntime: agent=<id> | session=... | ...".
# Any agent id matches.
_RUNTIME_LINE_RE = re.compile(r"\n\s*runtime: agent=[^\n]*\s*$", re.IGNORECASE)


def _user_texts(messages: list[dict]):
    """Raw user message texts, newest first, minus OpenClaw runtime context."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        if not isinstance(content, str):
            yield ""
            continue
        if content.lstrip().lower().startswith(_RUNTIME_CONTEXT_PREFIX):
            continue
        yield content


def _get_last_user_message(messages: list[dict]) -> str:
    """Text of the last real user message, stripped of OpenClaw envelopes."""
    for content in _user_texts(messages):
        content = _OPENCLAW_META_RE.sub("", content)
        content = _RUNTIME_LINE_RE.sub("", content.strip())
        content = _TIMESTAMP_ENVELOPE_RE.sub("", content)
        return _AUTOMATION_TAG_RE.sub("", content).strip()
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


async def _llm_call(
    prompt: str, max_tokens: int = 100, web_search: bool = False
) -> str | None:
    """Send a focused prompt to the first available LLM tier.

    Returns the response text, or None on failure. Used for both
    task suggestions (thinking) and task execution (working).

    When web_search=True, routes through CONFIG["search"] — a dedicated
    tier (OpenRouter `:online`) that performs server-side web search.
    The claude-sub bridge runs with no tools, so it cannot search.
    """
    _last_call_failures.clear()
    if web_search:
        text = await _llm_call_search(prompt, max_tokens)
        if text:
            _last_call_failures.clear()
        return text

    # The tier being tried, so the overall deadline can charge it.
    inflight: list[str] = []

    async def _try_tiers() -> str | None:
        deadline_at = time.monotonic() + LLM_CALL_DEADLINE
        for tier in CONFIG["tiers"]:
            tier_name = tier["name"]
            # The paid chat tier is never tried here: it is opt-in per chat
            # message (!paid / !claude). web_search=True does spend paid
            # :online credit, behind the floor in _llm_call_search.
            if tier.get("paid"):
                _last_call_failures[tier_name] = "not tried (paid is opt-in)"
                continue
            provider = tier["provider"]
            if provider not in PROVIDER_URLS:
                logger.warning(
                    "background LLM: skip %s: provider %r has no URL configured",
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
                    logger.info(
                        "background LLM: skip %s: balance %s",
                        tier_name,
                        describe_balance(),
                    )
                    _last_call_failures[tier_name] = (
                        f"out of credit (balance {describe_balance()})"
                    )
                    continue

            env_key = CONFIG["providers"][provider]["env_key"]
            if not os.getenv(env_key, ""):
                logger.info(
                    "background LLM: skip %s: %s is not set", tier_name, env_key
                )
                _last_call_failures[tier_name] = f"{env_key} not set"
                continue
            # Last gate, so a claimed half-open probe is always really used.
            if _circuit_open(tier_name):
                logger.info("background LLM: skip %s: breaker open", tier_name)
                cause = _tier_failure_reason.get(tier_name)
                _last_call_failures[tier_name] = (
                    f"{cause} (breaker open)" if cause else "breaker open"
                )
                continue

            # If the overall deadline fires first instead, the handler below
            # charges the in-flight tier, so a hang always reaches the breaker.
            budget = min(BACKGROUND_ATTEMPT_TIMEOUT, deadline_at - time.monotonic())
            if budget <= 0:
                logger.warning("background LLM: deadline reached before %s", tier_name)
                return None
            inflight[:] = [tier_name]
            text = await _background_attempt(
                tier_name, provider, tier["models"], prompt, max_tokens, budget
            )
            inflight.clear()
            if text:
                _last_call_failures.clear()
                return text
        return None

    # Cap total time so a hung provider can't compound across tiers and block
    # the heartbeat past the upstream client timeout. On timeout, return None —
    # callers (_llm_work_task / _llm_suggest_task) degrade gracefully.
    try:
        return await asyncio.wait_for(_try_tiers(), timeout=LLM_CALL_DEADLINE)
    except TimeoutError:
        logger.warning(
            "background LLM: exceeded %ss deadline across tiers (in flight: %s)",
            LLM_CALL_DEADLINE,
            inflight or "none",
        )
        for tier_name in inflight:
            _record_failure(tier_name)
            _note_failure(
                tier_name, f"no reply within the {LLM_CALL_DEADLINE}s deadline"
            )
        return None


def _prompt_body(prompt: str, max_tokens: int) -> dict:
    """Chat body for a single-prompt background call."""
    return {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }


async def _background_attempt(
    tier_name: str,
    provider: str,
    models: list[str],
    prompt: str,
    max_tokens: int,
    timeout: float,
    citations: bool = False,
) -> str | None:
    """One background provider call: send, parse, log and update the breaker.

    Returns the reply text, or None after logging why (tier, HTTP status and
    the first 200 chars of the body). Every failure counts against the
    tier's breaker; a timeout is a failure too.
    """
    api_key = os.getenv(CONFIG["providers"][provider]["env_key"], "")
    body = _prompt_body(prompt, max_tokens)
    try:
        resp = await asyncio.wait_for(
            _call_provider(provider, models, body, api_key), timeout=timeout
        )
    except TimeoutError:
        logger.warning("background LLM: %s: no reply within %.0fs", tier_name, timeout)
        _record_failure(tier_name)
        _note_failure(tier_name, f"no reply within {timeout:.0f}s")
        return None
    except Exception as exc:
        logger.warning("background LLM: %s: %s: %s", tier_name, type(exc).__name__, exc)
        _record_failure(tier_name)
        _note_failure(tier_name, f"connection failed ({type(exc).__name__})")
        return None

    if resp.status_code != 200:
        _note_failure(tier_name, _classify_failure(resp))
        if _is_rate_limited(resp):
            _record_rate_limit(tier_name)
        else:
            _record_failure(tier_name)
        logger.warning(
            "background LLM: %s: HTTP %s: %s",
            tier_name,
            resp.status_code,
            resp.text[:200],
        )
        return None
    try:
        msg = resp.json()["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        if citations:
            refs = _extract_openrouter_citations(msg.get("annotations") or [])
            if refs:
                text = f"{text}\n\n{refs}" if text else refs
    except (ValueError, LookupError, TypeError, AttributeError) as exc:
        logger.warning(
            "background LLM: %s: malformed reply (%s): %s",
            tier_name,
            type(exc).__name__,
            resp.text[:200],
        )
        _record_failure(tier_name)
        _note_failure(tier_name, "malformed reply")
        return None
    if not text:
        logger.warning(
            "background LLM: %s: empty reply: %s", tier_name, resp.text[:200]
        )
        _record_failure(tier_name)
        _note_failure(tier_name, "empty reply")
        return None
    _record_success(tier_name)
    _tier_failure_reason.pop(tier_name, None)
    return text


async def search_allowed() -> bool:
    """False when the balance floor holds the paid search tier.

    The search tier is denied while the OpenRouter balance is unknown or at
    or below OPENROUTER_BALANCE_FLOOR. Callers use this to tell that denial
    from an outage: a denied search is policy, not a provider failure. A
    missing search config or key is not the floor, so it reports allowed.
    """
    search_cfg = CONFIG.get("search")
    if not search_cfg or search_cfg["provider"] != "openrouter":
        return True
    if not os.getenv(CONFIG["providers"]["openrouter"]["env_key"], ""):
        return True
    remaining = await _check_openrouter_balance()
    return remaining is not None and remaining > OPENROUTER_BALANCE_FLOOR


async def _llm_call_search(prompt: str, max_tokens: int) -> str | None:
    """Route web-search tasks to the dedicated search tier (OpenRouter :online).

    The model name carries the `:online` suffix, which tells OpenRouter to run
    Exa-powered web search before generation and attach url_citation
    annotations to the response. We extract those citations and append them
    to the answer text so Telegram messages include real source links.

    `:online` models are paid, so this obeys the same balance floor as
    !paid (an unknown balance denies) and has its own breaker (SEARCH_TIER).
    """
    search_cfg = CONFIG.get("search")
    if not search_cfg:
        return None

    # The balance lookup and the attempt share one LLM_CALL_DEADLINE.
    deadline_at = time.monotonic() + LLM_CALL_DEADLINE
    provider = search_cfg["provider"]
    env_key = CONFIG["providers"][provider]["env_key"]
    if not os.getenv(env_key, ""):
        logger.info("background search: skip: %s is not set", env_key)
        _last_call_failures[SEARCH_TIER] = f"{env_key} not set"
        return None
    if not await search_allowed():
        logger.warning(
            "background search: skip: OpenRouter balance %s (floor $%.2f)",
            describe_balance(),
            OPENROUTER_BALANCE_FLOOR,
        )
        _last_call_failures[SEARCH_TIER] = (
            f"held by the balance floor (balance {describe_balance()})"
        )
        return None
    budget = deadline_at - time.monotonic()
    if budget <= 0:
        logger.warning("background search: deadline reached before the attempt")
        return None
    if _circuit_open(SEARCH_TIER):
        logger.info("background search: skip: breaker open")
        cause = _tier_failure_reason.get(SEARCH_TIER)
        _last_call_failures[SEARCH_TIER] = (
            f"{cause} (breaker open)" if cause else "breaker open"
        )
        return None
    return await _background_attempt(
        SEARCH_TIER,
        provider,
        search_cfg["models"],
        prompt,
        max_tokens,
        budget,
        citations=True,
    )


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
    see _is_persona_citation.
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


# ─── Tier Helpers ─────────────────────────────────────────────────


async def _notify_tier_change(tier_name: str) -> None:
    """Log a tier switch and remember the tier that served this request."""
    global _last_tier_used
    if _last_tier_used is not None and _last_tier_used != tier_name:
        _background_notify(
            f"tier-change:{_last_tier_used}:{tier_name}",
            f"*Tier switch*: `{_last_tier_used}` to `{tier_name}`",
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
    tier: dict,
    body: dict,
    status_msg_id: int | None = None,
    failures: dict[str, str] | None = None,
) -> tuple[JSONResponse | None, tuple[str, int | None] | None]:
    """Attempt one provider tier. Returns (response, rate_limit_info).

    On success: (JSONResponse, None)
    On rate limit: (None, (tier_name, reset_seconds))
    On other failure: (None, None)

    When ``failures`` is given, a failed attempt stores a short reason under
    the tier name, so the caller can say what actually went wrong.
    """
    tier_name = tier["name"]
    provider = tier["provider"]
    models = tier["models"]
    provider_config = CONFIG["providers"][provider]
    api_key = os.getenv(provider_config["env_key"], "")
    reasons = failures if failures is not None else {}

    if status_msg_id:
        _spawn_background(telegram.send_typing())
        _spawn_background(
            telegram.edit_status(status_msg_id, f"\u23f3 {tier['description']}")
        )

    try:
        non_stream_body = {**body, "stream": False}
        try:
            resp = await asyncio.wait_for(
                _call_provider(provider, models, non_stream_body, api_key),
                timeout=PROVIDER_TIMEOUT,
            )
        except TimeoutError:
            _record_failure(tier_name)
            reasons[tier_name] = f"no reply within {PROVIDER_TIMEOUT}s"
            _background_notify(tier_name, f"*Timeout* on `{tier_name}`")
            return None, None

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
                reasons[tier_name] = "garbled reply"
                return None, None

            if not first_content.strip():
                print(
                    f"[PROXY] empty response from {tier_name} after "
                    f"sanitization — skipping",
                    flush=True,
                )
                _record_failure(tier_name)
                reasons[tier_name] = "empty reply"
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
                reasons[tier_name] = "non-answer reply"
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
            reasons[tier_name] = "rate-limited" + (f", resets {reset}" if reset else "")
            _background_notify(
                tier_name,
                f"*Rate limited* on `{tier_name}`"
                + (f"\nResets {reset}" if reset else "")
                + (f"\n{detail}" if detail else ""),
            )
            return None, (tier_name, _get_reset_seconds(resp))

        _record_failure(tier_name)
        reasons[tier_name] = f"HTTP {resp.status_code}"
        _background_notify(
            tier_name,
            f"*Error* on `{tier_name}` (HTTP {resp.status_code}): {resp.text[:200]}",
        )
        return None, None

    except httpx.HTTPError as exc:
        _record_failure(tier_name)
        reasons[tier_name] = f"network error ({type(exc).__name__})"
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
        reasons[tier_name] = "malformed reply"
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
    """OpenAI-compatible embeddings via OpenRouter, for any bearer-token client.

    OpenClaw's own memory search is off (memory provider "none"), so it is
    not a built-in caller. Always uses the configured model
    (embeddings.config) so vectors stay comparable; the claude-sub bridge
    serves chat only.
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


_HELP_COMMANDS = frozenset(
    {"!help", "/help", "help", "help?", "!commands", "/commands"}
)

_TIER_COMMANDS = frozenset(
    {
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
    }
)

# Assistant history turns containing these are our own synthetic replies or
# hallucinated tool syntax; they are stripped before the next request.
_POISON_MARKERS = (
    "temporarily on pause",
    "took too long to respond",
    "<exec",
    "[[exec",
    "<tool_call",
)

# Appended as a TRAILING system message. LLMs weight instructions near the
# end of context most heavily: at the start (where soul.md lives) it gets
# lost, after the last user message the model sees it right before
# generating.
#
# Always injected, even when tools are present. Free-tier upstream models
# (the racing default) often get OpenAI tool schemas but don't actually
# support function calling — they invent fake <tool_call:paste>...XML
# that the proxy then strips, leaving the user with empty replies. The
# tools wording tells the model to either use the OpenAI tool-call format
# properly or answer in plain text — never improvise XML.
_ANTI_HALLUCINATION_TOOLS = (
    "[SYSTEM] If you need a tool, emit a real OpenAI tool_call object — "
    "never XML tags, bracket syntax, or <tool_call:NAME> blocks. "
    "Hallucinated tool syntax is stripped and the response is retried "
    "on another tier. If no tool fits, answer in plain text from your "
    "knowledge and conversation context."
)
_ANTI_HALLUCINATION_PLAIN = (
    "[SYSTEM] Respond now in plain text. "
    "No startup sequences. No initialization steps. "
    "Never output XML tags, bracket syntax, or tool_call blocks — "
    "those are not real tools and will be stripped. "
    "Answer directly using your knowledge and conversation context."
)


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
    last_line = msg_lower.rstrip().rsplit("\n", 1)[-1].strip()
    if msg_lower in _HELP_COMMANDS or last_line in _HELP_COMMANDS:
        resp = chat_commands._handle_help()
        if is_stream:
            return _wrap_json_as_sse(resp)
        return resp

    if msg_lower in _TIER_COMMANDS or last_line in _TIER_COMMANDS:
        resp = await chat_commands._handle_tier_command()
        if is_stream:
            return _wrap_json_as_sse(resp)
        return resp

    # Chat commands and the heartbeat work on the serving app's brain,
    # profile and scheduler, never on a module global.
    state = request.app.state
    brain_db = state.brain_db

    # Intercept heartbeat messages — run checks in Python instead of LLM
    if heartbeat._is_heartbeat(last_user_msg):
        print("[PROXY] heartbeat intercepted — running checks in Python", flush=True)
        return await heartbeat._handle_heartbeat(
            is_stream, brain_db=brain_db, profile=getattr(state, "profile", None)
        )

    # Intercept !task commands — enqueue, list, cancel, reprioritize
    # Check both full message and last line (fallback when metadata stripping fails)
    raw_last_line = last_user_msg.rstrip().rsplit("\n", 1)[-1].strip()
    task_action = chat_commands._extract_task_command(
        last_user_msg
    ) or chat_commands._extract_task_command(raw_last_line)
    if task_action is not None:
        return await chat_commands._dispatch_task_command(
            task_action, is_stream, brain_db=brain_db
        )

    # Intercept marketing commands (/sched, /scan, /projects, /marketing)
    marketing_cmd = chat_commands._extract_marketing_command(last_user_msg)
    if marketing_cmd is not None:
        return await chat_commands._dispatch_marketing_command(
            marketing_cmd,
            is_stream,
            brain_db=brain_db,
            scheduler=getattr(state, "scheduler", None),
        )

    # Intercept !persona / !learn (alias, TR-013) — explicit feedback and
    # on-demand reflection
    persona_args = chat_commands._extract_persona_command(last_user_msg)
    if persona_args is not None:
        return await chat_commands._dispatch_persona_command(
            persona_args,
            is_stream,
            brain_db=brain_db,
            profile_provider=state.profile_provider,
        )

    # Strip poisoned assistant messages from conversation history.
    # Uses both static markers (synthetic responses, tool hallucinations)
    # AND the deferral detector — any assistant message that would fail
    # _is_non_answer() gets stripped so the model doesn't see its own
    # previous failures and generate apologies or continued deferrals.
    # Never drop an assistant turn that carries tool_calls: its tool results
    # would be left orphaned and providers reject that history with a 400.
    messages = [
        msg
        for msg in messages
        if not (
            msg.get("role") == "assistant"
            and not msg.get("tool_calls")
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

    # Inject anti-hallucination reinforcement as a TRAILING system message
    # (see _ANTI_HALLUCINATION_* for why it goes last and why it is always
    # injected, tools or not).
    reminder = (
        _ANTI_HALLUCINATION_TOOLS if body.get("tools") else _ANTI_HALLUCINATION_PLAIN
    )
    messages.append({"role": "system", "content": reminder})

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
    # Why a tier is not answering: skipped here or failed in the race.
    failures: dict[str, str] = {}
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
            # the whole provider so claude-sub carries traffic instead of
            # every request stalling through failed OpenRouter attempts.
            if remaining is not None and remaining <= 0:
                if _should_notify("balance-guard"):
                    await telegram.notify(
                        f"*Balance guard* — OpenRouter balance ${remaining:.2f}.\n"
                        "Routing around OpenRouter until it's topped up."
                    )
                continue
        # Paid tiers are opt-in per message (!paid / !claude), never raced.
        if tier.get("paid") and not forced_tier:
            continue
        if tier.get("paid") and provider == "openrouter":
            remaining = await _check_openrouter_balance()
            # An unknown balance (lookup never succeeded or failing) denies
            # paid: the floor exists to protect credit we cannot see.
            if remaining is None or remaining <= OPENROUTER_BALANCE_FLOOR:
                failures[tier_name] = (
                    f"OpenRouter balance {describe_balance()} is not above "
                    f"the ${OPENROUTER_BALANCE_FLOOR:.2f} paid floor"
                )
                continue
        # Last gate: claiming the half-open probe slot must mean the tier
        # really is going to be called.
        if _circuit_open(tier_name):
            failures[tier_name] = "breaker open after repeated failures"
            continue
        available.append(tier)

    if not available:
        print(f"[PROXY] no tiers available: {failures}", flush=True)
        return _synthetic_response(
            "I'm temporarily on pause — no LLM tiers are available right now"
            + (f" ({_failure_summary(failures)})" if failures else "")
            + ". Try again later."
        )

    # Race all available tiers concurrently — first success wins
    tasks = {
        asyncio.create_task(
            _try_single_tier(tier, body, status_msg_id, failures),
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

    print(f"[PROXY] all tiers exhausted: {failures}", flush=True)
    raced = {name: failures.get(name, "failed") for name in tasks.values()}
    retry = (
        "Try again later."
        if forced_tier
        else "Send !paid or !claude to use the paid tier, or try again later."
    )
    if all(why.startswith("rate-limited") for why in raced.values()):
        return _synthetic_response(
            f"I'm temporarily on pause — the API tiers are rate-limited right now. "
            f"{retry}"
        )
    return _synthetic_response(
        f"I'm temporarily on pause — no tier answered ({_failure_summary(raced)}). "
        f"{retry}"
    )


def _failure_summary(failures: dict[str, str]) -> str:
    """``tier: reason; tier: reason`` for a user-facing failure line."""
    return "; ".join(f"{name}: {why}" for name, why in failures.items())
