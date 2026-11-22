"""Imperative shell: run the watch, keep its state and health, gate actions.

State lives in the brain database's ``homelab_state`` table, never in brain
pages. Nothing here is ever sent to an LLM: alerts, reports and replies are
rendered by deterministic code from sanitized strings.
"""

from __future__ import annotations

import asyncio
import logging
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

LOCKOUT_FAILS = 3
LOCKOUT_WINDOW = timedelta(minutes=10)
LOCKOUT_FOR = timedelta(minutes=10)

_watch_lock = asyncio.Lock()
_pending = PendingActions()
_bad_codes: list[datetime] = []
_lockout_until: datetime | None = None


def reset_actions() -> None:
    """Forget pending codes and lockout state (tests, config reload)."""
    global _pending, _lockout_until
    _pending = PendingActions()
    _bad_codes.clear()
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
    except Exception as exc:
        logger.warning("homelab: notify raised %s", type(exc).__name__)
        return False


# ─── State ──────────────────────────────────────────────────────────


def load_state(brain_db) -> WatchState:
    return WatchState.from_dict(brain_db.homelab_get("watch"))


async def live_observation(cfg: HomelabConfig, now: datetime | None = None):
    return await sources.observe(cfg, now or _utcnow())


def _record_health(
    brain_db, cfg: HomelabConfig, obs: Observation, state: WatchState, now: datetime
) -> None:
    previous = (brain_db.homelab_get("health") or {}).get("sources") or {}
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
    brain_db.homelab_put("health", {"sources": record, "last_run": now.isoformat()})


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


async def _deliver(brain_db, lines: list[str], notify: Notify) -> bool | None:
    """Send the alert lines as one message; undelivered lines go to the outbox."""
    if not lines:
        brain_db.homelab_put("outbox", {"lines": []})
        return None
    text, rest = _compose(lines)
    if await _send(notify, text):
        brain_db.homelab_put("outbox", {"lines": rest[-OUTBOX_CAP:]})
        return True
    brain_db.homelab_put("outbox", {"lines": lines[-OUTBOX_CAP:]})
    return False


async def run_watch(
    brain_db,
    cfg: HomelabConfig,
    *,
    now: datetime | None = None,
    notify: Notify = telegram.notify,
) -> dict[str, Any]:
    """One watch pass: observe, evaluate, persist, then alert.

    State is saved before delivery, so an alert that could not be sent is
    kept in the outbox and retried on the next pass instead of being lost.
    """
    if not cfg.configured:
        return {"status": "disabled"}
    async with _watch_lock:
        now = now or _utcnow()
        state = load_state(brain_db)
        obs = await sources.observe(cfg, now)
        new_state, alerts = findings.evaluate(state, obs, cfg.watch)
        brain_db.homelab_put("watch", new_state.to_dict())
        _record_health(brain_db, cfg, obs, new_state, now)
        outbox = (brain_db.homelab_get("outbox") or {}).get("lines") or []
        lines = [str(x) for x in outbox] + [a.text for a in alerts]
        delivered = await _deliver(brain_db, lines, notify)
    return {
        "status": "ok",
        "alerts": len(alerts),
        "queued": len(lines) - len(alerts),
        "delivered": delivered,
        "errors": dict(obs.errors),
    }


# ─── Health ─────────────────────────────────────────────────────────


def health(brain_db, cfg: HomelabConfig, *, detail: bool) -> dict[str, Any]:
    """Per-source health; never URLs or the token, error text only on detail."""
    rec = brain_db.homelab_get("health") or {}
    recorded = _as_dict(rec.get("sources"))
    out: dict[str, Any] = {}
    degraded = False
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
        if detail and configured:
            item["error"] = _short(entry.get("last_error") or "")
        if configured and (not ok or item["last_success"] is None):
            degraded = True
        out[source] = item
    status = "disabled" if not cfg.configured else "degraded" if degraded else "ok"
    return {"status": status, "sources": out, "last_run": rec.get("last_run")}


# ─── Chat text ──────────────────────────────────────────────────────


def render_net_full(obs: Observation, state: WatchState) -> str:
    """`!net`: the census view plus Kuma monitors and per-source outages."""
    lines = [render_net(obs, state)]
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
    code = _pending.issue(service, entity_id, now)
    # service and entity_id passed authorize, so they are [a-z0-9_.] only:
    # nothing attacker-chosen reaches the message.
    sent = await _send(
        notify,
        f"Confirm `{service}` on `{entity_id}`: reply `!home confirm {code}` "
        "within 2 minutes.",
    )
    if not sent:
        _pending.redeem(code, now)
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
        _bad_codes[:] = [t for t in _bad_codes if now - t < LOCKOUT_WINDOW]
        _bad_codes.append(now)
        if len(_bad_codes) >= LOCKOUT_FAILS:
            _bad_codes.clear()
            _lockout_until = now + LOCKOUT_FOR
            await _send(
                notify,
                "Homelab: 3 bad confirmation codes in 10 minutes. "
                "`!home` confirmations are disabled for 10 minutes.",
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
