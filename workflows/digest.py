"""Heartbeat digest: buffer completed work and flush it hourly.

The heartbeat runs every 10 minutes but Telegram should hear from it at
most hourly. This module owns the persisted buffer, the due check and the
message planning, plus the heartbeat stamps the watchdog reads. The
handler in `heartbeat.py` records into it.
"""

import json
import logging
import os
import re
import time

import telegram

logger = logging.getLogger("clawrange.digest")

# ─── Heartbeat Digest ──────────────────────────────────────────────
# Completed work buffers here. Once the hour elapses the heartbeat sends
# each item straight to Telegram (one message per item) and returns a
# short summary as its response, which OpenClaw relays; every other cycle
# stays silent.

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

HEARTBEAT_DIGEST_MAX_LINES = int(
    os.getenv("HEARTBEAT_DIGEST_MAX_LINES", "200")
)  # buffered items kept while sends keep failing; oldest are dropped first


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
    _write_stamp("ts", now)


def record_heartbeat_done(now: float | None = None) -> None:
    """Stamp the end of a heartbeat that ran to completion.

    The arrival stamp is written before any work, so a handler that raises
    later would keep it fresh; the watchdog reads this one instead.
    """
    _write_stamp("done_ts", now)


def _write_stamp(key: str, now: float | None) -> None:
    """Update one key of the heartbeat stamp file, keeping the other."""
    path = os.getenv("HEARTBEAT_SEEN_PATH", "/data/heartbeat_seen.json")
    try:
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("done_ts", None)
        data[key] = now if now is not None else time.time()
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump({"ts": data.get("ts"), "done_ts": data["done_ts"]}, fh)
        os.replace(tmp, path)
    except OSError as exc:
        # The stamp is an observability aid; a heartbeat must never fail
        # because it could not be recorded.
        logger.warning("heartbeat %s stamp failed: %s", key, exc)


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
    path = _digest_state_path()
    try:
        with open(path) as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        # The next save would overwrite the undelivered lines: keep the
        # unreadable file beside it for a human to recover.
        aside = f"{path}.corrupt"
        try:
            os.replace(path, aside)
            where = f"moved to {aside}"
        except OSError:
            where = "could not be moved aside"
        logger.error("digest state corrupt (%s); %s", exc, where)
        return
    if not isinstance(saved, dict):
        return
    lines = saved.get("lines")
    if isinstance(lines, list):
        _digest_state["lines"] = [str(x) for x in lines]
    try:
        _digest_state["last_flush"] = float(saved.get("last_flush") or 0.0)
    except (TypeError, ValueError):
        pass  # a bad clock only makes the next digest due now


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
        logger.error("digest state save failed: %s", exc)


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
    balance: str | None = None,
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

    ``balance`` is the rendered balance line (llm_proxy.describe_balance);
    without it the footer falls back to ``remaining`` and omits an unknown
    balance.

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
    if balance is not None:
        if balance != "not configured":
            footer.append(f"OpenRouter balance: {balance}")
    elif remaining is not None:
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
    _digest_state["lines"] = _digest_trim(carry + _digest_state["lines"][len(lines) :])
    _digest_save()


_DROPPED_RE = re.compile(r"^(\d+) older digest item\(s\) dropped")


def _digest_trim(lines: list[str]) -> list[str]:
    """Keep the newest HEARTBEAT_DIGEST_MAX_LINES lines.

    With Telegram unset or failing, every flush re-buffers every line, so
    the buffer (and the state file rewritten each heartbeat) would grow
    without bound. The oldest items go first, replaced by one marker line
    that carries the running count. Trimmed only here, after a flush, so a
    record during an in-flight send cannot shift the taken prefix.
    """
    dropped = 0
    if lines and (m := _DROPPED_RE.match(lines[0])):
        dropped = int(m.group(1))
        lines = lines[1:]
    room = max(HEARTBEAT_DIGEST_MAX_LINES - 1, 1)
    if len(lines) > room:
        over = len(lines) - room
        dropped += over
        lines = lines[-room:]
        logger.warning("digest buffer full: dropped %d oldest item(s)", over)
    if not dropped:
        return lines
    marker = (
        f"{dropped} older digest item(s) dropped: Telegram delivery kept "
        "failing. Full results: GET /task"
    )
    return [marker, *lines]


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
