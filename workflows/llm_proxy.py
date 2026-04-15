"""LLM proxy with three-tier fallback and Telegram notifications.

Sits between OpenClaw and LLM providers. Tries each tier in order,
notifies via Telegram on fallback transitions, and returns the first
successful response in OpenAI-compatible format (streaming or non-streaming).
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from telegram import delete_message, edit_status, notify, send_status, send_typing

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


def _circuit_open(tier_name: str) -> bool:
    """Check if a tier's circuit breaker is open (should be skipped)."""
    state = _circuit_state.get(tier_name)
    if not state:
        return False
    if state["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
        elapsed = time.monotonic() - state["last_failure"]
        if elapsed < CIRCUIT_COOLDOWN_SECONDS:
            return True
        _circuit_state.pop(tier_name, None)
    return False


def _record_failure(tier_name: str) -> None:
    state = _circuit_state.setdefault(tier_name, {"failures": 0, "last_failure": 0})
    state["failures"] += 1
    state["last_failure"] = time.monotonic()


def _record_success(tier_name: str) -> None:
    _circuit_state.pop(tier_name, None)


def _record_rate_limit(tier_name: str) -> None:
    """Trip circuit breaker immediately — a 429 is an explicit 'stop calling'."""
    _circuit_state[tier_name] = {
        "failures": CIRCUIT_FAILURE_THRESHOLD,
        "last_failure": time.monotonic(),
    }


# ─── Balance Guard (protect $10 free-tier threshold) ─────────────

OPENROUTER_CREDIT_BALANCE = float(os.getenv("OPENROUTER_CREDIT_BALANCE", "0"))
OPENROUTER_BALANCE_FLOOR = float(os.getenv("OPENROUTER_BALANCE_FLOOR", "10.0"))

_balance_cache: dict[str, float | None] = {"remaining": None, "checked_at": 0.0}
_BALANCE_CHECK_INTERVAL = 300  # re-check at most every 5 minutes


async def _check_openrouter_balance() -> float | None:
    """Return estimated remaining OpenRouter balance, or None if unknown.

    Calls the /auth/key endpoint at most once per _BALANCE_CHECK_INTERVAL.
    """
    now = time.monotonic()
    if now - (_balance_cache.get("checked_at") or 0) < _BALANCE_CHECK_INTERVAL:
        return _balance_cache.get("remaining")

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key or not OPENROUTER_CREDIT_BALANCE:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                usage = resp.json().get("data", {}).get("usage", 0)
                remaining = OPENROUTER_CREDIT_BALANCE - usage
                _balance_cache["remaining"] = remaining
                _balance_cache["checked_at"] = now
                return remaining
    except httpx.HTTPError:
        pass

    return _balance_cache.get("remaining")


# ─── Notification Debouncing ──────────────────────────────────────

_notification_last_sent: dict[str, float] = {}
_NOTIFICATION_DEBOUNCE_SECONDS = 120


def _should_notify(tier_name: str) -> bool:
    """Return True if enough time has passed since last notification for this tier."""
    now = time.monotonic()
    last = _notification_last_sent.get(tier_name, 0)
    if now - last < _NOTIFICATION_DEBOUNCE_SECONDS:
        return False
    _notification_last_sent[tier_name] = now
    return True


def _background_notify(tier_name: str, message: str) -> None:
    """Log tier events without sending to Telegram.

    Telegram notifications go to the user's conversation chat and look like
    garbled bot responses. Rate limit events are logged for debugging only.
    Explicit admin alerts (balance guard) use notify() directly.
    """
    if not _should_notify(tier_name):
        return
    clean = message.replace("*", "").replace("`", "")
    logger.info("[tier] %s", clean)


async def _safe_notify(message: str) -> None:
    """Fire-and-forget wrapper that logs failures instead of raising."""
    try:
        await notify(message)
    except Exception:
        logger.exception("Notification delivery failed")


# ─── Auto-Retry ──────────────────────────────────────────────────

MAX_AUTO_RETRY_WAIT = 30  # seconds — max hold time before returning synthetic response
PROVIDER_TIMEOUT = 45  # per-provider HTTP timeout (seconds) — was 90
REQUEST_DEADLINE = 60  # hard ceiling for the entire chat request (seconds)


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


def _get_reset_info(resp: httpx.Response) -> str:
    """Extract rate limit reset time as a human-readable string.

    Checks retry-after (seconds), x-ratelimit-reset-* (epoch/ISO),
    response body metadata, and error message text. Returns '' if nothing found.
    """
    # retry-after header — seconds until reset
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            seconds = int(retry_after)
            if seconds < 120:
                return f"in {seconds}s"
            return f"in ~{seconds // 60}min"
        except ValueError:
            pass

    # Reset timestamp headers (epoch or ISO)
    for header in (
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset",
        "ratelimit-reset",
    ):
        value = resp.headers.get(header)
        if not value:
            continue
        # Try epoch seconds
        try:
            reset_dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
            delta = reset_dt - datetime.now(timezone.utc)
            if delta > timedelta(0):
                mins = int(delta.total_seconds()) // 60
                return f"at {reset_dt.strftime('%H:%M UTC')} (~{mins}min)"
            return f"at {reset_dt.strftime('%H:%M UTC')}"
        except (ValueError, OSError):
            pass
        # Try ISO timestamp
        try:
            reset_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return f"at {reset_dt.strftime('%H:%M UTC')}"
        except (ValueError, AttributeError):
            pass

    # Check response body — metadata fields and error message text
    try:
        body = resp.json()
        error = body.get("error", {})
        if isinstance(error, dict):
            meta = error.get("metadata", {})
            if isinstance(meta, dict):
                # Explicit reset fields
                raw = meta.get("ratelimit_reset") or meta.get("retry_after")
                if raw:
                    return f"at {raw}"
                # OpenRouter puts provider error in metadata.raw
                raw_msg = meta.get("raw", "")
                if raw_msg:
                    parsed = _parse_reset_from_message(raw_msg)
                    if parsed:
                        return parsed
            # Fallback: extract timing from the top-level error message
            msg = error.get("message", "")
            if msg:
                parsed = _parse_reset_from_message(msg)
                if parsed:
                    return parsed
    except Exception:
        pass

    return ""


# Patterns to extract timing from OpenRouter error messages
_RESET_SECONDS_RE = re.compile(r"(\d+)\s*(?:second|sec|s\b)", re.IGNORECASE)
_RESET_MINUTES_RE = re.compile(r"(\d+)\s*(?:minute|min|m\b)", re.IGNORECASE)
_RESET_HOURS_RE = re.compile(r"(\d+)\s*(?:hour|hr|h\b)", re.IGNORECASE)


def _parse_reset_from_message(msg: str) -> str:
    """Extract reset timing from an error message string."""
    hours = _RESET_HOURS_RE.search(msg)
    if hours:
        h = int(hours.group(1))
        reset_dt = datetime.now(timezone.utc) + timedelta(hours=h)
        return f"at ~{reset_dt.strftime('%H:%M UTC')} (~{h}h)"

    minutes = _RESET_MINUTES_RE.search(msg)
    if minutes:
        m = int(minutes.group(1))
        return f"in ~{m}min"

    seconds = _RESET_SECONDS_RE.search(msg)
    if seconds:
        s = int(seconds.group(1))
        return f"in {s}s"

    # Daily limit — no specific time given
    if "day" in msg.lower() or "daily" in msg.lower():
        return "daily limit — resets in up to 24h"

    # Upstream provider throttling — transient, no specific reset time
    if "upstream" in msg.lower() or "retry shortly" in msg.lower():
        return "upstream provider throttled — retry in a few minutes"

    return ""


def _parse_reset_seconds_from_message(msg: str) -> int | None:
    """Extract reset time in seconds from error message text."""
    hours = _RESET_HOURS_RE.search(msg)
    if hours:
        return int(hours.group(1)) * 3600

    minutes = _RESET_MINUTES_RE.search(msg)
    if minutes:
        return int(minutes.group(1)) * 60

    seconds = _RESET_SECONDS_RE.search(msg)
    if seconds:
        return int(seconds.group(1))

    if "day" in msg.lower() or "daily" in msg.lower():
        return 86400

    if "upstream" in msg.lower() or "retry shortly" in msg.lower():
        return 20  # transient upstream throttle — conservative estimate

    return None


def _get_reset_seconds(resp: httpx.Response) -> int | None:
    """Extract rate limit reset time in seconds, or None if unknown."""
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return max(int(retry_after), 1)
        except ValueError:
            pass

    for header in (
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset",
        "ratelimit-reset",
    ):
        value = resp.headers.get(header)
        if not value:
            continue
        try:
            reset_dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
            delta = reset_dt - datetime.now(timezone.utc)
            if delta > timedelta(0):
                return max(int(delta.total_seconds()), 1)
        except (ValueError, OSError):
            pass

    try:
        body = resp.json()
        error = body.get("error", {})
        if isinstance(error, dict):
            meta = error.get("metadata", {})
            if isinstance(meta, dict):
                raw = meta.get("retry_after")
                if raw:
                    try:
                        return max(int(raw), 1)
                    except ValueError:
                        pass
                raw_msg = meta.get("raw", "")
                if raw_msg:
                    secs = _parse_reset_seconds_from_message(raw_msg)
                    if secs is not None:
                        return secs
            msg = error.get("message", "")
            if msg:
                secs = _parse_reset_seconds_from_message(msg)
                if secs is not None:
                    return secs
    except Exception:
        pass

    return None


def _get_rate_limit_detail(resp: httpx.Response) -> str:
    """Extract provider name and reason from a rate limit response."""
    try:
        body = resp.json()
        error = body.get("error", {})
        if isinstance(error, dict):
            meta = error.get("metadata", {})
            if isinstance(meta, dict):
                provider = meta.get("provider_name", "")
                raw = meta.get("raw", "")
                if provider and raw:
                    return f"({provider}: {raw[:120]})"
                if provider:
                    return f"(provider: {provider})"
                if raw:
                    return f"({raw[:120]})"
    except Exception:
        pass
    return ""


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


# XML-style hallucinated tool tags: <exec command="..."></exec>, <tool_call>...</tool_call>, etc.
_XML_TOOL_TAG = re.compile(
    r"<(?:exec|tool_call|function_call|search|browse|cron|write|read|subagents?)\b[^>]*>"
    r"[\s\S]*?"
    r"</(?:exec|tool_call|function_call|search|browse|cron|write|read|subagents?)>",
    re.IGNORECASE,
)
# Self-closing XML tags: <exec command="..."/>
_XML_SELF_CLOSING = re.compile(
    r"<(?:exec|tool_call|function_call|search|browse|cron|write|read|subagents?)\b[^/]*/\s*>",
    re.IGNORECASE,
)
# Unclosed XML tool tags: <exec ...> or <exec ... (runs to end of string)
_XML_UNCLOSED_TAG = re.compile(
    rf"<(?:{_TOOL_KEYWORDS})\b[\s\S]*$",
    re.IGNORECASE,
)
# Hallucinated metadata tags: <session_status>...</session_status>, <system>...</system>, etc.
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
                if f"!{keyword}" in text_lower:
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
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
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
        for i, word in enumerate(content.split(" ")):
            token = f" {word}" if i > 0 else word
            yield _make_chunk({"content": token})
        # Final chunk
        yield _make_chunk({}, finish_reason="stop")
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


async def _handle_tier_command() -> JSONResponse:
    """Return current tier status, balance, and circuit breaker state."""
    lines = ["Tier Status\n"]
    for tier in CONFIG["tiers"]:
        name = tier["name"]
        if _circuit_open(name):
            marker = "TRIPPED"
        elif name == _last_tier_used:
            marker = "ACTIVE"
        else:
            marker = "ready"
        lines.append(f"  [{marker}] {name} — {tier['description']}")

    remaining = await _check_openrouter_balance()
    lines.append("")
    if remaining is not None:
        lines.append(
            f"Balance: ${remaining:.2f} remaining (floor: ${OPENROUTER_BALANCE_FLOOR:.2f})"
        )
    else:
        lines.append("Balance: not configured (set OPENROUTER_CREDIT_BALANCE)")

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
  GET  /tier                     Tier status JSON"""
    return _synthetic_response(text)


# ─── !task Command ──────────────────────────────────────────────


_TASK_TRIGGERS = ("!task", "/task", "!tasks", "/tasks")
_TASK_ACTIONS = ("cancel", "priority", "list", "tail")


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
            f"Task Queue ({len(pending)} pending, {len(active)} active, {len(done)} done)\n"
        ]

        if active:
            lines.append("NOW PROCESSING:")
            for t in active:
                src = "ALEX" if t.get("source") == "user" else "SYS"
                lines.append(
                    f"  [{src}] #{t['id']} [P{t['priority']}] {t['description'][:55]}"
                )
            lines.append("")

        if pending:
            lines.append("QUEUED:")
            for t in pending:
                src = "ALEX" if t.get("source") == "user" else "SYS"
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
    "pending_reminder": 3600,  # 1 hr — remind Alex about P3+ tasks needing review
}

# Path to soul.md inside the container (mounted read-only).
_SOUL_MD_PATH = Path(__file__).parent / "soul.md"


def _load_soul() -> str:
    """Read soul.md for persona context. Returns empty string if missing."""
    try:
        return _SOUL_MD_PATH.read_text().strip()
    except FileNotFoundError:
        return ""


_TASK_CATEGORIES = [
    "client outreach or relationship building",
    "financial review (spending, billing, ROI)",
    "learning or skill development for Alex",
    "infrastructure optimization or cost reduction",
    "documentation or knowledge capture",
    "personal wellness or family reminder",
    "business development or lead generation",
    "tooling improvement or workflow automation",
    "security audit or compliance check",
    "research into new technologies or services",
]

# Track which category was last used to rotate through them.
_last_category_index = 0


def _build_thinking_prompt() -> str:
    """Build the LLM thinking prompt with recent task history and category rotation."""
    global _last_category_index
    soul = _load_soul()

    # Gather recent tasks and brain state for grounding
    recent_descriptions = []
    brain_summary = "Brain is empty — no entities recorded yet."
    try:
        from app import brain_db

        all_tasks = brain_db.list_tasks()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for t in all_tasks:
            try:
                created = datetime.fromisoformat(t["created_at"])
                if created > cutoff:
                    recent_descriptions.append(t["description"])
            except (ValueError, KeyError):
                continue

        pages = brain_db.list_pages(limit=20)
        if pages:
            by_type: dict[str, list[str]] = {}
            for p in pages:
                by_type.setdefault(p["page_type"], []).append(p["slug"])
            parts = [f"{k}: {', '.join(v)}" for k, v in by_type.items()]
            brain_summary = "Known entities in brain:\n  " + "\n  ".join(parts)
    except Exception:
        pass

    recent_block = ""
    if recent_descriptions:
        recent_list = "\n".join(f"  - {d}" for d in recent_descriptions[-10:])
        recent_block = (
            f"\n\nTasks already created in the last 24 hours (DO NOT repeat or suggest anything similar):\n"
            f"{recent_list}\n"
        )

    # Rotate through categories
    category = _TASK_CATEGORIES[_last_category_index % len(_TASK_CATEGORIES)]
    _last_category_index += 1

    if soul:
        return (
            f"{soul}\n\n"
            "---\n"
            f"Focus area for this cycle: **{category}**\n"
            f"\n{brain_summary}\n"
            f"{recent_block}\n"
            "RULES:\n"
            "- Only reference clients, people, or systems that exist in the brain above.\n"
            "- If the brain is empty, suggest tasks that BUILD knowledge: "
            "record a client, document a system, capture a decision.\n"
            "- Do NOT invent client names, people, or events.\n"
            "- Do NOT suggest sending emails or making calls — suggest PREPARING drafts or RESEARCHING info.\n"
            "- Tasks should be completable by an AI with access to web search and the brain API.\n\n"
            "Suggest exactly ONE actionable task. "
            "Respond with ONLY the task description (one sentence, no explanation, no quotes)."
        )
    return (
        f"You are Max, an executive assistant. Focus area: {category}. "
        "Suggest exactly ONE specific, actionable task that an AI can complete. "
        "Respond with ONLY the task description (one sentence)."
    )


def _is_heartbeat(msg: str) -> bool:
    """Detect heartbeat messages from max-ops agent."""
    return msg.lower().strip().startswith(_HEARTBEAT_MARKER)


def _proactive_ready(check_name: str) -> bool:
    """Return True if enough time has passed since the last run of this check."""
    interval = _PROACTIVE_INTERVALS.get(check_name, 3600)
    last_run = _proactive_state.get(check_name, 0.0)
    return (time.monotonic() - last_run) >= interval


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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
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

    When web_search=True, enables GLM's server-side web search tool
    for Z.AI tiers so the model can fetch live data.
    """
    for tier in CONFIG["tiers"]:
        if _circuit_open(tier["name"]):
            continue
        provider = tier["provider"]
        provider_config = CONFIG["providers"][provider]
        api_key = os.getenv(provider_config["env_key"], "")
        if not api_key:
            continue

        body: dict = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }

        # GLM built-in web_search — use the standard (non-coding) endpoint
        # which supports server-side tool execution.
        call_provider = provider
        if web_search and provider == "zai":
            call_provider = "zai-search"
            body["tools"] = [{"type": "web_search", "web_search": {"enable": True}}]

        try:
            resp = await _call_provider(call_provider, tier["models"], body, api_key)
            if resp.status_code == 200:
                text = (
                    resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                if text:
                    return text
        except Exception:
            pass
    return None


async def _llm_suggest_task() -> str | None:
    """Ask the LLM for one proactive task suggestion."""
    text = await _llm_call(_build_thinking_prompt(), max_tokens=100)
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
            if _circuit_open(name)
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
    done = sum(1 for t in all_tasks if t["status"] in ("completed", "failed"))

    return (
        "CURRENT SYSTEM STATE:\n"
        "Tiers:\n" + "\n".join(tier_lines) + "\n"
        f"Balance: {balance} (floor: ${OPENROUTER_BALANCE_FLOOR:.2f})\n"
        f"Last tier used: {_last_tier_used or 'none'}\n"
        f"Task queue: {pending} pending, {active} active, {done} done"
    )


async def _build_work_prompt(task_description: str, web_search: bool = False) -> str:
    """Build a prompt for the LLM to work on a specific task, with live system data."""
    soul = _load_soul()
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
            "and a suggested comment Alex can post.\n"
            "- Use numbered items, not bullet nesting.\n"
            "- Be specific — real post titles, real subreddits, real URLs.\n"
        )
        if web_search
        else ""
    )

    return (
        f"{context}"
        f"{state}\n"
        f"{brain_context}\n"
        f"---\n"
        f"You have been assigned this task:\n"
        f'"{task_description}"\n\n'
        "RULES:\n"
        "- Reference real data from the system state, brain, and web search results.\n"
        "- Do NOT invent URLs, post titles, or usernames. Only cite what you found.\n"
        "- If you have web search, use it to find live data before answering.\n"
        "- If the task requires an action you can't perform (sending email, making calls), "
        "describe what you prepared and what Alex needs to do to finish it.\n"
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
    return f"Could not reach LLM to work this task. Alex should handle manually: {description}"


async def _handle_heartbeat(is_stream: bool) -> JSONResponse | StreamingResponse:
    """Run heartbeat checks in Python instead of relying on the LLM.

    Three layers of initiative:
    1. Infrastructure monitoring — tripped tiers, low balance (every cycle)
    2. Task queue awareness — stale task nudges (every 30 min)
    3. LLM-powered thinking — self-directed task suggestions (every 1 hr)
    """
    from app import brain_db

    lines: list[str] = []
    tasks_created: list[dict] = []

    # ── 1. Tier status ──────────────────────────────────────────
    tripped = [tier["name"] for tier in CONFIG["tiers"] if _circuit_open(tier["name"])]

    remaining = await _check_openrouter_balance()

    # ── 2. Check for pending tasks ──────────────────────────────
    # Process one task per cycle — both system-generated and user-created.
    pending = brain_db.list_tasks(status="pending")

    if pending:
        task = pending[0]
        source = task.get("source", "system")
        label = "ALEX" if source == "user" else "SYSTEM"
        brain_db.claim_task(task["id"])
        result = await _llm_work_task(task["description"])
        brain_db.complete_task(task["id"], result, "completed")
        lines.append(f"[{label}] #{task['id']}: {task['description']}")
        lines.append(f"Result: {result}")

        # Direct Telegram notification
        await notify(
            f"[{label}] Task completed: #{task['id']}\n"
            f"{task['description']}\n\n"
            f"Result: {result}"
        )
    else:
        # ── PROACTIVE SCAN ──────────────────────────────────────
        all_tasks = brain_db.list_tasks()

        # Layer 1: Infrastructure — every cycle
        for name in tripped:
            desc = f"Investigate tier recovery: {name}"
            if not any(
                t["description"] == desc and t["status"] == "pending" for t in all_tasks
            ):
                t = brain_db.create_task(desc, priority=2)
                tasks_created.append(t)

        if remaining is not None and remaining < 5.0:
            desc = f"Low balance alert: ${remaining:.2f} remaining"
            if not any(
                "Low balance alert" in t["description"] and t["status"] == "pending"
                for t in all_tasks
            ):
                t = brain_db.create_task(desc, priority=1)
                tasks_created.append(t)

        # Layer 2: Stale task awareness — every 30 min
        if _proactive_ready("stale_tasks"):
            _proactive_mark("stale_tasks")
            stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
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

        # Layer 3: LLM self-directed thinking — every 1 hr
        if not tasks_created and _proactive_ready("llm_thinking"):
            _proactive_mark("llm_thinking")
            suggestion = await _llm_suggest_task()
            if suggestion and not _has_recent_task(all_tasks, suggestion):
                t = brain_db.create_task(suggestion, priority=3)
                tasks_created.append(t)

    # ── 3. Build response ───────────────────────────────────────
    # Only send a visible response when there's something worth reporting.
    # Empty heartbeat_ok is silent — OpenClaw won't relay it to Telegram.
    if not lines and not tasks_created:
        resp = _synthetic_response("")
    else:
        if tasks_created:
            lines.append(f"Created {len(tasks_created)} task(s):")
            for t in tasks_created:
                lines.append(f"  #{t['id']} [P{t['priority']}] {t['description'][:60]}")

        if tripped:
            lines.append(f"Tiers: {', '.join(tripped)} TRIPPED")
        if remaining is not None:
            lines.append(f"Balance: ${remaining:.2f}")

        resp = _synthetic_response("\n".join(lines))

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
        asyncio.create_task(send_typing())
        asyncio.create_task(edit_status(status_msg_id, f"\u23f3 {tier['description']}"))

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
                    f"[PROXY] empty response from {tier_name} after sanitization — skipping",
                    flush=True,
                )
                _record_failure(tier_name)
                return None, None

            if _is_non_answer(first_content):
                print(
                    f"[PROXY] non-answer from {tier_name}: {first_content[:80]!r} — skipping",
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
                    f"[PROXY] 429 on {tier_name} — headers: {dict(resp.headers)} body: {resp.text[:500]}",
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


# ─── Proxy Endpoint ───────────────────────────────────────────────


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(None),
):
    # Auth check — if PROXY_AUTH_TOKEN is set, require it
    if PROXY_AUTH_TOKEN:
        if not authorization or authorization != f"Bearer {PROXY_AUTH_TOKEN}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    raw_body = await request.json()
    messages = raw_body.get("messages", [])
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
    # Skip anti-hallucination when the request includes tool schemas —
    # the agent legitimately needs to use tools (heartbeat, ops tasks).
    has_tools = bool(body.get("tools"))
    if not has_tools:
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
    asyncio.create_task(send_typing())
    status_msg_id = await send_status("\u23f3")

    # Always use non-streaming internally for full sanitization + garbled
    # detection, then re-wrap as SSE if the client requested streaming.
    # Hard deadline prevents the proxy from blocking OpenClaw indefinitely.
    try:
        result = await asyncio.wait_for(
            _handle_non_streaming(body, forced_tier, status_msg_id=status_msg_id),
            timeout=REQUEST_DEADLINE,
        )
    except asyncio.TimeoutError:
        logger.warning("Request exceeded %ds deadline", REQUEST_DEADLINE)
        result = _synthetic_response(
            "I took too long to respond — the LLM providers might be slow. "
            "Please try again in a moment."
        )

    # Remove the status message — OpenClaw delivers the real response
    if status_msg_id:
        asyncio.create_task(delete_message(status_msg_id))

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
        if not api_key or _circuit_open(tier_name):
            continue
        if provider not in PROVIDER_URLS:
            continue
        if tier_name == "openrouter-paid" and not forced_tier:
            continue
        if tier_name == "openrouter-paid":
            remaining = await _check_openrouter_balance()
            if remaining is not None and remaining <= OPENROUTER_BALANCE_FLOOR:
                await notify(
                    f"*Balance guard* — ${remaining:.2f} remaining"
                    f"\nSkipping `{tier_name}` to protect free-tier quota"
                )
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
                    asyncio.create_task(
                        edit_status(
                            status_msg_id, f"\u23f3 Rate limited — retrying in {wait}s"
                        )
                    )
                await asyncio.sleep(wait)
                for name, _ in rate_limit_resets:
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
