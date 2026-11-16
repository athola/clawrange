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


class _StatusBrain:
    def __init__(self):
        self.status: list[tuple[str, str]] = []

    def update_schedule_status(self, schedule_id, last_run, last_status):
        self.status.append((schedule_id, last_status))


class TestPipelineStatusRedaction:
    """#47: a fetch error's URL (query tokens included) stays out of status."""

    @pytest.mark.asyncio
    async def test_connector_error_status_has_no_url(self, tmp_path, monkeypatch):
        import httpx

        from crm.sqlite_adapter import SQLiteCRM
        from tenant_profile import Profile

        secret_url = "https://portal.example/export?token=s3cret"
        profile = Profile(
            name="t",
            raw={
                "profile": "t",
                "connectors": [
                    {
                        "id": "portal-leads",
                        "source": {
                            "kind": "http_csv",
                            "url": secret_url,
                            "auth": {"kind": "none"},
                        },
                        "transform": {
                            "kind": "leads_clean",
                            "mapping": {"Email": "email"},
                            "required": ["email"],
                            "dedup_key": "email",
                        },
                        "sink": {
                            "kind": "crm",
                            "object": "leads",
                            "upsert_key": "email",
                        },
                    }
                ],
                "crm": {"adapter": "sqlite"},
            },
        )
        crm = SQLiteCRM(str(tmp_path / "crm.db"))
        crm.init()
        client = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(403))
        )
        brain = _StatusBrain()

        counts = await generators.pipeline_generator(
            brain,
            connector="portal-leads",
            profile=profile,
            crm=crm,
            schedule_id="lead-sync",
            http_client=client,
        )

        assert counts is None
        ((sched, status),) = brain.status
        assert sched == "lead-sync"
        assert status.startswith("error (")
        assert "403" in status
        assert "s3cret" not in status
        assert "portal.example" not in status


class TestPersonaReflect:
    """#15: reflect files under the active profile and survives chatty replies."""

    @pytest.mark.asyncio
    async def test_defaults_to_the_active_profile(self, monkeypatch):
        from unittest.mock import AsyncMock

        monkeypatch.setenv("CLAWRANGE_PROFILE", "marketing")
        monkeypatch.setattr(
            "llm_proxy._llm_call", AsyncMock(return_value="Lead with the answer.")
        )
        await generators.persona_reflect_generator(brain_db)
        assert len(brain_db.list_learnings(profile="marketing")) == 1
        assert brain_db.list_learnings(profile="starter") == []

    @pytest.mark.asyncio
    async def test_multi_line_reply_proposes_first_line(self, monkeypatch):
        from unittest.mock import AsyncMock

        reply = "\n  Lead with the answer.\nThis keeps Telegram replies short.\n"
        monkeypatch.setattr("llm_proxy._llm_call", AsyncMock(return_value=reply))
        await generators.persona_reflect_generator(brain_db, profile_name="cos")
        rows = brain_db.list_learnings(profile="cos")
        assert [r["content"] for r in rows] == ["Lead with the answer."]

    @pytest.mark.asyncio
    async def test_none_on_first_line_queues_nothing(self, monkeypatch):
        from unittest.mock import AsyncMock

        reply = "None.\nNothing stood out this week."
        monkeypatch.setattr("llm_proxy._llm_call", AsyncMock(return_value=reply))
        await generators.persona_reflect_generator(brain_db, profile_name="cos")
        assert brain_db.list_learnings(profile="cos") == []

    @pytest.mark.asyncio
    async def test_invalid_suggestion_is_skipped_not_raised(self, monkeypatch):
        from unittest.mock import AsyncMock

        import persona_learning

        too_long = "x" * (persona_learning.CONTENT_MAX + 1)
        monkeypatch.setattr("llm_proxy._llm_call", AsyncMock(return_value=too_long))
        await generators.persona_reflect_generator(brain_db, profile_name="cos")
        assert brain_db.list_learnings(profile="cos") == []
