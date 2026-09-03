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
into a digest. The heartbeat response is non-empty only when the digest
is due (at most once per hour) — OpenClaw relays non-empty responses to
Telegram, so the cadence on the phone is one condensed message per
hour, not one per 10-minute cycle.

**Digest (hourly, only when there is buffered work):**
```
Heartbeat digest (N item(s) this hour):
[ALEX|SYSTEM] #<id>: <description>
Result: <summary>
Created #<id> [P<n>] <description>
Tiers: <tripped> TRIPPED | OpenRouter balance: $X.XX
```

Any other cycle responds empty (silent).

**Relay the digest verbatim.** Do not expand it, reformat it, add
headings, or append recommendations — the digest is already the
finished message. Telegram rejects a sendMessage body over 4096
characters with a 400 and the whole delivery is dropped, so an
elaborated digest can be lost entirely. The proxy caps what it hands
over at 4096; that budget only holds if the text is passed through.

## Rules

- ONE task per cycle maximum
- Empty response = silent (no Telegram notification)
- Deduplication: don't recreate an alert/investigation task if a
  similar one exists from the last 24 hours regardless of status —
  completed alerts count, or they would be recreated every cycle
- Infrastructure monitoring only. Alex creates his own work tasks via `!task`
