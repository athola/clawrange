# Heartbeat Checklist

Runs every 5 minutes. Handled in Python by the proxy. No LLM needed.

## What Gets Checked

1. **Tier health**: any circuit breakers tripped?
2. **Balance**: below $5.00 threshold?
3. **Pending tasks**: anything in the queue to process?
4. **Brain health**: is the knowledge DB accessible? (via /healthz brain status)
5. **Research freshness** (daily, not every cycle): has a research
   session run in the last 24 hours? Handled server-side by the
   `research_pulse` generator: if research is stale it enqueues a
   `research:tome: <topic>` task (P3) that `scripts/tome_bridge.py`
   runs through the local `/tome:research` session. The personas have
   no POST tool, so they never trigger research directly.

## Behavior

### No pending tasks → Proactive Scan
- If a tier is tripped, create a task: `Investigate tier recovery: <name>` (P2)
- If balance is below $5.00, create a task: `Low balance alert: $X.XX remaining` (P1)
- If nothing triggers → respond `heartbeat_ok` (silent, no Telegram notification)

### Pending tasks exist → Process One
- Claim the highest-priority pending task
- Mark it completed with acknowledgment
- Report the result

## Response Format

**No issues, no tasks:**
```
heartbeat_ok
```

**Task completed:**
```
[TASK] #<id>: <description>
Result: <summary>
Tiers: all ready | Balance: $X.XX
```

**Issue found:**
```
Created N task(s):
  #<id> [P<n>] <description>
Tiers: <status> | Balance: $X.XX
```

## Rules

- ONE task per cycle maximum
- `heartbeat_ok` = silent (no Telegram notification)
- Deduplication: don't create tasks that already exist as pending
- Infrastructure monitoring only. Alex creates his own work tasks via `!task`
