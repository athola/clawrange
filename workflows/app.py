"""ClawRange Workflow Service — replaces n8n with testable Python endpoints."""

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from llm_proxy import router as llm_router
from telegram import notify

app = FastAPI(title="ClawRange Workflows", version="2.0.0")
app.include_router(llm_router)

# ─── Mock CRM Data ──────────────────────────────────────────────────

MOCK_LEADS = [
    {
        "name": "John Smith",
        "phone": "903-555-0100",
        "status": "New Lead",
        "lastContact": "2026-03-24",
        "interest": "3BR Jessup single-section",
        "nextStep": "Schedule lot visit",
        "assignedTo": "Mike (Sales)",
        "source": "Facebook Ad",
    },
    {
        "name": "Maria Garcia",
        "phone": "903-555-0200",
        "status": "Finance Review",
        "lastContact": "2026-03-22",
        "interest": "Titanium 4BR multi-section",
        "nextStep": "Waiting on FHA pre-approval",
        "assignedTo": "Sarah (Finance)",
        "source": "Walk-in",
    },
    {
        "name": "Robert Johnson",
        "phone": "903-555-0300",
        "status": "Appointment Set — Mar 28",
        "lastContact": "2026-03-25",
        "interest": "Jessup 2BR starter home",
        "nextStep": "Lot walkthrough Friday 10 AM",
        "assignedTo": "Mike (Sales)",
        "source": "Website",
    },
    {
        "name": "Ashley Williams",
        "phone": "903-555-0400",
        "status": "Appointment Set",
        "lastContact": "2026-03-26",
        "interest": "Titanium energy-efficient 3BR",
        "nextStep": "Follow-up call Monday",
        "assignedTo": "Sarah (Finance)",
        "source": "Referral",
    },
]


# ─── Health ─────────────────────────────────────────────────────────


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ─── Tier Status (direct endpoint, bypasses OpenClaw) ─────────────


@app.get("/tier")
async def tier_status():
    from llm_proxy import (
        CONFIG,
        OPENROUTER_BALANCE_FLOOR,
        _check_openrouter_balance,
        _circuit_open,
        _last_tier_used,
    )

    tiers = []
    for tier in CONFIG["tiers"]:
        name = tier["name"]
        if _circuit_open(name):
            marker = "TRIPPED"
        elif name == _last_tier_used:
            marker = "ACTIVE"
        else:
            marker = "ready"
        tiers.append(
            {"name": name, "status": marker, "description": tier["description"]}
        )

    remaining = await _check_openrouter_balance()
    return {
        "tiers": tiers,
        "last_used": _last_tier_used or "none",
        "balance_remaining": f"${remaining:.2f}"
        if remaining is not None
        else "not configured",
        "balance_floor": f"${OPENROUTER_BALANCE_FLOOR:.2f}",
        "paid_auto_fallback": "off",
    }


@app.post("/tier/notify")
async def tier_notify():
    """Send tier status to Telegram — hit this endpoint to get status in chat."""
    from llm_proxy import (
        CONFIG,
        OPENROUTER_BALANCE_FLOOR,
        _check_openrouter_balance,
        _circuit_open,
        _last_tier_used,
    )

    lines = ["*Tier Status*\n"]
    for tier in CONFIG["tiers"]:
        name = tier["name"]
        if _circuit_open(name):
            marker = "TRIPPED"
        elif name == _last_tier_used:
            marker = "ACTIVE"
        else:
            marker = "ready"
        lines.append(f"  [{marker}] {name}")

    remaining = await _check_openrouter_balance()
    if remaining is not None:
        lines.append(
            f"\nBalance: ${remaining:.2f} (floor: ${OPENROUTER_BALANCE_FLOOR:.2f})"
        )
    else:
        lines.append("\nBalance: not configured")
    lines.append(f"Last used: {_last_tier_used or 'none'}")
    lines.append("Paid fallback: off (use 'paid' keyword)")

    text = "\n".join(lines)
    sent = await notify(text)
    return {"sent": sent, "message": text}


# ─── Test Webhook (Connectivity Canary) ─────────────────────────────


@app.post("/webhook/test")
@app.post("/webhook-test/test")
def test_webhook(body: dict[str, Any] = {}):
    keys = list(body.keys())
    summary = ", ".join(f"{k}={body[k]!r}" for k in keys) if keys else "(empty payload)"
    return {
        "status": "ok",
        "message": f"received: {summary}",
        "receivedAt": datetime.now(timezone.utc).isoformat(),
        "payloadKeys": keys,
        "echo": body,
    }


# ─── Lead Status Lookup ────────────────────────────────────────────


class LeadQuery(BaseModel):
    name: str = ""
    phone: str = ""


@app.post("/webhook/lead-status")
@app.post("/webhook-test/lead-status")
def lead_status(query: LeadQuery):
    search_name = query.name.lower()
    search_phone = "".join(c for c in query.phone if c.isdigit())

    match = None
    for lead in MOCK_LEADS:
        name_hit = search_name and search_name in lead["name"].lower()
        phone_digits = "".join(c for c in lead["phone"] if c.isdigit())
        phone_hit = search_phone and phone_digits == search_phone
        if name_hit or phone_hit:
            match = lead
            break

    if not match:
        return {
            "status": "not_found",
            "message": f'No lead found matching name "{query.name}" or phone "{query.phone}".',
        }

    message = "\n".join(
        [
            f"Lead Status for {match['name']}",
            f"Phone: {match['phone']}",
            f"Current Status: {match['status']}",
            f"Interest: {match['interest']}",
            f"Last Contact: {match['lastContact']}",
            f"Next Step: {match['nextStep']}",
            f"Assigned To: {match['assignedTo']}",
        ]
    )
    return {"status": "found", "message": message, "lead": match}


# ─── Morning Briefing ──────────────────────────────────────────────


@app.get("/webhook/morning-briefing")
@app.post("/webhook/morning-briefing")
def morning_briefing():
    now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    lines = [
        f"MORNING BRIEFING — {now}",
        "Longview Home Center",
        "=" * 40,
        "",
        f"Total Active Leads: {len(MOCK_LEADS)}",
        "",
    ]
    for i, lead in enumerate(MOCK_LEADS, 1):
        lines.extend(
            [
                f"{i}. {lead['name']} ({lead['phone']})",
                f"   Status: {lead['status']}",
                f"   Interest: {lead['interest']}",
                f"   Source: {lead['source']}",
                "",
            ]
        )
    lines.append("---\nGenerated by ClawRange Workflow Service")

    return {
        "briefing": "\n".join(lines),
        "leadCount": len(MOCK_LEADS),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
