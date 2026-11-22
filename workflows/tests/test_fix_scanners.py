"""Regression tests for the PR #11 review fixes in scanners and generators."""

import sys
import time
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock

import httpx
import pytest

import generators
import github_search
import persona_learning
import reddit_search
import research
import rundown
from crm.sqlite_adapter import SQLiteCRM
from errors import UpstreamError
from reddit_search import RedditPost, SearchHealth
from tenant_profile import Profile

TOPICS = ["topic A", "topic B", "topic C"]


def _tome_tasks(*, brain_db) -> list[dict]:
    tasks = brain_db.list_tasks()
    return [t for t in tasks if t["description"].startswith("research:tome:")]


class TestResearchPulseRotation:
    """#20: a claimed or finished task must not stop the rotation."""

    @pytest.mark.asyncio
    async def test_rotates_through_every_topic_across_claims(self, brain_db):
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
    async def test_skips_topic_with_an_active_task(self, brain_db):
        await generators.research_pulse_generator(brain_db, topics=TOPICS)
        first = brain_db.list_tasks(status="pending")[0]
        brain_db.claim_task(first["id"])  # still running

        await generators.research_pulse_generator(brain_db, topics=TOPICS)
        descs = [t["description"] for t in _tome_tasks(brain_db=brain_db)]
        assert descs.count("research:tome: topic A") == 1
        assert "research:tome: topic B" in descs

    @pytest.mark.asyncio
    async def test_failed_topic_goes_to_the_back_of_the_queue(self, brain_db):
        for topic in ("topic B", "topic C", "topic A"):
            task = brain_db.create_task(f"research:tome: {topic}", source="schedule")
            brain_db.claim_task(task["id"])
            brain_db.complete_task(task["id"], "boom", status="failed")
        # topic B was queued longest ago, so it comes round first.
        await generators.research_pulse_generator(brain_db, topics=TOPICS)
        pending = [t["description"] for t in brain_db.list_tasks(status="pending")]
        assert pending == ["research:tome: topic B"]

    @pytest.mark.asyncio
    async def test_all_topics_open_skips(self, brain_db):
        for topic in TOPICS:
            brain_db.create_task(f"research:tome: {topic}", source="schedule")
        await generators.research_pulse_generator(brain_db, topics=TOPICS)
        assert len(_tome_tasks(brain_db=brain_db)) == len(TOPICS)


def _emerging_project(*, brain_db):
    brain_db.upsert_project(
        "clawrange",
        "acme",
        "clawrange",
        topics=["agent orchestration"],
        subreddits=["ClaudeAI"],
        search_terms=["agent orchestration"],
    )


def _emerging_post():

    return RedditPost(
        id="em1",
        url="https://reddit.com/r/AI_Agents/comments/em1",
        title="multi-agent orchestration patterns?",
        subreddit="AI_Agents",
        score=12,
        comments=3,
        created_utc="2026-05-09T07:00:00+00:00",
    )


def _emerging_hits(*, brain_db) -> int:
    rows = brain_db.list_subreddit_stats(project_slug="clawrange", curated_only=False)
    return sum(r["hits"] for r in rows if r["subreddit"] == "ai_agents")


