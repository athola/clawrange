"""Tests for marketing orchestrator: projects, schedules, scan_cache,
generators, scheduler, and marketing command parsing."""

import json

import pytest

from app import brain_db


# ─── Projects Table ──────────────────────────────────────────────


class TestProjects:
    def test_create_project(self):
        p = brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude-code", "plugins"],
            subreddits=["ClaudeAI", "LocalLLaMA"],
            search_terms=["claude code plugin"],
            posture="Lead with: browse and install plugins",
        )
        assert p["slug"] == "claude-night-market"
        assert p["owner"] == "athola"
        assert json.loads(p["topics"]) == ["claude-code", "plugins"]
        assert json.loads(p["subreddits"]) == ["ClaudeAI", "LocalLLaMA"]

    def test_get_project(self):
        brain_db.upsert_project("skrills", "athola", "skrills")
        p = brain_db.get_project("skrills")
        assert p is not None
        assert p["owner"] == "athola"

    def test_get_missing_project(self):
        assert brain_db.get_project("nonexistent") is None

    def test_list_projects(self):
        brain_db.upsert_project("proj-a", "athola", "proj-a")
        brain_db.upsert_project("proj-b", "athola", "proj-b")
        projects = brain_db.list_projects()
        assert len(projects) == 2
        slugs = {p["slug"] for p in projects}
        assert slugs == {"proj-a", "proj-b"}

    def test_upsert_project_updates_existing(self):
        brain_db.upsert_project("resume", "athola", "simple-resume")
        brain_db.upsert_project(
            "resume",
            "athola",
            "simple-resume",
            topics=["yaml", "pdf"],
        )
        p = brain_db.get_project("resume")
        assert json.loads(p["topics"]) == ["yaml", "pdf"]

    def test_delete_project(self):
        brain_db.upsert_project("to-delete", "athola", "to-delete")
        assert brain_db.delete_project("to-delete")
        assert brain_db.get_project("to-delete") is None

    def test_delete_missing_project(self):
        assert not brain_db.delete_project("nonexistent")


# ─── Schedules Table ─────────────────────────────────────────────


class TestSchedules:
    def test_create_schedule(self):
        s = brain_db.upsert_schedule(
            "morning_scan",
            "Morning marketing scan",
            "morning_scan",
            "0 9 * * *",
        )
        assert s["id"] == "morning_scan"
        assert s["name"] == "Morning marketing scan"
        assert s["kind"] == "morning_scan"
        assert s["cron"] == "0 9 * * *"

    def test_get_schedule_by_id(self):
        brain_db.upsert_schedule(
            "weekly", "Weekly traffic", "weekly_traffic", "0 8 * * 1"
        )
        s = brain_db.get_schedule("weekly")
        assert s is not None
        assert s["kind"] == "weekly_traffic"

    def test_get_schedule_by_name(self):
        brain_db.upsert_schedule("abc123", "My Schedule", "custom_scan", "*/30 * * * *")
        s = brain_db.get_schedule("My Schedule")
        assert s is not None
        assert s["id"] == "abc123"

    def test_list_schedules(self):
        brain_db.upsert_schedule("s1", "Schedule 1", "morning_scan", "0 9 * * *")
        brain_db.upsert_schedule("s2", "Schedule 2", "weekly_traffic", "0 8 * * 1")
        scheds = brain_db.list_schedules()
        assert len(scheds) == 2

    def test_pause_resume_schedule(self):
        brain_db.upsert_schedule("test-sched", "Test", "morning_scan", "0 9 * * *")
        brain_db.set_schedule_paused("test-sched", True)
        s = brain_db.get_schedule("test-sched")
        assert s["paused"] == 1

        brain_db.set_schedule_paused("test-sched", False)
        s = brain_db.get_schedule("test-sched")
        assert s["paused"] == 0

    def test_update_schedule_status(self):
        brain_db.upsert_schedule(
            "status-test", "Status Test", "morning_scan", "0 9 * * *"
        )
        brain_db.update_schedule_status("status-test", "2026-05-03T09:00:00Z", "ok")
        s = brain_db.get_schedule("status-test")
        assert s["last_run"] == "2026-05-03T09:00:00Z"
        assert s["last_status"] == "ok"

    def test_delete_schedule(self):
        brain_db.upsert_schedule("to-rm", "Remove Me", "custom_scan", "0 * * * *")
        assert brain_db.delete_schedule("to-rm")
        assert brain_db.get_schedule("to-rm") is None

    def test_upsert_schedule_updates(self):
        brain_db.upsert_schedule("upsert-test", "Original", "morning_scan", "0 9 * * *")
        brain_db.upsert_schedule(
            "upsert-test", "Updated Name", "custom_scan", "*/30 * * * *"
        )
        s = brain_db.get_schedule("upsert-test")
        assert s["name"] == "Updated Name"
        assert s["kind"] == "custom_scan"


# ─── Scan Cache ──────────────────────────────────────────────────


class TestScanCache:
    def test_mark_and_check_seen(self):
        brain_db.mark_seen("reddit_post", "abc123", "claude-night-market")
        assert brain_db.is_seen("reddit_post", "abc123", "claude-night-market")
        assert not brain_db.is_seen("reddit_post", "abc123", "different-project")
        assert not brain_db.is_seen("reddit_post", "xyz789", "claude-night-market")

    def test_get_unseen(self):
        brain_db.mark_seen("reddit_post", "seen1", "proj-a")
        brain_db.mark_seen("reddit_post", "seen2", "proj-a")
        unseen = brain_db.get_unseen(
            "reddit_post", ["seen1", "seen2", "unseen1"], "proj-a"
        )
        assert unseen == ["unseen1"]

    def test_get_unseen_empty(self):
        assert brain_db.get_unseen("reddit_post", [], "proj-a") == []

    def test_mark_seen_idempotent(self):
        brain_db.mark_seen("github_repo", "repo1", None)
        brain_db.mark_seen("github_repo", "repo1", None)
        assert brain_db.is_seen("github_repo", "repo1", None)


# ─── Subreddit Stats ─────────────────────────────────────────────


