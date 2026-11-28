"""Imperative shell: run the watch, keep its state and health, gate actions.

State lives in the brain database's ``homelab_state`` table, never in brain
pages. Nothing here is ever sent to an LLM: alerts, reports and replies are
rendered by deterministic code from sanitized strings.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import telegram
from errors import UpstreamError

from . import findings, sources
from .actions import PendingActions, authorize
from .config import HomelabConfig
from .models import SOURCES, Observation, WatchState
from .parse import safe_label
from .report import render_net

logger = logging.getLogger("clawrange.homelab")

Notify = Callable[[str], Awaitable[bool]]

MESSAGE_CAP = 3500
OUTBOX_CAP = 20
ERROR_CAP = 120
# The watch runs every 5 minutes; three missed passes means it has stopped.
HEALTH_STALE_AFTER = timedelta(minutes=15)
DROPPED_RE = re.compile(r"^(\d+) older homelab alerts dropped")
STATE_UNREADABLE_ALERT = "homelab watch state was unreadable; relearning baselines"

LOCKOUT_FAILS = 3
LOCKOUT_WINDOW = timedelta(minutes=10)
LOCKOUT_FOR = timedelta(minutes=10)
ISSUED_MEMORY = timedelta(minutes=10)
ISSUED_CAP = 50

_watch_lock = asyncio.Lock()
_pending = PendingActions()
_bad_codes: list[datetime] = []
_lockout_until: datetime | None = None
# Codes this process really issued (code -> when), so a replayed or late code
# is told apart from a guess and does not count toward the lockout.
_issued: dict[str, datetime] = {}


def reset_actions() -> None:
    """Forget pending codes and lockout state (tests, config reload)."""
    global _pending, _lockout_until
    _pending = PendingActions()
    _bad_codes.clear()
    _issued.clear()
    _lockout_until = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _short(text: str) -> str:
    """A one-line, length-capped error reason for storage and display."""
    return " ".join(str(text).split())[:ERROR_CAP]


async def _send(notify: Notify, text: str) -> bool:
    try:
        return bool(await notify(text))
    except Exception:
        # Local container logs only: the message (which may hold a code) is
        # never part of the log record, and the traceback carries no token.
        logger.exception("homelab: notify raised")
        return False


# ─── State ──────────────────────────────────────────────────────────


def load_state(brain_db) -> WatchState:
    return WatchState.from_dict(brain_db.homelab_get("watch"))


def _load_state_checked(brain_db) -> tuple[WatchState, bool]:
    """The watch state, and whether a stored row was there but unreadable."""
    value, unreadable = brain_db.homelab_load("watch")
    if unreadable:
        logger.error(
            "homelab: stored watch state is unreadable (not a JSON object); "
            "relearning baselines"
        )
        return WatchState(), True
    return WatchState.from_dict(value), False


async def live_observation(cfg: HomelabConfig, now: datetime | None = None):
    return await sources.observe(cfg, now or _utcnow())


def _health_record(
    brain_db, cfg: HomelabConfig, obs: Observation, state: WatchState, now: datetime
) -> dict[str, Any]:
    stored = brain_db.homelab_get("health") or {}
    previous = _as_dict(stored.get("sources"))
    record: dict[str, Any] = {}
    for source in SOURCES:
        if source not in cfg.configured:
            continue
        old = _as_dict(previous.get(source))
        ok = getattr(obs, source) is not None
        record[source] = {
            "ok": ok,
            "last_success": now.isoformat() if ok else old.get("last_success"),
            "failures": state.failures.get(source, 0),
            "last_error": "" if ok else _short(obs.errors.get(source, "")),
        }
    parts = {
        key.removeprefix("census."): _short(reason)
        for key, reason in obs.errors.items()
        if key.startswith("census.")
    }
    census_when = stored.get("census_when")
    if obs.census is not None:
        census_when = obs.census.when.isoformat()
    return {
        "sources": record,
        "last_run": now.isoformat(),
        "census_when": census_when,
        "census_parts": parts if obs.census is not None else stored.get("census_parts"),
    }


def _compose(lines: list[str]) -> tuple[str, list[str]]:
    """One message of alert lines, capped; the lines that did not fit."""
    header = f"Homelab: {len(lines)} alert(s)"
    shown: list[str] = []
    size = len(header) + 40  # room for the overflow note
    for line in lines:
        size += len(line) + 3
        if shown and size > MESSAGE_CAP:
            break
        shown.append(line[:MESSAGE_CAP])
    rest = lines[len(shown) :]
    text = header + "\n" + "\n".join(f"- {line}" for line in shown)
    if rest:
        text += f"\n({len(rest)} more follow)"
    return text, rest


def _cap_outbox(lines: list[str]) -> tuple[list[str], int]:
    """Keep the newest lines; summarise what fell off as one synthetic line."""
    if len(lines) <= OUTBOX_CAP:
        return lines, 0
    keep = lines[-(OUTBOX_CAP - 1) :]
    dropped = 0
    for line in lines[: len(lines) - len(keep)]:
        m = DROPPED_RE.match(line)
        dropped += int(m.group(1)) if m else 1
    note = f"{dropped} older homelab alerts dropped; see /net"
    return [note, *keep], dropped


def _outbox_lines(brain_db) -> list[str]:
    value, unreadable = brain_db.homelab_load("outbox")
    if unreadable or not isinstance(_as_dict(value).get("lines", []), list):
        logger.error("homelab: stored outbox is unreadable; starting it empty")
        return []
    return [str(x) for x in _as_dict(value).get("lines", [])]


async def run_watch(
    brain_db,
    cfg: HomelabConfig,
    *,
    now: datetime | None = None,
    notify: Notify = telegram.notify,
) -> dict[str, Any]:
    """One watch pass: observe, evaluate, persist, then alert.

    The new state, the health record and the outbox (old lines plus this
    pass's alerts) are stored in one transaction before anything is sent, so
    a crash or a failed send loses nothing; lines are removed from the outbox
    only after Telegram accepted them.
    """
    if not cfg.configured:
        return {"status": "disabled"}
    async with _watch_lock:
        now = now or _utcnow()
        state, unreadable = _load_state_checked(brain_db)
        obs = await sources.observe(cfg, now)
        new_state, alerts = findings.evaluate(state, obs, cfg.watch)
        texts = [a.text for a in alerts]
        if unreadable:
            texts.insert(0, STATE_UNREADABLE_ALERT)
        queued = _outbox_lines(brain_db)
        lines, dropped = _cap_outbox(queued + texts)
        if dropped:
            logger.warning("homelab: outbox full, dropped %d older alert(s)", dropped)
        brain_db.homelab_put_many(
            {
                "watch": new_state.to_dict(),
                "health": _health_record(brain_db, cfg, obs, new_state, now),
                "outbox": {"lines": lines},
            }
        )
        delivered: bool | None = None
        if lines:
            text, rest = _compose(lines)
            delivered = await _send(notify, text)
            if delivered:
                brain_db.homelab_put("outbox", {"lines": rest})
    return {
        "status": "ok",
        "alerts": len(texts),
        "queued": len(queued),
        "delivered": delivered,
        "dropped": dropped,
        "errors": dict(obs.errors),
    }


# ─── Health ─────────────────────────────────────────────────────────


def _age_exceeds(value: Any, now: datetime, limit: timedelta) -> bool:
    """True when ``value`` (an ISO time) is older than ``limit``; junk is stale."""
    if not isinstance(value, str):
        return False
    try:
        then = datetime.fromisoformat(value)
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return now - then > limit


def health(
    brain_db,
    cfg: HomelabConfig,
    *,
    detail: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Per-source health; never URLs or the token, error text only on detail.

    ``degraded`` also covers a watch that stopped running (no pass for
    three intervals) and a census file that has gone stale.
    """
    now = now or _utcnow()
    rec = brain_db.homelab_get("health") or {}
    recorded = _as_dict(rec.get("sources"))
    out: dict[str, Any] = {}
    problems: list[str] = []
    for source in SOURCES:
        configured = source in cfg.configured
        entry = _as_dict(recorded.get(source))
        ok = bool(entry.get("ok")) if configured else None
        item: dict[str, Any] = {
            "configured": configured,
            "ok": ok,
            "last_success": entry.get("last_success") if configured else None,
            "failures": int(entry.get("failures") or 0) if configured else 0,
        }
        if source in cfg.incomplete:
            item["incomplete"] = True
            problems.append(f"{source}_incomplete")
        if detail and configured:
            item["error"] = _short(entry.get("last_error") or "")
        if source == "ha" and cfg.local_error:
            problems.append("ha_local_file")
            if detail:
                item["local_error"] = cfg.local_error
        if configured and (not ok or item["last_success"] is None):
            problems.append(source)
        out[source] = item
    if cfg.configured:
        last_run = rec.get("last_run")
        if last_run is not None and _age_exceeds(last_run, now, HEALTH_STALE_AFTER):
            problems.append("watch_stalled")
        if "census" in cfg.configured and _age_exceeds(
            rec.get("census_when"),
            now,
            timedelta(hours=cfg.watch.census_max_age_hours),
        ):
            problems.append("census_stale")
        parts = _as_dict(rec.get("census_parts"))
        if "census" in cfg.configured and parts:
            problems.append("census_parts")
            if detail:
                out["census"]["part_errors"] = {
                    str(k): _short(v) for k, v in parts.items()
                }
    if not cfg.configured and not cfg.incomplete and not problems:
        status = "disabled"
    else:
        status = "degraded" if problems else "ok"
    return {
        "status": status,
        "sources": out,
        "last_run": rec.get("last_run"),
        "census_when": rec.get("census_when") if "census" in cfg.configured else None,
        "problems": problems,
    }


# ─── Chat text ──────────────────────────────────────────────────────


def render_net_full(
    obs: Observation,
    state: WatchState,
    kev_reviewed: frozenset[str] = frozenset(),
) -> str:
    """`!net`: the census view plus Kuma monitors and per-source outages."""
    lines = [render_net(obs, state, kev_reviewed)]
    for source in SOURCES:
        if source in obs.configured and getattr(obs, source) is None:
            detail = safe_label(obs.errors.get(source, ""), 80) or "no detail"
            lines.append(f"OUTAGE {findings.SOURCE_LABELS[source]}: {detail}")
    if obs.kuma:
        names = {1: "up", 0: "down", 2: "pending", 3: "maintenance"}
        lines.append("Kuma monitors:")
        for m in sorted(obs.kuma, key=lambda m: m.name.lower())[:30]:
            state_text = (
                names.get(m.status, "no data") if m.status is not None else "no data"
            )
            uptime = (
                f" {m.uptime_24h * 100:.1f}% 24h" if m.uptime_24h is not None else ""
            )
            lines.append(f"{safe_label(m.name)}: {state_text}{uptime}")
        if len(obs.kuma) > 30:
            lines.append(f"...and {len(obs.kuma) - 30} more")
    return "\n".join(lines)


# ─── Actions ────────────────────────────────────────────────────────


def _remember_issued(code: str, now: datetime) -> None:
    for old in [c for c, t in _issued.items() if now - t > ISSUED_MEMORY]:
        del _issued[old]
    _issued[code] = now
    while len(_issued) > ISSUED_CAP:
        _issued.pop(next(iter(_issued)))


async def request_action(
    cfg: HomelabConfig,
    service: str,
    entity_id: str,
    now: datetime,
    notify: Notify = telegram.notify,
) -> str:
    """Start a confirmed action. The code goes to Telegram, never the reply."""
    if "ha" not in cfg.configured:
        return "Home Assistant is not configured."
    reason = authorize(cfg.rules, service, entity_id)
    if reason:
        return f"Denied: {reason}."
    # Only issue a code for an entity Home Assistant really has: a typo in an
    # allow-listed pattern ("light.*") must not produce a code for nothing.
    try:
        known = {e.entity_id for e in await sources.fetch_ha_states(cfg)}
    except UpstreamError as exc:
        return (
            "Could not read Home Assistant to check the entity "
            f"({safe_label(str(exc), 60)}); nothing was run."
        )
    if entity_id not in known:
        return f"Denied: Home Assistant has no entity {safe_label(entity_id)}."
    code = _pending.issue(service, entity_id, now)
    _remember_issued(code, now)
    # service and entity_id passed authorize, so they are [a-z0-9_.] only:
    # nothing attacker-chosen reaches the message.
    sent = await _send(
        notify,
        f"Confirm `{service}` on `{entity_id}`: reply `/home confirm {code}` "
        "within 2 minutes.",
    )
    if not sent:
        _pending.redeem(code, now)
        _issued.pop(code, None)
        return "Could not send the confirmation code to Telegram; nothing was run."
    return (
        f"Confirmation code sent to Telegram separately for {safe_label(service)} "
        f"on {safe_label(entity_id)}. It expires in 2 minutes."
    )


async def confirm_action(
    cfg: HomelabConfig,
    code: str,
    now: datetime,
    notify: Notify = telegram.notify,
) -> str:
    """Redeem a code and run the action, after re-checking the policy."""
    global _lockout_until
    if _lockout_until is not None:
        if now < _lockout_until:
            return (
                "Confirmations are disabled for a few minutes after repeated bad codes."
            )
        _lockout_until = None
    redeemed = _pending.redeem(code, now)
    if redeemed is None:
        issued = _issued.get(code)
        if issued is not None and now - issued <= ISSUED_MEMORY:
            # A real code, replayed or late: a mistake, not a guess.
            return "That code was already used or has expired."
        _bad_codes[:] = [t for t in _bad_codes if now - t < LOCKOUT_WINDOW]
        _bad_codes.append(now)
        if len(_bad_codes) >= LOCKOUT_FAILS:
            _bad_codes.clear()
            _lockout_until = now + LOCKOUT_FOR
            if not await _send(
                notify,
                "Homelab: 3 bad confirmation codes in 10 minutes. "
                "`!home` confirmations are disabled for 10 minutes.",
            ):
                logger.warning(
                    "homelab: could not deliver the lockout notice to Telegram"
                )
            return (
                "Invalid code. Too many bad codes: "
                "confirmations are disabled for 10 minutes."
            )
        return "Invalid or expired code."
    _bad_codes.clear()
    service, entity_id = redeemed
    # The profile may have changed between request and confirmation.
    reason = authorize(cfg.rules, service, entity_id)
    if reason:
        return f"Denied: {reason}."
    if "ha" not in cfg.configured:
        return "Home Assistant is not configured."
    domain, _, name = service.partition(".")
    try:
        await sources.call_ha_service(cfg, domain, name, entity_id)
    except UpstreamError as exc:
        return f"Home Assistant call failed: {safe_label(str(exc), 80)}."
    return f"Done: {safe_label(service)} on {safe_label(entity_id)}."
