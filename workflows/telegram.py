"""Telegram notification module — sends alerts to a single authorized chat."""

import logging
import os

import httpx

logger = logging.getLogger("clawrange.telegram")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def notify(text: str) -> bool:
    """Send a message to the configured Telegram chat.

    Returns True on success, False on failure (never raises).
    """
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured — skipping notification")
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{API_BASE}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            if not r.is_success:
                logger.error("Telegram API error: %s %s", r.status_code, r.text)
                return False
            return True
    except httpx.HTTPError as exc:
        logger.error("Telegram request failed: %s", exc)
        return False


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