class TestSubredditStats:
    """Track per-(sub, project) impressions and hits so the operator
    can see which subreddits actually pay off and so the system can
    auto-promote emerging subs that pass a sustained-yield bar."""

    def test_record_search_increments_impressions(self):
        brain_db.record_subreddit_search("ClaudeAI", "claude-night-market", True)
        brain_db.record_subreddit_search("ClaudeAI", "claude-night-market", True)
        rows = brain_db.list_subreddit_stats(project_slug="claude-night-market")
        assert len(rows) == 1
        assert rows[0]["subreddit"] == "claudeai"
        assert rows[0]["impressions"] == 2
        assert rows[0]["hits"] == 0
        assert rows[0]["is_curated"] == 1

    def test_record_hit_increments_hits_and_sets_first_hit(self):
        brain_db.record_subreddit_hit("Construction", "skrills")
        brain_db.record_subreddit_hit("Construction", "skrills")
        rows = brain_db.list_subreddit_stats(project_slug="skrills")
        assert len(rows) == 1
        row = rows[0]
        assert row["hits"] == 2
        assert row["first_hit_at"] is not None
        assert row["last_hit_at"] is not None

    def test_record_search_then_hit_aggregates(self):
        brain_db.record_subreddit_search("ClaudeAI", "clawrange", True)
        brain_db.record_subreddit_hit("ClaudeAI", "clawrange")
        rows = brain_db.list_subreddit_stats(project_slug="clawrange")
        assert rows[0]["impressions"] == 1
        assert rows[0]["hits"] == 1

    def test_curated_filter(self):
        brain_db.record_subreddit_search("ClaudeAI", "p", True)
        brain_db.record_subreddit_search("emerging-sub", "p", False)
        all_ = brain_db.list_subreddit_stats(project_slug="p")
        curated = brain_db.list_subreddit_stats(project_slug="p", curated_only=True)
        emerging = brain_db.list_subreddit_stats(project_slug="p", curated_only=False)
        assert len(all_) == 2
        assert len(curated) == 1 and curated[0]["subreddit"] == "claudeai"
        assert len(emerging) == 1 and emerging[0]["subreddit"] == "emerging-sub"

    def test_find_promotion_candidates_threshold(self):
        """A non-curated sub with >= 5 hits and a first->last hit
        gap <= 14 days is promotable."""
        for _ in range(5):
            brain_db.record_subreddit_hit("AI_Agents", "clawrange")
        candidates = brain_db.find_promotion_candidates(
            "clawrange", min_hits=5, window_days=14
        )
        assert len(candidates) == 1
        assert candidates[0]["subreddit"] == "ai_agents"

    def test_find_promotion_skips_under_threshold(self):
        for _ in range(4):
            brain_db.record_subreddit_hit("LangChain", "clawrange")
        candidates = brain_db.find_promotion_candidates(
            "clawrange", min_hits=5, window_days=14
        )
        assert candidates == []

    def test_find_promotion_skips_curated(self):
        for _ in range(5):
            brain_db.record_subreddit_search("ClaudeAI", "clawrange", True)
            brain_db.record_subreddit_hit("ClaudeAI", "clawrange")
        candidates = brain_db.find_promotion_candidates("clawrange")
        assert candidates == []

    def test_mark_promoted_excludes_from_future_candidates(self):
        for _ in range(5):
            brain_db.record_subreddit_hit("AI_Agents", "clawrange")
        brain_db.mark_subreddit_promoted("AI_Agents", "clawrange")
        candidates = brain_db.find_promotion_candidates("clawrange")
        assert candidates == []

    def test_add_project_subreddit_idempotent(self):
        brain_db.upsert_project(
            "skrills", "athola", "skrills", subreddits=["Construction"]
        )
        assert brain_db.add_project_subreddit("skrills", "AI_Agents") is True
        # Already present, case-insensitive
        assert brain_db.add_project_subreddit("skrills", "ai_agents") is False
        proj = brain_db.get_project("skrills")
        assert proj is not None
        subs = json.loads(proj["subreddits"])
        assert "Construction" in subs
        assert "AI_Agents" in subs


# ─── Generators ──────────────────────────────────────────────────


