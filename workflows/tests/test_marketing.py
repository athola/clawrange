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
    async def test_not_configured(self):
        from reddit_search import is_configured, search_subreddits

        assert not await is_configured()
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
