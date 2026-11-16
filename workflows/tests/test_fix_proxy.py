"""Regression tests for the PR #11 review fixes in the LLM proxy (cluster K1)."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import auth
import llm_proxy

TOKEN = "k1-test-token"
GOOD = {"Authorization": f"Bearer {TOKEN}"}


def _chat(client: TestClient, text: str):
    return client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": text}]},
        headers=GOOD,
    )


def _reply(resp) -> str:
    return resp.json()["choices"][0]["message"]["content"]


# ─── #18 / B1: !persona and !learn run in-process ────────────────────


class _Loopback(httpx.AsyncClient):
    """An AsyncClient wired to the workflows app in-process."""

    def __init__(self, *args, **kwargs):
        import app as app_module

        kwargs["transport"] = httpx.ASGITransport(app=app_module.app)
        super().__init__(*args, **kwargs)


@pytest.fixture()
def authed_app(monkeypatch):
    """The real app with the real bearer check (conftest's bypass undone).

    Plain httpx clients are pointed at the same app in-process, so a
    loopback HTTP self-call (the old implementation) reaches the real auth
    dependency instead of a closed port: reverting the fix fails with 401.
    """
    import app as app_module

    app = app_module.app
    saved = app.dependency_overrides.pop(auth.require_token, None)
    monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(httpx, "AsyncClient", _Loopback)
    yield TestClient(app), app_module.brain_db
    if saved is not None:
        app.dependency_overrides[auth.require_token] = saved


def _learnings(db, content: str) -> list[dict]:
    return [r for r in db.list_learnings() if r["content"] == content]


class TestPersonaInProcess:
    def test_persona_feedback_queues_learning(self, authed_app):
        client, db = authed_app
        r = _chat(client, "!persona Lead with the recommendation in one line.")
        assert r.status_code == 200
        rows = _learnings(db, "Lead with the recommendation in one line.")
        assert rows, _reply(r)
        assert rows[0]["status"] == "pending"
        assert rows[0]["source"] == "feedback"
        assert "queued" in _reply(r).lower()

    def test_learn_alias_queues_learning(self, authed_app):
        client, db = authed_app
        r = _chat(client, "!learn Keep status replies under three lines.")
        assert r.status_code == 200
        assert _learnings(db, "Keep status replies under three lines."), _reply(r)

    def test_target_default_matches_api(self, authed_app):
        """Chat feedback lands under the same heading as a bare API proposal."""
        from persona_api import Proposal

        client, db = authed_app
        _chat(client, "!persona Prefer plain words over jargon.")
        rows = _learnings(db, "Prefer plain words over jargon.")
        assert rows
        assert rows[0]["target"] == Proposal.model_fields["target"].default

    def test_reflect_queues_reflection(self, authed_app):
        client, db = authed_app

        async def fake_llm(prompt, max_tokens=100, web_search=False):
            return "Open with the answer, then the detail."

        with patch.object(llm_proxy, "_llm_call", fake_llm):
            r = _chat(client, "!persona reflect")
        assert r.status_code == 200
        rows = _learnings(db, "Open with the answer, then the detail.")
        assert rows, _reply(r)
        assert rows[0]["source"] == "reflect"
        assert "reflection" in _reply(r).lower()

    def test_invalid_feedback_says_why(self, authed_app):
        client, db = authed_app
        r = _chat(client, "!persona " + "x" * 5000)
        assert r.status_code == 200
        assert "not queued" in _reply(r).lower()

    @pytest.mark.parametrize("text", ["!personality test", "!learning plan"])
    def test_longer_word_is_not_the_command(self, authed_app, text):
        """Only the whole first word triggers the command (K2 S6/S7 class)."""
        client, db = authed_app
        before = len(db.list_learnings())
        with (
            patch.object(llm_proxy, "_handle_persona_command", AsyncMock()) as handler,
            _balance(50.0),
        ):
            with patch.object(
                llm_proxy, "_call_provider", AsyncMock(return_value=_ok())
            ):
                _chat(client, text)
        assert not handler.called
        assert len(db.list_learnings()) == before

    def test_reflect_failure_is_reported(self, authed_app, monkeypatch):
        import persona_api

        def boom(profile_name):
            raise RuntimeError("generator exploded")

        monkeypatch.setattr(persona_api, "_run_reflection", boom)
        client, _db = authed_app
        r = _chat(client, "!persona reflect")
        assert r.status_code == 200
        assert "failed" in _reply(r).lower()


# ─── Shared helpers for the tier tests ───────────────────────────────

BODY = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(llm_proxy, "_balance_status", {"ok_at": None, "error": None})


def _ok(text: str = "Here is a proper answer to the question.") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


def _balance(value):
    return patch.object(
        llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=value)
    )


def _text(resp) -> str:
    return json.loads(bytes(resp.body))["choices"][0]["message"]["content"]


# ─── #30: an unknown balance must not unlock !paid ───────────────────

FLOOR = llm_proxy.OPENROUTER_BALANCE_FLOOR


class TestPaidFloor:
    @pytest.mark.parametrize("balance", [None, 4.0, FLOOR])
    def test_paid_denied_when_unknown_or_at_floor(self, keys, balance):
        call = AsyncMock(return_value=_ok())
        with _balance(balance), patch.object(llm_proxy, "_call_provider", call):
            resp = asyncio.run(llm_proxy._handle_non_streaming(BODY, "openrouter-paid"))
        assert not call.called
        text = _text(resp)
        assert "balance" in text.lower()
        # The user already asked for paid: don't tell them to send !paid.
        assert "!paid" not in text

    def test_paid_allowed_above_floor(self, keys):
        call = AsyncMock(return_value=_ok())
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            resp = asyncio.run(llm_proxy._handle_non_streaming(BODY, "openrouter-paid"))
        assert call.called
        assert json.loads(bytes(resp.body))["_clawrange_tier"] == "openrouter-paid"


# ─── #23: background :online search obeys the floor and the breaker ──


class TestSearchGuard:
    @pytest.mark.parametrize("balance", [None, 3.0, FLOOR])
    def test_search_denied_when_unknown_or_at_floor(self, keys, balance):
        call = AsyncMock(return_value=_ok())
        with _balance(balance), patch.object(llm_proxy, "_call_provider", call):
            assert asyncio.run(llm_proxy._llm_call("q", web_search=True)) is None
        assert not call.called

    def test_search_allowed_above_floor(self, keys):
        call = AsyncMock(return_value=_ok("Found it."))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            assert asyncio.run(llm_proxy._llm_call("q", web_search=True)) == "Found it."

    def test_search_failures_trip_the_breaker(self, keys):
        call = AsyncMock(return_value=httpx.Response(500, text="boom"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            for _ in range(llm_proxy.CIRCUIT_FAILURE_THRESHOLD + 2):
                asyncio.run(llm_proxy._llm_call("q", web_search=True))
        assert call.call_count == llm_proxy.CIRCUIT_FAILURE_THRESHOLD

    def test_search_timeout_counts_as_failure(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "LLM_CALL_DEADLINE", 0.1)

        async def hang(*args):
            await asyncio.sleep(5)

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", hang):
            assert asyncio.run(llm_proxy._llm_call("q", web_search=True)) is None
        assert llm_proxy._circuit_state[llm_proxy.SEARCH_TIER]["failures"] == 1


# ─── #32: a hung tier must not use up the whole background deadline ──


def _free_tiers() -> list[dict]:
    return [
        t
        for t in llm_proxy.CONFIG["tiers"]
        if not (t.get("paid") or t["name"] == "openrouter-paid")
    ]


class TestBackgroundAttemptTimeout:
    def test_attempt_timeout_leaves_room_for_every_free_tier(self):
        assert (
            llm_proxy.BACKGROUND_ATTEMPT_TIMEOUT * len(_free_tiers())
            < llm_proxy.LLM_CALL_DEADLINE
        )

    def test_hung_first_tier_fails_over(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "LLM_CALL_DEADLINE", 2.0)
        monkeypatch.setattr(llm_proxy, "BACKGROUND_ATTEMPT_TIMEOUT", 0.2)

        async def first_hangs(provider, models, body, api_key):
            if provider == "openrouter":
                await asyncio.sleep(5)
            return _ok("from zai")

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", first_hangs):
            assert asyncio.run(llm_proxy._llm_call("q")) == "from zai"
        assert llm_proxy._circuit_state["openrouter-free"]["failures"] == 1

    def test_overall_deadline_records_the_inflight_tier(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "LLM_CALL_DEADLINE", 0.2)

        async def hang(*args):
            await asyncio.sleep(5)

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", hang):
            assert asyncio.run(llm_proxy._llm_call("q")) is None
        assert llm_proxy._circuit_state["openrouter-free"]["failures"] == 1

    def test_chat_tier_timeout_records_failure(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "PROVIDER_TIMEOUT", 0.1)

        async def hang(*args):
            await asyncio.sleep(2)
            return _ok()

        tier = llm_proxy.CONFIG["tiers"][1]
        with patch.object(llm_proxy, "_call_provider", hang):
            resp, rl = asyncio.run(llm_proxy._try_single_tier(tier, BODY))
        assert resp is None and rl is None
        assert llm_proxy._circuit_state[tier["name"]]["failures"] == 1


# ─── #26: failures are logged and reported as what they were ─────────


class TestFailureReporting:
    def test_llm_call_logs_tier_status_and_body(self, keys, caplog):
        caplog.set_level(logging.INFO, logger="clawrange.llm_proxy")
        call = AsyncMock(return_value=httpx.Response(503, text="upstream overloaded"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            assert asyncio.run(llm_proxy._llm_call("q")) is None
        log = caplog.text
        assert "openrouter-free" in log and "zai-direct" in log
        assert "503" in log and "upstream overloaded" in log

    def test_llm_call_logs_empty_reply(self, keys, caplog):
        caplog.set_level(logging.INFO, logger="clawrange.llm_proxy")
        call = AsyncMock(return_value=_ok(""))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            assert asyncio.run(llm_proxy._llm_call("q")) is None
        assert "empty" in caplog.text

    def test_llm_call_logs_skipped_tier(self, monkeypatch, caplog):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        caplog.set_level(logging.INFO, logger="clawrange.llm_proxy")
        assert asyncio.run(llm_proxy._llm_call("q")) is None
        assert "zai-direct" in caplog.text and "ZAI_API_KEY" in caplog.text

    def test_exhausted_reply_names_the_real_failures(self, keys):
        call = AsyncMock(return_value=httpx.Response(500, text="boom"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            text = _text(asyncio.run(llm_proxy._handle_non_streaming(BODY)))
        assert "HTTP 500" in text
        assert "rate-limited" not in text
        # Still a history poison marker, so the reply is never fed back.
        assert "temporarily on pause" in text

    def test_exhausted_reply_when_rate_limited(self, keys):
        call = AsyncMock(return_value=httpx.Response(429, text="slow down"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            text = _text(asyncio.run(llm_proxy._handle_non_streaming(BODY)))
        assert "rate-limited" in text

    def test_chat_reply_lists_reasons_end_to_end(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
        call = AsyncMock(return_value=httpx.Response(404, text="no such model"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            import app as app_module

            r = _chat(TestClient(app_module.app), "hello")
        assert "HTTP 404" in _reply(r)

    def test_background_notify_logs_every_event(self, caplog):
        caplog.set_level(logging.INFO, logger="clawrange.llm_proxy")
        llm_proxy._background_notify("zai-direct", "*Error* on `zai-direct` (HTTP 500)")
        llm_proxy._background_notify("zai-direct", "*Error* on `zai-direct` (HTTP 502)")
        assert "HTTP 500" in caplog.text and "HTTP 502" in caplog.text


# ─── #31: history cleanup never orphans a tool result ────────────────


class TestToolCallHistory:
    def test_assistant_tool_call_turn_survives_cleanup(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
        preamble = "Let me search the web for that first, then I'll gather context."
        assert llm_proxy._is_non_answer(preamble)  # would be stripped as a deferral
        messages = [
            {"role": "user", "content": "What changed in the release?"},
            {
                "role": "assistant",
                "content": preamble,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "release notes"},
            {"role": "user", "content": "So?"},
        ]
        sent: list[list[dict]] = []

        async def capture(provider, models, body, api_key):
            sent.append(body["messages"])
            return _ok()

        import app as app_module

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", capture):
            r = TestClient(app_module.app).post(
                "/v1/chat/completions", json={"messages": messages}, headers=GOOD
            )
        assert r.status_code == 200
        assert sent
        roles = [m["role"] for m in sent[0]]
        assert roles[:4] == ["user", "assistant", "tool", "user"]
        assert sent[0][1].get("tool_calls")