class TestEmergingHitsAfterDelivery:
    """#38: emerging hits count once, and only for a delivered digest."""

    @pytest.mark.asyncio
    async def test_rerun_does_not_recount_emerging_hits(self, monkeypatch, brain_db):
        _emerging_project(brain_db=brain_db)
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

        assert _emerging_hits(brain_db=brain_db) == 1
        assert brain_db.is_seen("reddit_post", "em1", "clawrange")
        assert "multi-agent orchestration" not in notify.await_args.args[0]

    @pytest.mark.asyncio
    async def test_failed_delivery_records_no_emerging_hit(self, monkeypatch, brain_db):
        _emerging_project(brain_db=brain_db)
        monkeypatch.setattr(
            "reddit_search.search_subreddits", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(
            "reddit_search.search_all", AsyncMock(return_value=[_emerging_post()])
        )
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=False))

        await generators.morning_digest_generator(brain_db)

        assert _emerging_hits(brain_db=brain_db) == 0
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
    async def test_defaults_to_the_active_profile(self, monkeypatch, brain_db):
        monkeypatch.setenv("CLAWRANGE_PROFILE", "marketing")
        monkeypatch.setattr(
            "llm_proxy._llm_call", AsyncMock(return_value="Lead with the answer.")
        )
        await generators.persona_reflect_generator(brain_db)
        assert len(brain_db.list_learnings(profile="marketing")) == 1
        assert brain_db.list_learnings(profile="starter") == []

    @pytest.mark.asyncio
    async def test_multi_line_reply_proposes_first_line(self, monkeypatch, brain_db):
        reply = "\n  Lead with the answer.\nThis keeps Telegram replies short.\n"
        monkeypatch.setattr("llm_proxy._llm_call", AsyncMock(return_value=reply))
        await generators.persona_reflect_generator(brain_db, profile_name="cos")
        rows = brain_db.list_learnings(profile="cos")
        assert [r["content"] for r in rows] == ["Lead with the answer."]

    @pytest.mark.asyncio
    async def test_none_on_first_line_queues_nothing(self, monkeypatch, brain_db):
        reply = "None.\nNothing stood out this week."
        monkeypatch.setattr("llm_proxy._llm_call", AsyncMock(return_value=reply))
        await generators.persona_reflect_generator(brain_db, profile_name="cos")
        assert brain_db.list_learnings(profile="cos") == []

    @pytest.mark.asyncio
    async def test_invalid_suggestion_is_skipped_not_raised(
        self, monkeypatch, brain_db
    ):

        too_long = "x" * (persona_learning.CONTENT_MAX + 1)
        monkeypatch.setattr("llm_proxy._llm_call", AsyncMock(return_value=too_long))
        await generators.persona_reflect_generator(brain_db, profile_name="cos")
        assert brain_db.list_learnings(profile="cos") == []


class TestGeneratorHelpers:
    """#49: the shared status and ranking helpers."""

    def test_delivery_status(self):
        assert generators._delivery_status(2, 2, "x") == "delivered 2/2 msgs (x)"
        assert generators._delivery_status(1, 2, "x") == "FAILED telegram 1/2 msgs (x)"

    def test_record_status_survives_a_broken_table(self):
        class Broken:
            def update_schedule_status(self, *a):
                raise RuntimeError("db locked")

        generators._record_status(Broken(), "hot_pulse", "ok")  # no raise

    def test_engagement_rank_orders_by_relevance_and_score(self):

        def post(score):
            return RedditPost(
                id=str(score),
                url="u",
                title="t",
                subreddit="s",
                score=score,
                comments=0,
                created_utc="2026-01-01T00:00:00+00:00",
            )

        picks = [(post(1), 1.0), (post(100), 1.0), (post(1), 3.0)]
        ranked = sorted(picks, key=generators._engagement_rank, reverse=True)
        assert [(p.score, r) for p, r in ranked] == [(100, 1.0), (1, 3.0), (1, 1.0)]


class TestResearchHealthWebChannels:
    """#42: web search runs on OpenRouter :online, so only its key counts."""

    @pytest.mark.parametrize("channel", ["discourse_web", "triz"])
    def test_zai_key_alone_is_not_configured(self, monkeypatch, channel):
        monkeypatch.setenv("ZAI_API_KEY", "z")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        info = research.channel_health()[channel]
        assert info["configured"] is False
        assert "OPENROUTER_API_KEY" in info["reason"]
        assert "ZAI" not in info["reason"]
        assert info["source"] == "openrouter-online"

    @pytest.mark.parametrize("channel", ["discourse_web", "triz"])
    def test_openrouter_key_configures_web(self, monkeypatch, channel):
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        assert research.channel_health()[channel]["configured"] is True