class TestGenerators:
    @pytest.mark.asyncio
    async def test_morning_scan_generator(self):
        brain_db.upsert_project(
            "test-proj",
            "athola",
            "test-proj",
            topics=["test"],
            subreddits=["test"],
            search_terms=["test"],
        )
        from generators import morning_scan_generator

        await morning_scan_generator(brain_db)

        tasks = brain_db.list_tasks(status="pending")
        assert len(tasks) == 2
        descs = {t["description"] for t in tasks}
        assert any("Scan reddit" in d for d in descs)
        assert any("Scan github" in d for d in descs)

    @pytest.mark.asyncio
    async def test_morning_scan_filtered(self):
        brain_db.upsert_project("proj-1", "athola", "proj-1")
        brain_db.upsert_project("proj-2", "athola", "proj-2")
        from generators import morning_scan_generator

        await morning_scan_generator(brain_db, project_slugs=["proj-1"])

        tasks = brain_db.list_tasks(status="pending")
        assert len(tasks) == 2
        assert all("proj-1" in t["description"] for t in tasks)

    @pytest.mark.asyncio
    async def test_weekly_traffic_generator(self):
        brain_db.upsert_project("traffic-proj", "athola", "traffic-proj")
        from generators import weekly_traffic_generator

        await weekly_traffic_generator(brain_db)

        tasks = brain_db.list_tasks(status="pending")
        assert len(tasks) == 1
        assert "traffic snapshot" in tasks[0]["description"].lower()

    @pytest.mark.asyncio
    async def test_custom_scan_generator(self):
        from generators import custom_scan_generator

        await custom_scan_generator(brain_db, topic="test search query")

        tasks = brain_db.list_tasks(status="pending")
        assert len(tasks) == 1
        assert "test search query" in tasks[0]["description"]

    def test_generators_registry(self):
        from generators import GENERATORS

        assert "morning_scan" in GENERATORS
        assert "weekly_traffic" in GENERATORS
        assert "awesome_lists_watch" in GENERATORS
        assert "custom_scan" in GENERATORS
        assert "content_idea" in GENERATORS

    @pytest.mark.asyncio
    async def test_content_idea_generator_uses_recent_research(self):
        """
        GIVEN a recent research session in the brain
        WHEN content_idea_generator runs
        THEN it enqueues at least one task per project that
             references the session topic and at least one finding URL.
        """
        from generators import content_idea_generator

        brain_db.upsert_project(
            "skrills",
            "athola",
            "skrills",
            topics=["chrome-extension", "trade-skills"],
            posture="Lead with: trade skill capture",
        )
        session = brain_db.create_research_session(
            "trade skills chrome extensions", ["discourse", "code"]
        )
        brain_db.add_research_finding(
            session_id=session["id"],
            source="reddit",
            channel="discourse",
            title="What chrome extensions do trades use?",
            url="https://reddit.com/r/Construction/post/123",
            relevance=0.7,
            summary="Discussion about chrome extensions for site supervisors",
            metadata={"score": 80},
        )
        brain_db.complete_research_session(session["id"])

        await content_idea_generator(brain_db, project_slugs=["skrills"])

        tasks = brain_db.list_tasks(status="pending")
        assert len(tasks) >= 1
        idea_tasks = [t for t in tasks if "content idea" in t["description"].lower()]
        assert len(idea_tasks) >= 1
        # At least one task should reference the actual finding URL.
        assert any("reddit.com/r/Construction" in t["description"] for t in idea_tasks)

    @pytest.mark.asyncio
    async def test_content_idea_generator_skips_when_no_research(self):
        """No recent sessions -> no tasks emitted."""
        from generators import content_idea_generator

        brain_db.upsert_project("empty-proj", "athola", "empty-proj")
        await content_idea_generator(brain_db, project_slugs=["empty-proj"])

        tasks = brain_db.list_tasks(status="pending")
        # No findings to base ideas on; generator emits nothing.
        assert tasks == []

    @pytest.mark.asyncio
    async def test_comment_draft_generator_emits_review_task(self):
        """
        GIVEN a Reddit URL and a project slug
        WHEN comment_draft_generator runs
        THEN it enqueues a single comment-draft task tagged for
             human review and never enqueues a 'post' task.
        """
        from generators import comment_draft_generator

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude-code", "plugins"],
            posture="Lead with: a curated marketplace for Claude Code plugins.",
        )

        await comment_draft_generator(
            brain_db,
            post_url="https://www.reddit.com/r/ClaudeAI/comments/abc123/",
            post_summary="OP wants to know how to share custom skills.",
            project_slug="claude-night-market",
        )

        tasks = brain_db.list_tasks(status="pending")
        assert len(tasks) == 1
        t = tasks[0]
        # Always pending, never auto-sent
        assert t["status"] == "pending"
        # Marked as a draft for review
        assert (
            "comment draft" in t["description"].lower()
            or "[draft]" in t["description"].lower()
        )
        # Carries the source URL and project context
        assert "reddit.com/r/ClaudeAI" in t["description"]
        assert "claude-night-market" in t["description"]

    @pytest.mark.asyncio
    async def test_comment_draft_generator_requires_url(self):
        from generators import comment_draft_generator

        # Missing url -> no task created, no exception
        brain_db.upsert_project("p", "athola", "p")
        await comment_draft_generator(brain_db, post_url="", project_slug="p")
        assert brain_db.list_tasks(status="pending") == []

    @pytest.mark.asyncio
    async def test_comment_draft_generator_handles_unknown_project(self):
        """Unknown project slug -> still emit a task; posture defaults."""
        from generators import comment_draft_generator

        await comment_draft_generator(
            brain_db,
            post_url="https://news.ycombinator.com/item?id=12345",
            project_slug="not-a-real-project",
        )
        tasks = brain_db.list_tasks(status="pending")
        assert len(tasks) == 1
        assert "12345" in tasks[0]["description"]

    def test_comment_draft_in_registry(self):
        from generators import GENERATORS

        assert "comment_draft" in GENERATORS

    def test_seed_default_schedules_creates_daily_content_idea(self):
        """
        GIVEN seed_default_schedules has been called
        WHEN we list schedules
        THEN at minimum the daily-content-idea schedule exists with a
             cron string that fires at a morning hour.
        """
        from generators import seed_default_schedules

        seed_default_schedules(brain_db)
        scheds = {s["id"]: s for s in brain_db.list_schedules()}
        assert "daily-content-idea" in scheds
        sched = scheds["daily-content-idea"]
        assert sched["kind"] == "content_idea"
        assert sched["cron"].split()[1] in {"7", "8", "9", "10"}

    def test_seed_default_schedules_idempotent(self):
        """Calling twice does not duplicate, and preserves user edits."""
        from generators import seed_default_schedules

        seed_default_schedules(brain_db)
        first = brain_db.list_schedules()
        sched = brain_db.get_schedule("daily-content-idea")
        brain_db.upsert_schedule(
            sched["id"],
            sched["name"],
            sched["kind"],
            "*/30 * * * *",
        )
        seed_default_schedules(brain_db)
        second = brain_db.list_schedules()
        assert len(first) == len(second)
        edited = brain_db.get_schedule("daily-content-idea")
        assert edited["cron"] == "*/30 * * * *"

    def test_personal_brand_project_seed_exists(self):
        """
        GIVEN seed_default_projects has been called
        WHEN we look up 'personal-brand'
        THEN it exists with athola owner and AI-systems posture.
        """
        from generators import seed_default_projects

        seed_default_projects(brain_db)
        pb = brain_db.get_project("personal-brand")
        assert pb is not None
        assert pb["owner"] == "athola"
        topics = json.loads(pb["topics"])
        # Should at least mention AI-systems / agents / plugins
        joined = " ".join(topics).lower()
        assert "agent" in joined or "ai" in joined or "plugin" in joined

    def test_clawrange_project_seed_exists(self):
        """
        GIVEN seed_default_projects has been called
        WHEN we look up 'clawrange'
        THEN athola/clawrange exists with subreddits covering the
             AI-coding communities (vibecoding/claudecode/codex).
        """
        from generators import seed_default_projects

        seed_default_projects(brain_db)
        cr = brain_db.get_project("clawrange")
        assert cr is not None
        assert cr["owner"] == "athola"
        assert cr["repo"] == "clawrange"
        subs = {s.lower() for s in json.loads(cr["subreddits"])}
        # At least one of the AI-coding communities the user listed
        assert subs & {
            "vibecoding",
            "opensourceai",
            "claudecode",
            "claudeai",
            "codex",
            "sideprojects",
        }


# ─── Morning Digest Generator ────────────────────────────────────


