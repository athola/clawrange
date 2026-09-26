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

    async def _send(payload: dict) -> tuple[bool, int, str]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{API_BASE}/sendMessage", json=payload)
            return r.is_success, r.status_code, r.text

    try:
        ok, status, body = await _send(
            {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
        if ok:
            return True
        # Markdown parse failure -> retry as plain text. Telegram
        # auto-detects URLs in plain mode, so links stay clickable;
        # only inline bold formatting is lost.
        if status == 400 and "parse" in body.lower():
            logger.warning("Telegram Markdown rejected (parse error), retrying plain")
            ok2, status2, body2 = await _send({"chat_id": CHAT_ID, "text": text})
            if ok2:
                return True
            logger.error("Telegram plain retry failed: %s %s", status2, body2)
            return False
        logger.error("Telegram API error: %s %s", status, body)
        return False
    except httpx.HTTPError as exc:
        logger.error("Telegram request failed: %s", exc)
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
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                f"{API_BASE}/sendChatAction",
                json={"chat_id": CHAT_ID, "action": "typing"},
            )
            return r.is_success
    except httpx.HTTPError:
        return False


async def send_status(text: str) -> int | None:
    """Send a status message and return its message_id for later editing/deletion."""
    if not BOT_TOKEN or not CHAT_ID:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{API_BASE}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text},
            )
            if r.is_success:
                return r.json().get("result", {}).get("message_id")
    except httpx.HTTPError:
        pass
    return None


async def edit_status(message_id: int, text: str) -> bool:
    """Edit a previously sent status message."""
    if not BOT_TOKEN or not CHAT_ID or not message_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{API_BASE}/editMessageText",
                json={
                    "chat_id": CHAT_ID,
                    "message_id": message_id,
                    "text": text,
                },
            )
            return r.is_success
    except httpx.HTTPError:
        return False


async def delete_message(message_id: int) -> bool:
    """Delete a message by ID."""
    if not BOT_TOKEN or not CHAT_ID or not message_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                f"{API_BASE}/deleteMessage",
                json={"chat_id": CHAT_ID, "message_id": message_id},
            )
            return r.is_success
    except httpx.HTTPError:
        return False