class TestResearchRequestChannels:
    """#46: a typo'd channel is a 422, not a 200 with zero findings."""

    def test_unknown_channel_is_rejected(self, client):
        resp = client.post("/research", json={"topic": "x", "channels": ["cdoe"]})
        assert resp.status_code == 422

    def test_known_channels_and_web_alias_are_accepted(self, client, monkeypatch):
        async def empty(topic, **kw):
            return []

        for name in ("_fetch_github", "_fetch_web"):
            monkeypatch.setattr(research, name, empty)
        resp = client.post(
            "/research", json={"topic": "x", "channels": ["code", "web"]}
        )
        assert resp.status_code == 200
        assert resp.json()["channels"] == ["code", "discourse_web"]

    def test_literal_matches_the_dispatch_table(self):

        names = set(get_args(research.ResearchChannel))
        assert names == set(research._CHANNEL_FETCHER_NAMES) | {"web"}


# ─── #26: upstream failures raise instead of returning [] ───────────


def _github_client(repos=None, issues=None):
    async def call(result):
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(parsed_data=SimpleNamespace(items=result))

    search = SimpleNamespace(
        async_repos=lambda **kw: call(repos),
        async_issues_and_pull_requests=lambda **kw: call(issues),
    )
    return SimpleNamespace(rest=SimpleNamespace(search=search))


class TestGitHubSearchRaises:
    @pytest.mark.asyncio
    async def test_repo_search_failure_raises(self, monkeypatch):
        client = _github_client(repos=RuntimeError("403 rate limited"))
        monkeypatch.setattr(github_search, "_get_client", lambda: client)
        with pytest.raises(UpstreamError, match="rate limited"):
            await github_search.search_repos("x")

    @pytest.mark.asyncio
    async def test_issue_search_failure_raises(self, monkeypatch):
        client = _github_client(issues=RuntimeError("timeout"))
        monkeypatch.setattr(github_search, "_get_client", lambda: client)
        with pytest.raises(UpstreamError):
            await github_search.search_issues("x")

    @pytest.mark.asyncio
    async def test_missing_client_raises(self, monkeypatch):
        monkeypatch.setattr(github_search, "_get_client", lambda: None)
        with pytest.raises(UpstreamError):
            await github_search.search_repos("x")
        with pytest.raises(UpstreamError):
            await github_search.search_issues("x")

    @pytest.mark.asyncio
    async def test_zero_results_is_not_an_error(self, monkeypatch):
        client = _github_client(repos=[], issues=[])
        monkeypatch.setattr(github_search, "_get_client", lambda: client)
        assert await github_search.search_repos("x") == []
        assert await github_search.search_issues("x") == []


def _reddit_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(reddit_search.httpx, "AsyncClient", factory)
    for var in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)


def _oauth_env(monkeypatch):
    for var in (
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
    ):
        monkeypatch.setenv(var, "x")


def _fake_asyncpraw(monkeypatch, fail=(), close_fails=False):
    """Install a fake asyncpraw whose subreddit search fails for `fail`."""

    _oauth_env(monkeypatch)

    class Sub:
        def __init__(self, name):
            self.name = name

        async def search(self, *a, **kw):
            if self.name in fail:
                raise RuntimeError(f"r/{self.name} 503")
            yield SimpleNamespace(
                id=f"{self.name}1",
                subreddit=self.name,
                title="t",
                score=1,
                num_comments=0,
                created_utc=time.time(),
                selftext="",
            )

    class Reddit:
        def __init__(self, **kw):
            pass

        async def subreddit(self, name):
            return Sub(name)

        async def close(self):
            if close_fails:
                raise RuntimeError("close failed")

    monkeypatch.setitem(sys.modules, "asyncpraw", SimpleNamespace(Reddit=Reddit))