class TestMorningDigestGenerator:
    """The 8am morning_digest_generator delivers a Telegram rundown
    of comment-worthy Reddit posts in the last 24h, scoped to
    tracked projects, deduplicated against scan_cache."""

    @pytest.mark.asyncio
    async def test_morning_digest_in_registry(self):
        from generators import GENERATORS

        assert "morning_digest" in GENERATORS

    @pytest.mark.asyncio
    async def test_morning_digest_skips_when_no_projects(self, monkeypatch):
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator

        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        # No projects in DB
        await morning_digest_generator(brain_db)
        notify_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_morning_digest_calls_search_with_24h_window(self, monkeypatch):
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude-code", "plugins"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )

        search_mock = AsyncMock(return_value=[])
        monkeypatch.setattr("reddit_search.search_subreddits", search_mock)
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))

        await morning_digest_generator(brain_db)

        # Every call must specify a 24h window
        assert search_mock.await_count >= 1
        for call in search_mock.await_args_list:
            kwargs = call.kwargs
            assert kwargs.get("since") == "24h"

    @pytest.mark.asyncio
    async def test_morning_digest_groups_posts_by_project(self, monkeypatch):
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin", "marketplace"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )
        brain_db.upsert_project(
            "skrills",
            "athola",
            "skrills",
            topics=["trade skill capture", "chrome extension"],
            subreddits=["Construction"],
            search_terms=["trade skill capture"],
        )

        cnm_post = RedditPost(
            id="cnm1",
            url="https://reddit.com/r/ClaudeAI/comments/cnm1",
            title="Best claude code plugin marketplace?",
            subreddit="ClaudeAI",
            score=42,
            comments=10,
            created_utc="2026-05-09T07:00:00+00:00",
        )
        skr_post = RedditPost(
            id="skr1",
            url="https://reddit.com/r/Construction/comments/skr1",
            title="Chrome extension for trade skill capture",
            subreddit="Construction",
            score=15,
            comments=4,
            created_utc="2026-05-09T07:30:00+00:00",
        )

        async def fake_search(topic, subs, **kw):
            t = topic.lower()
            if "plugin" in t or "marketplace" in t:
                return [cnm_post]
            if "trade" in t or "chrome" in t:
                return [skr_post]
            return []

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await morning_digest_generator(brain_db)

        notify_mock.assert_awaited_once()
        msg = notify_mock.await_args.args[0]
        assert "claude-night-market" in msg
        assert "skrills" in msg
        assert "cnm1" in msg
        assert "skr1" in msg

    @pytest.mark.asyncio
    async def test_morning_digest_dedupes_seen_posts(self, monkeypatch):
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )
        # Already surfaced this post yesterday
        brain_db.mark_seen("reddit_post", "old1", "claude-night-market")

        old = RedditPost(
            id="old1",
            url="https://reddit.com/r/ClaudeAI/comments/old1",
            title="Old claude code plugin post",
            subreddit="ClaudeAI",
            score=99,
            comments=99,
            created_utc="2026-05-09T07:00:00+00:00",
        )

        async def fake_search(*a, **kw):
            return [old]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await morning_digest_generator(brain_db)

        # Already-seen post must not appear in any notify message.
        if notify_mock.await_count:
            for call in notify_mock.await_args_list:
                assert "old1" not in call.args[0]

    @pytest.mark.asyncio
    async def test_morning_digest_marks_seen_no_draft_tasks(self, monkeypatch):
        """The digest marks delivered posts as seen (so tomorrow won't
        repeat them) but no longer queues [DRAFT] comment tasks. Alex
        reads the digest, clicks the direct links, and writes his own
        replies."""
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
            posture="Lead with: a curated marketplace.",
        )
        post = RedditPost(
            id="fresh1",
            url="https://reddit.com/r/ClaudeAI/comments/fresh1",
            title="What claude code plugin do you recommend?",
            subreddit="ClaudeAI",
            score=20,
            comments=5,
            created_utc="2026-05-09T07:00:00+00:00",
        )

        async def fake_search(*a, **kw):
            return [post]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))

        pre = len(brain_db.list_tasks(status="pending"))
        await morning_digest_generator(brain_db)

        assert brain_db.is_seen("reddit_post", "fresh1", "claude-night-market")
        post_tasks = brain_db.list_tasks(status="pending")
        draft_tasks = [t for t in post_tasks if "[DRAFT]" in t["description"].upper()]
        assert draft_tasks == [], (
            f"comment-draft suggestion is removed; got {draft_tasks}"
        )
        assert len(post_tasks) == pre, "no new tasks should be queued"

    @pytest.mark.asyncio
    async def test_morning_digest_renders_relevance_snippet(self, monkeypatch):
        """Each pick must carry a short 'why relevant + comment angle'
        snippet directly in the Telegram message — no separate task,
        no example reply, just enough context to decide whether to
        click through and comment."""
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )
        post = RedditPost(
            id="snippet1",
            url="https://reddit.com/r/ClaudeAI/comments/snippet1/best_plugin",
            title="Best claude code plugin marketplace?",
            subreddit="ClaudeAI",
            score=20,
            comments=5,
            created_utc="2026-05-09T07:00:00+00:00",
        )

        async def fake_search(*a, **kw):
            return [post]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await morning_digest_generator(brain_db)

        notify_mock.assert_awaited_once()
        msg = notify_mock.await_args.args[0]
        # Direct URL appears verbatim so Alex can tap to open.
        assert post.url in msg
        # 'why' line shows the matched keyword and engagement.
        assert "claude code plugin" in msg.lower()
        assert "5 comments" in msg or "5 comment" in msg or "5 pts" in msg

    @pytest.mark.asyncio
    async def test_morning_digest_includes_user_extra_subreddits(self, monkeypatch):
        """The scan set must include the AI-coding subreddits the user
        explicitly listed (vibecoding, opensourceai, claudecode, etc.)
        even when no project subscribes to them directly."""
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator

        brain_db.upsert_project(
            "clawrange",
            "athola",
            "clawrange",
            topics=["personal ai ops"],
            subreddits=["selfhosted"],  # deliberately narrow
            search_terms=["personal ai ops"],
        )

        search_mock = AsyncMock(return_value=[])
        monkeypatch.setattr("reddit_search.search_subreddits", search_mock)
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))

        await morning_digest_generator(brain_db)

        all_subs_seen: set[str] = set()
        for call in search_mock.await_args_list:
            subs_arg = (
                call.args[1]
                if len(call.args) > 1
                else call.kwargs.get("subreddits", [])
            )
            all_subs_seen.update(s.lower() for s in subs_arg)
        for required in ("vibecoding", "claudecode", "claudeai"):
            assert required in all_subs_seen, (
                f"expected {required} in scan set, got {all_subs_seen}"
            )

    @pytest.mark.asyncio
    async def test_morning_digest_popular_bonus_tier(self, monkeypatch):
        """Subreddit-affinity bonus tier: a project subscribing to a
        sub gets up to popular_bonus_cap picks from that sub even
        when no literal keyword match, provided the score clears the
        adaptive threshold (popularity_multiplier × subreddit median).

        Regression for the case where skrills (subs: Construction,
        ITCareerQuestions, SideProject) was returning zero picks
        because its terms ('trade skill capture') rarely appear
        verbatim in those subs."""
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "skrills",
            "athola",
            "skrills",
            topics=["trade skill capture"],
            subreddits=["Construction"],
            search_terms=["trade skill capture"],
            posture="Lead with: capture trade-skill knowledge.",
        )

        # Five r/Construction posts with no literal 'trade skill' match.
        # Median score is 5 -> 2x threshold = 10. Posts at 200 and 150
        # qualify; posts at 1, 5, 10 do not (10 ties median*2 = 10,
        # so it qualifies; we'll keep 1 and 5 below threshold).
        sub_posts = [
            RedditPost(
                id=f"con{i}",
                url=f"https://reddit.com/r/Construction/comments/con{i}",
                title=f"Random construction post {i}",
                subreddit="Construction",
                score=score,
                comments=2,
                created_utc="2026-05-09T07:00:00+00:00",
            )
            for i, score in enumerate([1, 5, 10, 150, 200], start=1)
        ]

        async def fake_search(*a, **kw):
            return sub_posts

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await morning_digest_generator(
            brain_db,
            popularity_multiplier=2.0,
            popular_bonus_cap=2,
        )

        notify_mock.assert_awaited_once()
        digest = notify_mock.await_args.args[0]

        # The two highest-scoring posts must appear (subreddit-affinity
        # bonus tier kicked in) even with rel == 0.
        assert "con5" in digest, "score=200 post must be in digest"
        assert "con4" in digest, "score=150 post must be in digest"
        # Sub-threshold posts must NOT be picked as bonus.
        assert "con1" not in digest, "score=1 post must NOT be picked"
        assert "con2" not in digest, "score=5 post must NOT be picked"
        # Bonus picks rendered with the ★ marker.
        assert "★" in digest, "bonus picks must be marked with ★"

    @pytest.mark.asyncio
    async def test_morning_digest_records_search_impressions(self, monkeypatch):
        """Each (sub, project) pair gets at least one impression
        recorded per cycle, so the stats table has a denominator."""
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )

        monkeypatch.setattr(
            "reddit_search.search_subreddits", AsyncMock(return_value=[])
        )
        monkeypatch.setattr("reddit_search.search_all", AsyncMock(return_value=[]))
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))

        await morning_digest_generator(brain_db)

        rows = brain_db.list_subreddit_stats(project_slug="claude-night-market")
        subs = {r["subreddit"] for r in rows}
        assert "claudeai" in subs, "ClaudeAI must have an impression recorded"
        # User-extras subs should also be searched even though not curated
        assert "vibecoding" in subs

    @pytest.mark.asyncio
    async def test_morning_digest_records_hit_for_surfaced_post(self, monkeypatch):
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )
        post = RedditPost(
            id="hit1",
            url="https://reddit.com/r/ClaudeAI/comments/hit1",
            title="claude code plugin question",
            subreddit="ClaudeAI",
            score=10,
            comments=2,
            created_utc="2026-05-09T07:00:00+00:00",
        )

        async def fake_search(*a, **kw):
            return [post]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        monkeypatch.setattr("reddit_search.search_all", AsyncMock(return_value=[]))
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))

        await morning_digest_generator(brain_db)

        rows = brain_db.list_subreddit_stats(project_slug="claude-night-market")
        cn = next(r for r in rows if r["subreddit"] == "claudeai")
        assert cn["hits"] >= 1

    @pytest.mark.asyncio
    async def test_morning_digest_discovers_emerging_sub(self, monkeypatch):
        """search_all returns a post in a non-curated sub matching
        project terms — the digest must surface it under '🆕 Emerging
        subs' AND record a stats hit so the auto-promote path can
        eventually fire."""
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "clawrange",
            "athola",
            "clawrange",
            topics=["agent orchestration"],
            subreddits=["ClaudeAI"],
            search_terms=["agent orchestration"],
        )
        emerging_post = RedditPost(
            id="em1",
            url="https://reddit.com/r/AI_Agents/comments/em1",
            title="multi-agent orchestration patterns?",
            subreddit="AI_Agents",
            score=12,
            comments=3,
            created_utc="2026-05-09T07:00:00+00:00",
        )

        monkeypatch.setattr(
            "reddit_search.search_subreddits", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(
            "reddit_search.search_all",
            AsyncMock(return_value=[emerging_post]),
        )
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await morning_digest_generator(brain_db)

        notify_mock.assert_awaited_once()
        msg = notify_mock.await_args.args[0]
        assert "🆕 Emerging subs" in msg
        assert "AI_Agents" in msg or "ai_agents" in msg.lower()
        # Hit recorded for the emerging sub
        rows = brain_db.list_subreddit_stats(project_slug="clawrange")
        ai = next((r for r in rows if r["subreddit"] == "ai_agents"), None)
        assert ai is not None and ai["hits"] >= 1

    @pytest.mark.asyncio
    async def test_morning_digest_auto_promotes_emerging_sub(self, monkeypatch):
        """A non-curated sub with ≥5 hits in 14 days gets folded into
        the project's subreddits list and the digest report mentions
        it as newly promoted."""
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator

        brain_db.upsert_project(
            "clawrange",
            "athola",
            "clawrange",
            topics=["agent orchestration"],
            subreddits=["ClaudeAI"],
            search_terms=["agent orchestration"],
        )
        # Pre-seed stats: AI_Agents has crossed the threshold
        for _ in range(5):
            brain_db.record_subreddit_hit("AI_Agents", "clawrange")

        monkeypatch.setattr(
            "reddit_search.search_subreddits", AsyncMock(return_value=[])
        )
        monkeypatch.setattr("reddit_search.search_all", AsyncMock(return_value=[]))
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await morning_digest_generator(brain_db)

        notify_mock.assert_awaited_once()
        msg = notify_mock.await_args.args[0]
        assert "Newly promoted" in msg
        assert "AI_Agents" in msg or "ai_agents" in msg
        # Project's subreddits list now includes AI_Agents
        proj = brain_db.get_project("clawrange")
        assert proj is not None
        subs = json.loads(proj["subreddits"])
        assert any(s.lower() == "ai_agents" for s in subs)

    @pytest.mark.asyncio
    async def test_morning_digest_includes_subreddit_report(self, monkeypatch):
        """Even when no picks land, the digest renders a one-paragraph
        coverage report listing tracked subs and confirming no new
        promotions this cycle. Operator wants visibility into the
        scan footprint."""
        from unittest.mock import AsyncMock

        from generators import morning_digest_generator

        brain_db.upsert_project(
            "clawrange",
            "athola",
            "clawrange",
            topics=["x"],
            subreddits=["ClaudeAI", "selfhosted"],
            search_terms=["x"],
        )
        # Force at least one promoted sub by pre-seeding stats
        for _ in range(5):
            brain_db.record_subreddit_hit("AI_Agents", "clawrange")

        monkeypatch.setattr(
            "reddit_search.search_subreddits", AsyncMock(return_value=[])
        )
        monkeypatch.setattr("reddit_search.search_all", AsyncMock(return_value=[]))
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await morning_digest_generator(brain_db)

        notify_mock.assert_awaited_once()
        msg = notify_mock.await_args.args[0]
        assert "Subreddit coverage report" in msg
        assert "Tracking" in msg

    def test_morning_digest_schedule_registered_after_seed(self):
        """seed_default_projects must register a 0 8 * * * schedule
        for the morning_digest generator (idempotently)."""
        from generators import seed_default_projects

        seed_default_projects(brain_db)
        # Calling twice must not duplicate
        seed_default_projects(brain_db)

        sched = brain_db.get_schedule("morning_digest")
        assert sched is not None
        assert sched["kind"] == "morning_digest"
        assert sched["cron"].strip() == "0 8 * * *"


# ─── Hot Pulse Generator (5-min) ─────────────────────────────────


class TestHotPulseGenerator:
    """The 5-min hot_pulse_generator surfaces brand-new Reddit posts
    relevant to tracked projects, deduplicating only against its own
    pulse history (independent of the 24h digest's dedup namespace)."""

    @pytest.mark.asyncio
    async def test_hot_pulse_in_registry(self):
        from generators import GENERATORS

        assert "hot_pulse" in GENERATORS

    @pytest.mark.asyncio
    async def test_hot_pulse_calls_search_with_default_window(self, monkeypatch):
        """Default window is 15min lookback — the cron still fires
        every 5min but each fire scans a wider net to catch sparse
        Reddit activity without missing anything between fires."""
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )

        search_mock = AsyncMock(return_value=[])
        monkeypatch.setattr("reddit_search.search_subreddits", search_mock)
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))

        await hot_pulse_generator(brain_db)

        assert search_mock.await_count >= 1
        for call in search_mock.await_args_list:
            assert call.kwargs.get("since") == "15m"

    @pytest.mark.asyncio
    async def test_hot_pulse_window_is_overridable(self, monkeypatch):
        """An explicit `window` kwarg overrides the default."""
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )

        search_mock = AsyncMock(return_value=[])
        monkeypatch.setattr("reddit_search.search_subreddits", search_mock)
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))

        await hot_pulse_generator(brain_db, window="5m")

        for call in search_mock.await_args_list:
            assert call.kwargs.get("since") == "5m"

    @pytest.mark.asyncio
    async def test_hot_pulse_renders_link_and_relevance(self, monkeypatch):
        """The pulse message must include the post's direct URL and a
        short 'why relevant' snippet — no example comment text."""
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )
        post = RedditPost(
            id="hot1",
            url="https://reddit.com/r/ClaudeAI/comments/hot1/just_posted",
            title="Anyone using a claude code plugin?",
            subreddit="ClaudeAI",
            score=2,
            comments=0,
            created_utc="2026-05-09T17:55:00+00:00",
        )

        async def fake_search(*a, **kw):
            return [post]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await hot_pulse_generator(brain_db)

        notify_mock.assert_awaited_once()
        msg = notify_mock.await_args.args[0]
        assert post.url in msg, "direct Reddit link required"
        assert "claude code plugin" in msg.lower(), "relevance snippet required"
        # Comment-draft text MUST NOT appear.
        assert "draft" not in msg.lower()
        assert "3-5 sentence" not in msg.lower()
        assert "write a" not in msg.lower()

    @pytest.mark.asyncio
    async def test_hot_pulse_dedups_within_pulse_namespace(self, monkeypatch):
        """Re-running the pulse with the same post must NOT re-deliver
        it — dedup is via scan_cache kind='reddit_pulse'."""
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )
        post = RedditPost(
            id="dedup1",
            url="https://reddit.com/r/ClaudeAI/comments/dedup1",
            title="claude code plugin question",
            subreddit="ClaudeAI",
            score=1,
            comments=0,
            created_utc="2026-05-09T17:55:00+00:00",
        )

        async def fake_search(*a, **kw):
            return [post]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        # First fire delivers
        await hot_pulse_generator(brain_db)
        assert notify_mock.await_count == 1

        # Second fire same post -> no new delivery
        await hot_pulse_generator(brain_db)
        assert notify_mock.await_count == 1, "duplicate pulse must be suppressed"

    @pytest.mark.asyncio
    async def test_hot_pulse_dedup_independent_of_morning_digest(self, monkeypatch):
        """A post the morning_digest already marked as seen
        (kind='reddit_post') must STILL surface in hot_pulse — Alex
        explicitly wants the digest to potentially repeat pulse picks
        in case he missed them."""
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )
        # Pre-mark as seen by morning_digest namespace.
        brain_db.mark_seen("reddit_post", "indep1", "claude-night-market")

        post = RedditPost(
            id="indep1",
            url="https://reddit.com/r/ClaudeAI/comments/indep1",
            title="claude code plugin question",
            subreddit="ClaudeAI",
            score=1,
            comments=0,
            created_utc="2026-05-09T17:55:00+00:00",
        )

        async def fake_search(*a, **kw):
            return [post]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await hot_pulse_generator(brain_db)

        notify_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hot_pulse_does_not_truncate_relevant_posts(self, monkeypatch):
        """All relevant posts in the 5-min window must appear — no
        truncation to a small fixed cap. Operator complained that
        a single match was the only thing surfaced when more were
        expected; 5-min windows are tight, but when matches exist
        every one should land in the message."""
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )

        # Five distinct relevant posts in the 5-min window
        posts = [
            RedditPost(
                id=f"many{i}",
                url=f"https://reddit.com/r/ClaudeAI/comments/many{i}",
                title=f"Question about claude code plugin #{i}",
                subreddit="ClaudeAI",
                score=i,
                comments=0,
                created_utc="2026-05-09T17:55:00+00:00",
            )
            for i in range(1, 6)
        ]

        async def fake_search(*a, **kw):
            return posts

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await hot_pulse_generator(brain_db)

        msg = notify_mock.await_args.args[0]
        for i in range(1, 6):
            assert f"many{i}" in msg, f"post many{i} should appear, got:\n{msg}"

    @pytest.mark.asyncio
    async def test_hot_pulse_does_not_queue_draft_tasks(self, monkeypatch):
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "claude-night-market",
            "athola",
            "claude-night-market",
            topics=["claude code plugin"],
            subreddits=["ClaudeAI"],
            search_terms=["claude code plugin"],
        )
        post = RedditPost(
            id="nodraft1",
            url="https://reddit.com/r/ClaudeAI/comments/nodraft1",
            title="claude code plugin question",
            subreddit="ClaudeAI",
            score=1,
            comments=0,
            created_utc="2026-05-09T17:55:00+00:00",
        )

        async def fake_search(*a, **kw):
            return [post]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))

        pre = len(brain_db.list_tasks(status="pending"))
        await hot_pulse_generator(brain_db)
        post_tasks = brain_db.list_tasks(status="pending")
        assert len(post_tasks) == pre

    @pytest.mark.asyncio
    async def test_hot_pulse_surfaces_semantic_match_with_baseline(self, monkeypatch):
        """A post returned by Reddit's search but lacking the literal
        project term in title/snippet must still surface — Reddit's
        upstream relevance is trusted with a baseline rel=0.5. The
        post should render with the ◇ semantic-tier marker."""
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "clawrange",
            "athola",
            "clawrange",
            topics=["agent orchestration"],
            subreddits=["ClaudeAI"],
            search_terms=["agent orchestration"],
        )
        # Title intentionally avoids "agent orchestration" verbatim.
        # Reddit returned it for our query — we trust that.
        semantic_post = RedditPost(
            id="sem1",
            url="https://reddit.com/r/selfhosted/comments/sem1",
            title="Crate Beta — runtime for distributed AI workers",
            subreddit="selfhosted",
            score=2,
            comments=0,
            created_utc="2026-05-09T19:15:00+00:00",
            snippet="A new runtime for orchestrating ...",
        )

        async def fake_search(*a, **kw):
            return [semantic_post]

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await hot_pulse_generator(brain_db)

        notify_mock.assert_awaited_once()
        msg = notify_mock.await_args.args[0]
        assert "sem1" in msg, (
            "semantic-only match must surface (was being filtered out)"
        )
        # Tier marker for semantic match
        assert "◇" in msg

    @pytest.mark.asyncio
    async def test_hot_pulse_renders_category_buckets(self, monkeypatch):
        """Picks group by which search query brought them, so the
        operator can see which category is currently active for a
        project. Category line appears above the picks for that bucket."""
        from unittest.mock import AsyncMock

        from generators import hot_pulse_generator
        from reddit_search import RedditPost

        brain_db.upsert_project(
            "clawrange",
            "athola",
            "clawrange",
            topics=["claude code workflow"],
            subreddits=["ClaudeCode"],
            search_terms=["claude code workflow", "agent orchestration"],
        )

        cw_post = RedditPost(
            id="cw1",
            url="https://reddit.com/r/ClaudeCode/comments/cw1",
            title="My claude code workflow tips",
            subreddit="ClaudeCode",
            score=5,
            comments=2,
            created_utc="2026-05-09T19:15:00+00:00",
        )
        ao_post = RedditPost(
            id="ao1",
            url="https://reddit.com/r/selfhosted/comments/ao1",
            title="Self-hosted multi-agent runtime",
            subreddit="selfhosted",
            score=3,
            comments=0,
            created_utc="2026-05-09T19:15:00+00:00",
        )

        async def fake_search(query, *a, **kw):
            if "workflow" in query:
                return [cw_post]
            if "orchestration" in query:
                return [ao_post]
            return []

        monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
        notify_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify_mock)

        await hot_pulse_generator(brain_db)

        msg = notify_mock.await_args.args[0]
        # Both category headers appear
        assert 'Category: "claude code workflow"' in msg
        assert 'Category: "agent orchestration"' in msg
        # Both posts present
        assert "cw1" in msg
        assert "ao1" in msg

    def test_hot_pulse_schedule_registered_after_seed(self):
        """seed_default_projects must register a */5 * * * * schedule
        for the hot_pulse generator (idempotently)."""
        from generators import seed_default_projects

        seed_default_projects(brain_db)
        seed_default_projects(brain_db)

        sched = brain_db.get_schedule("hot_pulse")
        assert sched is not None
        assert sched["kind"] == "hot_pulse"
        assert sched["cron"].strip() == "*/5 * * * *"


