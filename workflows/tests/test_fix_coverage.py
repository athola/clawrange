"""Behaviour tests for the coverage gaps named in issue #43 (cluster K43).

Each test drives the real code path (the chat endpoint, the heartbeat
handler, the scheduler helpers) and asserts on what the user or the queue
sees, so a regression in the branch fails the test rather than only
dropping a covered line.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from apscheduler.triggers.cron import CronTrigger
from fastapi.testclient import TestClient

import digest
import generators
import github_search
import heartbeat
import llm_proxy
import reddit_search
import research_router
import scheduler as sch
import tenant_profile
from app import app, brain_db
from errors import UpstreamError

TOKEN = "k43-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def chat(monkeypatch):
    """POST one user message through the real proxy router; return the reply."""
    monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    client = TestClient(app)

    def send(text: str, **body) -> str:
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": text}], **body},
            headers=AUTH,
        )
        assert r.status_code == 200
        return r.json()["choices"][0]["message"]["content"]

    return send


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")


def _balance(value):
    return patch.object(
        llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=value)
    )


def _ok(text: str = "Here is a proper answer to the question.") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


# ─── llm_proxy: tool-call responses through the single-tier path ─────

TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "web_search", "arguments": '{"q": "release"}'},
}


def _tool_call_reply() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [TOOL_CALL],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )


class TestToolCallSingleTier:
    def test_null_content_tool_call_is_passed_through(self, keys):
        """Null content would fail the empty-reply check; tool calls skip it."""
        tier = next(t for t in llm_proxy.CONFIG["tiers"] if t["name"] == "zai-direct")
        call = AsyncMock(return_value=_tool_call_reply())
        with patch.object(llm_proxy, "_call_provider", call):
            resp, rl = asyncio.run(
                llm_proxy._try_single_tier(
                    tier, {"messages": [{"role": "user", "content": "hi"}]}
                )
            )
        assert rl is None
        data = json.loads(bytes(resp.body))
        assert data["_clawrange_tier"] == "zai-direct"
        assert data["choices"][0]["message"]["tool_calls"] == [TOOL_CALL]
        assert "zai-direct" not in llm_proxy._circuit_state

    def test_tool_call_reaches_a_streaming_client(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
        call = AsyncMock(return_value=_tool_call_reply())
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            r = TestClient(app).post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "what changed?"}],
                    "tools": [{"type": "function", "function": {"name": "web_search"}}],
                    "stream": True,
                },
                headers=AUTH,
            )
        assert r.status_code == 200
        chunks = [
            json.loads(line[len("data: ") :])
            for line in r.text.splitlines()
            if line.startswith("data: {")
        ]
        deltas = [c["choices"][0]["delta"] for c in chunks]
        streamed = [d["tool_calls"] for d in deltas if d.get("tool_calls")]
        assert streamed and streamed[0][0]["function"]["name"] == "web_search"
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


# ─── llm_proxy: !paid routing and the balance floor, end to end ──────


def _recording_provider(calls: list[tuple[str, list[str]]]):
    async def provider(name, models, body, api_key):
        calls.append((name, list(models)))
        return _ok()

    return provider


def _paid_models() -> list[str]:
    paid = next(t for t in llm_proxy.CONFIG["tiers"] if t.get("paid"))
    return list(paid["models"])


class TestPaidRouting:
    def test_paid_hint_calls_only_the_paid_tier(self, keys, chat):
        calls: list = []
        with (
            _balance(50.0),
            patch.object(llm_proxy, "_call_provider", _recording_provider(calls)),
        ):
            chat("!paid summarise the release notes")
        assert calls == [("openrouter", _paid_models())]

    def test_no_hint_never_calls_the_paid_tier(self, keys, chat):
        calls: list = []
        with (
            _balance(50.0),
            patch.object(llm_proxy, "_call_provider", _recording_provider(calls)),
        ):
            chat("summarise the release notes")
        assert calls
        assert all(models != _paid_models() for _, models in calls)

    def test_paid_hint_below_the_floor_makes_no_call(self, keys, chat):
        calls: list = []
        below = llm_proxy.OPENROUTER_BALANCE_FLOOR - 1
        with (
            _balance(below),
            patch.object(llm_proxy, "_call_provider", _recording_provider(calls)),
        ):
            text = chat("!claude summarise the release notes")
        assert calls == []
        assert "paid floor" in text
        assert "no LLM tiers are available" in text


# ─── heartbeat: blocked results, alerts, interrupted digest burst ────


@pytest.fixture()
def quiet(monkeypatch):
    """Known balance, no proactive layer due, Telegram stubbed."""
    monkeypatch.setattr(
        llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=50.0)
    )
    monkeypatch.setenv("HEARTBEAT_LLM_SUGGEST", "0")
    now = time.monotonic()
    heartbeat._proactive_state["stale_tasks"] = now
    heartbeat._proactive_state["llm_thinking"] = now
    monkeypatch.setattr(heartbeat, "_llm_retry_counts", {})
    monkeypatch.setattr(heartbeat, "_balance_unknown_since", None)
    monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))


def _beat() -> None:
    asyncio.run(heartbeat._handle_heartbeat(False, brain_db=brain_db))


@pytest.mark.usefixtures("quiet")
class TestHeartbeatBlocked:
    def test_needs_input_result_closes_the_task_as_blocked(self, monkeypatch):
        task = brain_db.create_task("Draft the onboarding email for new clients")
        answer = "Status: BLOCKED - need your preferred sender name first."
        monkeypatch.setattr(heartbeat, "_llm_work_task", AsyncMock(return_value=answer))
        _beat()
        done = brain_db.get_task(task["id"])
        assert done["status"] == "blocked"
        assert done["result"] == answer
        assert brain_db.list_tasks(status="pending") == []
        lines = digest._digest_state["lines"]
        assert any(ln.startswith(f"Blocked #{task['id']}:") for ln in lines)
        # The full answer stays on the task; the digest gets the short line.
        assert not any(answer in ln for ln in lines)

    def test_finished_result_still_completes(self, monkeypatch):
        task = brain_db.create_task("Draft the onboarding email for new clients")
        monkeypatch.setattr(
            heartbeat, "_llm_work_task", AsyncMock(return_value="Subject: Welcome")
        )
        _beat()
        assert brain_db.get_task(task["id"])["status"] == "completed"


def _low_alerts() -> list[dict]:
    return [
        t
        for t in brain_db.list_tasks()
        if t["description"].startswith("Low balance alert:")
    ]


@pytest.mark.usefixtures("quiet")
class TestLowBalanceAlert:
    def _beat_with_balance(self, monkeypatch, value):
        monkeypatch.setattr(
            llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=value)
        )
        _beat()

    def test_below_floor_creates_one_priority_one_alert(self, monkeypatch):
        self._beat_with_balance(monkeypatch, 3.0)
        alerts = _low_alerts()
        assert [a["description"] for a in alerts] == [
            "Low balance alert: $3.00 remaining"
        ]
        assert alerts[0]["priority"] == 1
        assert any(
            "Low balance alert: $3.00" in ln for ln in digest._digest_state["lines"]
        )

    @pytest.mark.parametrize("value", [llm_proxy.OPENROUTER_BALANCE_FLOOR, 50.0, None])
    def test_at_or_above_floor_or_unknown_creates_none(self, monkeypatch, value):
        self._beat_with_balance(monkeypatch, value)
        assert _low_alerts() == []


@pytest.mark.usefixtures("quiet")
class TestTierRecovery:
    def test_tripped_tier_gets_a_priority_two_alert(self, monkeypatch):
        monkeypatch.setattr(
            llm_proxy, "_circuit_open", lambda name, probe=True: name == "zai-direct"
        )
        _beat()
        alerts = [
            t
            for t in brain_db.list_tasks()
            if t["description"] == "Investigate tier recovery: zai-direct"
        ]
        assert len(alerts) == 1 and alerts[0]["priority"] == 2

    def test_alert_for_a_still_tripped_tier_says_so(self, monkeypatch):
        task = brain_db.create_task("Investigate tier recovery: zai-direct", 2)
        monkeypatch.setattr(
            llm_proxy, "_circuit_open", lambda name, probe=True: name == "zai-direct"
        )
        _beat()
        done = brain_db.get_task(task["id"])
        assert done["status"] == "completed"
        assert "TRIPPED (circuit open)" in done["result"]

    def test_alert_after_recovery_says_recovered(self, monkeypatch):
        task = brain_db.create_task("Investigate tier recovery: zai-direct", 2)
        monkeypatch.setattr(llm_proxy, "_circuit_open", lambda name, probe=True: False)
        _beat()
        assert "recovered (circuit closed)" in brain_db.get_task(task["id"])["result"]


class TestInterruptedDigestBurst:
    def test_cancel_mid_burst_keeps_every_item(self, monkeypatch):
        for i in range(3):
            digest._digest_record(f"item {i}")
        sent: list[str] = []

        async def notify(text):
            if sent:
                raise asyncio.CancelledError
            sent.append(text)
            return True

        monkeypatch.setattr("telegram.notify", notify)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(heartbeat._flush_digest([], [], 50.0))
        assert len(sent) == 1
        # Nothing is lost: even the item that went out stays buffered,
        # since the burst cannot say which sends really landed.
        assert digest._digest_state["lines"] == ["item 0", "item 1", "item 2"]

    def test_completed_burst_releases_the_items(self, monkeypatch):
        for i in range(2):
            digest._digest_record(f"item {i}")
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))
        text = asyncio.run(heartbeat._flush_digest([], [], 50.0))
        assert digest._digest_state["lines"] == []
        assert "2 item(s)" in text


# ─── scheduler: catch-up skips, run-now errors, duration aliases ─────


class TestCatchUpSkips:
    def _queue(self, monkeypatch, schedules, opted_in):
        monkeypatch.setattr(sch, "catch_up_ids", lambda: opted_in)
        fake = MagicMock()
        sch._queue_catch_ups(fake, schedules, brain_db, "UTC")
        return [c.kwargs["id"] for c in fake.add_job.call_args_list]

    def _missed(self) -> dict:
        # Daily at 00:00 UTC, never run: the latest fire was missed unless
        # it is more than CATCH_UP_WINDOW ago.
        now = datetime.now(UTC)
        fire = (now - timedelta(minutes=1)).strftime("%M %H")
        return {"kind": "morning_scan", "cron": f"{fire} * * *", "last_run": None}

    def test_missed_opted_in_schedule_is_queued(self, monkeypatch):
        ids = self._queue(monkeypatch, [{"id": "a", **self._missed()}], {"a"})
        assert ids == ["catchup_a"]

    def test_paused_schedule_is_skipped(self, monkeypatch):
        rows = [{"id": "a", "paused": 1, **self._missed()}]
        assert self._queue(monkeypatch, rows, {"a"}) == []

    def test_schedule_not_opted_in_is_skipped(self, monkeypatch):
        assert self._queue(monkeypatch, [{"id": "a", **self._missed()}], set()) == []

    def test_schedule_that_already_ran_is_skipped(self, monkeypatch):
        row = {"id": "a", **self._missed(), "last_run": datetime.now(UTC).isoformat()}
        row["last_status"] = "ok"
        assert self._queue(monkeypatch, [row], {"a"}) == []


@pytest.mark.asyncio
class TestRunScheduleNowErrors:
    async def test_unknown_schedule_raises(self):
        with pytest.raises(ValueError, match="Schedule not found"):
            await sch.run_schedule_now(None, brain_db, "missing")

    async def test_unknown_kind_raises(self):
        brain_db.upsert_schedule("odd", "Odd", "no_such_kind", "0 9 * * *")
        with pytest.raises(ValueError, match="Unknown generator kind"):
            await sch.run_schedule_now(None, brain_db, "odd")

    async def test_generator_error_is_recorded_as_error(self, monkeypatch):
        async def boom(brain_db, **kw):
            raise RuntimeError("feed down")

        monkeypatch.setitem(sch.GENERATORS, "k43_boom", boom)
        brain_db.upsert_schedule("b", "B", "k43_boom", "0 9 * * *")
        out = await sch.run_schedule_now(None, brain_db, "b")
        assert out["status"] == "error"
        assert "feed down" in brain_db.get_schedule("b")["last_status"]


def _gaps(expr: str, fires: int) -> set[timedelta]:
    """Distinct gaps between consecutive fires of a schedule expression."""
    trigger = CronTrigger(**sch._parse_cron(expr), timezone="UTC")
    t = datetime(2026, 1, 1, tzinfo=UTC)
    times = []
    prev = None
    for _ in range(fires):
        t = trigger.get_next_fire_time(prev, t)
        times.append(t)
        prev = t
        t = t + timedelta(seconds=1)
    return {b - a for a, b in zip(times, times[1:], strict=False)}


class TestDurationAliases:
    @pytest.mark.parametrize(
        ("expr", "gap"),
        [
            ("every 6h", timedelta(hours=6)),
            ("every 8h", timedelta(hours=8)),
            ("every 30m", timedelta(minutes=30)),
            ("every 15m", timedelta(minutes=15)),
            ("every 1d", timedelta(days=1)),
        ],
    )
    def test_accepted_alias_fires_at_even_gaps(self, expr, gap):
        assert _gaps(expr, 80) == {gap}

    @pytest.mark.parametrize("expr", ["every 7h", "every 45m", "every 2d"])
    def test_alias_with_uneven_gaps_is_rejected(self, expr):
        """*/7 on hours fires 0,7,14,21 then 0 again: a 3h gap at midnight."""
        with pytest.raises(ValueError, match="evenly"):
            sch._parse_cron(expr)

    def test_uneven_alias_never_reaches_the_schedule_table(self, chat):
        text = chat('/sched add bad cron "every 7h" -- morning_scan')
        assert text.startswith("Schedule not added")
        assert brain_db.list_schedules() == []


# ─── tenant_profile.hour_in_window ───────────────────────────────────


class TestHourInWindow:
    @pytest.mark.parametrize(
        ("hour", "inside"),
        [(21, False), (22, True), (23, True), (0, True), (5, True), (6, False)],
    )
    def test_wrap_around_window(self, hour, inside):
        now = datetime(2026, 1, 15, hour, 30, tzinfo=UTC)
        assert tenant_profile.hour_in_window(now, 22, 6, "UTC") is inside

    def test_wrap_around_uses_local_time(self):
        # 04:00 UTC is 22:00 CST the previous evening.
        now = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)
        assert tenant_profile.hour_in_window(now, 22, 2, "America/Chicago")
        assert not tenant_profile.hour_in_window(now, 22, 2, "UTC")

    def test_empty_window_is_never_inside(self):
        for hour in range(24):
            now = datetime(2026, 1, 15, hour, tzinfo=UTC)
            assert not tenant_profile.hour_in_window(now, 3, 3, "UTC")

    def test_generators_quiet_hours_is_this_helper(self):
        assert generators._is_quiet_hours is tenant_profile.hour_in_window


# ─── chat_commands: /sched, /scan, /projects, !task, !tier ───────────


def _post(i: int, snippet: str | None = None) -> reddit_search.RedditPost:
    return reddit_search.RedditPost(
        id=f"p{i}",
        url=f"https://reddit.com/r/x/{i}",
        title=f"post {i}",
        subreddit="x",
        score=i,
        comments=0,
        created_utc="2026-01-01T00:00:00Z",
        snippet=snippet,
    )


def _repo(i: int, description: str | None = None) -> github_search.GitHubRepo:
    return github_search.GitHubRepo(
        id=i,
        full_name=f"o/r{i}",
        url=f"https://github.com/o/r{i}",
        stars=10 + i,
        description=description,
    )


def _issue(i: int) -> github_search.GitHubIssue:
    return github_search.GitHubIssue(
        id=i,
        number=i,
        title=f"issue {i}",
        url=f"https://github.com/o/r/issues/{i}",
        state="open",
        repository="o/r",
    )


class TestSchedCommand:
    def test_list_empty_explains_how_to_add(self, chat):
        assert chat("/sched").startswith("No schedules configured")

    def test_list_shows_status_and_paused_filter(self, chat):
        brain_db.upsert_schedule("a1", "Morning", "morning_scan", "0 9 * * *")
        brain_db.upsert_schedule("b2", "Evening", "morning_scan", "0 18 * * *")
        assert "No paused schedules." == chat("/sched list --paused")
        brain_db.set_schedule_paused("b2", True)
        listing = chat("/sched list")
        assert "[ACTIVE] Morning (a1)" in listing
        assert "[PAUSED] Evening (b2)" in listing
        paused = chat("/sched list --paused")
        assert "Evening" in paused and "Morning" not in paused

    def test_show_by_name_and_unknown(self, chat):
        brain_db.upsert_schedule("a1", "Morning", "morning_scan", "0 9 * * *")
        text = chat("/sched show a1")
        assert "Schedule: Morning (a1)" in text and "Cron: 0 9 * * *" in text
        assert chat("/sched info nope") == "Schedule not found: nope"

    def test_add_persists_a_valid_schedule(self, chat):
        text = chat('/sched add nightly cron "0 2 * * *" -- morning_scan')
        assert text.startswith("Schedule added: nightly")
        rows = brain_db.list_schedules()
        assert [(r["name"], r["cron"], r["kind"]) for r in rows] == [
            ("nightly", "0 2 * * *", "morning_scan")
        ]

    @pytest.mark.parametrize(
        ("msg", "reply"),
        [
            ('/sched add x cron "0 2 * *', "Parse error"),
            ("/sched add x cron", "Usage: /sched add"),
            ('/sched add x cron "not a cron" -- morning_scan', "Schedule not added"),
            ('/sched add x cron "0 2 * * *" -- no_such_kind', "Schedule not added"),
        ],
    )
    def test_add_rejects_bad_input_without_writing(self, chat, msg, reply):
        assert chat(msg).startswith(reply)
        assert brain_db.list_schedules() == []

    def test_pause_resume_remove(self, chat):
        brain_db.upsert_schedule("a1", "Morning", "morning_scan", "0 9 * * *")
        assert chat("/sched pause a1") == "Paused: Morning (a1)"
        assert brain_db.get_schedule("a1")["paused"]
        assert chat("/sched resume a1") == "Resumed: Morning (a1)"
        assert not brain_db.get_schedule("a1")["paused"]
        assert chat("/sched rm a1") == "Removed: Morning (a1)"
        assert brain_db.get_schedule("a1") is None
        assert chat("/sched pause a1") == "Schedule not found: a1"

    def test_run_reports_error_status(self, chat, monkeypatch):
        async def boom(brain_db, **kw):
            raise RuntimeError("feed down")

        monkeypatch.setitem(sch.GENERATORS, "k43_boom", boom)
        brain_db.upsert_schedule("b", "B", "k43_boom", "0 9 * * *")
        text = chat("/sched run b")
        assert text.startswith("Ran schedule: b")
        assert "error" in text and "feed down" in text

    def test_run_unknown_target(self, chat):
        assert chat("/sched run ghost") == "Schedule not found: ghost"

    def test_unknown_subcommand_shows_usage(self, chat):
        assert chat("/sched frobnicate").startswith("Usage:\n  /sched list")


class TestScanCommand:
    def test_reddit_usage_without_topic(self, chat):
        assert chat("/scan reddit").startswith("Usage: /scan reddit")

    def test_reddit_lists_ten_and_counts_the_rest(self, chat, monkeypatch):
        posts = [_post(i, snippet="s" * 200 if i == 1 else None) for i in range(1, 13)]
        search = AsyncMock(return_value=posts)
        monkeypatch.setattr(reddit_search, "search_subreddits", search)
        text = chat("/scan reddit agents --subs a,b --since 3d")
        assert text.startswith("Reddit scan: agents (12 results)")
        assert "10. post 10" in text and "post 11" not in text
        assert "... and 2 more" in text
        assert "   " + "s" * 120 + "\n" in text  # snippet clipped to 120
        args, kwargs = search.await_args
        assert args == ("agents", ["a", "b"])
        assert kwargs["since"] == "3d"

    def test_reddit_no_results_and_upstream_error(self, chat, monkeypatch):
        monkeypatch.setattr(
            reddit_search, "search_subreddits", AsyncMock(return_value=[])
        )
        assert chat("/scan reddit agents --subs a").startswith("No Reddit results")
        monkeypatch.setattr(
            reddit_search,
            "search_subreddits",
            AsyncMock(side_effect=UpstreamError("429 from reddit")),
        )
        assert (
            chat("/scan reddit agents --subs a")
            == "Reddit search unavailable: 429 from reddit"
        )

    def test_github_repos_with_star_filter(self, chat, monkeypatch):
        search = AsyncMock(return_value=[_repo(1, "a tool"), _repo(2)])
        monkeypatch.setattr(github_search, "search_repos", search)
        text = chat("/scan github mcp server --stars 7")
        assert text.startswith("GitHub scan (repos): mcp server (2 results)")
        assert "o/r1 (11*)\n   a tool" in text
        assert "o/r2 (12*)\n   No description" in text
        assert search.await_args.kwargs["min_stars"] == 7

    def test_github_bad_star_count_falls_back_to_zero(self, chat, monkeypatch):
        search = AsyncMock(return_value=[_repo(1)])
        monkeypatch.setattr(github_search, "search_repos", search)
        chat("/scan github mcp --stars lots")
        assert search.await_args.kwargs["min_stars"] == 0

    def test_github_issues(self, chat, monkeypatch):
        monkeypatch.setattr(
            github_search, "search_issues", AsyncMock(return_value=[_issue(4)])
        )
        text = chat("/scan github flaky test --kind issues")
        assert text.startswith("GitHub scan (issues): flaky test (1 results)")
        assert "1. issue 4\n   https://github.com/o/r/issues/4" in text

    def test_github_usage_empty_and_upstream(self, chat, monkeypatch):
        assert chat("/scan github --kind issues").startswith("Usage: /scan github")
        monkeypatch.setattr(github_search, "search_repos", AsyncMock(return_value=[]))
        assert chat("/scan github mcp") == "No GitHub results for 'mcp'"
        monkeypatch.setattr(
            github_search,
            "search_repos",
            AsyncMock(side_effect=UpstreamError("rate limit")),
        )
        assert chat("/scan github mcp") == "GitHub search unavailable: rate limit"

    def test_github_traffic_without_projects_or_unknown_slug(self, chat):
        assert chat("/scan github traffic").startswith("No projects tracked")
        assert chat("/scan github traffic ghost") == "Project not found: ghost"

    def test_github_traffic_unavailable_names_the_cause(self, chat, monkeypatch):
        brain_db.upsert_project("skrills", "athola", "skrills", [], [])
        monkeypatch.setattr(
            github_search, "get_self_traffic", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            github_search, "is_configured", AsyncMock(return_value=False)
        )
        assert "needs GITHUB_PAT" in chat("/scan github traffic")

    def test_web_usage_result_and_failures(self, chat, monkeypatch):
        assert chat("/scan web") == "Usage: /scan web <prompt>"
        llm = AsyncMock(return_value="Found three posts.")
        monkeypatch.setattr(llm_proxy, "_llm_call", llm)
        assert chat("/scan web new mcp servers") == "Found three posts."
        assert llm.await_args.kwargs["web_search"] is True
        llm.return_value = None
        assert chat("/scan web x") == (
            "Web search unavailable: no search tier answered."
        )
        llm.side_effect = UpstreamError("all tiers down")
        assert chat("/scan web x") == "Web search unavailable: all tiers down"

    def test_unknown_scan_shows_usage(self, chat):
        assert chat("/scan hn agents").startswith("Usage:\n  /scan reddit")

    def test_handler_exception_is_a_reply_not_a_500(self, chat, monkeypatch):
        monkeypatch.setattr(
            reddit_search, "search_subreddits", AsyncMock(side_effect=KeyError("x"))
        )
        assert chat("/scan reddit agents --subs a").startswith("Error:")


class TestProjectsCommand:
    def test_empty_list(self, chat):
        assert chat("/projects").startswith("No projects tracked")

    def test_add_show_list_remove(self, chat):
        text = chat(
            "/projects add tool acme/tool --topics mcp,agents --subs ClaudeAI "
            '--posture "helpful first"'
        )
        assert text == "Project added: tool (acme/tool)"
        project = brain_db.get_project("tool")
        assert (project["owner"], project["repo"]) == ("acme", "tool")
        assert project["posture"] == "helpful first"
        shown = chat("/projects show tool")
        assert "Repo: acme/tool" in shown and "mcp" in shown
        listing = chat("/projects list")
        assert "tool — acme/tool" in listing and "subs: ClaudeAI" in listing
        assert chat("/projects rm tool") == "Removed project: tool"
        assert brain_db.get_project("tool") is None

    @pytest.mark.parametrize(
        ("msg", "reply"),
        [
            ('/projects add tool "acme/tool', "Parse error"),
            ("/projects add tool", "Usage: /projects add"),
            ("/projects add tool acme-tool", "Format: owner/repo"),
            ("/projects show ghost", "Project not found: ghost"),
            ("/projects rm ghost", "Project not found: ghost"),
            ("/projects frobnicate", "Usage:\n  /projects list"),
        ],
    )
    def test_bad_input(self, chat, msg, reply):
        assert chat(msg).startswith(reply)
        assert brain_db.list_projects() == []


class TestTaskCommand:
    def test_list_shows_active_queued_and_recent(self, chat):
        active = brain_db.create_task("index the brain")
        brain_db.claim_task(active["id"])
        brain_db.create_task("draft the post", source="user")
        done = brain_db.create_task("check the feeds")
        brain_db.claim_task(done["id"])
        brain_db.complete_task(done["id"], "all feeds healthy")
        text = chat("!tasks")
        assert text.startswith("Task Queue (1 pending, 1 active, 1 done)")
        assert text.index("NOW PROCESSING:") < text.index("index the brain")
        assert text.index("QUEUED:") < text.index("draft the post")
        assert "RECENT:" in text and "all feeds healthy" in text

    def test_empty_queue_and_empty_tail(self, chat):
        assert chat("!tasks").startswith("Task queue is empty")
        assert chat("!task tail") == "No finished tasks yet."

    def test_tail_is_newest_first(self, chat):
        for desc in ("first job", "second job"):
            t = brain_db.create_task(desc)
            brain_db.claim_task(t["id"])
            brain_db.complete_task(t["id"], f"{desc} done")
        text = chat("!task tail")
        assert text.startswith("Recent Tasks (last 2 finished)")
        assert text.index("second job") < text.index("first job")

    def test_cancel_and_priority(self, chat):
        t = brain_db.create_task("tidy the queue")
        assert chat(f"!task priority {t['id']} 9") == (
            f"Usage: !task priority {t['id']} <1-5>"
        )
        assert chat(f"!task priority {t['id']} 1") == (
            f"Task #{t['id']} priority set to 1."
        )
        assert brain_db.get_task(t["id"])["priority"] == 1
        assert chat(f"!task cancel {t['id']}") == f"Task #{t['id']} cancelled."
        assert brain_db.get_task(t["id"])["status"] == "cancelled"
        assert chat("!task cancel nope") == "Task #nope not found."

    def test_cancel_keeps_a_finished_result(self, chat):
        t = brain_db.create_task("check the feeds")
        brain_db.claim_task(t["id"])
        brain_db.complete_task(t["id"], "all feeds healthy")
        assert chat(f"!task cancel {t['id']}") == (
            f"Task #{t['id']} already completed."
        )
        assert brain_db.get_task(t["id"])["status"] == "completed"

    def test_create_failure_is_reported(self, chat, monkeypatch):
        monkeypatch.setattr(
            brain_db, "create_task", MagicMock(side_effect=RuntimeError("disk full"))
        )
        assert chat("!task write the report") == "Failed to queue task: disk full"


class TestTierCommand:
    def test_shows_last_used_tier(self, chat, monkeypatch):
        monkeypatch.setattr(llm_proxy, "_last_tier_used", "zai-direct")
        monkeypatch.setattr(
            llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=None)
        )
        assert "Last used: zai-direct" in chat("!tier")


# ─── research_router: marketing scan branches ───────────────────────


def _scan(description: str):
    return asyncio.run(research_router._try_marketing_scan(description, brain_db))


class TestMarketingScanRouting:
    def test_unknown_project_falls_through(self):
        assert _scan("Scan reddit for ghost: weekly") is None
        assert _scan("Scan github for ghost: weekly") is None

    def test_reddit_scan_without_results(self, monkeypatch):
        brain_db.upsert_project("tool", "acme", "tool", ["mcp"], ["ClaudeAI"])
        search = AsyncMock(return_value=[])
        monkeypatch.setattr(reddit_search, "search_subreddits", search)
        assert _scan("Scan reddit for tool") == (
            "Reddit scan for tool: no new relevant posts found."
        )
        # No search terms: the topics make the query.
        assert search.await_args.args == ("mcp", ["ClaudeAI"])

    def test_reddit_scan_marks_only_shown_posts_seen(self, monkeypatch):
        brain_db.upsert_project(
            "tool", "acme", "tool", [], ["ClaudeAI"], search_terms=["a", "b"]
        )
        posts = [_post(i) for i in range(1, 11)]
        search = AsyncMock(return_value=posts)
        monkeypatch.setattr(reddit_search, "search_subreddits", search)
        text = _scan("Scan reddit for tool")
        assert text.startswith("Reddit scan for tool (10 results)")
        assert search.await_args.args[0] == "a OR b"
        assert brain_db.is_seen("reddit_post", "p8", "tool")
        assert not brain_db.is_seen("reddit_post", "p9", "tool")

    def test_github_scan_lists_repos_and_issues(self, monkeypatch):
        brain_db.upsert_project("tool", "acme", "tool", ["mcp"], [])
        monkeypatch.setattr(
            github_search, "search_repos", AsyncMock(return_value=[_repo(1, "x")])
        )
        monkeypatch.setattr(
            github_search, "search_issues", AsyncMock(return_value=[_issue(3)])
        )
        text = _scan("Scan github for tool")
        assert text.startswith("GitHub scan for tool (1 repos, 1 issues)")
        assert "o/r1 (11*) — x" in text and "issue 3 — o/r#3" in text
        assert brain_db.is_seen("github_repo", "1", "tool")

    def test_traffic_without_pat_is_an_answer(self, monkeypatch):
        monkeypatch.setattr(
            github_search, "get_self_traffic", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            github_search, "is_configured", AsyncMock(return_value=False)
        )
        text = _scan("Weekly traffic snapshot for acme/tool")
        assert "needs GITHUB_PAT" in text

    def test_traffic_api_failure_with_pat_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(
            github_search, "get_self_traffic", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            github_search, "is_configured", AsyncMock(return_value=True)
        )
        with pytest.raises(UpstreamError, match="API call failed"):
            _scan("Weekly traffic snapshot for acme/tool")

    def test_awesome_list_watch_goes_to_the_llm(self):
        assert _scan("Awesome-list watch: tool not yet listed") is None

    def test_unmatched_description(self):
        assert _scan("Write the weekly summary") is None