class TestRedditSearchRaises:
    @pytest.mark.asyncio
    async def test_public_search_all_refused_raises(self, monkeypatch):
        _reddit_transport(monkeypatch, lambda req: httpx.Response(500))
        health = SearchHealth()
        with pytest.raises(UpstreamError):
            await reddit_search.search_subreddits("x", ["a", "b"], health=health)
        assert health.failures == {"500": 2}

    @pytest.mark.asyncio
    async def test_public_search_without_health_raises(self, monkeypatch):
        _reddit_transport(monkeypatch, lambda req: httpx.Response(403))
        with pytest.raises(UpstreamError):
            await reddit_search.search_subreddits("x", ["a"])

    @pytest.mark.asyncio
    async def test_partial_failure_returns_what_was_found(self, monkeypatch):
        statuses = iter([500, 200])

        def handler(req):
            return httpx.Response(next(statuses), json={"data": {"children": []}})

        _reddit_transport(monkeypatch, handler)
        health = SearchHealth()
        out = await reddit_search.search_subreddits("x", ["a", "b"], health=health)
        assert out == []
        assert health.ok == 1

    @pytest.mark.asyncio
    async def test_cached_feed_counts_as_a_successful_look(self, monkeypatch):
        feed = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

        def handler(req):
            if req.url.path.endswith(".rss"):
                return httpx.Response(200, text=feed)
            return httpx.Response(403)

        _reddit_transport(monkeypatch, handler)
        monkeypatch.setattr(reddit_search, "_sleep", AsyncMock())
        health = SearchHealth()
        for topic in ("one", "two"):
            out = await reddit_search.search_subreddits(
                topic, ["a"], health=health, feed_fallback=True
            )
            assert out == []

    @pytest.mark.asyncio
    async def test_failed_feed_raises_on_every_call(self, monkeypatch):
        _reddit_transport(monkeypatch, lambda req: httpx.Response(403))
        monkeypatch.setattr(reddit_search, "_sleep", AsyncMock())
        health = SearchHealth()
        for topic in ("one", "two"):
            with pytest.raises(UpstreamError):
                await reddit_search.search_subreddits(
                    topic, ["a"], health=health, feed_fallback=True
                )

    @pytest.mark.asyncio
    async def test_oauth_without_asyncpraw_raises_and_records(self, monkeypatch):
        _oauth_env(monkeypatch)
        monkeypatch.setitem(sys.modules, "asyncpraw", None)
        health = SearchHealth()
        with pytest.raises(UpstreamError):
            await reddit_search.search_subreddits("x", ["a"], health=health)
        assert health.requests == 1 and health.ok == 0
        assert not rundown.reddit_status(health).ok

    @pytest.mark.asyncio
    async def test_oauth_client_error_raises_and_records(self, monkeypatch):
        _oauth_env(monkeypatch)

        def bad_reddit(**kw):
            raise RuntimeError("401 invalid_grant")

        monkeypatch.setitem(
            sys.modules, "asyncpraw", SimpleNamespace(Reddit=bad_reddit)
        )
        health = SearchHealth()
        with pytest.raises(UpstreamError, match="invalid_grant"):
            await reddit_search.search_subreddits("x", ["a"], health=health)
        assert health.failures == {"error": 1}

    @pytest.mark.asyncio
    async def test_oauth_every_subreddit_failing_raises(self, monkeypatch):
        _fake_asyncpraw(monkeypatch, fail={"a", "b"})
        health = SearchHealth()
        with pytest.raises(UpstreamError):
            await reddit_search.search_subreddits("x", ["a", "b"], health=health)
        assert health.failures == {"error": 2} and health.ok == 0

    @pytest.mark.asyncio
    async def test_oauth_partial_failure_returns_results(self, monkeypatch):
        _fake_asyncpraw(monkeypatch, fail={"a"})
        health = SearchHealth()
        out = await reddit_search.search_subreddits("x", ["a", "b"], health=health)
        assert [p.id for p in out] == ["b1"]
        assert health.ok == 1

    @pytest.mark.asyncio
    async def test_oauth_close_failure_keeps_results(self, monkeypatch):
        _fake_asyncpraw(monkeypatch, close_fails=True)
        out = await reddit_search.search_subreddits("x", ["b"])
        assert [p.id for p in out] == ["b1"]

    @pytest.mark.asyncio
    async def test_search_all_http_error_raises(self, monkeypatch):
        _reddit_transport(monkeypatch, lambda req: httpx.Response(429))
        health = SearchHealth()
        with pytest.raises(UpstreamError):
            await reddit_search.search_all("x", health=health)
        assert health.failures == {"429": 1}

    @pytest.mark.asyncio
    async def test_search_all_transport_error_raises(self, monkeypatch):
        def handler(req):
            raise httpx.ConnectError("down")

        _reddit_transport(monkeypatch, handler)
        with pytest.raises(UpstreamError):
            await reddit_search.search_all("x")

    @pytest.mark.asyncio
    async def test_search_all_tripped_raises(self):
        health = SearchHealth()
        for _ in range(SearchHealth.BLOCK_LIMIT):
            health.record(403)
        with pytest.raises(UpstreamError):
            await reddit_search.search_all("x", health=health)
        assert health.skipped == 1


