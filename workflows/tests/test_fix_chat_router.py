"""Regression tests for the PR #11 review fixes in chat_commands and
research_router (cluster K2)."""

from unittest.mock import AsyncMock

import pytest

import generators
import github_search
import reddit_search
import research
import research_router
from app import brain_db
from errors import UpstreamError


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
        monkeypatch.setattr(github_search, "GITHUB_PAT", "ghp_x")
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
