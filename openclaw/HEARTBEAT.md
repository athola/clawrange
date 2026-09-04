# Heartbeat Checklist

Runs every 10 minutes (08:00–20:00 active hours). Handled in Python by
the proxy. No LLM needed.

## What Gets Checked

1. **Tier health**: any circuit breakers tripped?
2. **OpenRouter balance**: read live from the OpenRouter credits API
   (`/api/v1/credits`), cached 5 minutes. Below the floor
   (`OPENROUTER_BALANCE_FLOOR`, default $10) → alert task. At or below
   $0 → OpenRouter tiers are skipped entirely and zai-direct carries
   traffic (no stalling, no failed attempts).
3. **Pending tasks**: anything in the queue to process? One per cycle.
4. **Brain health**: is the knowledge DB accessible? (via /healthz brain status)
5. **Research freshness** (daily, not every cycle): has a research
   session run in the last 24 hours? Handled server-side by the
   `research_pulse` generator: if research is stale it enqueues a
   `research:tome: <topic>` task (P3) that `scripts/tome_bridge.py`
   runs through the local `/tome:research` session.

## Behavior

### No pending tasks → Proactive Scan
- If a tier is tripped, create a task: `Investigate tier recovery: <name>` (P2)
- If the OpenRouter balance is below the floor, create a task:
  `Low balance alert: $X.XX remaining` (P1)
- If nothing triggers → respond empty (silent, no Telegram notification)

### Pending tasks exist → Process One
- Claim the highest-priority pending task
- Run it: structured scans via the marketing scanners, research-shaped
  tasks via the `/research` orchestrator (real Reddit/GitHub/web results
  with URLs), everything else via the LLM
- Mark it completed with the result

## Reporting: hourly digest, not per-event messages

Everything worth reporting (task completions, created tasks) buffers
into a digest. At most once per hour, the flush delivers it as a small
burst of Telegram messages: each item goes out directly from the
workflows service (one message per item, so long results arrive whole —
the proxy chunks any item past Telegram's 4096-character limit and
marks continuations with a leading `…`), followed by one closing
summary that this heartbeat response carries and OpenClaw relays.

**Direct item messages (sent by the proxy, arrive first):**
```
Heartbeat digest (N item(s) this hour):
[ALEX|SYSTEM] #<id>: <description>
Result: <summary>
Created #<id> [P<n>] <description>
```

**Closing summary (this response — relay it):**
```
Heartbeat digest: N item(s) this hour — sent as M message(s) above.
Waiting on you: #<id> <description>
Tiers: <tripped> TRIPPED | OpenRouter balance: $X.XX
```

Any other cycle responds empty (silent).

**Waiting-on-you lines** re-surface tasks whose result came back
BLOCKED (a question for Alex) — they stay in the summary footer until a
later task answers them or they age out (7 days), because the ask-once
design otherwise goes mute while waiting on the answer. When the digest
has no other content, the response is still `Heartbeat digest: nothing
new this hour.` plus those lines.

**Relay the summary verbatim.** Do not expand it, reformat it, add
headings, or append recommendations — the item messages have already
been sent by the proxy; the summary is the finished closing message.
Telegram rejects a sendMessage body over 4096 characters with a 400 and
the whole delivery is dropped, so an elaborated summary can be lost
entirely. The proxy caps what it hands over at 4096; that budget only
holds if the text is passed through.

## Watchdog (scheduler-side, not the heartbeat)

The workflows APScheduler runs a 5-minute watchdog
(`workflows/watchdog.py`) independent of OpenClaw: the proxy stamps
every heartbeat arrival, and if none arrive for 15+ minutes during
active hours (08:00–20:00 local), it messages Telegram directly. A
wedged OpenClaw is otherwise total silence — the digest only flushes
when a heartbeat prompt arrives. Re-alerts every 2h while stalled,
one recovery notice when heartbeats return.

## Rules

- ONE task per cycle maximum
- Empty response = silent (no Telegram notification)
- Deduplication: don't recreate an alert/investigation task if a
  similar one exists from the last 24 hours regardless of status —
  completed alerts count, or they would be recreated every cycle
- Infrastructure monitoring only. Alex creates his own work tasks via `!task`
