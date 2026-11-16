"""Regression tests for the PR #11 review fixes in scanners and generators."""

import pytest

import generators
from app import brain_db

TOPICS = ["topic A", "topic B", "topic C"]


def _tome_tasks() -> list[dict]:
    tasks = brain_db.list_tasks()
    return [t for t in tasks if t["description"].startswith("research:tome:")]


class TestResearchPulseRotation:
    """#20: a claimed or finished task must not stop the rotation."""

    @pytest.mark.asyncio
    async def test_rotates_through_every_topic_across_claims(self):
        queued: list[str] = []
        for _ in range(len(TOPICS) * 2):
            await generators.research_pulse_generator(brain_db, topics=TOPICS)
            pending = brain_db.list_tasks(status="pending")
            assert len(pending) == 1
            task = pending[0]
            queued.append(task["description"].removeprefix("research:tome: "))
            # The bridge claims and finishes the task before the next fire.
            brain_db.claim_task(task["id"])
            brain_db.complete_task(task["id"], "done")
        assert queued == TOPICS + TOPICS

    @pytest.mark.asyncio
    async def test_skips_topic_with_an_active_task(self):
        await generators.research_pulse_generator(brain_db, topics=TOPICS)
        first = brain_db.list_tasks(status="pending")[0]
        brain_db.claim_task(first["id"])  # still running

        await generators.research_pulse_generator(brain_db, topics=TOPICS)
        descs = [t["description"] for t in _tome_tasks()]
        assert descs.count("research:tome: topic A") == 1
        assert "research:tome: topic B" in descs

    @pytest.mark.asyncio
    async def test_failed_topic_goes_to_the_back_of_the_queue(self):
        for topic in ("topic B", "topic C", "topic A"):
            task = brain_db.create_task(f"research:tome: {topic}", source="schedule")
            brain_db.claim_task(task["id"])
            brain_db.complete_task(task["id"], "boom", status="failed")
        # topic B was queued longest ago, so it comes round first.
        await generators.research_pulse_generator(brain_db, topics=TOPICS)
        pending = [t["description"] for t in brain_db.list_tasks(status="pending")]
        assert pending == ["research:tome: topic B"]

    @pytest.mark.asyncio
    async def test_all_topics_open_skips(self):
        for topic in TOPICS:
            brain_db.create_task(f"research:tome: {topic}", source="schedule")
        await generators.research_pulse_generator(brain_db, topics=TOPICS)
        assert len(_tome_tasks()) == len(TOPICS)
