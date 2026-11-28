# Heartbeat Checklist

Runs every 10 minutes (08:00 to 20:00 active hours). The proxy answers it
in Python (`workflows/heartbeat.py`); the prompt never reaches a model.
The LLM is used only to work a pending task and, on idle cycles, to
suggest one grounded task.

If you are a model reading this, the proxy did not intercept the
heartbeat. Reply `NO_REPLY` and create no tasks: the checks below run
in Python, and acting on them here duplicates their tasks.

## What Gets Checked

1. **Tier health**: any circuit breakers tripped?
2. **OpenRouter balance**: read live from the OpenRouter credits API
   (`/api/v1/credits`), cached 5 minutes. Below the floor
   (`OPENROUTER_BALANCE_FLOOR`, default $10): alert task. At or below
   $0: OpenRouter tiers are skipped entirely and claude-sub carries
   traffic (no stalling, no failed attempts). A balance that cannot be
   read for 30 minutes raises a `Balance unknown alert:` task.
3. **Stuck tasks**: a task still `active` after
   `HEARTBEAT_ACTIVE_REAP_HOURS` (default 6) lost its worker. It is
   marked failed with a digest line; re-create it to retry.
4. **Pending tasks**: anything in the queue to process? One per cycle.
5. **Research freshness** (daily, not every cycle): has a research
   session run in the last 24 hours? Handled server-side by the
   `research_pulse` generator: if research is stale it enqueues a
   `research:tome: <topic>` task (P3) that `scripts/tome_bridge.py`
   runs through the local `/tome:research` session.

## Behavior

### No pending tasks: Proactive Scan
- If a tier is tripped, create a task: `Investigate tier recovery: <name>` (P2)
- If the OpenRouter balance is below the floor, create a task:
  `Low balance alert: $X.XX remaining` (P1)
- At most every 30 minutes: if tasks have waited over 4 hours, create one
  `Stale queue: N task(s) pending over 4 hours; review the queue` (P2).
  The scan runs only when no heartbeat-owned task is pending, so these
  wait on someone else; the nudge closes from the live queue (no LLM),
  listing each task under the tome bridge, you, or the heartbeat
- At most every 15 minutes, when nothing above fired: ask the LLM for one
  research task grounded in the tracked projects and deal targets (P3).
  A suggestion that names none of them is dropped. Set
  `HEARTBEAT_LLM_SUGGEST=0` to turn this off.
- If nothing triggers: reply `NO_REPLY`, which the automation treats as
  silent (no Telegram message)

### Pending tasks exist: Process One
- Claim the highest-priority pending task
- Run it: status alerts close with live facts and no LLM, structured
  scans go to the marketing scanners, research-shaped tasks to the
  `/research` orchestrator (real Reddit/GitHub/web results with URLs),
  everything else to the LLM
- Mark it completed with the result, or blocked when the result is a
  question for the operator, or failed when the work raised
- If no LLM tier answers, put the task back to pending. The digest line
  names each tier's cause (`out of credit`, `rate-limited`, `empty
  reply`, `API key rejected`). On its 6th outage cycle (about an hour)
  it is failed instead, so one task cannot stall the queue

## Reporting: hourly digest, not per-event messages

Everything worth reporting (task completions, created tasks) buffers
into a digest. At most once per hour, the flush delivers it as a small
burst of Telegram messages: each item goes out directly from the
workflows service (one message per item, so long results arrive whole;
the proxy chunks any item past Telegram's 4096-character limit and
marks continuations with a leading `…`), followed by one closing
summary that this heartbeat response carries and OpenClaw relays.

**Direct item messages (sent by the proxy, arrive first):**
```
Heartbeat digest (N item(s) this hour):
[<OPERATOR>|SYSTEM] #<id>: <description>
Result: <summary>
Created #<id> [P<n>] <description>
```

**Closing summary (this response, relay it):**
```
Heartbeat digest: N item(s) this hour — sent as M message(s) above.
Waiting on you: #<id> <description>
Tiers: <tripped> TRIPPED | OpenRouter balance: $X.XX
```

Any other cycle responds empty (silent).

**Waiting-on-you lines** re-surface tasks whose result came back
BLOCKED (a question for the operator). They stay in the summary footer
until a later task answers them or they age out (7 days), because the
ask-once design otherwise goes mute while waiting on the answer. When
the digest has no other content, the response is still `Heartbeat
digest: nothing new this hour.` plus those lines.

**Relay the summary verbatim.** Do not expand it, reformat it, add
headings, or append recommendations. The item messages have already
been sent by the proxy; the summary is the finished closing message.
Telegram rejects a sendMessage body over 4096 characters with a 400 and
the whole delivery is dropped, so an elaborated summary can be lost
entirely. The proxy caps what it hands over at 4096; that budget only
holds if the text is passed through.

## Watchdog (scheduler-side, not the heartbeat)

The workflows APScheduler runs a 5-minute watchdog
(`workflows/watchdog.py`) independent of OpenClaw: the proxy stamps
every heartbeat arrival, and if none arrive for 15+ minutes during
active hours (08:00 to 20:00 local), it messages Telegram directly. A
wedged OpenClaw is otherwise total silence, since the digest only
flushes when a heartbeat prompt arrives. Re-alerts every 2h while
stalled, one recovery notice when heartbeats return.

## Rules

- ONE task per cycle maximum
- Empty response = silent (no Telegram notification)
- Deduplication: don't recreate an alert/investigation task if a
  similar one exists from the last 24 hours regardless of status.
  Completed alerts count, or they would be recreated every cycle
- The heartbeat creates only the alert, stale-queue and grounded
  research tasks above. The operator creates their own work tasks via
  `!task`
