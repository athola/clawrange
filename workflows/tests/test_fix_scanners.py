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


def _emerging_project():
    brain_db.upsert_project(
        "clawrange",
        "acme",
        "clawrange",
        topics=["agent orchestration"],
        subreddits=["ClaudeAI"],
        search_terms=["agent orchestration"],
    )


def _emerging_post():
    from reddit_search import RedditPost

    return RedditPost(
        id="em1",
        url="https://reddit.com/r/AI_Agents/comments/em1",
        title="multi-agent orchestration patterns?",
        subreddit="AI_Agents",
        score=12,
        comments=3,
        created_utc="2026-05-09T07:00:00+00:00",
    )


def _emerging_hits() -> int:
    rows = brain_db.list_subreddit_stats(project_slug="clawrange", curated_only=False)
    return sum(r["hits"] for r in rows if r["subreddit"] == "ai_agents")


class TestEmergingHitsAfterDelivery:
    """#38: emerging hits count once, and only for a delivered digest."""

    @pytest.mark.asyncio
    async def test_rerun_does_not_recount_emerging_hits(self, monkeypatch):
        from unittest.mock import AsyncMock

        _emerging_project()
        monkeypatch.setattr(
            "reddit_search.search_subreddits", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(
            "reddit_search.search_all", AsyncMock(return_value=[_emerging_post()])
        )
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify)

        await generators.morning_digest_generator(brain_db)
        assert "AI_Agents" in notify.await_args.args[0]
        await generators.morning_digest_generator(brain_db)

        assert _emerging_hits() == 1
        assert brain_db.is_seen("reddit_post", "em1", "clawrange")
        assert "multi-agent orchestration" not in notify.await_args.args[0]

    @pytest.mark.asyncio
    async def test_failed_delivery_records_no_emerging_hit(self, monkeypatch):
        from unittest.mock import AsyncMock

        _emerging_project()
        monkeypatch.setattr(
            "reddit_search.search_subreddits", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(
            "reddit_search.search_all", AsyncMock(return_value=[_emerging_post()])
        )
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=False))

        await generators.morning_digest_generator(brain_db)

        assert _emerging_hits() == 0
        assert not brain_db.is_seen("reddit_post", "em1", "clawrange")
