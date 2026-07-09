# Income Review Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly `income_review` scheduler generator that enqueues a `[DRAFT] income: weekly strategy review` task and pings Telegram, so the $50 crypto testbed gets reevaluated every Sunday without any autonomous trading.

**Architecture:** One new async generator in `workflows/generators.py`, registered in `GENERATORS`, mirroring `research_pulse_generator` (idempotent enqueue into the existing brain task queue). Scheduling itself uses the existing `/sched` API at runtime — no scheduler changes.

**Tech Stack:** Python 3.11, FastAPI service internals (`brain_db`, `telegram.notify`), pytest + pytest-asyncio.

## Global Constraints

- Work happens on branch `income-loop` (current branch `variable-config` is scope-guard RED).
- The generator must never place trades or call exchanges — it only enqueues a `[DRAFT]` task (spec: "No autonomous execution").
- Telegram failure must not lose the task: enqueue first, notify second; `notify` never raises.
- New generator must be added to the `GENERATORS` registry (CLAUDE.md convention).
- Tests live in `workflows/tests/`; run with `cd workflows && python -m pytest tests/test_income_review.py -v`.

---

### Task 1: `income_review_generator`

**Files:**
- Modify: `workflows/generators.py` (new function above the `GENERATORS` dict; new registry entry)
- Test: `workflows/tests/test_income_review.py` (create)

**Interfaces:**
- Consumes: `brain_db.list_tasks(status: str|None) -> list[dict]`, `brain_db.create_task(description: str, priority: int = 3, source: str = "system") -> dict`, `telegram.notify(text: str) -> bool` (async, never raises).
- Produces: `async def income_review_generator(brain_db, **kwargs) -> None`, registry key `"income_review"`. Task description constant `INCOME_REVIEW_DESC = "[DRAFT] income: weekly strategy review"`.

- [ ] **Step 1: Write the failing tests**

Create `workflows/tests/test_income_review.py`:

```python
"""Tests for the income_review generator.

The $50 crypto testbed (docs/income-strategy.md) is reevaluated weekly.
income_review_generator enqueues a [DRAFT] review task for human
approval — it never trades or calls exchanges — and pings Telegram.
"""

import pytest

import generators
from app import brain_db


def _pending_descs() -> list[str]:
    return [t["description"] for t in brain_db.list_tasks(status="pending")]


def test_registered_in_generators():
    assert "income_review" in generators.GENERATORS


@pytest.mark.asyncio
async def test_enqueues_draft_review_task(monkeypatch):
    sent = []

    async def fake_notify(text):
        sent.append(text)
        return True

    monkeypatch.setattr("telegram.notify", fake_notify)
    await generators.income_review_generator(brain_db)
    pending = _pending_descs()
    assert len(pending) == 1
    assert pending[0].startswith(generators.INCOME_REVIEW_DESC)
    # Review checklist rides in the task description so the agent
    # claiming it needs no other context.
    assert "P&L" in pending[0]
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_skips_when_review_already_pending(monkeypatch):
    sent = []

    async def fake_notify(text):
        sent.append(text)
        return True

    monkeypatch.setattr("telegram.notify", fake_notify)
    await generators.income_review_generator(brain_db)
    await generators.income_review_generator(brain_db)
    assert len(_pending_descs()) == 1
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_task_survives_telegram_failure(monkeypatch):
    async def failing_notify(text):
        return False

    monkeypatch.setattr("telegram.notify", failing_notify)
    await generators.income_review_generator(brain_db)
    assert len(_pending_descs()) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd workflows && python -m pytest tests/test_income_review.py -v`
Expected: FAIL — `AssertionError` on registry test and `AttributeError: module 'generators' has no attribute 'income_review_generator'` on the rest.

- [ ] **Step 3: Write the minimal implementation**

In `workflows/generators.py`, above the `GENERATORS` dict:

```python
INCOME_REVIEW_DESC = "[DRAFT] income: weekly strategy review"

_INCOME_REVIEW_CHECKLIST = (
    "Cover: balances and P&L vs the two benchmarks ($50 held as USDC; "
    "$50 all-in BTC on day one), the paper sleeve's 20-week-SMA decision "
    "and hypothetical result, fees paid this week, and proposed "
    "adjustments with cited sources (single-source claims flagged). "
    "Hard rules in docs/income-strategy.md apply; this task is a draft "
    "for operator approval — never execute trades."
)


async def income_review_generator(brain_db, **kwargs) -> None:
    """Enqueue the weekly $50-testbed strategy review as a [DRAFT] task.

    Mirrors research_pulse: idempotent (skips while a review is still
    pending), enqueue-first so a Telegram outage never loses the task,
    and never acts on its own — the review is a draft the operator
    approves on Telegram. See docs/income-strategy.md for the protocol.
    """
    from telegram import notify

    pending = {t["description"] for t in brain_db.list_tasks(status="pending")}
    if any(d.startswith(INCOME_REVIEW_DESC) for d in pending):
        logger.info("income_review: review already queued, skipping")
        return

    brain_db.create_task(
        f"{INCOME_REVIEW_DESC} — {_INCOME_REVIEW_CHECKLIST}",
        priority=2,
        source="schedule",
    )
    logger.info("income_review: enqueued weekly review")
    if not await notify(
        "📋 Weekly income review queued — claim the [DRAFT] task to run it."
    ):
        logger.warning("income_review: telegram notify failed (task kept)")
```

Add to the `GENERATORS` dict:

```python
    "income_review": income_review_generator,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd workflows && python -m pytest tests/test_income_review.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full suite and linters**

Run: `cd workflows && python -m pytest tests/ -q` then `ruff check generators.py tests/test_income_review.py`
Expected: all tests pass, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add workflows/generators.py workflows/tests/test_income_review.py
git commit -m "feat(income): weekly income_review generator enqueues draft strategy review"
```

---

### Post-plan operator step (not a code task)

Once the stack is up, schedule it for Sundays 16:00 (container-local
time, America/Chicago). Note: cron fields pass straight to APScheduler,
where numeric day-of-week `0` means Monday — always use the `sun` name:

```bash
curl -sX POST localhost:5678/sched -H 'content-type: application/json' \
  -d '{"id": "weekly-income-review", "name": "Weekly income strategy review ($50 testbed)", "kind": "income_review", "cron": "0 16 * * sun"}'
```