# ─── Scheduler Module ────────────────────────────────────────────


class TestSchedulerModule:
    def test_parse_cron_standard(self):
        from scheduler import _parse_cron

        result = _parse_cron("0 9 * * *")
        assert result == {
            "minute": "0",
            "hour": "9",
            "day": "*",
            "month": "*",
            "day_of_week": "*",
        }

    def test_parse_cron_duration_hours(self):
        from scheduler import _parse_cron

        result = _parse_cron("every 6h")
        assert result == {"hour": "*/6"}

    def test_parse_cron_duration_minutes(self):
        from scheduler import _parse_cron

        result = _parse_cron("every 30m")
        assert result == {"minute": "*/30"}

    def test_parse_cron_duration_days(self):
        from scheduler import _parse_cron

        result = _parse_cron("every 2d")
        assert result == {"day": "*/2"}

    def test_parse_cron_invalid(self):
        from scheduler import _parse_cron

        with pytest.raises(ValueError):
            _parse_cron("invalid")

    def test_parse_cron_min_clamp(self):
        from scheduler import _parse_cron

        result = _parse_cron("every 1m")
        assert result == {"minute": "*/5"}

    @pytest.mark.asyncio
    async def test_init_scheduler_attaches_jobs_for_db_schedules(self):
        """Regression: init_scheduler must actually attach APScheduler
        jobs for every active schedule in the DB. The previous
        SQLAlchemy jobstore tried to serialise brain_db (which holds a
        live sqlite3.Connection) and silently degraded to unscheduled
        mode, meaning the 8am morning_digest cron never fired."""
        from scheduler import init_scheduler

        brain_db.upsert_schedule(
            "morning_digest",
            "Morning Reddit digest",
            "morning_digest",
            "0 8 * * *",
        )

        scheduler = init_scheduler(brain_db)
        try:
            assert scheduler is not None, (
                "scheduler must initialise with a serialisable jobstore"
            )
            jobs = scheduler.get_jobs()
            job_ids = [j.id for j in jobs]
            assert "marketing_morning_digest" in job_ids, (
                f"expected marketing_morning_digest in {job_ids}"
            )
            morning_job = next(j for j in jobs if j.id == "marketing_morning_digest")
            assert morning_job.next_run_time is not None
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)