class TestResearchChannelsRaise:
    @pytest.mark.asyncio
    async def test_academic_raises_when_both_sources_fail(self, monkeypatch):
        async def fail_get(self, url, **kw):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(httpx.AsyncClient, "get", fail_get)
        with pytest.raises(UpstreamError):
            await research._fetch_academic("x")

    @pytest.mark.asyncio
    async def test_academic_returns_the_source_that_worked(self, monkeypatch):
        async def get(self, url, **kw):
            if "arxiv" in url:
                raise httpx.ConnectError("down")
            payload = {"data": [{"title": "Paper", "url": "https://s2/p", "year": 1}]}
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.AsyncClient, "get", get)
        out = await research._fetch_academic("x")
        assert [f.title for f in out] == ["Paper"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fetcher", ["_fetch_web", "_fetch_triz"])
    async def test_llm_channels_raise_when_the_tier_fails(self, monkeypatch, fetcher):
        monkeypatch.setattr("llm_proxy._llm_call", AsyncMock(return_value=None))
        with pytest.raises(UpstreamError):
            await getattr(research, fetcher)("x")


class TestOrchestrateErrors:
    @pytest.mark.asyncio
    async def test_partial_failure_is_reported_per_channel(self, monkeypatch):
        async def boom(topic, **kw):
            raise UpstreamError()

        async def ok(topic, **kw):
            return [research.Finding("github", "code", "t", "https://g/1", 0.5, "")]

        monkeypatch.setattr(research, "_fetch_reddit", boom)
        monkeypatch.setattr(research, "_fetch_github", ok)
        result = await research.orchestrate_research(
            "topic", channels=["discourse", "code"]
        )
        assert result["total"] == 1
        # An exception with no message still names what went wrong.
        assert result["errors"] == {"discourse": "UpstreamError"}

    @pytest.mark.asyncio
    async def test_every_channel_failing_raises(self, monkeypatch):
        async def boom(topic, **kw):
            raise UpstreamError("down")

        for name in ("_fetch_reddit", "_fetch_github", "_fetch_web"):
            monkeypatch.setattr(research, name, boom)
        with pytest.raises(UpstreamError, match="discourse"):
            await research.orchestrate_research("topic")

    @pytest.mark.asyncio
    async def test_zero_findings_without_errors_is_fine(self, monkeypatch):
        async def empty(topic, **kw):
            return []

        for name in ("_fetch_reddit", "_fetch_github", "_fetch_web"):
            monkeypatch.setattr(research, name, empty)
        result = await research.orchestrate_research("topic")
        assert result["total"] == 0 and result["errors"] == {}


_PROJECT = {
    "slug": "p",
    "owner": "o",
    "repo": "r",
    "topics": '["agent orchestration"]',
    "subreddits": '["ClaudeAI"]',
    "search_terms": '["agent orchestration"]',
}


async def _nothing(*a, **kw):
    return []


class TestRundownReportsFailedSources:
    @pytest.mark.asyncio
    async def test_github_failure_is_an_error_not_zero_items(self, monkeypatch):
        client = _github_client(issues=RuntimeError("502 bad gateway"))
        monkeypatch.setattr(github_search, "_get_client", lambda: client)
        monkeypatch.setattr(rundown, "fetch_hn", _nothing)
        monkeypatch.setattr(rundown, "fetch_web", _nothing)
        _evidence, statuses = await rundown.gather_other_channels([_PROJECT])
        gh = next(s for s in statuses if s.name == "GitHub issues")
        assert not gh.ok
        assert "1/1 projects failed" in gh.detail

    @pytest.mark.asyncio
    async def test_one_project_failing_keeps_the_channel_up(self, monkeypatch):
        calls = iter([RuntimeError("down"), []])

        async def flaky(**kw):
            result = next(calls)
            if isinstance(result, Exception):
                raise result
            return SimpleNamespace(parsed_data=SimpleNamespace(items=result))

        search = SimpleNamespace(async_issues_and_pull_requests=flaky)
        client = SimpleNamespace(rest=SimpleNamespace(search=search))
        monkeypatch.setattr(github_search, "_get_client", lambda: client)
        monkeypatch.setattr(rundown, "fetch_hn", _nothing)
        monkeypatch.setattr(rundown, "fetch_web", _nothing)
        other = {**_PROJECT, "slug": "q"}
        _evidence, statuses = await rundown.gather_other_channels([_PROJECT, other])
        gh = next(s for s in statuses if s.name == "GitHub issues")
        assert gh.ok
        assert "1/2 projects failed" in gh.detail

    def test_reddit_skips_without_requests_are_not_ok(self):
        health = SearchHealth(mode="public")
        health.skipped = 2
        assert not rundown.reddit_status(health).ok


class TestGeneratorsSurviveScannerOutage:
    """Generators keep running and record the outage when scanners raise."""

    @pytest.mark.asyncio
    async def test_hot_pulse_records_outage(self, monkeypatch, brain_db):
        brain_db.upsert_project(
            "p", "o", "r", topics=["x"], subreddits=["a"], search_terms=["x"]
        )
        brain_db.upsert_schedule("hot_pulse", "Hot pulse", "hot_pulse", "*/5 * * * *")
        _reddit_transport(monkeypatch, lambda req: httpx.Response(500))
        monkeypatch.setattr(generators, "_is_quiet_hours", lambda *a: False)
        monkeypatch.setattr(generators, "_LAST_HOT_PULSE_DELIVERY_AT", None)
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify)

        await generators.hot_pulse_generator(brain_db)

        status = brain_db.get_schedule("hot_pulse")["last_status"]
        assert status.startswith("reddit outage")
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_morning_digest_reports_reddit_and_github_down(
        self, monkeypatch, brain_db
    ):
        brain_db.upsert_project(
            "p", "o", "r", topics=["x"], subreddits=["a"], search_terms=["x"]
        )
        _reddit_transport(monkeypatch, lambda req: httpx.Response(500))
        monkeypatch.setattr(github_search, "_get_client", lambda: None)
        monkeypatch.setattr(rundown, "fetch_hn", _nothing)
        monkeypatch.setattr(rundown, "fetch_web", _nothing)
        monkeypatch.setattr(rundown, "synthesize", AsyncMock(return_value=""))
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr("telegram.notify", notify)

        await generators.morning_digest_generator(brain_db)

        # The report still goes out, naming both outages.
        text = "\n".join(c.args[0] for c in notify.await_args_list)
        assert "HTTP 500" in text
        assert "1/1 projects failed" in text
