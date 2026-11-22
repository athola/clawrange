"""Regression tests for the PR #11 review fixes in chat_commands and
research_router (cluster K2)."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.responses import JSONResponse, StreamingResponse

import chat_commands
import generators
import github_search
import llm_proxy
import reddit_search
import research
import research_router
import tenant_profile
from errors import UpstreamError
from tests.testapp import brain_db


def _post(i: int) -> reddit_search.RedditPost:
    return reddit_search.RedditPost(
        id=f"p{i}",
        url=f"https://reddit.com/r/x/{i}",
        title=f"post {i}",
        subreddit="x",
        score=1,
        comments=0,
        created_utc="2026-01-01T00:00:00Z",
    )


def _repo(i: int) -> github_search.GitHubRepo:
    return github_search.GitHubRepo(
        id=i, full_name=f"o/r{i}", url=f"https://github.com/o/r{i}", stars=10
    )


def _traffic() -> github_search.GitHubTrafficSnapshot:
    return github_search.GitHubTrafficSnapshot(
        owner="athola",
        repo="skrills",
        views_count=10,
        views_uniques=3,
        clones_count=2,
        clones_uniques=1,
        fetched_at="2026-01-01T00:00:00Z",
    )


def _text(resp) -> str:
    assert isinstance(resp, JSONResponse)
    body = json.loads(bytes(resp.body))
    return body["choices"][0]["message"]["content"]


@pytest.fixture()
def project():
    brain_db.upsert_project(
        "skrills",
        "athola",
        "skrills",
        topics=["skills"],
        subreddits=["ClaudeAI", "LocalLLaMA"],
        search_terms=["claude skills"],
    )
    return brain_db.get_project("skrills")


# ─── #19: generator output must reach the scanners ─────────────────


class TestGeneratorRouterContract:
    @pytest.mark.asyncio
    async def test_morning_scan_tasks_reach_the_scanners(self, project, monkeypatch):
        reddit = AsyncMock(return_value=[_post(1)])
        repos = AsyncMock(return_value=[_repo(1)])
        issues = AsyncMock(return_value=[])
        monkeypatch.setattr(reddit_search, "search_subreddits", reddit)
        monkeypatch.setattr(github_search, "search_repos", repos)
        monkeypatch.setattr(github_search, "search_issues", issues)

        await generators.morning_scan_generator(brain_db)
        tasks = brain_db.list_tasks(status="pending")
        assert len(tasks) == 2
        for t in tasks:
            result = await research_router._try_marketing_scan(
                t["description"], brain_db
            )
            assert result is not None, t["description"]

        reddit.assert_awaited_once()
        assert reddit.call_args.args[1] == ["ClaudeAI", "LocalLLaMA"]
        repos.assert_awaited_once()
        issues.assert_awaited_once()
        assert brain_db.is_seen("reddit_post", "p1", "skrills")

    @pytest.mark.asyncio
    async def test_weekly_traffic_task_reaches_the_traffic_api(
        self, project, monkeypatch
    ):
        traffic = AsyncMock(return_value=_traffic())
        monkeypatch.setattr(github_search, "get_self_traffic", traffic)

        await generators.weekly_traffic_generator(brain_db)
        (task,) = brain_db.list_tasks(status="pending")
        result = await research_router._try_marketing_scan(
            task["description"], brain_db
        )
        traffic.assert_awaited_once_with("athola", "skrills")
        assert result is not None and "Views: 10" in result


# ─── #26: upstream failures are not empty results ──────────────────


class TestRouterUpstreamFailures:
    @pytest.mark.asyncio
    async def test_reddit_failure_propagates(self, project, monkeypatch):
        monkeypatch.setattr(
            reddit_search,
            "search_subreddits",
            AsyncMock(side_effect=UpstreamError("all subs blocked")),
        )
        with pytest.raises(UpstreamError):
            await research_router._try_marketing_scan(
                "Scan reddit for skrills: search", brain_db
            )

    @pytest.mark.asyncio
    async def test_research_upstream_failure_propagates(self, monkeypatch):
        monkeypatch.setattr(
            research,
            "orchestrate_research",
            AsyncMock(side_effect=UpstreamError("every channel failed")),
        )
        with pytest.raises(UpstreamError):
            await research_router._try_research_task(
                "find reddit threads on chrome extensions"
            )

    @pytest.mark.asyncio
    async def test_no_findings_with_channel_errors_names_the_errors(self, monkeypatch):
        monkeypatch.setattr(
            research,
            "orchestrate_research",
            AsyncMock(return_value={"findings": [], "errors": {"code": "rate limit"}}),
        )
        result = await research_router._try_research_task(
            "find reddit threads on chrome extensions"
        )
        assert result is not None
        assert result != "Research: no findings."
        assert "code" in result and "rate limit" in result

    @pytest.mark.asyncio
    async def test_partial_failure_listed_under_findings(self, monkeypatch):
        finding = {"channel": "discourse", "title": "t", "url": "https://x/1"}
        monkeypatch.setattr(
            research,
            "orchestrate_research",
            AsyncMock(
                return_value={"findings": [finding], "errors": {"code": "rate limit"}}
            ),
        )
        result = await research_router._try_research_task(
            "find reddit threads on chrome extensions"
        )
        assert result is not None
        assert "https://x/1" in result and "rate limit" in result

    @pytest.mark.asyncio
    async def test_traffic_without_pat_says_so(self, monkeypatch):
        monkeypatch.setattr(github_search, "GITHUB_PAT", "")
        monkeypatch.setattr(
            github_search, "get_self_traffic", AsyncMock(return_value=None)
        )
        result = await research_router._try_marketing_scan(
            "Weekly traffic snapshot for athola/skrills: pull", brain_db
        )
        assert result is not None and "GITHUB_PAT" in result

    @pytest.mark.asyncio
    async def test_traffic_api_failure_with_pat_raises(self, monkeypatch):
        monkeypatch.setattr(github_search, "GITHUB_PAT", "test-pat")
        monkeypatch.setattr(
            github_search, "get_self_traffic", AsyncMock(return_value=None)
        )
        with pytest.raises(UpstreamError) as exc:
            await research_router._try_marketing_scan(
                "Weekly traffic snapshot for athola/skrills: pull", brain_db
            )
        assert "GITHUB_PAT" not in str(exc.value)


class TestMarkSeenOnlyShown:
    @pytest.mark.asyncio
    async def test_reddit_marks_only_shown_posts(self, project, monkeypatch):
        monkeypatch.setattr(
            reddit_search,
            "search_subreddits",
            AsyncMock(return_value=[_post(i) for i in range(12)]),
        )
        await research_router._try_marketing_scan(
            "Scan reddit for skrills: search", brain_db
        )
        assert brain_db.is_seen("reddit_post", "p7", "skrills")
        assert not brain_db.is_seen("reddit_post", "p8", "skrills")

    @pytest.mark.asyncio
    async def test_github_marks_only_shown_repos(self, project, monkeypatch):
        monkeypatch.setattr(
            github_search,
            "search_repos",
            AsyncMock(return_value=[_repo(i) for i in range(10)]),
        )
        monkeypatch.setattr(github_search, "search_issues", AsyncMock(return_value=[]))
        await research_router._try_marketing_scan(
            "Scan github for skrills: find", brain_db
        )
        assert brain_db.is_seen("github_repo", "4", "skrills")
        assert not brain_db.is_seen("github_repo", "5", "skrills")


class TestChatScanFailures:
    @pytest.mark.asyncio
    async def test_reddit_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            reddit_search,
            "search_subreddits",
            AsyncMock(side_effect=UpstreamError("403 everywhere")),
        )
        text = await chat_commands._handle_scan_command(
            "reddit", "mcp --subs x", brain_db=brain_db
        )
        assert text.startswith("Reddit search unavailable: 403 everywhere")

    @pytest.mark.asyncio
    async def test_github_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            github_search,
            "search_repos",
            AsyncMock(side_effect=UpstreamError("rate limited")),
        )
        text = await chat_commands._handle_scan_command(
            "github", "mcp", brain_db=brain_db
        )
        assert text.startswith("GitHub search unavailable: rate limited")

    @pytest.mark.asyncio
    async def test_web_search_failure_is_not_no_results(self, monkeypatch):
        monkeypatch.setattr(llm_proxy, "_llm_call", AsyncMock(return_value=None))
        text = await chat_commands._handle_scan_command(
            "web", "mcp servers", brain_db=brain_db
        )
        assert "unavailable" in text.lower()


# ─── #44 / S8: /scan github traffic ────────────────────────────────


class TestScanGithubTraffic:
    @pytest.mark.asyncio
    async def test_traffic_is_reachable(self, project, monkeypatch):
        traffic = AsyncMock(return_value=_traffic())
        repos = AsyncMock(return_value=[])
        monkeypatch.setattr(github_search, "get_self_traffic", traffic)
        monkeypatch.setattr(github_search, "search_repos", repos)
        text = await chat_commands._handle_scan_command(
            "github", "traffic skrills", brain_db=brain_db
        )
        traffic.assert_awaited_once_with("athola", "skrills")
        repos.assert_not_awaited()
        assert "Views: 10" in text

    @pytest.mark.asyncio
    async def test_traffic_prefix_word_is_a_topic(self, monkeypatch):
        traffic = AsyncMock(return_value=_traffic())
        repos = AsyncMock(return_value=[])
        monkeypatch.setattr(github_search, "get_self_traffic", traffic)
        monkeypatch.setattr(github_search, "search_repos", repos)
        await chat_commands._handle_scan_command(
            "github", "trafficking", brain_db=brain_db
        )
        traffic.assert_not_awaited()
        repos.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_traffic_api_failure_with_pat_does_not_blame_pat(
        self, project, monkeypatch
    ):
        monkeypatch.setattr(github_search, "GITHUB_PAT", "test-pat")
        monkeypatch.setattr(
            github_search, "get_self_traffic", AsyncMock(return_value=None)
        )
        text = await chat_commands._handle_scan_command(
            "github", "traffic skrills", brain_db=brain_db
        )
        assert "unavailable" in text and "GITHUB_PAT" not in text


# ─── #34 / S6 / S7: first-token matching ───────────────────────────


class TestWordBoundaries:
    @pytest.mark.parametrize(
        "msg",
        [
            "!task listen to the podcast",
            "!task tailor the resume",
            "!task tailscale cert renewal",
            "!task cancellation policy",
            "!task prioritize the backlog",
        ],
    )
    def test_task_prefix_words_create_tasks(self, msg):
        cmd = chat_commands._extract_task_command(msg)
        assert cmd == {"type": "create", "description": msg.split(None, 1)[1]}

    @pytest.mark.parametrize(
        ("msg", "kind"),
        [("!task list", "list"), ("!task tail", "tail"), ("!task TAIL", "tail")],
    )
    def test_task_subcommands_still_match(self, msg, kind):
        assert chat_commands._extract_task_command(msg) == {"type": kind}

    @pytest.mark.parametrize(
        "msg", ["/schedule my meeting", "/scanner broke", "/projectsfoo", "!marketer"]
    )
    def test_marketing_prefix_needs_a_boundary(self, msg):
        assert chat_commands._extract_marketing_command(msg) is None

    @pytest.mark.parametrize(
        ("msg", "verb", "subcmd"),
        [
            ("/sched", "sched", "list"),
            ("/sched list --paused", "sched", "list"),
            ("!scan  reddit mcp", "scan", "reddit"),
            ("/projects\tlist", "projects", "list"),
        ],
    )
    def test_marketing_commands_still_parse(self, msg, verb, subcmd):
        cmd = chat_commands._extract_marketing_command(msg)
        assert cmd is not None
        assert (cmd["verb"], cmd["subcmd"]) == (verb, subcmd)


# ─── #33 / G10: /sched uses the live scheduler ─────────────────────


@pytest_asyncio.fixture()
async def live_scheduler():
    sched = AsyncIOScheduler(timezone="UTC")
    sched.start()
    try:
        yield sched
    finally:
        sched.shutdown(wait=False)


class TestSchedLiveScheduler:
    @pytest.mark.asyncio
    async def test_add_pause_resume_rm_by_name(self, live_scheduler):
        text = await chat_commands._handle_sched_command(
            "add",
            'myscan cron "0 9 * * *" -- morning_scan',
            brain_db=brain_db,
            scheduler=live_scheduler,
        )
        assert "Schedule added" in text
        sched = brain_db.get_schedule("myscan")
        job_id = f"marketing_{sched['id']}"
        assert live_scheduler.get_job(job_id) is not None

        await chat_commands._handle_sched_command(
            "pause", "myscan", brain_db=brain_db, scheduler=live_scheduler
        )
        assert brain_db.get_schedule("myscan")["paused"]
        job = live_scheduler.get_job(job_id)
        assert job is None or job.next_run_time is None

        await chat_commands._handle_sched_command(
            "resume", "myscan", brain_db=brain_db, scheduler=live_scheduler
        )
        assert live_scheduler.get_job(job_id).next_run_time is not None

        text = await chat_commands._handle_sched_command(
            "rm", "myscan", brain_db=brain_db, scheduler=live_scheduler
        )
        assert "Removed" in text
        assert live_scheduler.get_job(job_id) is None
        assert brain_db.get_schedule("myscan") is None

    @pytest.mark.asyncio
    async def test_add_validates_cron(self, live_scheduler):
        text = await chat_commands._handle_sched_command(
            "add",
            'bad cron "not a cron" -- morning_scan',
            brain_db=brain_db,
            scheduler=live_scheduler,
        )
        assert "invalid cron" in text.lower()
        assert brain_db.get_schedule("bad") is None

    @pytest.mark.asyncio
    async def test_list_paused_filters(self):
        brain_db.upsert_schedule("a", "Alpha", "morning_scan", "0 9 * * *")
        brain_db.upsert_schedule("b", "Beta", "morning_scan", "0 9 * * *")
        brain_db.set_schedule_paused("b", True)
        text = await chat_commands._handle_sched_command(
            "list", "--paused", brain_db=brain_db
        )
        assert "Beta" in text and "Alpha" not in text

    @pytest.mark.asyncio
    async def test_marketing_alias_runs_generator_without_schedule(
        self, project, monkeypatch
    ):
        cmd = chat_commands._extract_marketing_command("/marketing")
        text = await chat_commands._handle_sched_command(
            cmd["subcmd"], cmd["args"], brain_db=brain_db
        )
        assert "not found" not in text.lower()
        assert len(brain_db.list_tasks(status="pending")) == 2


# ─── #51: RECENT / tail order and blocked tasks ────────────────────


def _close(desc: str, status: str, when: str, priority: int = 3) -> str:
    t = brain_db.create_task(desc, priority=priority)
    brain_db.complete_task(t["id"], f"result {desc}", status=status)
    brain_db._conn.execute(
        "UPDATE tasks SET completed_at=? WHERE id=?", (when, t["id"])
    )
    brain_db._conn.commit()
    return t["id"]


class TestRecentTasks:
    def _seed(self):
        # Priority order would put "old-urgent" last-shown; completion
        # order puts "newest" first.
        _close("old-urgent", "completed", "2026-01-01T00:00:00", priority=1)
        _close("middle", "failed", "2026-01-02T00:00:00", priority=5)
        _close("asked", "blocked", "2026-01-03T00:00:00", priority=5)
        _close("newest", "completed", "2026-01-04T00:00:00", priority=5)

    @pytest.mark.asyncio
    async def test_tail_newest_first_and_includes_blocked(self):
        self._seed()
        text = _text(
            await chat_commands._dispatch_task_command(
                {"type": "tail"}, brain_db=brain_db
            )
        )
        assert "asked" in text
        assert text.index("newest") < text.index("asked") < text.index("middle")

    @pytest.mark.asyncio
    async def test_list_recent_shows_last_three_by_completion(self):
        self._seed()
        text = _text(
            await chat_commands._dispatch_task_command(
                {"type": "list"}, brain_db=brain_db
            )
        )
        recent = text.split("RECENT:")[1]
        assert "newest" in recent and "asked" in recent and "middle" in recent
        assert "old-urgent" not in recent


# ─── #51 / #48: help text, ops name, logging, balance ──────────────


class TestHelpAndText:
    def test_help_lists_only_handled_commands(self):
        text = _text(chat_commands._handle_help())
        for gone in ("!remember", "!recall", "!page", "web_fetch"):
            assert gone not in text
        assert "/marketing" in text

    @pytest.mark.asyncio
    async def test_task_created_names_configured_ops_agent(self, monkeypatch):
        prof = SimpleNamespace(assistant={"ops": {"agent": "ops", "name": "Ops Bot"}})
        monkeypatch.setattr(tenant_profile, "active_profile", lambda: prof)
        text = _text(
            await chat_commands._dispatch_task_command(
                {"type": "create", "description": "check backups"}, brain_db=brain_db
            )
        )
        assert "Ops Bot will pick this up" in text
        assert "Max Ops" not in text

    @pytest.mark.asyncio
    async def test_task_created_without_ops_name(self, monkeypatch):
        monkeypatch.setattr(tenant_profile, "active_profile", lambda: None)
        text = _text(
            await chat_commands._dispatch_task_command(
                {"type": "create", "description": "check backups"}, brain_db=brain_db
            )
        )
        assert "Max Ops" not in text
        assert "pick this up" in text

    @pytest.mark.asyncio
    async def test_dispatch_error_logged_with_traceback(self, monkeypatch, caplog):
        monkeypatch.setattr(
            chat_commands,
            "_handle_projects_command",
            AsyncMock(side_effect=RuntimeError("boom")),
        )
        with caplog.at_level(logging.ERROR):
            resp = await chat_commands._dispatch_marketing_command(
                {"verb": "projects", "subcmd": "list", "args": ""}, brain_db=brain_db
            )
        assert "boom" in _text(resp)
        assert any(r.exc_info for r in caplog.records)

    @pytest.mark.asyncio
    async def test_tier_balance_uses_describe_balance(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(
            llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            llm_proxy, "_balance_status", {"ok_at": None, "error": "HTTP 500"}
        )
        text = _text(await chat_commands._handle_tier_command())
        assert "unknown (lookup failed: HTTP 500)" in text
        assert "not configured" not in text


# ─── #49 / #34: !task handlers end to end, one SSE wrap ────────────


class TestTaskHandlersEndToEnd:
    @pytest.mark.asyncio
    async def test_stream_wraps_once(self):
        resp = await chat_commands._dispatch_task_command(
            {"type": "list"}, is_stream=True, brain_db=brain_db
        )
        assert isinstance(resp, StreamingResponse)
        resp = await chat_commands._dispatch_marketing_command(
            {"verb": "projects", "subcmd": "list", "args": ""},
            is_stream=True,
            brain_db=brain_db,
        )
        assert isinstance(resp, StreamingResponse)

    @pytest.mark.asyncio
    async def test_cancel_and_priority(self):
        t = brain_db.create_task("x", priority=3)
        cmd = chat_commands._extract_task_command(f"!task priority {t['id']} 1")
        text = _text(await chat_commands._dispatch_task_command(cmd, brain_db=brain_db))
        assert "priority set to 1" in text
        assert brain_db.get_task(t["id"])["priority"] == 1

        cmd = chat_commands._extract_task_command(f"!task cancel {t['id']}")
        text = _text(await chat_commands._dispatch_task_command(cmd, brain_db=brain_db))
        assert "cancelled" in text
        assert brain_db.get_task(t["id"])["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_list_shows_active_and_queued(self):
        a = brain_db.create_task("being done")
        brain_db.claim_task(a["id"])
        brain_db.create_task("waiting")
        text = _text(
            await chat_commands._dispatch_task_command(
                {"type": "list"}, brain_db=brain_db
            )
        )
        assert text.index("NOW PROCESSING:") < text.index("being done")
        assert text.index("QUEUED:") < text.index("waiting")
