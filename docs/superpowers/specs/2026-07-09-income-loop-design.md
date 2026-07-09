# Income Loop — Design

Date: 2026-07-09. Status: approved-by-default (autonomous session; user
pre-approved brainstorm → plan → execute in the task request).

## Purpose

Let the operator task ClawRange, from Telegram, with describing,
researching, and (approval-gated) implementing income-generation
techniques. First concrete instance: a $50 crypto testbed account with
a weekly strategy-review loop (see `docs/income-strategy.md`).

## Constraints and assumptions

- **Honesty constraint**: the system must never claim or chase
  "reliable high weekly returns"; reviews benchmark against holding
  USDC and holding BTC, and every proposal needs cited sources.
- **No autonomous execution**: all trades/strategy changes are
  `[DRAFT]` tasks requiring explicit human approval (existing
  marketing convention, reused verbatim).
- Scope-guard: `variable-config` branch is RED; new code lands on a
  fresh branch (`income-loop`), docs may land on the current branch.
- No exchange account exists yet; nothing may depend on live keys.

## Architecture — reuse the existing pipeline

No new pipeline. The income loop is a new *user* of existing parts:

| Need | Existing primitive |
|------|--------------------|
| "Describe technique X" | LLM proxy chat via John-117 (citation discipline) |
| "Research technique X" | `research:tome: <topic>` task + tome bridge (`research_pulse` pattern) |
| Weekly reevaluation | New `income_review` generator → `[DRAFT]` task + Telegram |
| Approval gate | Existing task queue claim/result + `[DRAFT]` convention |
| Memory (entries, paper trades, rejected ideas) | Brain notes with `income:` prefix |

## Components

### Phase 0 — docs (this branch, done)
- `docs/income-strategy.md`: strategy v0, risk rules, setup steps,
  weekly protocol, Telegram surface.

### Phase 1 — operator manual steps (user only)
- Open exchange account, deposit $50, place initial orders, store
  trade-only API keys in `.env`, report entry prices to the brain.

### Phase 2 — `income_review` generator (branch `income-loop`)
- `income_review_generator(brain_db, **kwargs)` in
  `workflows/generators.py`, registered in `GENERATORS`.
- Behavior (mirrors `research_pulse`): idempotent — skip if an open
  `income:` review task already exists; otherwise enqueue
  `[DRAFT] income: weekly strategy review` with a description that
  embeds the review checklist from the strategy doc; send a Telegram
  notification; write schedule status.
- Scheduled via existing `/sched` API (weekly, Sunday), no scheduler
  changes needed.
- Tests in `workflows/tests/test_generators.py` (or the module's
  existing test home): enqueue-on-empty, skip-on-duplicate, telegram
  notify called, registry contains `income_review`.

### Phase 3 — deferred (needs Phase 1 + 4 weeks of history)
- `!income` proxy command family (status, review, propose).
- Exchange adapter (ccxt) for balance reads, then approval-gated
  order placement with trade-only keys. Paper engine first.

## Error handling

- Generator failures log and write schedule status, never raise into
  the scheduler (existing generator convention).
- Telegram send failures do not lose the task — the task row is the
  source of truth; the heartbeat surfaces unclaimed tasks.

## Out of scope

- Any strategy promising fixed weekly returns.
- Leverage/derivatives support.
- Auto-execution of trades without a human approval step.