# ─── Marketing Command Parsing ───────────────────────────────────


class TestMarketingCommandParsing:
    def test_extract_sched_list(self):
        from llm_proxy import _extract_marketing_command

        cmd = _extract_marketing_command("/sched list")
        assert cmd["verb"] == "sched"
        assert cmd["subcmd"] == "list"

    def test_extract_sched_add(self):
        from llm_proxy import _extract_marketing_command

        cmd = _extract_marketing_command(
            '/sched add myscan cron "0 9 * * *" -- morning_scan'
        )
        assert cmd["verb"] == "sched"
        assert cmd["subcmd"] == "add"

    def test_extract_scan_reddit(self):
        from llm_proxy import _extract_marketing_command

        cmd = _extract_marketing_command(
            "/scan reddit claude code plugins --subs ClaudeAI,LocalLLaMA"
        )
        assert cmd["verb"] == "scan"
        assert cmd["subcmd"] == "reddit"

    def test_extract_scan_github(self):
        from llm_proxy import _extract_marketing_command

        cmd = _extract_marketing_command(
            "/scan github claude code --kind repos --stars 5"
        )
        assert cmd["verb"] == "scan"
        assert cmd["subcmd"] == "github"

    def test_extract_projects_list(self):
        from llm_proxy import _extract_marketing_command

        cmd = _extract_marketing_command("/projects list")
        assert cmd["verb"] == "projects"
        assert cmd["subcmd"] == "list"

    def test_extract_marketing_alias(self):
        from llm_proxy import _extract_marketing_command

        cmd = _extract_marketing_command("/marketing")
        assert cmd["verb"] == "sched"
        assert cmd["subcmd"] == "run"
        assert cmd["args"] == "morning_scan"

    def test_extract_non_marketing(self):
        from llm_proxy import _extract_marketing_command

        assert _extract_marketing_command("!task do something") is None
        assert _extract_marketing_command("hello") is None

    def test_bang_prefix(self):
        from llm_proxy import _extract_marketing_command

        cmd = _extract_marketing_command("!scan web test query")
        assert cmd["verb"] == "scan"

    def test_scan_args_parsing(self):
        from llm_proxy import _parse_scan_args

        topic, subs, proj, since = _parse_scan_args(
            "claude plugins --subs ClaudeAI,LocalLLaMA --project cnm --since 30d"
        )
        assert topic == "claude plugins"
        assert subs == ["ClaudeAI", "LocalLLaMA"]
        assert proj == "cnm"
        assert since == "30d"


