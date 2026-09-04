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
