"""Telegram notification module — sends alerts to a single authorized chat."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger("clawrange.telegram")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Telegram rejects a sendMessage body over 4096 chars; stay a little under
# so the plain-text retry never tips a chunk over the edge.
MAX_CHARS = 4000

# Bot API timeouts: message sends, and the fire-and-forget typing/delete calls.
SEND_TIMEOUT_S = 10.0
QUICK_TIMEOUT_S = 5.0


async def _call(
    method: str, payload: dict, timeout: float = SEND_TIMEOUT_S
) -> httpx.Response | None:
    """POST one Bot API method. None when unconfigured or on network error."""
    if not BOT_TOKEN or not CHAT_ID:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"{API_BASE}/{method}", json={"chat_id": CHAT_ID, **payload}
            )
    except httpx.HTTPError as exc:
        logger.warning("Telegram %s failed: %s", method, exc)
        return None


async def notify(text: str) -> bool:
    """Send a message to the configured Telegram chat.

    Returns True on success, False on failure (never raises).
    Tries Markdown parse mode first; on a 400 "can't parse entities"
    error (which happens when post titles contain unbalanced
    underscores, asterisks, or brackets — common in Reddit titles),
    retries as plain text. URLs auto-linkify in plain mode anyway.
    """
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured — skipping notification")
        return False

    r = await _call("sendMessage", {"text": text, "parse_mode": "Markdown"})
    if r is None:
        return False
    if r.is_success:
        return True
    # Markdown parse failure -> retry as plain text. Telegram auto-detects
    # URLs in plain mode, so links stay clickable; only bold is lost.
    if r.status_code == 400 and "parse" in r.text.lower():
        logger.warning("Telegram Markdown rejected (parse error), retrying plain")
        r2 = await _call("sendMessage", {"text": text})
        if r2 is not None and r2.is_success:
            return True
        if r2 is not None:
            logger.error("Telegram plain retry failed: %s %s", r2.status_code, r2.text)
        return False
    logger.error("Telegram API error: %s %s", r.status_code, r.text)
    return False


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split a long report into sendable chunks.

    Packs whole paragraphs, then whole lines, so a `[title](url)` line is
    never cut in half. Only a single line longer than `limit` is hard-split,
    at a word boundary when it has one.
    """
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    for line in text.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            pieces.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        pieces.append(line)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            continue
        if current.strip():
            chunks.append(current.strip("\n"))
        current = piece
    if current.strip():
        chunks.append(current.strip("\n"))
    return chunks


def split_sentences(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split prose (one over-long digest item) into sendable parts.

    Unlike split_text, which packs whole lines so a Markdown link is never
    cut, this cuts long running text at sentence ends.

    Cuts preferentially after a sentence terminator or newline, then at a
    word boundary, hard-cutting only when the item has neither. Continuation
    parts carry an ellipsis prefix so a burst of messages reads as one item
    flowing on rather than unrelated entries.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break
        head = rest[:limit]
        cut = max(
            head.rfind(". "), head.rfind("! "), head.rfind("? "), head.rfind("\n")
        )
        if cut < limit // 2:
            cut = head.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(rest[: cut + 1].rstrip())
        rest = rest[cut + 1 :].lstrip()
    # Exactly one marker on every continuation part.
    return [parts[0], *("… " + p for p in parts[1:])]


async def notify_long(
    text: str,
    attempts: int = 3,
    backoff: float = 2.0,
    _sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[int, int]:
    """Send a report of any length as consecutive messages.

    Each chunk is retried with exponential backoff. Returns
    `(chunks_sent, chunks_total)` so callers can record partial delivery
    instead of treating it as success.
    """
    sleep = _sleep or asyncio.sleep
    chunks = split_text(text)
    sent = 0
    for chunk in chunks:
        for attempt in range(attempts):
            if await notify(chunk):
                sent += 1
                break
            if attempt < attempts - 1:
                await sleep(backoff * (2**attempt))
    return sent, len(chunks)


async def send_typing() -> bool:
    """Send 'typing' chat action so the user sees the bot is working."""
    r = await _call("sendChatAction", {"action": "typing"}, timeout=QUICK_TIMEOUT_S)
    return r is not None and r.is_success


async def send_status(text: str) -> int | None:
    """Send a status message and return its message_id for later editing/deletion."""
    r = await _call("sendMessage", {"text": text})
    if r is None or not r.is_success:
        return None
    return r.json().get("result", {}).get("message_id")


async def edit_status(message_id: int, text: str) -> bool:
    """Edit a previously sent status message."""
    if not message_id:
        return False
    r = await _call("editMessageText", {"message_id": message_id, "text": text})
    return r is not None and r.is_success


async def delete_message(message_id: int) -> bool:
    """Delete a message by ID."""
    if not message_id:
        return False
    r = await _call(
        "deleteMessage", {"message_id": message_id}, timeout=QUICK_TIMEOUT_S
    )
    return r is not None and r.is_success