# ─── Marketing Command Dispatch ──────────────────────────────────


class TestMarketingCommandDispatch:
    @pytest.mark.asyncio
    async def test_sched_list_empty(self):
        from llm_proxy import _handle_sched_command

        result = await _handle_sched_command("list", "")
        assert "No schedules configured" in result

    @pytest.mark.asyncio
    async def test_sched_list_with_items(self):
        from llm_proxy import _handle_sched_command

        brain_db.upsert_schedule(
            "test-sched", "Test Schedule", "morning_scan", "0 9 * * *"
        )
        result = await _handle_sched_command("list", "")
        assert "Test Schedule" in result
        assert "ACTIVE" in result

    @pytest.mark.asyncio
    async def test_sched_show(self):
        from llm_proxy import _handle_sched_command

        brain_db.upsert_schedule("show-test", "Showable", "custom_scan", "*/30 * * * *")
        result = await _handle_sched_command("show", "show-test")
        assert "Showable" in result
        assert "custom_scan" in result

    @pytest.mark.asyncio
    async def test_sched_show_missing(self):
        from llm_proxy import _handle_sched_command

        result = await _handle_sched_command("show", "nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_projects_list_empty(self):
        from llm_proxy import _handle_projects_command

        result = await _handle_projects_command("list", "")
        assert "No projects tracked" in result

    @pytest.mark.asyncio
    async def test_projects_add_and_show(self):
        from llm_proxy import _handle_projects_command

        result = await _handle_projects_command(
            "add", "test-proj athola/test-proj --topics yaml,pdf --subs Python"
        )
        assert "added" in result

        result = await _handle_projects_command("show", "test-proj")
        assert "athola/test-proj" in result

    @pytest.mark.asyncio
    async def test_projects_rm(self):
        from llm_proxy import _handle_projects_command

        brain_db.upsert_project("rm-test", "athola", "rm-test")
        result = await _handle_projects_command("rm", "rm-test")
        assert "Removed" in result

    @pytest.mark.asyncio
    async def test_sched_help(self):
        from llm_proxy import _handle_sched_command

        result = await _handle_sched_command("unknown", "")
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_scan_help(self):
        from llm_proxy import _handle_scan_command

        result = await _handle_scan_command("unknown", "")
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_projects_help(self):
        from llm_proxy import _handle_projects_command

        result = await _handle_projects_command("unknown", "")
        assert "Usage" in result


# ─── API Endpoints ───────────────────────────────────────────────


class TestMarketingAPIEndpoints:
    def test_projects_crud(self, client):
        # Create
        resp = client.post(
            "/projects",
            json={
                "slug": "api-test",
                "owner": "athola",
                "repo": "api-test",
                "topics": ["test"],
                "subreddits": ["ClaudeAI"],
                "search_terms": ["test query"],
                "posture": "Test posture",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["slug"] == "api-test"

        # List
        resp = client.get("/projects")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

        # Get
        resp = client.get("/projects/api-test")
        assert resp.status_code == 200
        assert resp.json()["owner"] == "athola"

        # Delete
        resp = client.delete("/projects/api-test")
        assert resp.status_code == 200

        # Verify deleted
        resp = client.get("/projects/api-test")
        assert resp.status_code == 404

    def test_schedule_crud(self, client):
        # Create
        resp = client.post(
            "/sched",
            json={
                "id": "test-sched-api",
                "name": "API Test Schedule",
                "kind": "custom_scan",
                "cron": "0 9 * * *",
                "kwargs": {"topic": "test"},
            },
        )
        assert resp.status_code == 200

        # List
        resp = client.get("/sched")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

        # Get
        resp = client.get("/sched/test-sched-api")
        assert resp.status_code == 200

        # Delete
        resp = client.delete("/sched/test-sched-api")
        assert resp.status_code == 200

    def test_scan_reddit_no_topic(self, client):
        resp = client.post("/scan/reddit", json={})
        assert resp.status_code == 400

    def test_scan_github_no_topic(self, client):
        resp = client.post("/scan/github", json={})
        assert resp.status_code == 400

    def test_scan_web(self, client):
        resp = client.post("/scan/web", json={"prompt": "test query"})
        assert resp.status_code == 200

    def test_scan_web_no_prompt(self, client):
        resp = client.post("/scan/web", json={})
        assert resp.status_code == 400


# ─── Reddit Adapter Unit Tests ───────────────────────────────────


class TestRedditAdapter:
    @pytest.mark.asyncio
    async def test_no_creds_uses_public_fallback(self):
        """When OAuth credentials aren't configured, search_subreddits
        must fall back to Reddit's public JSON endpoint instead of
        returning an empty list. This keeps the morning_digest useful
        on a fresh deploy before the operator wires script-app creds."""
        import json as _json
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, patch

        import httpx

        from reddit_search import is_configured, search_subreddits

        assert not await is_configured()

        now_ts = datetime.now(timezone.utc).timestamp()
        payload = {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc123",
                            "title": "Best claude code plugin for new users?",
                            "subreddit": "ClaudeAI",
                            "permalink": "/r/ClaudeAI/comments/abc123/best_plugin/",
                            "score": 42,
                            "num_comments": 7,
                            "created_utc": now_ts,
                            "selftext": "Looking for recommendations.",
                        }
                    }
                ]
            }
        }
        mock_resp = httpx.Response(200, content=_json.dumps(payload).encode())
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("reddit_search.httpx.AsyncClient", return_value=mock_client):
            results = await search_subreddits(
                "claude code plugin", ["ClaudeAI"], since="24h"
            )

        assert len(results) == 1
        assert results[0].id == "abc123"
        assert results[0].subreddit == "ClaudeAI"
        assert "reddit.com" in results[0].url
        assert results[0].score == 42

    @pytest.mark.asyncio
    async def test_public_fallback_swallows_network_errors(self):
        from unittest.mock import AsyncMock, patch

        from reddit_search import search_subreddits

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("network down"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("reddit_search.httpx.AsyncClient", return_value=mock_client):
            results = await search_subreddits("test", ["ClaudeAI"])

        assert results == []

    def test_parse_since(self):
        from reddit_search import _parse_since

        assert _parse_since("1h") == "hour"
        assert _parse_since("24h") == "day"
        assert _parse_since("7d") == "week"
        assert _parse_since("30d") == "month"
        assert _parse_since("unknown") == "week"


# ─── GitHub Adapter Unit Tests ───────────────────────────────────


class TestGitHubAdapter:
    @pytest.mark.asyncio
    async def test_search_without_config(self):
        from github_search import search_repos

        # Without githubkit installed, returns empty
        results = await search_repos("test")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_traffic_without_config(self):
        from github_search import get_self_traffic

        result = await get_self_traffic("owner", "repo")
        assert result is None
