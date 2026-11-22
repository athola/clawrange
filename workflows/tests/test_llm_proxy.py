"""Tests for the LLM proxy with three-tier fallback."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import digest
import heartbeat
import llm_proxy
import research_router
import telegram
from tests.testapp import app, brain_db

client = TestClient(app)

CHAT_BODY = {
    "messages": [{"role": "user", "content": "hello"}],
}

SUCCESSFUL_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
}

FAKE_ENV = {
    "OPENROUTER_API_KEY": "test-key",
    "ZAI_API_KEY": "test-key",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "PROXY_AUTH_TOKEN": "test-token",
}

AUTH_HEADER = {"Authorization": "Bearer test-token"}


def _mock_response(
    status_code: int = 200,
    body: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    content = json.dumps(body or SUCCESSFUL_RESPONSE).encode()
    return httpx.Response(
        status_code=status_code, content=content, headers=headers or {}
    )


def _take_digest(proxy, *args, **kwargs):
    """Drain the digest the way the heartbeat does (take_ex + commit),
    treating every message as delivered."""
    messages, summary, owners, lines = proxy._digest_take_ex(*args, **kwargs)
    proxy._digest_commit(lines, owners, set())
    return messages, summary


def _plan_messages(proxy, lines):
    """One Telegram message per buffered item (the digest plan's messages)."""
    return proxy._digest_plan(lines)[0]


def _reset_state():
    """Reset all mutable proxy state between tests."""
    import llm_proxy

    llm_proxy._circuit_state.clear()
    llm_proxy._notification_last_sent.clear()
    digest._digest_state["lines"] = []
    digest._digest_state["last_flush"] = time.time()


# ─── Provider mock helpers ────────────────────────────────────────


async def _all_succeed(provider, models, body, api_key):
    return _mock_response(200)


async def _openrouter_rate_limited(provider, models, body, api_key):
    if provider == "openrouter":
        return _mock_response(429, {"error": "rate limited"})
    return _mock_response(200)


async def _all_rate_limited(provider, models, body, api_key):
    return _mock_response(429, {"error": "rate limited"})


async def _openrouter_502(provider, models, body, api_key):
    if provider == "openrouter":
        return _mock_response(502, {"error": "bad gateway"})
    return _mock_response(200)


# ─── Auth ─────────────────────────────────────────────────────────


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestProxyAuth:
    """Verify auth is enforced."""

    def test_rejects_missing_auth(self):
        r = client.post("/v1/chat/completions", json=CHAT_BODY)
        assert r.status_code == 401

    def test_rejects_wrong_token(self):
        r = client.post(
            "/v1/chat/completions",
            json=CHAT_BODY,
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_all_succeed)
    def test_accepts_valid_token(self, _mock_call, _mock_bg):
        _reset_state()
        r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200


# ─── Tier Fallback ───────────────────────────────────────────────


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestProxyTierFallback:
    """Verify the proxy tries tiers in order and falls back on errors."""

    def setup_method(self):
        _reset_state()

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_all_succeed)
    def test_all_succeed_returns_a_free_tier(self, mock_call, mock_bg):
        """With concurrent dispatch, any available free tier can win the race."""
        r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200
        assert r.json()["_clawrange_tier"] in ("openrouter-free", "zai-direct")
        # No error/rate-limit notifications — only tier-change logs are OK
        for call in mock_bg.call_args_list:
            assert "Error" not in call.args[1]
            assert "Rate limited" not in call.args[1]

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_openrouter_rate_limited)
    def test_default_routes_to_zai_skipping_openrouter(self, mock_call, mock_bg):
        """Default forces zai-direct; openrouter is never tried."""
        r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200
        assert r.json()["_clawrange_tier"] == "zai-direct"

    @patch("llm_proxy._background_notify")
    @patch("telegram.notify", new_callable=AsyncMock)
    @patch("llm_proxy._check_openrouter_balance", new_callable=AsyncMock)
    @patch("llm_proxy._call_provider", side_effect=_all_succeed)
    def test_zero_balance_routes_around_openrouter(
        self, mock_call, mock_balance, mock_tg, _mock_bg
    ):
        """A zero/negative balance blocks every OpenRouter model (free
        included) with 402s — skip the provider entirely so zai-direct
        carries traffic instead of stalling."""
        import llm_proxy

        mock_balance.return_value = -1.28
        llm_proxy._notification_last_sent.clear()
        try:
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            assert r.json()["_clawrange_tier"] == "zai-direct"
            providers = [c.args[0] for c in mock_call.call_args_list]
            assert "openrouter" not in providers
        finally:
            llm_proxy._notification_last_sent.clear()

    @patch("llm_proxy._background_notify")
    @patch("telegram.notify", new_callable=AsyncMock)
    @patch("llm_proxy._check_openrouter_balance", new_callable=AsyncMock)
    @patch("llm_proxy._call_provider", side_effect=_all_succeed)
    def test_balance_guard_notify_throttled(
        self, mock_call, mock_balance, mock_tg, _mock_bg
    ):
        """The skip notice fires at most once per debounce window, not
        once per skipped tier per request."""
        import llm_proxy

        mock_balance.return_value = -1.28
        llm_proxy._notification_last_sent.clear()
        try:
            for _ in range(2):
                r = client.post(
                    "/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER
                )
                assert r.status_code == 200
            guard_calls = [
                c
                for c in mock_tg.call_args_list
                if "Balance guard" in (c.args[0] if c.args else "")
            ]
            assert len(guard_calls) <= 1
        finally:
            llm_proxy._notification_last_sent.clear()

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_all_rate_limited)
    def test_all_tiers_exhausted_returns_synthetic(self, mock_call, mock_bg):
        """When all tiers are rate limited, return a friendly synthetic response."""
        r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        # The rate-limit reply, not the generic "no tier answered" one: both
        # start with "temporarily on pause".
        assert "rate-limited right now" in content
        assert "no tier answered" not in content

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_openrouter_502)
    def test_tier1_500_falls_back(self, mock_call, _mock_bg):
        r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200
        assert r.json()["_clawrange_tier"] == "zai-direct"

    @patch("llm_proxy._background_notify")
    def test_synthetic_response_stripped_from_history(self, _mock_bg):
        """Synthetic 'on pause' messages in chat history must be stripped so the LLM
        doesn't parrot them back."""
        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": (
                    "I'm temporarily on pause \u2014 the free API tiers are "
                    "rate-limited "
                    "right now. Send !paid or !claude to use the paid tier, "
                    "or try again later."
                ),
            },
            {"role": "user", "content": "try again"},
        ]
        mock_caller = AsyncMock(return_value=_mock_response(200))
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json={"messages": messages},
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200
            sent_messages = mock_caller.call_args.args[2]["messages"]
            # 2 user messages (assistant stripped) + trailing system
            # (anti-hallucination)
            assert len(sent_messages) == 3
            assert sent_messages[-1]["role"] == "system"
            assert not any(m["role"] == "assistant" for m in sent_messages)

    @patch("llm_proxy._background_notify")
    def test_hallucinated_tool_calls_stripped_from_history(self, _mock_bg):
        """Assistant messages with hallucinated <exec> tags must be stripped."""
        messages = [
            {"role": "user", "content": "research some skills"},
            {
                "role": "assistant",
                "content": (
                    'Let me research that.\n<exec command="ls /app/docs/</exec>"'
                ),
            },
            {"role": "user", "content": "try again"},
        ]
        mock_caller = AsyncMock(return_value=_mock_response(200))
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json={"messages": messages},
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200
            sent_messages = mock_caller.call_args.args[2]["messages"]
            assert len(sent_messages) == 3
            assert sent_messages[-1]["role"] == "system"
            assert not any(m["role"] == "assistant" for m in sent_messages)

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider")
    def test_reasoning_content_used_when_content_empty(self, mock_call, _mock_bg):
        """GLM 5.1 reasoning models put output in reasoning_content — proxy merges
        it."""
        reasoning_body = {
            "id": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": (
                            "The user said hello, so I should greet them."
                        ),
                    }
                }
            ],
        }
        mock_call.return_value = _mock_response(200, reasoning_body)
        r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        assert "greet them" in content
        assert "reasoning_content" not in data["choices"][0]["message"]

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider")
    def test_reasoning_content_used_when_content_null(self, mock_call, _mock_bg):
        """Z.AI sometimes returns content: null — proxy merges reasoning_content."""
        reasoning_body = {
            "id": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "Analyzing the request carefully.",
                    }
                }
            ],
        }
        mock_call.return_value = _mock_response(200, reasoning_body)
        r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert "Analyzing" in content

    @patch("llm_proxy._background_notify")
    def test_colon_suffixed_tool_call_xml_is_stripped(self, _mock_bg):
        """Free-tier models hallucinate <tool_call:NAME>...</tool_call:NAME>
        when they get OpenAI tool schemas but the upstream doesn't support
        function calling. The closing tag is colon-suffixed too, which the
        original regex missed — so the entire block leaked to the user."""
        from llm_proxy import _sanitize_response

        raw = (
            "Let me gather real data before documenting anything.\n\n"
            "<tool_call:paste>\n"
            "<tool_name>web_fetch</tool_name>\n"
            "http://msp-workflows:5678/healthz\n"
            "</tool_call:paste>\n\n"
            "<tool_call:paste>\n"
            "<tool_name>web_fetch</tool_name>\n"
            "http://msp-workflows:5678/tier\n"
            "</tool_call:paste>"
        )
        cleaned = _sanitize_response(raw)
        assert "tool_call" not in cleaned
        assert "msp-workflows" not in cleaned
        assert "Let me gather real data" in cleaned

    @patch("llm_proxy._background_notify")
    def test_colon_suffixed_tool_call_preserves_text_between_blocks(self, _mock_bg):
        """The proper fix matches both <tool_call:NAME>...</tool_call:NAME>
        so prose between hallucinated blocks survives. The catch-all
        unclosed-tag fallback alone would nuke everything after the first
        opener — including a second sentence the user actually wants."""
        from llm_proxy import _sanitize_response

        raw = (
            "Checking health.\n"
            "<tool_call:paste><tool_name>web_fetch</tool_name>"
            "http://msp-workflows:5678/healthz</tool_call:paste>\n"
            "Now checking balance.\n"
            "<tool_call:paste><tool_name>web_fetch</tool_name>"
            "http://msp-workflows:5678/tier</tool_call:paste>\n"
            "Done."
        )
        cleaned = _sanitize_response(raw)
        assert "Checking health" in cleaned
        assert "Now checking balance" in cleaned
        assert "Done" in cleaned
        assert "tool_call" not in cleaned

    @patch("llm_proxy._background_notify")
    def test_hallucinated_tool_call_response_falls_to_next_tier(self, _mock_bg):
        """When a tier returns *only* hallucinated <tool_call:NAME> XML
        (the John-117 ops-task failure mode), sanitization empties the
        response and the race must skip to the next tier — not return the
        empty string to the user."""
        hallucinated_body = {
            "id": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "<tool_call:paste>\n"
                            "<tool_name>web_fetch</tool_name>\n"
                            "http://msp-workflows:5678/healthz\n"
                            "</tool_call:paste>"
                        ),
                    }
                }
            ],
        }
        clean_body = {
            "id": "test",
            "object": "chat.completion",
            "choices": [
                {"message": {"role": "assistant", "content": "Stack is healthy."}}
            ],
        }

        async def mock_call(provider, models, body, api_key):
            if provider == "openrouter":
                return _mock_response(200, hallucinated_body)
            return _mock_response(200, clean_body)

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            data = r.json()
            assert data["_clawrange_tier"] == "zai-direct"
            assert "Stack is healthy" in data["choices"][0]["message"]["content"]

    @patch("llm_proxy._background_notify")
    def test_anti_hallucination_injected_even_when_tools_present(self, _mock_bg):
        """The original gate skipped anti-hallucination when body.tools
        existed, trusting the model to use real function calls. Free-tier
        upstreams fake tool calls instead — the rule must always run, with
        wording adapted to require the OpenAI tool-call format when tools
        are available."""
        mock_caller = AsyncMock(return_value=_mock_response(200))
        body_with_tools = {
            "messages": [{"role": "user", "content": "check tier"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "web_fetch",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions", json=body_with_tools, headers=AUTH_HEADER
            )
            assert r.status_code == 200
            sent_messages = mock_caller.call_args.args[2]["messages"]
            assert sent_messages[-1]["role"] == "system"
            last = sent_messages[-1]["content"].lower()
            assert "tool_call" in last or "xml" in last or "bracket" in last

    @patch("llm_proxy._background_notify")
    def test_garbled_response_falls_to_next_tier(self, _mock_bg):
        """If a tier returns garbled (no-space) text, skip it and try next."""
        garbled_body = {
            "id": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Alex,Idon'tseeanyactiveresearchsub-agentsrunningrightnow."
                            "Letmecheckifthere'saresearchdocumentthatwascreated:"
                        ),
                    }
                }
            ],
        }

        async def mock_call(provider, models, body, api_key):
            if provider == "openrouter":
                return _mock_response(200, garbled_body)
            return _mock_response(200)  # zai returns clean response

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            assert r.json()["_clawrange_tier"] == "zai-direct"


# ─── Body Allowlist ──────────────────────────────────────────────


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestProxyBodyAllowlist:
    """Verify dangerous body fields are stripped."""

    def setup_method(self):
        _reset_state()

    @patch("llm_proxy._background_notify")
    def test_strips_dangerous_fields_but_keeps_tools(self, _mock_bg):
        """Dangerous fields (model override, route) are stripped.
        tools/tool_choice are allowed through for heartbeat tool use."""
        mock_caller = AsyncMock(return_value=_mock_response(200))
        with patch("llm_proxy._call_provider", mock_caller):
            body = {
                **CHAT_BODY,
                "model": "gpt-4o",
                "tools": [{"type": "function"}],
                "tool_choice": "auto",
                "route": "fallback",
            }
            client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
            call_body = mock_caller.call_args.args[2]
            assert "tools" in call_body  # Allowed — needed for heartbeat
            assert "tool_choice" in call_body  # Allowed
            assert "route" not in call_body  # Stripped — not in allowlist
            assert "model" not in call_body  # Stripped — proxy controls model
            assert "messages" in call_body


# ─── Circuit Breaker ─────────────────────────────────────────────


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestProxyCircuitBreaker:
    """Verify circuit breaker behavior — rate limits trip immediately."""

    def setup_method(self):
        _reset_state()

    @patch("llm_proxy._background_notify")
    def test_rate_limit_trips_circuit_immediately(self, _mock_bg):
        """A single 429 should trip the circuit — no need for 3 failures.

        With concurrent racing, OpenRouter-free still serves when ZAI is tripped.
        The test verifies ZAI is NOT retried on subsequent requests."""
        call_count = {"zai": 0}

        async def mock_call(provider, models, body, api_key):
            if provider == "zai":
                call_count["zai"] += 1
                return _mock_response(429, {"error": "rate limited"})
            return _mock_response(200)

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            # First request: zai 429 → circuit trips, but openrouter-free succeeds
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            assert call_count["zai"] == 1

            # Second request: zai skipped (circuit open), openrouter-free serves
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            assert "_clawrange_tier" in r.json()  # real response, not synthetic
            assert call_count["zai"] == 1  # NOT called again

    @patch("llm_proxy._background_notify")
    def test_transient_errors_still_need_threshold(self, _mock_bg):
        """Non-rate-limit errors should require CIRCUIT_FAILURE_THRESHOLD failures.

        With concurrent racing, OpenRouter-free serves while ZAI accumulates
        failures. The test verifies ZAI is still retried (circuit not yet open)."""
        call_count = {"zai": 0}

        async def mock_call(provider, models, body, api_key):
            if provider == "zai":
                call_count["zai"] += 1
                return _mock_response(502, {"error": "bad gateway"})
            return _mock_response(200)

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            # First request: zai 502 but openrouter-free succeeds
            client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)

            # Second request: zai still tried (circuit not yet open, below threshold)
            client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert call_count["zai"] == 2  # Still being called


# ─── Balance Guard ───────────────────────────────────────────────


class TestBalanceGuard:
    """Verify paid tier is blocked when balance is near the $10 floor."""

    def setup_method(self):
        _reset_state()

    def test_check_returns_none_when_not_configured(self):
        import asyncio

        import llm_proxy

        llm_proxy._balance_cache.clear()
        try:
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}):
                result = asyncio.run(llm_proxy._check_openrouter_balance())
            assert result is None
        finally:
            llm_proxy._balance_cache.clear()

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"})
    def test_balance_reads_credits_api(self):
        """remaining = total_credits - total_usage, straight from /credits."""
        import asyncio

        import llm_proxy

        llm_proxy._balance_cache.clear()

        mock_resp = httpx.Response(
            200,
            content=json.dumps(
                {"data": {"total_credits": 28.69, "total_usage": 5.50}}
            ).encode(),
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        try:
            with patch("llm_proxy.httpx.AsyncClient", return_value=mock_client):
                result = asyncio.run(llm_proxy._check_openrouter_balance())
                assert result == 23.19  # 28.69 - 5.50
                called_url = mock_client.get.call_args[0][0]
                assert called_url.endswith("/api/v1/credits")
        finally:
            llm_proxy._balance_cache.clear()


# ─── Tier Command Interception ───────────────────────────────────


@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestTierCommand:
    """Verify /tier command interception returns synthetic response."""

    def test_tier_command_intercepted(self):
        for cmd in [
            "/tier",
            "!tier",
            "tier status",
            "claw status",
            "clawstatus",
            "status",
            "status?",
            "what's the status?",
            "what is the status",
            "whats your status",
            "what's your status?",
            "your status",
        ]:
            r = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": cmd}]},
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200, f"Failed for command: {cmd}"
            content = r.json()["choices"][0]["message"]["content"]
            assert "Tier Status" in content, f"Missing status for command: {cmd}"

    def test_command_with_metadata_prefix(self):
        """Status command works even with OpenClaw metadata prefix."""
        msg = (
            'Context (untrusted metadata):\n```json\n{"channel": "telegram"}'
            "\n```\nclaw status"
        )
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": msg}]},
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert "Tier Status" in content

    def test_command_with_unknown_prefix(self):
        """Status command matches via last-line fallback even if metadata regex
        fails."""
        msg = "some unrecognized metadata block\nclaw status"
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": msg}]},
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert "Tier Status" in content

    def test_normal_message_not_intercepted(self):
        """A normal message should NOT trigger the tier command."""
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers=AUTH_HEADER,
        )
        content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        assert "Tier Status" not in content


@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestHelpCommand:
    """Verify !help command returns full command reference."""

    def test_help_command_intercepted(self):
        for cmd in ["!help", "/help", "help", "!commands", "/commands"]:
            r = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": cmd}]},
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200, f"Failed for command: {cmd}"
            content = r.json()["choices"][0]["message"]["content"]
            assert "ClawRange Command Reference" in content, (
                f"Missing reference for: {cmd}"
            )

    def test_help_includes_all_sections(self):
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "!help"}]},
            headers=AUTH_HEADER,
        )
        content = r.json()["choices"][0]["message"]["content"]
        assert "TASK MANAGEMENT" in content
        assert "BRAIN" in content
        assert "SYSTEM STATUS" in content
        assert "BRAIN API" in content
        assert "HEALTH" in content
        assert "!task" in content
        # No proxy handler exists for these, so help must not list them.
        assert "!recall" not in content
        assert "!remember" not in content
        assert "!tier" in content
        assert "/brain/search" in content
        assert "PERSONA" in content
        assert "!persona <feedback>" in content
        assert "!persona reflect" in content
        assert "!learn" in content

    def test_help_with_metadata_prefix(self):
        msg = "some metadata block\n!help"
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": msg}]},
            headers=AUTH_HEADER,
        )
        content = r.json()["choices"][0]["message"]["content"]
        assert "ClawRange Command Reference" in content


# ─── Notification Debouncing ─────────────────────────────────────


class TestNotificationDebouncing:
    """Verify notifications are debounced per tier."""

    def setup_method(self):
        _reset_state()

    def test_first_call_allows_notification(self):
        from llm_proxy import _should_notify

        assert _should_notify("test-tier") is True

    def test_immediate_repeat_is_debounced(self):
        from llm_proxy import _should_notify

        _should_notify("test-tier")  # first call — allowed
        assert _should_notify("test-tier") is False  # debounced

    def test_different_tiers_are_independent(self):
        from llm_proxy import _should_notify

        _should_notify("tier-a")
        assert _should_notify("tier-b") is True  # different tier, not debounced

    def test_debounce_expires(self):
        import llm_proxy

        # Relative to the clock, not 0: time.monotonic() counts from boot,
        # so on a freshly booted CI runner 0 is still inside the window.
        llm_proxy._notification_last_sent["test-tier"] = (
            time.monotonic() - llm_proxy._NOTIFICATION_DEBOUNCE_SECONDS - 1
        )
        assert llm_proxy._should_notify("test-tier") is True

    def test_debounce_expires_on_freshly_booted_host(self, monkeypatch):
        import llm_proxy

        monkeypatch.setattr(llm_proxy.time, "monotonic", lambda: 50.0)
        llm_proxy._notification_last_sent["test-tier"] = (
            50.0 - llm_proxy._NOTIFICATION_DEBOUNCE_SECONDS - 1
        )
        assert llm_proxy._should_notify("test-tier") is True


# ─── Auto-Retry ──────────────────────────────────────────────────


class TestResetSecondsParsing:
    """Verify numeric reset time extraction for auto-retry decisions."""

    def test_message_reset_reads_seconds_minutes_and_limits(self):
        from llm_proxy import _message_reset

        assert _message_reset("try again in 30 seconds") == (30, "in 30s")
        assert _message_reset("Rate limit resets in 5 minutes") == (300, "in ~5min")
        assert _message_reset("upstream provider throttled")[0] == 20
        secs, text = _message_reset("exceeded daily free limit")
        assert secs == 86400 and "24h" in text
        assert _message_reset("something went wrong") is None

    def test_get_reset_seconds_from_retry_after_header(self):
        from llm_proxy import _get_reset_seconds

        resp = httpx.Response(429, content=b"{}", headers={"retry-after": "15"})
        assert _get_reset_seconds(resp) == 15

    def test_get_reset_seconds_none_when_no_info(self):
        from llm_proxy import _get_reset_seconds

        resp = httpx.Response(429, content=b"{}")
        assert _get_reset_seconds(resp) is None


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestAutoRetry:
    """Verify proxy auto-retries when rate limit has a short reset window."""

    def setup_method(self):
        _reset_state()

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy.asyncio.sleep", new_callable=AsyncMock)
    def test_auto_retries_on_short_rate_limit(self, mock_sleep, _mock_bg):
        """When ALL free tiers 429 with short reset, proxy waits and retries."""
        attempt = {"n": 0}

        async def mock_call(provider, models, body, api_key):
            attempt["n"] += 1
            if attempt["n"] <= 2:
                # First pass: all tiers rate-limited with 5s reset
                return _mock_response(
                    429, {"error": "rate limited"}, headers={"retry-after": "5"}
                )
            # Retry pass: succeed
            return _mock_response(200)

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            assert "_clawrange_tier" in r.json()  # real response, not synthetic
            mock_sleep.assert_awaited_once_with(5)

    @patch("llm_proxy._background_notify")
    def test_no_retry_when_reset_too_long(self, _mock_bg):
        """When reset time exceeds MAX_AUTO_RETRY_WAIT, return synthetic immediately."""

        async def mock_call(provider, models, body, api_key):
            return _mock_response(
                429,
                {"error": {"message": "exceeded daily free limit"}},
            )

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            content = r.json()["choices"][0]["message"]["content"]
            assert "rate-limited right now" in content
            assert "no tier answered" not in content

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy.asyncio.sleep", new_callable=AsyncMock)
    def test_retry_fails_returns_synthetic(self, mock_sleep, _mock_bg):
        """If retry also fails, return synthetic response (no infinite loop)."""

        async def mock_call(provider, models, body, api_key):
            # Always 429 — both attempts fail
            return _mock_response(
                429, {"error": "rate limited"}, headers={"retry-after": "5"}
            )

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            content = r.json()["choices"][0]["message"]["content"]
            assert "rate-limited right now" in content
            assert "no tier answered" not in content
            # Sleep should be called once (first attempt retries, second doesn't)
            mock_sleep.assert_awaited_once()


# ─── Concurrent Dispatch + Request Deadline ─────────────────────


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestConcurrentDispatch:
    """Verify tiers are raced concurrently and deadline caps total time."""

    def setup_method(self):
        _reset_state()

    @patch("llm_proxy._background_notify")
    def test_slow_tier_loses_to_fast_tier(self, _mock_bg):
        """When one tier is slow and another is fast, the fast one wins."""
        import asyncio

        async def mock_call(provider, models, body, api_key):
            if provider == "openrouter":
                await asyncio.sleep(5)  # slow
                return _mock_response(200)
            return _mock_response(200)  # zai is fast

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            assert r.json()["_clawrange_tier"] == "zai-direct"

    @patch("llm_proxy._background_notify")
    def test_request_deadline_returns_synthetic(self, _mock_bg):
        """When all tiers exceed the request deadline, return a timeout response."""
        import asyncio

        original_deadline = __import__("llm_proxy").REQUEST_DEADLINE

        async def mock_call(provider, models, body, api_key):
            await asyncio.sleep(10)  # both tiers slow
            return _mock_response(200)

        try:
            # Set a very short deadline for the test
            __import__("llm_proxy").REQUEST_DEADLINE = 0.5
            with patch("llm_proxy._call_provider", side_effect=mock_call):
                r = client.post(
                    "/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER
                )
                assert r.status_code == 200
                content = r.json()["choices"][0]["message"]["content"]
                assert "took too long" in content
        finally:
            __import__("llm_proxy").REQUEST_DEADLINE = original_deadline

    @patch("llm_proxy._background_notify")
    def test_one_tier_fails_other_succeeds(self, _mock_bg):
        """When one tier errors and another succeeds, the success wins."""

        async def mock_call(provider, models, body, api_key):
            if provider == "openrouter":
                return _mock_response(502, {"error": "bad gateway"})
            return _mock_response(200)

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            assert r.json()["_clawrange_tier"] == "zai-direct"


# ─── Rate Limit Detection ───────────────────────────────────────


class TestRateLimitDetection:
    """Verify rate limit detection from status codes and response bodies."""

    def test_429_is_rate_limited(self):
        from llm_proxy import _is_rate_limited

        resp = httpx.Response(429, content=b'{"error": "too many"}')
        assert _is_rate_limited(resp) is True

    def test_400_with_rate_limit_message_detected(self):
        from llm_proxy import _is_rate_limited

        body = json.dumps({"error": {"message": "Rate limit exceeded for free tier"}})
        resp = httpx.Response(400, content=body.encode())
        assert _is_rate_limited(resp) is True

    def test_400_without_rate_limit_not_detected(self):
        from llm_proxy import _is_rate_limited

        body = json.dumps({"error": {"message": "Invalid request format"}})
        resp = httpx.Response(400, content=body.encode())
        assert _is_rate_limited(resp) is False

    def test_200_is_not_rate_limited(self):
        from llm_proxy import _is_rate_limited

        resp = httpx.Response(200, content=b"{}")
        assert _is_rate_limited(resp) is False

    def test_get_reset_from_retry_after_seconds(self):
        from llm_proxy import _get_reset_info

        resp = httpx.Response(429, content=b"{}", headers={"retry-after": "90"})
        result = _get_reset_info(resp)
        assert result == "in 90s"

    def test_get_reset_from_retry_after_minutes(self):
        from llm_proxy import _get_reset_info

        resp = httpx.Response(429, content=b"{}", headers={"retry-after": "3600"})
        result = _get_reset_info(resp)
        assert result == "in ~60min"

    def test_get_reset_empty_when_no_headers(self):
        from llm_proxy import _get_reset_info

        resp = httpx.Response(429, content=b"{}")
        assert _get_reset_info(resp) == ""


# ─── Response Sanitization ───────────────────────────────────────


class TestNonAnswerDetection:
    """Verify agent-hallucination / deferral responses are caught.

    Detection uses phrase-counting: 3+ deferral phrases in a short
    response = non-answer, regardless of exact wording.
    """

    def test_detects_startup_sequence(self):
        from llm_proxy import _is_non_answer

        assert _is_non_answer(
            "Let me do my full startup sequence first, then research this properly."
        )

    def test_detects_startup_properly(self):
        from llm_proxy import _is_non_answer

        assert _is_non_answer(
            "Let me do my startup properly and then give you a thorough answer."
        )

    def test_detects_read_files_and_context(self):
        from llm_proxy import _is_non_answer

        assert _is_non_answer(
            "I'll complete my startup sequence first, then address your excellent "
            "question about enhancing our setup with Socratic learning methods. "
            "Let me read the required files to understand my context."
        )

    def test_detects_catch_up_on_context(self):
        from llm_proxy import _is_non_answer

        assert _is_non_answer(
            "Hey Alex — I see you've been waiting on this. Let me do this right.\n\n"
            "First, let me catch up on my context, then I'll research what's "
            "available and give you a real answer."
        )

    def test_detects_initializing(self):
        from llm_proxy import _is_non_answer

        assert _is_non_answer("I'll initialize first, then give you a thorough answer.")

    def test_detects_research_deferral(self):
        from llm_proxy import _is_non_answer

        assert _is_non_answer(
            "I need to research this first, then I'll give you a real answer."
        )

    def test_passes_normal_response(self):
        from llm_proxy import _is_non_answer

        assert not _is_non_answer(
            "We carry Jessup and Titanium brand manufactured homes. "
            "Want me to set up a time for you to come see what's on the lot?"
        )

    def test_passes_connect_with_team(self):
        """'Let me connect you' is exempted by the customer-action check."""
        from llm_proxy import _is_non_answer

        assert not _is_non_answer(
            "Let me connect you with our finance team to go over the details."
        )

    def test_passes_reach_out(self):
        from llm_proxy import _is_non_answer

        assert not _is_non_answer(
            "Let me have someone reach out to you first, then we can go from there."
        )

    def test_passes_long_response(self):
        from llm_proxy import _is_non_answer

        text = "A" * 301
        assert not _is_non_answer(text)

    def test_passes_short_greeting(self):
        from llm_proxy import _is_non_answer

        assert not _is_non_answer("Hey there! How can I help you today?")

    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
    @patch("llm_proxy._background_notify")
    def test_non_answer_falls_to_next_tier(self, _mock_bg):
        """If one tier returns a deferral, the other tier's response wins."""
        _reset_state()

        async def mock_call(provider, models, body, api_key):
            if provider == "openrouter":
                return _mock_response(
                    200,
                    {
                        "id": "test",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": (
                                        "Let me do my full startup sequence first."
                                    ),
                                }
                            }
                        ],
                    },
                )
            return _mock_response(200)

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert r.status_code == 200
            assert r.json()["_clawrange_tier"] == "zai-direct"


class TestResponseSanitization:
    """Verify hallucinated tool-call blocks are stripped from responses."""

    def test_strips_closed_tool_blocks(self):
        from llm_proxy import _sanitize_response

        text = (
            "Let me try searching.\n"
            "[[web_search\n[query]\n8 pillars of wellness\n[/query]\n]]\n"
            "Here is what I found."
        )
        result = _sanitize_response(text)
        assert "[[web_search" not in result
        assert "[query]" not in result
        assert "Here is what I found." in result

    def test_strips_unclosed_tool_blocks(self):
        from llm_proxy import _sanitize_response

        text = (
            "Starting now.\n[[exec]\n[command]\ntimeout 30 curl example.com\n[/command]"
        )
        result = _sanitize_response(text)
        assert "[[exec" not in result
        assert "[command]" not in result
        assert "Starting now." in result

    def test_strips_write_blocks_with_content(self):
        from llm_proxy import _sanitize_response

        text = (
            "I'll save this.\n"
            "[[write]\n[filename]\n/tmp/test.md\n[/filename]\n"
            "[content]\n# Hello\nWorld\n[/content]\n"
            "Done saving."
        )
        result = _sanitize_response(text)
        assert "[[write" not in result
        assert "/tmp/test.md" not in result
        assert "Done saving." in result

    def test_preserves_normal_text(self):
        from llm_proxy import _sanitize_response

        text = "We carry Jessup and Titanium brand manufactured homes."
        assert _sanitize_response(text) == text

    def test_strips_cron_blocks(self):
        from llm_proxy import _sanitize_response

        text = "Checking cron.\n[[cron\n[action=list]\n[/cron]]\nAll set."
        result = _sanitize_response(text)
        assert "[[cron" not in result
        assert "All set." in result

    def test_detects_garbled_glm_output(self):
        from llm_proxy import _is_garbled

        garbled = (
            "Alex,Idon'tseeanyactiveresearchsub-agentsrunningrightnow."
            "Letmecheckifthere'saresearchdocumentthatwascreated:"
        )
        assert _is_garbled(garbled) is True

    def test_normal_text_not_garbled(self):
        from llm_proxy import _is_garbled

        normal = "Alex, I don't see any active research sub-agents running right now."
        assert _is_garbled(normal) is False

    def test_short_text_not_garbled(self):
        from llm_proxy import _is_garbled

        assert _is_garbled("OK") is False
        assert _is_garbled("NoSpacesHere") is False  # too short to flag

    def test_strips_subagents_xml_tags(self):
        from llm_proxy import _sanitize_response

        text = 'Checking.\n<subagents action="list"></subagents>\nDone.'
        result = _sanitize_response(text)
        assert "<subagents" not in result
        assert "Checking." in result
        assert "Done." in result

    def test_strips_subagent_bracket_blocks(self):
        from llm_proxy import _sanitize_response

        text = "Here.\n[[subagents\n[action=list]\n]]\nOK."
        result = _sanitize_response(text)
        assert "[[subagents" not in result
        assert "OK." in result

    def test_strips_malformed_exec_tag(self):
        from llm_proxy import _sanitize_response

        text = 'Let me research that.\n<exec command="ls /app/docs/</exec>"'
        result = _sanitize_response(text)
        assert "<exec" not in result
        assert "Let me research that." in result

    def test_strips_unclosed_xml_exec_tag(self):
        from llm_proxy import _sanitize_response

        text = 'Here.\n<exec command="cat /etc/passwd">\noutput line'
        result = _sanitize_response(text)
        assert "<exec" not in result
        assert "Here." in result

    def test_collapses_excessive_newlines(self):
        from llm_proxy import _sanitize_response

        text = "Before.\n\n\n\n\nAfter."
        result = _sanitize_response(text)
        assert "\n\n\n" not in result
        assert "Before.\n\nAfter." == result

    def test_strips_session_status_tags(self):
        from llm_proxy import _sanitize_response

        text = "Hello.<session_status></session_status> How are you?"
        result = _sanitize_response(text)
        assert "<session_status>" not in result
        assert "Hello." in result
        assert "How are you?" in result

    def test_strips_session_status_with_content(self):
        from llm_proxy import _sanitize_response

        text = "Hi.<session_status>idle</session_status> What can I help with?"
        result = _sanitize_response(text)
        assert "<session_status>" not in result
        assert "idle" not in result
        assert "Hi." in result

    def test_strips_thinking_tags(self):
        from llm_proxy import _sanitize_response

        text = "Sure.<thinking>I should check the CRM.</thinking> Let me look that up."
        result = _sanitize_response(text)
        assert "<thinking>" not in result
        assert "Let me look that up." in result


# ─── Persona Backstory Stripping (search-tier prompt hygiene) ─────


@pytest.fixture
def marketing_persona(monkeypatch):
    """Run as the marketing tenant: its John-117 persona carries the
    search_hygiene config, and OPERATOR_NAME names the operator. Persona
    rules are profile data, so these tests say which profile they test."""
    import tenant_profile

    monkeypatch.setenv("CLAWRANGE_PROFILE", "marketing")
    monkeypatch.setenv("OPERATOR_NAME", "Alex")
    tenant_profile._ACTIVE.clear()
    yield
    tenant_profile._ACTIVE.clear()


@pytest.mark.usefixtures("marketing_persona")
class TestPersonaBackstoryStripping:
    """When tasks route to the OpenRouter `:online` search tier, Exa runs
    web search on the full prompt. The Halo character backstory in soul.md
    pollutes that search with Halopedia / Master Chief citations. Tasks
    routed to the search tier must get a soul.md with the backstory
    sections stripped — operational sections (Marketing Mode, Research
    Mode, What You Can Do) stay so the model still understands its job."""

    def test_strips_who_you_are_section(self):
        from heartbeat import _strip_persona_backstory

        soul = (
            "You are John-117, Alex's executive assistant.\n\n"
            "## Who You Are\n"
            "- Master Chief Petty Officer John-117 — Spartan-II super-soldier\n"
            "- Born John in 2511 on Eridanus II\n\n"
            "## What You Can Do\n"
            "- Manage the task queue\n"
        )
        stripped = _strip_persona_backstory(soul)
        assert "Spartan-II" not in stripped
        assert "Master Chief Petty Officer" not in stripped
        assert "Eridanus II" not in stripped
        # Operational section preserved
        assert "Manage the task queue" in stripped

    def test_strips_your_character_section(self):
        from heartbeat import _strip_persona_backstory

        soul = (
            "## Your Character\n"
            "- Leader of Blue Team, feared by the Covenant as 'The Demon'\n"
            "- Wears MJOLNIR Powered Assault Armor, partnered with Cortana\n\n"
            "## Marketing Mode\n"
            "- Lurk and comment first\n"
        )
        stripped = _strip_persona_backstory(soul)
        assert "MJOLNIR" not in stripped
        assert "Cortana" not in stripped
        assert "Covenant" not in stripped
        # Operational section preserved
        assert "Lurk and comment first" in stripped

    def test_preserves_inline_johh_117_reference_in_operational_text(self):
        """The backstory blocks are the search-pollution source. Stray
        mentions of John-117 in operational instructions (e.g.
        'You [John-117] manage the task queue') are unavoidable but rare
        enough that Exa won't pivot to Halopedia for them."""
        from heartbeat import _strip_persona_backstory

        soul = "## Marketing Mode\nJohn-117 drafts comments for Alex's products.\n"
        stripped = _strip_persona_backstory(soul)
        assert "Marketing Mode" in stripped

    def test_empty_soul_returns_empty(self):
        from heartbeat import _strip_persona_backstory

        assert _strip_persona_backstory("") == ""
        assert _strip_persona_backstory("   ") == ""


# ─── Work Prompt Persona Routing ──────────────────────────────────


@pytest.mark.usefixtures("marketing_persona")
class TestWorkPromptPersonaRouting:
    """Search-tier work prompts must be clean of Halo character noise.
    Non-search work prompts keep the full persona so John-117 stays in
    character for direct responses."""

    _FAKE_SOUL = (
        "You are John-117.\n\n"
        "## Who You Are\n"
        "- Master Chief Petty Officer John-117 — Spartan-II super-soldier\n"
        "- Eridanus II, Cortana, MJOLNIR\n\n"
        "## Your Character\n"
        "- Feared by the Covenant\n\n"
        "## What You Can Do\n"
        "- Manage tasks for Alex\n"
    )

    @patch.dict("os.environ", FAKE_ENV)
    @patch("heartbeat._load_soul")
    def test_search_prompt_omits_halo_character_tokens(self, mock_soul):
        import asyncio

        from heartbeat import _build_work_prompt

        mock_soul.return_value = self._FAKE_SOUL
        prompt = asyncio.run(
            _build_work_prompt(
                "Research AI agent frameworks shipping this week",
                web_search=True,
                brain_db=brain_db,
            )
        )
        lower = prompt.lower()
        for token in (
            "spartan",
            "master chief",
            "mjolnir",
            "cortana",
            "covenant",
            "eridanus",
        ):
            assert token not in lower, (
                f"search-tier prompt leaked Halo token {token!r} — Exa "
                f"will return Halopedia citations again"
            )

    @patch.dict("os.environ", FAKE_ENV)
    @patch("heartbeat._load_soul")
    def test_non_search_prompt_keeps_full_persona(self, mock_soul):
        """Regression guard: direct chat (no web search) still needs the
        full John-117 character for in-character replies. The strip only
        applies to search-tier routes."""
        import asyncio

        from heartbeat import _build_work_prompt

        mock_soul.return_value = self._FAKE_SOUL
        prompt = asyncio.run(
            _build_work_prompt(
                "Summarize the current task queue", web_search=False, brain_db=brain_db
            )
        )
        # Persona character tokens should appear (soul.md is loaded in full)
        assert "Master Chief" in prompt
        assert "Spartan-II" in prompt


# ─── Irrelevant Citation Filtering ────────────────────────────────


@pytest.mark.usefixtures("marketing_persona")
class TestIrrelevantCitationFiltering:
    """Belt-and-suspenders: even if the prompt-stripping fix lets a stray
    Halo token through, the citation-extraction layer drops Halopedia and
    other character-backstory URLs so the user's Telegram message stays
    clean."""

    def test_drops_halopedia_citations(self):
        from llm_proxy import _extract_openrouter_citations

        annotations = [
            {
                "type": "url_citation",
                "url_citation": {
                    "url": "https://www.halopedia.org/John-117",
                    "title": "John-117 - Character - Halopedia, the Halo wiki",
                    "content": "Master Chief Petty Officer John-117",
                },
            },
            {
                "type": "url_citation",
                "url_citation": {
                    "url": "https://github.com/anthropics/claude-code",
                    "title": "Claude Code plugin marketplace",
                    "content": "Plugin marketplace for Claude Code",
                },
            },
        ]
        out = _extract_openrouter_citations(annotations)
        assert "halopedia" not in out.lower()
        assert "John-117" not in out
        # The legitimate citation survives
        assert "claude-code" in out

    def test_drops_wikipedia_character_pages(self):
        from llm_proxy import _extract_openrouter_citations

        annotations = [
            {
                "type": "url_citation",
                "url_citation": {
                    "url": "https://en.wikipedia.org/wiki/Master_Chief_(Halo)",
                    "title": "Master Chief (Halo)",
                    "content": "Master Chief Petty Officer John-117",
                },
            },
        ]
        out = _extract_openrouter_citations(annotations)
        assert out == ""

    def test_keeps_legitimate_results(self):
        from llm_proxy import _extract_openrouter_citations

        annotations = [
            {
                "type": "url_citation",
                "url_citation": {
                    "url": "https://news.ycombinator.com/item?id=12345",
                    "title": "Show HN: AI agent framework",
                    "content": "Open source agent framework",
                },
            },
        ]
        out = _extract_openrouter_citations(annotations)
        assert "ycombinator" in out


# ─── SSE Wrapping (streaming fallback) ──────────────────────────


class TestWrapJsonAsSse:
    """Verify non-streaming JSON gets re-wrapped as valid SSE for streaming clients."""

    def test_produces_valid_sse_chunks(self):
        from llm_proxy import _wrap_json_as_sse

        json_resp = JSONResponse(
            content={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": "glm-5.1",
                "choices": [
                    {"message": {"role": "assistant", "content": "Hello world"}}
                ],
                "_clawrange_tier": "zai-direct",
            }
        )
        sse_resp = _wrap_json_as_sse(json_resp)
        assert sse_resp.media_type == "text/event-stream"

        # Collect all SSE lines
        import asyncio

        async def _collect():
            chunks = []
            async for chunk in sse_resp.body_iterator:
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_collect())
        raw = "".join(chunks)

        # Must end with [DONE]
        assert "data: [DONE]" in raw

        # Parse data lines
        data_lines = [
            ln
            for ln in raw.split("\n")
            if ln.startswith("data: ") and "[DONE]" not in ln
        ]
        assert (
            len(data_lines) >= 3
        )  # role chunk + at least 1 content chunk + stop chunk

        # First chunk: role only, no content
        first = json.loads(data_lines[0].removeprefix("data: "))
        assert first["object"] == "chat.completion.chunk"
        assert first["choices"][0]["delta"] == {"role": "assistant"}

        # Content chunks reconstruct the original text
        content_parts = []
        for line in data_lines[1:]:
            parsed = json.loads(line.removeprefix("data: "))
            delta = parsed["choices"][0]["delta"]
            if "content" in delta:
                content_parts.append(delta["content"])
        assert "".join(content_parts) == "Hello world"

        # Last data chunk has finish_reason
        last = json.loads(data_lines[-1].removeprefix("data: "))
        assert last["choices"][0]["finish_reason"] == "stop"

    def test_tier_tag_preserved(self):
        from llm_proxy import _wrap_json_as_sse

        json_resp = JSONResponse(
            content={
                "id": "test",
                "object": "chat.completion",
                "model": "glm-5.1",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "_clawrange_tier": "zai-direct",
            }
        )
        sse_resp = _wrap_json_as_sse(json_resp)

        import asyncio

        async def _collect():
            return "".join([c async for c in sse_resp.body_iterator])

        raw = asyncio.run(_collect())
        first_data = json.loads(raw.split("\n")[0].removeprefix("data: "))
        assert first_data["_clawrange_tier"] == "zai-direct"


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestStreamingReturnsSSE:
    """Verify that stream=true requests return SSE via non-streaming + re-wrap."""

    def setup_method(self):
        _reset_state()

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_all_succeed)
    def test_stream_request_returns_sse(self, _mock_call, _mock_bg):
        """A stream=true request should return text/event-stream with valid SSE."""
        r = client.post(
            "/v1/chat/completions",
            json={**CHAT_BODY, "stream": True},
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "data: [DONE]" in r.text

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_all_succeed)
    def test_non_stream_request_returns_json(self, _mock_call, _mock_bg):
        """A stream=false request should return regular JSON."""
        r = client.post(
            "/v1/chat/completions",
            json=CHAT_BODY,
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "choices" in r.json()

    @patch("llm_proxy._background_notify")
    def test_stream_sanitizes_garbled_and_tool_tags(self, _mock_bg):
        """Streaming requests get full sanitization (garbled + tool tag removal)."""
        garbled_tool_body = {
            "id": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Letmecheckifthere'saspecificresearch"
                        "documentthatwascreated:"
                        "<read><path>/home/node/test.md</path></read>",
                    }
                }
            ],
        }

        async def mock_call(provider, models, body, api_key):
            if provider == "openrouter":
                return _mock_response(200, garbled_tool_body)
            return _mock_response(200)  # zai returns clean

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            r = client.post(
                "/v1/chat/completions",
                json={**CHAT_BODY, "stream": True},
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200
            # Garbled openrouter response was skipped → zai-direct used
            assert "<read>" not in r.text
            assert "data: [DONE]" in r.text


# ─── Telegram ────────────────────────────────────────────────────


class TestTelegramNotify:
    """Basic tests for the Telegram notification module."""

    def test_notify_disabled_without_env(self):
        import asyncio

        import telegram

        original_token = telegram.BOT_TOKEN
        telegram.BOT_TOKEN = ""
        try:
            result = asyncio.run(telegram.notify("test"))
            assert result is False
        finally:
            telegram.BOT_TOKEN = original_token


# ─── Circuit Breaker State ───────────────────────────────────────


class TestCircuitBreakerState:
    """Direct unit tests for _record_failure, _record_success, _record_rate_limit."""

    def setup_method(self):
        _reset_state()

    def test_record_failure_increments(self):
        import llm_proxy

        llm_proxy._record_failure("test-tier")
        assert llm_proxy._circuit_state["test-tier"]["failures"] == 1
        llm_proxy._record_failure("test-tier")
        assert llm_proxy._circuit_state["test-tier"]["failures"] == 2

    def test_record_success_clears_state(self):
        import llm_proxy

        llm_proxy._record_failure("test-tier")
        llm_proxy._record_failure("test-tier")
        assert "test-tier" in llm_proxy._circuit_state
        llm_proxy._record_success("test-tier")
        assert "test-tier" not in llm_proxy._circuit_state

    def test_record_success_on_clean_tier_is_noop(self):
        import llm_proxy

        llm_proxy._record_success("nonexistent")
        assert "nonexistent" not in llm_proxy._circuit_state

    def test_record_rate_limit_trips_immediately(self):
        import llm_proxy

        llm_proxy._record_rate_limit("test-tier")
        assert llm_proxy._circuit_open("test-tier") is True

    def test_circuit_closed_below_threshold(self):
        import llm_proxy

        llm_proxy._record_failure("test-tier")
        llm_proxy._record_failure("test-tier")
        assert llm_proxy._circuit_open("test-tier") is False

    def test_circuit_opens_at_threshold(self):
        import llm_proxy

        for _ in range(llm_proxy.CIRCUIT_FAILURE_THRESHOLD):
            llm_proxy._record_failure("test-tier")
        assert llm_proxy._circuit_open("test-tier") is True

    def test_circuit_resets_after_cooldown(self):
        import time

        import llm_proxy

        llm_proxy._record_rate_limit("test-tier")
        assert llm_proxy._circuit_open("test-tier") is True
        # Fake the last_failure to be in the past
        llm_proxy._circuit_state["test-tier"]["last_failure"] = (
            time.monotonic() - llm_proxy.CIRCUIT_COOLDOWN_SECONDS - 1
        )
        # Half-open: one probe is allowed, state is kept until it succeeds.
        assert llm_proxy._circuit_open("test-tier") is False
        assert "test-tier" in llm_proxy._circuit_state
        llm_proxy._record_success("test-tier")
        assert "test-tier" not in llm_proxy._circuit_state


# ─── Tier Hint Detection ─────────────────────────────────────────


class TestDetectTierHint:
    """Unit tests for _detect_tier_hint() keyword routing."""

    def test_paid_keyword(self):
        from llm_proxy import _detect_tier_hint

        msgs = [{"role": "user", "content": "!paid tell me about homes"}]
        assert _detect_tier_hint(msgs) == "openrouter-paid"

    def test_claude_keyword(self):
        from llm_proxy import _detect_tier_hint

        msgs = [{"role": "user", "content": "!claude what models do you carry?"}]
        assert _detect_tier_hint(msgs) == "openrouter-paid"

    def test_zai_keyword(self):
        from llm_proxy import _detect_tier_hint

        msgs = [{"role": "user", "content": "!zai hello"}]
        assert _detect_tier_hint(msgs) == "zai-direct"

    def test_z_dot_ai_keyword(self):
        from llm_proxy import _detect_tier_hint

        msgs = [{"role": "user", "content": "!z.ai hello"}]
        assert _detect_tier_hint(msgs) == "zai-direct"

    def test_glm_keyword(self):
        from llm_proxy import _detect_tier_hint

        msgs = [{"role": "user", "content": "!glm research this"}]
        assert _detect_tier_hint(msgs) == "zai-direct"

    def test_no_bang_prefix_no_match(self):
        """Plain 'paid' without ! should NOT trigger routing."""
        from llm_proxy import _detect_tier_hint

        msgs = [{"role": "user", "content": "I already paid for this"}]
        assert _detect_tier_hint(msgs) is None

    def test_empty_messages(self):
        from llm_proxy import _detect_tier_hint

        assert _detect_tier_hint([]) is None

    def test_only_assistant_messages(self):
        from llm_proxy import _detect_tier_hint

        msgs = [{"role": "assistant", "content": "!paid"}]
        assert _detect_tier_hint(msgs) is None

    def test_uses_last_user_message_only(self):
        from llm_proxy import _detect_tier_hint

        msgs = [
            {"role": "user", "content": "!paid first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "hello"},
        ]
        assert _detect_tier_hint(msgs) is None

    def test_list_content_format(self):
        """Vision-style messages with list content should be parsed."""
        from llm_proxy import _detect_tier_hint

        msgs = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "!paid describe this image"}],
            }
        ]
        assert _detect_tier_hint(msgs) == "openrouter-paid"

    def test_metadata_block_stripped(self):
        """Sender metadata containing keywords should not trigger routing."""
        from llm_proxy import _detect_tier_hint

        msgs = [
            {
                "role": "user",
                "content": (
                    'sender (untrusted metadata):\n```json\n{"name":"paid"}\n```\n'
                    "what homes do you carry?"
                ),
            }
        ]
        assert _detect_tier_hint(msgs) is None


# ─── _get_last_user_message() ────────────────────────────────────


class TestGetLastUserMessage:
    """Unit tests for _get_last_user_message() with edge cases."""

    # OpenClaw 2026.8+ appends a runtime-context user message after the real
    # one and prefixes the text with a timestamp envelope (captured shapes).
    RUNTIME_CONTEXT = (
        "OpenClaw runtime context:\n## Interrupted inputs\nnone in the latest "
        "retained-input page\n\n## Temporal Context\nCurrent date: 2026-11-15"
        "\nTime zone: UTC\nEnd OpenClaw runtime context."
    )

    def test_skips_trailing_openclaw_runtime_context(self):
        from llm_proxy import _get_last_user_message

        msgs = [
            {"role": "user", "content": "!help"},
            {"role": "user", "content": self.RUNTIME_CONTEXT},
        ]
        assert _get_last_user_message(msgs) == "!help"

    def test_strips_timestamp_envelope(self):
        from llm_proxy import _get_last_user_message

        msgs = [{"role": "user", "content": "[Sun 2026-11-15 22:21 UTC] !help"}]
        assert _get_last_user_message(msgs) == "!help"

    # How OpenClaw 2026.11.6 presents an automation job's message (logged
    # verbatim from the live proxy after the 2026.11.3 -> 2026.11.6 upgrade).
    CRON_TAGGED_HEARTBEAT = (
        "[cron:ee854ad9-1791-4919-9dfb-897c7e3cb935 heartbeat-max-ops] "
        "ClawRange heartbeat: run the scheduled checks in the monitor scratch. "
        "If nothing needs attention, reply NO_REPLY."
    )

    def test_strips_automation_job_tag(self):
        """GIVEN a heartbeat sent by an OpenClaw automation job
        WHEN the proxy extracts the last user message
        THEN the leading [cron:<id> <name>] tag is gone and the marker leads."""
        import heartbeat
        from llm_proxy import _get_last_user_message

        msgs = [{"role": "user", "content": self.CRON_TAGGED_HEARTBEAT}]
        extracted = _get_last_user_message(msgs)
        assert extracted.startswith("ClawRange heartbeat:")
        assert heartbeat._is_heartbeat(extracted)

    def test_strips_automation_tag_after_timestamp_envelope(self):
        """GIVEN a job tag behind a timestamp envelope
        THEN both are stripped."""
        from llm_proxy import _get_last_user_message

        msgs = [
            {
                "role": "user",
                "content": "[Sat 2026-11-21 22:30 UTC] [cron:abc-123 nightly] !help",
            }
        ]
        assert _get_last_user_message(msgs) == "!help"

    def test_ordinary_bracketed_text_is_kept(self):
        """GIVEN a user message that merely starts with brackets
        THEN it is not mistaken for a job tag."""
        from llm_proxy import _get_last_user_message

        for text in ("[note] call Dana", "[cron] what is cron?", "[cron:] hi"):
            msgs = [{"role": "user", "content": text}]
            assert _get_last_user_message(msgs) == text

    def test_heartbeat_prompt_survives_runtime_context(self):
        import heartbeat
        from llm_proxy import _get_last_user_message

        msgs = [
            {"role": "user", "content": "ClawRange heartbeat: run the checks. ..."},
            {"role": "user", "content": self.RUNTIME_CONTEXT},
        ]
        assert heartbeat._is_heartbeat(_get_last_user_message(msgs))

    def test_strips_first_turn_runtime_line(self):
        # The first turn of a fresh session carries a runtime summary line.
        from llm_proxy import _get_last_user_message

        msgs = [
            {
                "role": "user",
                "content": "!tasks\n\nRuntime: agent=max | session=agent:max:main"
                " | host=abc | os=linux | node=v24.22.1",
            }
        ]
        assert _get_last_user_message(msgs) == "!tasks"

    def test_tier_hint_ignores_runtime_context(self):
        from llm_proxy import _detect_tier_hint

        msgs = [
            {"role": "user", "content": "!paid summarise this"},
            {"role": "user", "content": self.RUNTIME_CONTEXT},
        ]
        assert _detect_tier_hint(msgs) == "openrouter-paid"

    def test_simple_text(self):
        from llm_proxy import _get_last_user_message

        msgs = [{"role": "user", "content": "hello world"}]
        assert _get_last_user_message(msgs) == "hello world"

    def test_list_content(self):
        from llm_proxy import _get_last_user_message

        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "url": "http://example.com/img.png"},
                ],
            }
        ]
        assert _get_last_user_message(msgs) == "describe"

    def test_strips_metadata_prefix(self):
        from llm_proxy import _get_last_user_message

        msgs = [
            {
                "role": "user",
                "content": (
                    'sender (untrusted metadata):\n```json\n{"id":"123"}\n```\n'
                    "actual question"
                ),
            }
        ]
        assert _get_last_user_message(msgs) == "actual question"

    def test_empty_messages(self):
        from llm_proxy import _get_last_user_message

        assert _get_last_user_message([]) == ""

    def test_non_string_content(self):
        from llm_proxy import _get_last_user_message

        msgs = [{"role": "user", "content": 42}]
        assert _get_last_user_message(msgs) == ""

    def test_skips_assistant_messages(self):
        from llm_proxy import _get_last_user_message

        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        assert _get_last_user_message(msgs) == "second"


# ─── _call_provider() ────────────────────────────────────────────


@patch.dict("os.environ", FAKE_ENV)
class TestCallProvider:
    """Unit tests for _call_provider() HTTP construction."""

    def _make_mock_client(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    def test_posts_to_correct_url(self):
        import asyncio

        import llm_proxy

        mock_client = self._make_mock_client()
        with patch("llm_proxy.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(
                llm_proxy._call_provider(
                    "openrouter", ["model-a"], {"messages": []}, "key-123"
                )
            )
        call_kwargs = mock_client.post.call_args
        assert "openrouter.ai" in call_kwargs.args[0]
        assert call_kwargs.kwargs["json"]["model"] == "model-a"

    def test_openrouter_multi_model_adds_models_field(self):
        import asyncio

        import llm_proxy

        mock_client = self._make_mock_client()
        with patch("llm_proxy.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(
                llm_proxy._call_provider(
                    "openrouter", ["model-a", "model-b"], {"messages": []}, "key-123"
                )
            )
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["model"] == "model-a"
        assert payload["models"] == ["model-a", "model-b"]

    def test_zai_single_model_no_models_field(self):
        import asyncio

        import llm_proxy

        mock_client = self._make_mock_client()
        with patch("llm_proxy.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(
                llm_proxy._call_provider(
                    "zai", ["glm-5.1"], {"messages": []}, "key-456"
                )
            )
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["model"] == "glm-5.1"
        assert "models" not in payload

    def test_auth_header_set(self):
        import asyncio

        import llm_proxy

        mock_client = self._make_mock_client()
        with patch("llm_proxy.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(
                llm_proxy._call_provider(
                    "openrouter", ["m"], {"messages": []}, "my-secret-key"
                )
            )
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my-secret-key"


# ─── Sanitization Combos ─────────────────────────────────────────


class TestSanitizationCombos:
    """Test _sanitize_response with multiple hallucination types in one string."""

    def test_tool_block_plus_xml_tag(self):
        from llm_proxy import _sanitize_response

        text = (
            "Here's my answer.\n"
            "[[exec]]ls /app[[/exec]]\n"
            '<read path="/etc/passwd">contents</read>\n'
            "Hope that helps!"
        )
        result = _sanitize_response(text)
        assert "[[exec]]" not in result
        assert "<read" not in result
        assert "Here's my answer." in result
        assert "Hope that helps!" in result

    def test_thinking_plus_session_status(self):
        from llm_proxy import _sanitize_response

        text = (
            "<thinking>I need to analyze this carefully.</thinking>\n"
            "<session_status>active</session_status>\n"
            "The answer is 42."
        )
        result = _sanitize_response(text)
        assert "<thinking>" not in result
        assert "<session_status>" not in result
        assert "The answer is 42." in result

    def test_unclosed_block_at_end(self):
        from llm_proxy import _sanitize_response

        text = "Let me check.\n[[write]]file.txt\nsome content"
        result = _sanitize_response(text)
        assert "[[write]]" not in result
        assert "Let me check." in result

    def test_self_closing_xml_tag_strips_to_end(self):
        """Unclosed tag regex is greedy — strips from <exec to end of string.
        This is intentional: anything after a hallucinated tag is suspect."""
        from llm_proxy import _sanitize_response

        text = 'I found the file. <exec command="cat /etc/hosts"/> Done.'
        result = _sanitize_response(text)
        assert "<exec" not in result
        assert "I found the file." in result

    def test_excessive_newlines_collapsed(self):
        from llm_proxy import _sanitize_response

        text = "Before.\n\n\n\n\n\nAfter."
        result = _sanitize_response(text)
        assert "\n\n\n" not in result
        assert "Before.\n\nAfter." == result


# ─── Balance Cache ───────────────────────────────────────────────


class TestBalanceCache:
    """Test _check_openrouter_balance caching behavior."""

    def setup_method(self):
        _reset_state()

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"})
    def test_returns_cached_value_within_interval(self):
        """Second call within 5 min should return cached value without HTTP call."""
        import asyncio
        import time

        import llm_proxy

        llm_proxy._balance_cache.clear()
        # Pre-populate cache
        llm_proxy._balance_cache["remaining"] = 15.0
        llm_proxy._balance_cache["checked_at"] = time.monotonic()

        try:
            result = asyncio.run(llm_proxy._check_openrouter_balance())
            assert result == 15.0  # Returned cached, no HTTP call needed
        finally:
            llm_proxy._balance_cache.clear()

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"})
    def test_refetches_after_interval(self):
        """After cache expires, should make a new HTTP call."""
        import asyncio
        import time

        import llm_proxy

        llm_proxy._balance_cache.clear()
        # Set cache as expired
        llm_proxy._balance_cache["remaining"] = 15.0
        llm_proxy._balance_cache["checked_at"] = (
            time.monotonic() - llm_proxy._BALANCE_CHECK_INTERVAL - 1
        )

        mock_resp = httpx.Response(
            200,
            content=json.dumps(
                {"data": {"total_credits": 20.0, "total_usage": 8.0}}
            ).encode(),
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        try:
            with patch("llm_proxy.httpx.AsyncClient", return_value=mock_client):
                result = asyncio.run(llm_proxy._check_openrouter_balance())
            assert result == 12.0  # 20.0 - 8.0
            mock_client.get.assert_called_once()
        finally:
            llm_proxy._balance_cache.clear()

    def test_returns_none_when_api_key_missing(self):
        import asyncio

        import llm_proxy

        llm_proxy._balance_cache.clear()

        try:
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}):
                result = asyncio.run(llm_proxy._check_openrouter_balance())
            assert result is None
        finally:
            llm_proxy._balance_cache.clear()

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"})
    def test_returns_stale_cache_on_http_error(self):
        """If the API call fails, return the last known value."""
        import asyncio
        import time

        import llm_proxy

        llm_proxy._balance_cache.clear()
        # Stale cache
        llm_proxy._balance_cache["remaining"] = 10.0
        llm_proxy._balance_cache["checked_at"] = (
            time.monotonic() - llm_proxy._BALANCE_CHECK_INTERVAL - 1
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        try:
            with patch("llm_proxy.httpx.AsyncClient", return_value=mock_client):
                result = asyncio.run(llm_proxy._check_openrouter_balance())
            assert result == 10.0  # Stale cache returned
        finally:
            llm_proxy._balance_cache.clear()


# ─── Notification Debouncing ─────────────────────────────────────


class TestShouldNotify:
    """GIVEN the _should_notify debounce guard
    WHEN called for a tier
    THEN it should allow the first call and suppress duplicates within the interval."""

    def setup_method(self):
        _reset_state()

    def test_first_call_returns_true(self):
        import llm_proxy

        assert llm_proxy._should_notify("test-tier") is True

    def test_second_call_within_interval_returns_false(self):
        import llm_proxy

        llm_proxy._should_notify("test-tier")
        assert llm_proxy._should_notify("test-tier") is False

    def test_different_tiers_are_independent(self):
        import llm_proxy

        assert llm_proxy._should_notify("tier-a") is True
        assert llm_proxy._should_notify("tier-b") is True
        # tier-a still suppressed
        assert llm_proxy._should_notify("tier-a") is False

    def test_allows_after_debounce_expires(self):
        import time

        import llm_proxy

        llm_proxy._should_notify("test-tier")
        # Fake the timestamp to be in the past
        llm_proxy._notification_last_sent["test-tier"] = (
            time.monotonic() - llm_proxy._NOTIFICATION_DEBOUNCE_SECONDS - 1
        )
        assert llm_proxy._should_notify("test-tier") is True


# ─── Rate Limit Detail Extraction ────────────────────────────────


class TestGetRateLimitDetail:
    """GIVEN a rate-limited HTTP response
    WHEN _get_rate_limit_detail is called
    THEN it should extract provider name and error context."""

    def test_extracts_provider_and_raw(self):
        from llm_proxy import _get_rate_limit_detail

        resp = httpx.Response(
            429,
            content=json.dumps(
                {
                    "error": {
                        "message": "rate limited",
                        "metadata": {
                            "provider_name": "DeepSeek",
                            "raw": "Too many requests, retry in 30s",
                        },
                    }
                }
            ).encode(),
        )
        detail = _get_rate_limit_detail(resp)
        assert "DeepSeek" in detail
        assert "Too many requests" in detail

    def test_provider_only(self):
        from llm_proxy import _get_rate_limit_detail

        resp = httpx.Response(
            429,
            content=json.dumps(
                {
                    "error": {
                        "message": "rate limited",
                        "metadata": {"provider_name": "Anthropic"},
                    }
                }
            ).encode(),
        )
        detail = _get_rate_limit_detail(resp)
        assert "Anthropic" in detail

    def test_raw_only(self):
        from llm_proxy import _get_rate_limit_detail

        resp = httpx.Response(
            429,
            content=json.dumps(
                {
                    "error": {
                        "message": "rate limited",
                        "metadata": {"raw": "quota exceeded"},
                    }
                }
            ).encode(),
        )
        detail = _get_rate_limit_detail(resp)
        assert "quota exceeded" in detail

    def test_no_metadata_returns_empty(self):
        from llm_proxy import _get_rate_limit_detail

        resp = httpx.Response(
            429, content=json.dumps({"error": {"message": "nope"}}).encode()
        )
        assert _get_rate_limit_detail(resp) == ""

    def test_malformed_json_returns_empty(self):
        from llm_proxy import _get_rate_limit_detail

        resp = httpx.Response(429, content=b"not json")
        assert _get_rate_limit_detail(resp) == ""


# ─── Reset Seconds Extraction ────────────────────────────────────


class TestGetResetSeconds:
    """GIVEN a rate-limited response with timing info
    WHEN _get_reset_seconds is called
    THEN it should return seconds until reset."""

    def test_retry_after_header(self):
        from llm_proxy import _get_reset_seconds

        resp = httpx.Response(429, headers={"retry-after": "45"}, content=b"")
        assert _get_reset_seconds(resp) == 45

    def test_retry_after_minimum_one(self):
        from llm_proxy import _get_reset_seconds

        resp = httpx.Response(429, headers={"retry-after": "0"}, content=b"")
        assert _get_reset_seconds(resp) == 1

    def test_body_retry_after_in_metadata(self):
        from llm_proxy import _get_reset_seconds

        resp = httpx.Response(
            429,
            content=json.dumps(
                {
                    "error": {
                        "message": "rate limited",
                        "metadata": {"retry_after": "30"},
                    }
                }
            ).encode(),
        )
        assert _get_reset_seconds(resp) == 30

    def test_message_with_seconds(self):
        from llm_proxy import _get_reset_seconds

        resp = httpx.Response(
            429,
            content=json.dumps(
                {"error": {"message": "Please retry in 20 seconds"}}
            ).encode(),
        )
        assert _get_reset_seconds(resp) == 20

    def test_message_with_minutes(self):
        from llm_proxy import _get_reset_seconds

        resp = httpx.Response(
            429,
            content=json.dumps(
                {"error": {"message": "Rate limit resets in 5 minutes"}}
            ).encode(),
        )
        assert _get_reset_seconds(resp) == 300

    def test_returns_none_when_no_info(self):
        from llm_proxy import _get_reset_seconds

        resp = httpx.Response(429, content=b"{}")
        assert _get_reset_seconds(resp) is None


# ─── Tier Change Notification ────────────────────────────────────


class TestNotifyTierChange:
    """GIVEN the tier tracking state
    WHEN _notify_tier_change is called
    THEN it should update _last_tier_used and log transitions."""

    def setup_method(self):
        _reset_state()
        import llm_proxy

        llm_proxy._last_tier_used = None

    def test_sets_initial_tier(self):
        import asyncio

        import llm_proxy

        asyncio.run(llm_proxy._notify_tier_change("openrouter-free"))
        assert llm_proxy._last_tier_used == "openrouter-free"

    def test_updates_on_change(self):
        import asyncio

        import llm_proxy

        llm_proxy._last_tier_used = "openrouter-free"
        asyncio.run(llm_proxy._notify_tier_change("zai-direct"))
        assert llm_proxy._last_tier_used == "zai-direct"

    def test_same_tier_no_notification(self):
        import asyncio

        import llm_proxy

        llm_proxy._last_tier_used = "zai-direct"
        with patch.object(llm_proxy, "_background_notify") as mock_bg:
            asyncio.run(llm_proxy._notify_tier_change("zai-direct"))
        mock_bg.assert_not_called()

    def test_different_tier_triggers_notification(self):
        import asyncio

        import llm_proxy

        llm_proxy._last_tier_used = "openrouter-free"
        with patch.object(llm_proxy, "_background_notify") as mock_bg:
            asyncio.run(llm_proxy._notify_tier_change("zai-direct"))
        mock_bg.assert_called_once()
        assert "openrouter-free" in mock_bg.call_args.args[1]
        assert "zai-direct" in mock_bg.call_args.args[1]


# ─── Rate Limit Body Detection ───────────────────────────────────


class TestIsRateLimitedBodyParsing:
    """GIVEN non-429 responses with rate limit keywords in the body
    WHEN _is_rate_limited is called
    THEN it should detect rate limiting from error message text."""

    def test_400_with_rate_limit_keyword_in_error_dict(self):
        from llm_proxy import _is_rate_limited

        resp = httpx.Response(
            400,
            content=json.dumps(
                {
                    "error": {
                        "message": "You have exceeded the rate limit for this model"
                    }
                }
            ).encode(),
        )
        assert _is_rate_limited(resp) is True

    def test_403_with_too_many_requests(self):
        from llm_proxy import _is_rate_limited

        resp = httpx.Response(
            403,
            content=json.dumps(
                {"error": {"message": "Too many requests from this IP"}}
            ).encode(),
        )
        assert _is_rate_limited(resp) is True

    def test_400_with_string_error_field(self):
        from llm_proxy import _is_rate_limited

        resp = httpx.Response(
            400,
            content=json.dumps({"error": "rate_limit_exceeded"}).encode(),
        )
        assert _is_rate_limited(resp) is True

    def test_400_without_keywords_not_rate_limited(self):
        from llm_proxy import _is_rate_limited

        resp = httpx.Response(
            400,
            content=json.dumps({"error": {"message": "invalid model name"}}).encode(),
        )
        assert _is_rate_limited(resp) is False

    def test_500_never_treated_as_rate_limit(self):
        from llm_proxy import _is_rate_limited

        resp = httpx.Response(
            500,
            content=json.dumps(
                {"error": {"message": "rate limit internal error"}}
            ).encode(),
        )
        assert _is_rate_limited(resp) is False


# ─── Research Router ──────────────────────────────────────────────


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestResearchRouter:
    """Research-shaped tasks route to the web-capable orchestrator
    instead of the tool-less chat LLM."""

    def setup_method(self):
        _reset_state()

    @patch("research.orchestrate_research", new_callable=AsyncMock)
    def test_research_task_runs_orchestrator(self, mock_orch):
        import asyncio

        mock_orch.return_value = {
            "findings": [
                {
                    "channel": "discourse",
                    "title": "How do you capture trade-skill knowledge?",
                    "url": "https://reddit.com/r/trades/x",
                    "relevance": 9,
                    "summary": "field-capture discussion",
                }
            ]
        }
        result = asyncio.run(
            research_router._try_research_task(
                "Search Reddit for live threads on trade-skill knowledge capture"
            )
        )
        assert result is not None
        assert "reddit.com/r/trades/x" in result
        mock_orch.assert_called_once()
        assert mock_orch.call_args.kwargs["channels"] == [
            "discourse",
            "code",
            "discourse_web",
        ]

    def test_non_research_task_returns_none(self):
        import asyncio

        assert (
            asyncio.run(research_router._try_research_task("Check z.ai pricing"))
            is None
        )
        assert asyncio.run(research_router._try_research_task("Reply to Alex")) is None

    @patch("research.orchestrate_research", new_callable=AsyncMock)
    def test_content_idea_task_not_hijacked_by_research_router(self, mock_orch):
        """The content-idea template quotes research words ("research on",
        "surfaced", "Reddit/HN comment") but is a drafting job for the LLM.
        The orchestrator must not steal it and re-run research."""
        import asyncio

        desc = (
            "Content idea for claude-night-market: research on 'trade-skill "
            'capture\' surfaced "TRIZ analogies" (). Draft three angles - '
            "(1) technical post, (2) useful Reddit/HN comment, (3) X thread."
        )
        result = asyncio.run(research_router._try_research_task(desc))
        assert result is None
        mock_orch.assert_not_called()

    @patch("research.orchestrate_research", new_callable=AsyncMock)
    def test_findings_header_does_not_echo_description(self, mock_orch):
        """The digest line already shows the task description above the
        result; echoing it inside the result header just doubles the text
        and crowds the blocked-detector's 400-char head window."""
        import asyncio

        mock_orch.return_value = {
            "findings": [
                {
                    "channel": "discourse",
                    "title": "What chrome extensions do trades use?",
                    "url": "https://reddit.com/r/trades/x",
                }
            ]
        }
        result = asyncio.run(
            research_router._try_research_task(
                "Search Reddit for live threads on trade-skill knowledge capture"
            )
        )
        assert result is not None
        assert result.startswith("Research findings (1):")
        assert "trade-skill knowledge capture" not in result
        assert "reddit.com/r/trades/x" in result

    @patch("research.orchestrate_research", new_callable=AsyncMock)
    def test_no_findings_reported(self, mock_orch):
        import asyncio

        mock_orch.return_value = {"findings": []}
        result = asyncio.run(
            research_router._try_research_task(
                "research web articles on tacit knowledge"
            )
        )
        assert result is not None
        assert "no findings" in result.lower()

    @patch("research.orchestrate_research", new_callable=AsyncMock)
    def test_orchestrator_failure_falls_back_to_llm(self, mock_orch):
        import asyncio

        mock_orch.side_effect = RuntimeError("channels down")
        result = asyncio.run(
            research_router._try_research_task(
                "find reddit threads on chrome extensions"
            )
        )
        assert result is None

    @patch("research.orchestrate_research", new_callable=AsyncMock)
    def test_heartbeat_processes_research_task_via_orchestrator(self, mock_orch):
        """A queued research task completes with real citable URLs, and
        the heartbeat response stays silent (digest carries it)."""
        import time as _t

        from app import TaskCreate as TC
        from app import create_task

        heartbeat._proactive_state["stale_tasks"] = _t.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _t.monotonic()

        mock_orch.return_value = {
            "findings": [
                {
                    "channel": "discourse",
                    "title": "HN: Tools for capturing field knowledge",
                    "url": "https://news.ycombinator.com/item?id=1",
                    "relevance": 8,
                    "summary": "hn thread",
                }
            ]
        }
        t = create_task(
            TC(
                description=(
                    "Search Reddit and HN for live threads on trade-skill "
                    "knowledge capture"
                ),
                priority=1,
            ),
            brain_db=brain_db,
        )

        r = client.post(
            "/v1/chat/completions",
            json=self._hb_body(),
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "NO_REPLY"

        done = brain_db.get_task(t["id"])
        assert done["status"] == "completed"
        assert "news.ycombinator.com" in done["result"]
        mock_orch.assert_called_once()

    @staticmethod
    def _hb_body():
        return {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "clawrange heartbeat: run the scheduled checks in the "
                        "monitor scratch. do not infer or repeat old tasks "
                        "from prior chats. if nothing needs attention, reply "
                        "with exactly: heartbeat_ok"
                    ),
                }
            ]
        }


# ─── Heartbeat Interceptor ──────────────────────────────────────


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestHeartbeatInterceptor:
    """Verify heartbeat messages are handled in Python, not forwarded to LLM."""

    def setup_method(self):
        _reset_state()
        # Clear persistent task queue via brain_db

        for table in ("tasks",):
            brain_db._conn.execute(f"DELETE FROM {table}")
        brain_db._conn.commit()

    def _heartbeat_body(self):
        return {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "clawrange heartbeat: run the scheduled checks in the "
                        "monitor scratch. do not infer or repeat old tasks "
                        "from prior chats. if nothing needs attention, reply "
                        "heartbeat_ok."
                    ),
                }
            ],
        }

    def test_heartbeat_intercepted_no_llm_call(self):
        """Heartbeat message should NOT call the LLM provider for basic checks."""
        import time as _time

        # Suppress proactive LLM thinking so we only test deterministic path
        heartbeat._proactive_state["stale_tasks"] = _time.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _time.monotonic()

        mock_caller = AsyncMock(return_value=_mock_response(200))
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200
            mock_caller.assert_not_called()

    def test_automation_job_heartbeat_is_intercepted(self):
        """GIVEN the heartbeat as OpenClaw 2026.11.6 automations send it,
        tagged [cron:<id> <name>]
        WHEN it reaches the proxy with nothing to report
        THEN no LLM is called and the reply is the quiet NO_REPLY token."""
        import time as _time

        heartbeat._proactive_state["stale_tasks"] = _time.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _time.monotonic()
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": TestGetLastUserMessage.CRON_TAGGED_HEARTBEAT,
                }
            ]
        }
        mock_caller = AsyncMock(return_value=_mock_response(200))
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
        assert r.status_code == 200
        mock_caller.assert_not_called()
        assert r.json()["choices"][0]["message"]["content"] == "NO_REPLY"

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_heartbeat_processes_pending_task_hourly_digest(self, mock_notify):
        """Task completions buffer into the hourly digest: no per-completion
        Telegram notification, silent heartbeat response, and the buffered
        result delivered as the next digest once the hour elapses."""
        import time as _time

        from app import TaskCreate as TC
        from app import create_task

        # Suppress proactive layers so only the pending task is processed
        heartbeat._proactive_state["stale_tasks"] = _time.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _time.monotonic()

        create_task(
            TC(description="Test task for heartbeat", priority=2), brain_db=brain_db
        )

        mock_resp = _mock_response(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "Checked logs — no anomalies found in the last 24h."
                            ),
                        }
                    }
                ],
            },
        )
        mock_caller = AsyncMock(return_value=mock_resp)
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
            content = r.json()["choices"][0]["message"]["content"]
            assert content == "NO_REPLY"  # silent: completion buffered, not relayed
            mock_caller.assert_called_once()

        # No per-completion Telegram notification
        mock_notify.assert_not_called()

        tasks = brain_db.list_tasks()
        completed = [t for t in tasks if t["status"] == "completed"]
        assert len(completed) >= 1
        assert "no anomalies" in completed[0]["result"]

        # An hour later the digest delivers the buffered completion: the
        # item goes straight to Telegram and the relayed response is the
        # closing summary.
        digest._digest_state["last_flush"] = (
            _time.time() - digest.HEARTBEAT_DIGEST_INTERVAL - 1
        )
        r = client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )
        content = r.json()["choices"][0]["message"]["content"]
        assert "1 item(s) this hour" in content
        sent = [call.args[0] for call in mock_notify.await_args_list]
        assert any(
            "Test task for heartbeat" in text and "no anomalies" in text
            for text in sent
        )

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_digest_carries_long_results_unclipped(self, mock_notify):
        """Research-shaped results ran past the old 500-char digest clip and
        arrived cut mid-word. A result now gets most of a Telegram message:
        the sentinel at char 2,000 must survive, bounded by the send limit."""
        import time as _time

        from app import TaskCreate as TC
        from app import create_task

        heartbeat._proactive_state["stale_tasks"] = _time.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _time.monotonic()

        create_task(TC(description="Long result task", priority=2), brain_db=brain_db)

        long_result = (
            "Filler sentence. " * 125
            + "PAST-OLD-CLIP survives past the old 500-char cut. "
            + "Tail filler sentence. " * 125
        )
        mock_resp = _mock_response(
            200,
            {"choices": [{"message": {"role": "assistant", "content": long_result}}]},
        )
        mock_caller = AsyncMock(return_value=mock_resp)
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200

        digest._digest_state["last_flush"] = (
            _time.time() - digest.HEARTBEAT_DIGEST_INTERVAL - 1
        )
        client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )

        sent = [call.args[0] for call in mock_notify.await_args_list]
        assert any("PAST-OLD-CLIP" in text for text in sent)
        assert all(len(text) <= telegram.MAX_CHARS for text in sent)

    @patch("heartbeat._llm_suggest_task", new_callable=AsyncMock, return_value=None)
    @patch("llm_proxy._check_openrouter_balance", new_callable=AsyncMock)
    def test_low_balance_alert_not_recreated_after_completion(
        self, mock_balance, _mock_suggest
    ):
        """A completed alert within 24h suppresses new alerts: the old
        pending-only dedup recreated the alert every cycle, spamming
        Telegram with a duplicate wall of text per heartbeat."""

        mock_balance.return_value = 3.0

        existing = brain_db.create_task(
            "Low balance alert: $3.00 remaining", priority=1
        )
        brain_db.complete_task(existing["id"], "noted", "completed")
        baseline = len(
            [
                t
                for t in brain_db.list_tasks()
                if "Low balance alert" in t["description"]
            ]
        )
        assert baseline == 1

        r = client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        alerts = [
            t for t in brain_db.list_tasks() if "Low balance alert" in t["description"]
        ]
        assert len(alerts) == 1, (
            f"expected no new alert, got {[t['id'] for t in alerts]}"
        )
        assert alerts[0]["id"] == existing["id"]

    @patch("llm_proxy._call_provider", new_callable=AsyncMock)
    @patch("llm_proxy._check_openrouter_balance", new_callable=AsyncMock)
    def test_status_alert_closes_with_live_facts_not_llm(
        self, mock_balance, mock_caller
    ):
        """A queued balance alert (e.g. a phantom negative recorded before
        the credits-API fix) completes with the LIVE balance from Python —
        the LLM never sees it, so it cannot dramatize stale numbers into
        an alarmist report."""
        import time as _time

        heartbeat._proactive_state["stale_tasks"] = _time.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _time.monotonic()

        mock_balance.return_value = 28.69
        t = brain_db.create_task("Low balance alert: $-1.33 remaining", priority=1)

        r = client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        mock_caller.assert_not_called()  # no LLM for status alerts

        done = brain_db.get_task(t["id"])
        assert done["status"] == "completed"
        assert "$28.69" in done["result"]
        assert "openrouter active" in done["result"]
        assert "-1.33" not in done["result"]

    @patch("llm_proxy._call_provider", new_callable=AsyncMock)
    @patch("llm_proxy._check_openrouter_balance", new_callable=AsyncMock)
    def test_tier_recovery_alert_closes_with_live_state(
        self, mock_balance, mock_caller
    ):
        """Tier-recovery alert tasks also close in Python with the current
        circuit state."""
        import time as _time

        heartbeat._proactive_state["stale_tasks"] = _time.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _time.monotonic()

        mock_balance.return_value = 28.69
        t = brain_db.create_task(
            "Investigate tier recovery: openrouter-free", priority=2
        )

        r = client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        mock_caller.assert_not_called()

        done = brain_db.get_task(t["id"])
        assert done["status"] == "completed"
        assert "openrouter-free" in done["result"]
        assert "recovered (circuit closed)" in done["result"]

    def test_heartbeat_silent_when_no_issues(self):
        """With no pending tasks, no infra issues, and proactive checks
        not yet due, reply NO_REPLY: the token OpenClaw automations treat
        as a quiet result (an empty reply can surface a missing-answer notice)."""
        # Mark all proactive checks as just-ran so they don't fire
        import time as _time

        heartbeat._proactive_state["stale_tasks"] = _time.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _time.monotonic()

        r = client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )
        content = r.json()["choices"][0]["message"]["content"]
        assert content == "NO_REPLY"

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_heartbeat_stale_task_detection(self, mock_notify):
        """Heartbeat processes stale pending tasks and creates nudge tasks."""
        import llm_proxy

        # Ensure stale_tasks check is ready to fire
        heartbeat._proactive_state.pop("stale_tasks", None)
        # Suppress LLM thinking
        heartbeat._proactive_state["llm_thinking"] = __import__("time").monotonic()

        from datetime import timedelta

        # Create a task and backdate its created_at to 5 hours ago
        old_task = brain_db.create_task("old task from earlier")
        old_time = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        brain_db._conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (old_time, old_task["id"]),
        )
        brain_db._conn.commit()

        # The LLM must answer: an unreachable LLM re-queues the task (G5).
        with patch.object(
            llm_proxy, "_llm_call", AsyncMock(return_value="Reviewed it.")
        ):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
        content = r.json()["choices"][0]["message"]["content"]
        # The stale task is processed and buffered into the digest; the
        # heartbeat response itself stays silent.
        assert content == "NO_REPLY"
        statuses = {t["id"]: t["status"] for t in brain_db.list_tasks()}
        assert statuses[old_task["id"]] == "completed"

        # ...and the completion surfaces in the next hourly digest
        digest._digest_state["last_flush"] = (
            time.time() - digest.HEARTBEAT_DIGEST_INTERVAL - 1
        )
        r = client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )
        content = r.json()["choices"][0]["message"]["content"]
        # The item goes straight to Telegram; the relayed response summarizes.
        assert "this hour" in content
        sent = [call.args[0] for call in mock_notify.await_args_list]
        assert any("old task from earlier" in text for text in sent)

    @patch(
        "heartbeat._current_thinking_subjects",
        return_value=["Strix Halo 128GB"],
    )
    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_heartbeat_llm_thinking(self, mock_notify, _subjects):
        """The heartbeat asks the LLM for a task suggestion when due."""

        # Ensure LLM thinking is ready to fire, stale tasks is not
        heartbeat._proactive_state.pop("llm_thinking", None)
        heartbeat._proactive_state["stale_tasks"] = __import__("time").monotonic()

        mock_resp = _mock_response(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "Research Reddit threads on Strix Halo 128GB "
                                "inference speed"
                            ),
                        }
                    }
                ],
            },
        )
        mock_caller = AsyncMock(return_value=mock_resp)
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
            content = r.json()["choices"][0]["message"]["content"]
            assert content == "NO_REPLY"  # created tasks buffer into the digest

            # Once the digest hour elapses the suggestion is delivered
            digest._digest_state["last_flush"] = (
                time.time() - digest.HEARTBEAT_DIGEST_INTERVAL - 1
            )
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
            content = r.json()["choices"][0]["message"]["content"]
            assert "this hour" in content
            sent = [call.args[0] for call in mock_notify.await_args_list]
            assert any("Strix Halo 128GB" in text for text in sent)

        all_tasks = brain_db.list_tasks()
        llm_tasks = [t for t in all_tasks if "Strix Halo 128GB" in t["description"]]
        assert len(llm_tasks) == 1

    def _think_with(self, suggestion: str) -> list[dict]:
        """Run one heartbeat whose LLM thinking returns `suggestion`."""

        heartbeat._proactive_state.pop("llm_thinking", None)
        heartbeat._proactive_state["stale_tasks"] = time.monotonic()
        with (
            patch.dict("os.environ", {"HEARTBEAT_LLM_SUGGEST": "1"}),
            patch(
                "heartbeat._current_thinking_subjects",
                return_value=TestGroundedThinking.SUBJECTS,
            ),
            patch(
                "heartbeat._llm_suggest_task",
                new_callable=AsyncMock,
                return_value=suggestion,
            ),
        ):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
        assert r.json()["choices"][0]["message"]["content"] == "NO_REPLY"
        return [t for t in brain_db.list_tasks() if t["description"] == suggestion]

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_heartbeat_thinking_off_switch(self, _notify, monkeypatch):
        """HEARTBEAT_LLM_SUGGEST=0 stops self-invented work entirely."""
        monkeypatch.setenv("HEARTBEAT_LLM_SUGGEST", "0")
        suggestion = (
            "Query ClawRange FastAPI workflows documentation and best practices "
            "to identify optimization patterns"
        )

        heartbeat._proactive_state.pop("llm_thinking", None)
        heartbeat._proactive_state["stale_tasks"] = time.monotonic()
        with patch(
            "heartbeat._llm_suggest_task",
            new_callable=AsyncMock,
            return_value=suggestion,
        ) as sug:
            client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
        sug.assert_not_awaited()
        assert not [t for t in brain_db.list_tasks() if t["description"] == suggestion]

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_heartbeat_drops_ungrounded_task(self, _notify):
        """The real 23d5f755: needs internal docs no worker path can read."""
        suggestion = TestGroundedThinking.INVENTED[0]
        assert self._think_with(suggestion) == []

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_heartbeat_queues_grounded_task(self, _notify):
        suggestion = TestGroundedThinking.LEGIT[0]
        assert len(self._think_with(suggestion)) == 1

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_heartbeat_skips_thinking_without_subjects(self, _notify):

        heartbeat._proactive_state.pop("llm_thinking", None)
        heartbeat._proactive_state["stale_tasks"] = time.monotonic()
        with (
            patch("heartbeat._current_thinking_subjects", return_value=[]),
            patch("heartbeat._llm_suggest_task", new_callable=AsyncMock) as sug,
        ):
            client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
        sug.assert_not_awaited()

    def test_heartbeat_llm_thinking_graceful_failure(self):
        """LLM failure during thinking doesn't break the heartbeat."""

        heartbeat._proactive_state.pop("llm_thinking", None)
        heartbeat._proactive_state["stale_tasks"] = __import__("time").monotonic()

        mock_caller = AsyncMock(side_effect=Exception("network error"))
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
            # Should still return silent — LLM failure doesn't break heartbeat
            content = r.json()["choices"][0]["message"]["content"]
            assert content == "NO_REPLY"

    def test_llm_call_total_deadline_caps_tier_hangs(self):
        """_llm_call must enforce a total deadline across tiers.

        Regression: the heartbeat path (``_handle_heartbeat`` ->
        ``_llm_work_task``/``_llm_suggest_task`` -> ``_llm_call``) iterated
        tiers sequentially with a 45s per-call httpx timeout and no aggregate
        cap. A slow/hung provider compounded to ~135s (3 tiers x 45s), blocking
        the heartbeat request past the upstream OpenClaw client timeout — which
        surfaced "LLM request timed out" alerts from the Telegram heartbeat.
        Unlike ``test_heartbeat_llm_thinking_graceful_failure`` (instant
        exception), this exercises a *slow* provider so the compounding shows.
        """
        import asyncio
        import time as _time

        import llm_proxy

        async def _hang(provider, models, body, api_key):
            # Slower than the (shrunk) deadline — simulates a hung provider.
            await asyncio.sleep(2)
            return _mock_response(200)

        original = llm_proxy.LLM_CALL_DEADLINE
        llm_proxy.LLM_CALL_DEADLINE = 0.3
        try:
            with patch("llm_proxy._call_provider", _hang):
                start = _time.monotonic()
                result = asyncio.run(llm_proxy._llm_call("ping", max_tokens=10))
                elapsed = _time.monotonic() - start
        finally:
            llm_proxy.LLM_CALL_DEADLINE = original

        # Deadline hit -> no usable text, treated as a graceful miss upstream.
        assert result is None
        # Bounded by the deadline, NOT 3 tiers x 2s sequential.
        assert elapsed < 1.0, (
            f"_llm_call blocked {elapsed:.2f}s — deadline not enforced"
        )


class TestSemanticDedup:
    """Tests for keyword-overlap task deduplication."""

    def test_extract_keywords_filters_stop_words(self):
        kw = heartbeat._extract_keywords(
            "Review the ClawRange tier allocation for current balance"
        )
        assert "the" not in kw
        assert "for" not in kw
        assert "clawrange" in kw
        assert "tier" in kw
        assert "allocation" in kw
        assert "balance" in kw

    def test_extract_keywords_skips_short_words(self):
        kw = heartbeat._extract_keywords("go do it now")
        assert "go" not in kw
        assert "do" not in kw
        assert "it" not in kw
        assert "now" in kw

    def test_semantic_dedup_catches_rephrased_tasks(self):
        """Two differently worded tier-review tasks should be flagged as duplicates."""
        from heartbeat import _has_recent_task

        queue = [
            {
                "id": "abc123",
                "description": (
                    "Review ClawRange tier allocation against current MSP client load"
                ),
                "status": "pending",
                "priority": 3,
                "created_at": datetime.now(UTC).isoformat(),
            }
        ]
        # Rephrased version of the same task
        similar = "Review ClawRange service tier balance and flag approaching limits"
        assert _has_recent_task(queue, similar) is True

    def test_semantic_dedup_allows_different_tasks(self):
        """Genuinely different tasks should not be flagged as duplicates."""
        from heartbeat import _has_recent_task

        queue = [
            {
                "id": "abc123",
                "description": (
                    "Review ClawRange tier allocation against MSP client load"
                ),
                "status": "pending",
                "priority": 3,
                "created_at": datetime.now(UTC).isoformat(),
            }
        ]
        different = "Send weekly invoice summary to accounting team"
        assert _has_recent_task(queue, different) is False

    def test_semantic_dedup_ignores_expired_tasks(self):
        """Tasks older than the cutoff should not block new ones."""
        from heartbeat import _has_recent_task

        old_time = (
            datetime.now(UTC) - __import__("datetime").timedelta(hours=25)
        ).isoformat()
        queue = [
            {
                "id": "abc123",
                "description": (
                    "Review ClawRange tier allocation against MSP client load"
                ),
                "status": "pending",
                "priority": 3,
                "created_at": old_time,
            }
        ]
        similar = "Review ClawRange service tier balance and limits"
        assert _has_recent_task(queue, similar) is False

    @pytest.mark.parametrize(
        ("existing", "suggestion"),
        [
            (
                "Review ClawRange tier allocation against MSP client load",
                "Review ClawRange service tier balance and limits",
            ),
            ("Send weekly invoice summary", "Draft blog outline on homelab"),
            ("Draft LinkedIn post", "Draft post"),
            ("the and of", "Draft LinkedIn post about caching"),
        ],
    )
    def test_recent_task_dedup_uses_the_shared_overlap_rule(self, existing, suggestion):
        """S11: the heartbeat dedup and the waiting-on-you footer share one
        keyword-overlap rule, so they agree on every pair."""
        task = {"description": existing, "created_at": datetime.now(UTC).isoformat()}
        expected = heartbeat._descriptions_overlap({"description": suggestion}, task)
        assert heartbeat._has_recent_task([task], suggestion) is expected

    def test_recent_task_dedup_delegates_to_descriptions_overlap(self, monkeypatch):
        """S11: _has_recent_task carries no private copy of the rule."""
        calls = []
        monkeypatch.setattr(
            heartbeat,
            "_descriptions_overlap",
            lambda a, b: calls.append((a, b)) or True,
        )
        task = {"description": "zzz", "created_at": datetime.now(UTC).isoformat()}
        assert heartbeat._has_recent_task([task], "qqq") is True
        assert calls == [({"description": "qqq"}, task)]

    def test_llm_thinking_interval_is_fifteen_minutes(self):
        """Verify LLM thinking interval is 900s (15 min), not 300s."""
        from heartbeat import _PROACTIVE_INTERVALS

        assert _PROACTIVE_INTERVALS["llm_thinking"] == 900


class TestNonWebSearchTierIteration:
    """Non-search calls iterate CONFIG['tiers'] in order, first wins."""

    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy._call_provider", new_callable=AsyncMock)
    @patch("llm_proxy._circuit_open", return_value=False)
    def test_non_web_search_uses_all_tiers(self, _cb, mock_call):
        """When web_search=False, all tiers are available (first wins)."""
        import asyncio

        from llm_proxy import _llm_call

        mock_call.return_value = httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "hello"}}]},
        )

        result = asyncio.run(_llm_call("greet me", max_tokens=100, web_search=False))

        assert result == "hello"
        assert mock_call.call_count == 1
        # First tier is openrouter-free
        assert mock_call.call_args[0][0] == "openrouter"


class TestOpenRouterCitationExtraction:
    """Extract url_citation annotations from OpenRouter :online responses."""

    def test_extracts_url_citations(self):
        from llm_proxy import _extract_openrouter_citations

        annotations = [
            {
                "type": "url_citation",
                "url_citation": {
                    "url": "https://example.com/a",
                    "title": "Example A",
                    "content": "snippet A",
                    "start_index": 0,
                    "end_index": 10,
                },
            },
            {
                "type": "url_citation",
                "url_citation": {
                    "url": "https://example.com/b",
                    "title": "Example B",
                    "content": "snippet B",
                },
            },
        ]
        result = _extract_openrouter_citations(annotations)
        assert "https://example.com/a" in result
        assert "https://example.com/b" in result
        assert "Example A" in result
        assert "1." in result
        assert "2." in result

    def test_empty_returns_empty(self):
        from llm_proxy import _extract_openrouter_citations

        assert _extract_openrouter_citations([]) == ""

    def test_non_url_citation_ignored(self):
        from llm_proxy import _extract_openrouter_citations

        annotations = [{"type": "function_call"}]
        assert _extract_openrouter_citations(annotations) == ""

    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy._call_provider", new_callable=AsyncMock)
    @patch("llm_proxy._circuit_open", return_value=False)
    # Search is paid: it needs a known balance above the floor (#23).
    @patch("llm_proxy._check_openrouter_balance", AsyncMock(return_value=50.0))
    def test_openrouter_citations_appended_to_content(self, _cb, mock_call):
        """OpenRouter response with content + annotations merges them."""
        import asyncio

        from llm_proxy import _llm_call

        mock_call.return_value = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Here is the answer.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://example.com",
                                        "title": "Source",
                                        "content": "Some context",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )
        result = asyncio.run(_llm_call("find stuff", max_tokens=500, web_search=True))

        assert result is not None
        assert "Here is the answer." in result
        assert "https://example.com" in result
        assert "Source" in result


class TestTaskNeedsWeb:
    """Keyword-driven detection that decides if a task needs live web data."""

    def test_keyword_match_triggers_web_search(self):
        """
        GIVEN a task description containing a known web-search keyword
        WHEN _task_needs_web is called
        THEN it returns True so the work runner enables GLM web search.
        """
        from heartbeat import _task_needs_web

        for desc in (
            "Find recent reddit posts about MSP pricing",
            "Search for latest hacker news on Claude",
            "Scan Lobsters for new self-hosted tools",
            "Trending posts on x.com today",
            "Give me a rundown of the news",
        ):
            assert _task_needs_web(desc) is True, f"expected web for: {desc!r}"

    def test_no_keyword_returns_false(self):
        """
        GIVEN a task description with no web-search keywords
        WHEN _task_needs_web is called
        THEN it returns False so the LLM answers from training data.
        """
        from heartbeat import _task_needs_web

        for desc in (
            "Summarize today's standup notes",
            "Draft the invoice for Acme Co",
            "Compute the next maintenance window",
            "Review the brain entries for client X",
        ):
            assert _task_needs_web(desc) is False, f"unexpected web for: {desc!r}"

    def test_match_is_case_insensitive(self):
        """Keyword detection lowercases the input — REDDIT and Reddit must both
        match."""
        from heartbeat import _task_needs_web

        assert _task_needs_web("Scan REDDIT for promo opportunities") is True
        assert _task_needs_web("scan reddit for promo opportunities") is True

    def test_keyword_set_locks_in_known_keywords(self):
        """
        Lock the canonical keyword set. If a keyword is removed, this fails so
        the change becomes a conscious decision rather than silent drift.
        """
        from heartbeat import _WEB_SEARCH_KEYWORDS

        # Anchors that must remain — adding new keywords is fine, deletions need
        # an explicit human review since downstream tasks rely on them.
        anchors = {"reddit", "search", "news", "trending", "scan", "web"}
        missing = anchors - _WEB_SEARCH_KEYWORDS
        assert not missing, f"web-search anchor keywords removed: {missing}"


class TestLlmWorkTaskRouting:
    """_llm_work_task wires _task_needs_web into _llm_call with the right max_tokens."""

    @patch.dict("os.environ", FAKE_ENV)
    def test_web_task_uses_1500_max_tokens_and_zai_search(self):
        """
        GIVEN a task description that triggers web search
        WHEN _llm_work_task runs
        THEN _llm_call is invoked with max_tokens=1500 and web_search=True
             (1500 was lifted from 500 so detailed link lists fit in Telegram).
        """
        import asyncio

        import llm_proxy

        captured = {}

        async def fake_call(prompt, max_tokens, web_search=False):
            captured["max_tokens"] = max_tokens
            captured["web_search"] = web_search
            captured["prompt"] = prompt
            return "ok"

        async def fake_prompt(desc, web_search=False, brain_db=None):
            return f"PROMPT[{web_search}]: {desc}"

        with (
            patch.object(llm_proxy, "_llm_call", side_effect=fake_call),
            patch.object(heartbeat, "_build_work_prompt", side_effect=fake_prompt),
        ):
            result = asyncio.run(
                heartbeat._llm_work_task("Scan reddit for MSP pain points", brain_db)
            )

        assert result == "ok"
        assert captured["max_tokens"] == 1500
        assert captured["web_search"] is True

    @patch.dict("os.environ", FAKE_ENV)
    def test_non_web_task_uses_500_max_tokens(self):
        """
        GIVEN a task description without web keywords
        WHEN _llm_work_task runs
        THEN _llm_call is invoked with max_tokens=500 and web_search=False.
        """
        import asyncio

        import llm_proxy

        captured = {}

        async def fake_call(prompt, max_tokens, web_search=False):
            captured["max_tokens"] = max_tokens
            captured["web_search"] = web_search
            return "summary"

        async def fake_prompt(desc, web_search=False, brain_db=None):
            return f"PROMPT[{web_search}]: {desc}"

        with (
            patch.object(llm_proxy, "_llm_call", side_effect=fake_call),
            patch.object(heartbeat, "_build_work_prompt", side_effect=fake_prompt),
        ):
            result = asyncio.run(
                heartbeat._llm_work_task("Summarize standup notes", brain_db)
            )

        assert result == "summary"
        assert captured["max_tokens"] == 500
        assert captured["web_search"] is False

    @patch.dict("os.environ", FAKE_ENV)
    @pytest.mark.usefixtures("marketing_persona")
    def test_empty_llm_response_raises_llm_unavailable(self):
        """
        GIVEN _llm_call returns an empty string (all tiers down)
        WHEN _llm_work_task runs
        THEN it raises LLMUnavailable, so the heartbeat re-queues the task
             instead of storing an apology as its completed result.
        """
        import asyncio

        import llm_proxy

        async def fake_call(prompt, max_tokens, web_search=False):
            return ""

        async def fake_prompt(desc, web_search=False, brain_db=None):
            return "PROMPT"

        with (
            patch.object(llm_proxy, "_llm_call", side_effect=fake_call),
            patch.object(heartbeat, "_build_work_prompt", side_effect=fake_prompt),
        ):
            with pytest.raises(heartbeat.LLMUnavailable):
                asyncio.run(heartbeat._llm_work_task("Send weekly invoice", brain_db))


class TestBuildWorkPromptFormatting:
    """_build_work_prompt injects Telegram-format rules only when web_search
    is enabled."""

    @patch.dict("os.environ", FAKE_ENV)
    def test_web_search_branch_includes_telegram_format_rules(self):
        """
        GIVEN web_search=True
        WHEN _build_work_prompt runs
        THEN the prompt contains Telegram-oriented format rules
             (numbered items, full URLs, suggested comments).
        """
        import asyncio

        with (
            patch.object(heartbeat, "_load_soul", return_value=""),
            patch.object(
                heartbeat,
                "_gather_system_state",
                new=AsyncMock(return_value="STATE"),
            ),
        ):
            prompt = asyncio.run(
                heartbeat._build_work_prompt(
                    "Find reddit threads", web_search=True, brain_db=brain_db
                )
            )

        assert "OUTPUT FORMAT (for Telegram delivery):" in prompt
        assert "Include full URLs" in prompt
        assert "suggested comment" in prompt
        assert "numbered items" in prompt.lower()

    @patch.dict("os.environ", FAKE_ENV)
    def test_non_web_search_branch_omits_telegram_format_rules(self):
        """
        GIVEN web_search=False
        WHEN _build_work_prompt runs
        THEN no Telegram format rules are appended (saves tokens for tasks
             that don't return link lists).
        """
        import asyncio

        with (
            patch.object(heartbeat, "_load_soul", return_value=""),
            patch.object(
                heartbeat,
                "_gather_system_state",
                new=AsyncMock(return_value="STATE"),
            ),
        ):
            prompt = asyncio.run(
                heartbeat._build_work_prompt(
                    "Summarize standup", web_search=False, brain_db=brain_db
                )
            )

        assert "OUTPUT FORMAT (for Telegram delivery):" not in prompt
        assert "suggested comment" not in prompt

    @patch.dict("os.environ", FAKE_ENV)
    def test_url_invention_guard_always_present(self):
        """
        Anti-hallucination guard for URLs is in the shared RULES block, so it
        must appear regardless of web_search flag. Locks in the wording the
        proxy relies on for grounding.
        """
        import asyncio

        with (
            patch.object(heartbeat, "_load_soul", return_value=""),
            patch.object(
                heartbeat,
                "_gather_system_state",
                new=AsyncMock(return_value="STATE"),
            ),
        ):
            for flag in (True, False):
                prompt = asyncio.run(
                    heartbeat._build_work_prompt(
                        "any task", web_search=flag, brain_db=brain_db
                    )
                )
                assert "Do NOT invent URLs" in prompt, f"missing for web={flag}"


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestAntiHallucinationTrailer:
    """Trailing system message must steer the model away from fake tool syntax."""

    def setup_method(self):
        _reset_state()

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_all_succeed)
    def test_trailing_message_blocks_fake_tool_syntax(self, mock_call, _mock_bg):
        """
        GIVEN a chat request without tool schemas
        WHEN the proxy forwards the request
        THEN a trailing system message is appended that blocks fabricated
             XML/bracket/tool_call output WITHOUT denying real capabilities
             (regression guard for commit 2fa753c).
        """
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers=AUTH_HEADER,
        )
        assert r.status_code == 200
        sent_messages = mock_call.call_args.args[2]["messages"]
        trailer = sent_messages[-1]
        assert trailer["role"] == "system"
        content = trailer["content"]
        # Current wording — assert the steering language is present.
        assert "XML tags" in content
        assert "bracket syntax" in content
        assert "tool_call" in content
        # Regression guard: the old "you have zero tools" wording is gone.
        assert "zero tools" not in content.lower()

    @patch("llm_proxy._background_notify")
    @patch("llm_proxy._call_provider", side_effect=_all_succeed)
    def test_trailing_message_present_with_tools_demands_openai_format(
        self, mock_call, _mock_bg
    ):
        """
        GIVEN a chat request that includes a tools schema
        WHEN the proxy forwards the request
        THEN the trailer IS appended with wording that requires the OpenAI
             tool_call format and forbids XML/bracket alternatives. Free-tier
             upstream models often get tool schemas they don't actually
             support and improvise <tool_call:NAME> XML — the rule must
             always run, with wording adapted to the tools-present case.
        """
        body = {
            "messages": [{"role": "user", "content": "do work"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "noop",
                        "description": "no-op",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
        r = client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
        assert r.status_code == 200
        sent_messages = mock_call.call_args.args[2]["messages"]
        trailer = sent_messages[-1]
        assert trailer["role"] == "system"
        content = trailer["content"]
        # Demands real OpenAI tool_call format
        assert "tool_call" in content.lower()
        # Explicitly forbids XML/bracket alternatives
        assert "xml" in content.lower() or "bracket" in content.lower()


# ─── Embeddings (OpenRouter, for bearer-token clients) ────────────

EMBED_BODY = {"input": "remember this", "model": "text-embedding-3-small"}

FAKE_EMBED_RESPONSE = {
    "object": "list",
    "model": "embedding-3",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
    "usage": {"prompt_tokens": 3, "total_tokens": 3},
}


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestEmbeddings:
    """/v1/embeddings serves bearer-token clients through OpenRouter (a GLM Coding
    Plan Z.AI key cannot embed: error 1211). The configured model is always
    used, so every stored vector is comparable."""

    def test_rejects_missing_auth(self):
        r = client.post("/v1/embeddings", json=EMBED_BODY)
        assert r.status_code == 401

    def test_missing_input_returns_400(self):
        r = client.post("/v1/embeddings", json={"model": "x"}, headers=AUTH_HEADER)
        assert r.status_code == 400

    @patch("embeddings.request", new_callable=AsyncMock)
    def test_returns_embeddings_via_openrouter(self, mock_req):
        mock_req.return_value = _mock_response(200, FAKE_EMBED_RESPONSE)
        r = client.post("/v1/embeddings", json=EMBED_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200
        assert r.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]
        inputs, cfg = mock_req.call_args.args
        # Whatever model the caller names, the configured one is used, keyed
        # with OPENROUTER_API_KEY.
        assert cfg.model == "openai/text-embedding-3-small"
        assert cfg.api_key == "test-key"
        assert inputs == [EMBED_BODY["input"]] or inputs == EMBED_BODY["input"]

    def test_missing_key_returns_503(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}, clear=False):
            r = client.post("/v1/embeddings", json=EMBED_BODY, headers=AUTH_HEADER)
        assert r.status_code == 503

    @patch("embeddings.request", new_callable=AsyncMock)
    def test_upstream_non_200_maps_to_same_status(self, mock_req):
        """Upstream errors must surface as errors, never as HTTP 200 bodies
        that a client would ingest as embeddings."""
        mock_req.return_value = _mock_response(429, {"error": "rate limited"})
        r = client.post("/v1/embeddings", json=EMBED_BODY, headers=AUTH_HEADER)
        assert r.status_code == 429
        assert "429" in r.json()["detail"]

    @patch("embeddings.request", new_callable=AsyncMock)
    def test_upstream_transport_error_is_502(self, mock_req):
        mock_req.side_effect = httpx.ConnectError("boom")
        r = client.post("/v1/embeddings", json=EMBED_BODY, headers=AUTH_HEADER)
        assert r.status_code == 502


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestPersonaCommand:
    def test_persona_feedback_intercepted(self):
        body = {
            "messages": [
                {"role": "user", "content": "!persona lead with the recommendation"}
            ]
        }
        with patch("persona_learning.propose", return_value={"id": "ab12"}) as m:
            r = client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
        assert r.status_code == 200
        assert m.called
        content = r.json()["choices"][0]["message"]["content"].lower()
        assert "draft" in content or "queued" in content

    def test_persona_command_case_insensitive(self):
        body = {
            "messages": [
                {"role": "user", "content": "!Persona Lead with THE recommendation"}
            ]
        }
        with patch("persona_learning.propose", return_value={"id": "cd34"}) as m:
            r = client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
        assert r.status_code == 200
        assert m.called
        # the proposed content must preserve original casing of the args
        assert "Lead with THE recommendation" in m.call_args.args[4]

    def test_learn_alias_intercepted(self):
        """Spec §6: !learn <feedback> is an alias for !persona <feedback>."""
        body = {"messages": [{"role": "user", "content": "!learn keep answers short"}]}
        with patch("persona_learning.propose", return_value={"id": "ef56"}) as m:
            r = client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
        assert r.status_code == 200
        assert m.called
        assert "keep answers short" in m.call_args.args[4]

    # The default-target check (N1) moved to test_fix_proxy.py, where it runs
    # against a real learning row instead of a helper signature.

    def test_persona_propose_failure_degrades_gracefully(self):
        """N2: a rejected proposal yields a friendly synthetic response,
        not a raw 500 to Telegram."""
        body = {"messages": [{"role": "user", "content": "!persona be brief"}]}
        with patch("persona_learning.propose", side_effect=ValueError("too long")):
            r = client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"].lower()
        assert "not queued" in content and "too long" in content

    def test_persona_reflect_failure_degrades_gracefully(self):
        body = {"messages": [{"role": "user", "content": "!persona reflect"}]}
        with patch("persona_api._run_reflection", side_effect=RuntimeError("down")):
            r = client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"].lower()
        assert "failed" in content


class TestDigestPersistence:
    """The heartbeat digest is the only path to Telegram, so its clock and
    buffer must survive a process restart.

    Before this, ``_digest_state`` was built at import with
    ``time.monotonic()``. Every container recreate or ``uvicorn --reload``
    reset the clock to zero and dropped the buffer, so a process restarting
    more often than HEARTBEAT_DIGEST_INTERVAL never reported at all.
    """

    def _fresh(self, monkeypatch, tmp_path):
        state_file = tmp_path / "digest.json"
        monkeypatch.setenv("HEARTBEAT_DIGEST_STATE", str(state_file))
        monkeypatch.setattr(digest, "_digest_state", {"lines": [], "last_flush": None})
        return digest, state_file

    def test_buffered_lines_survive_restart(self, monkeypatch, tmp_path):
        proxy, state_file = self._fresh(monkeypatch, tmp_path)
        proxy._digest_record("Created #1 [P3] investigate tier recovery")
        proxy._digest_record("[SYSTEM] #2: low balance")

        # Simulate the re-import that a restart or --reload performs.
        monkeypatch.setattr(proxy, "_digest_state", {"lines": [], "last_flush": None})
        proxy._digest_load()

        assert proxy._digest_state["lines"] == [
            "Created #1 [P3] investigate tier recovery",
            "[SYSTEM] #2: low balance",
        ]

    def test_clock_survives_restart(self, monkeypatch, tmp_path):
        proxy, _ = self._fresh(monkeypatch, tmp_path)
        now = time.time()
        proxy._digest_record("something")
        _take_digest(proxy, now, None, [])

        monkeypatch.setattr(proxy, "_digest_state", {"lines": [], "last_flush": None})
        # A restart one second later must NOT reset the hour.
        assert proxy._digest_due(now + 1) is False

    def test_due_immediately_when_no_state_file(self, monkeypatch, tmp_path):
        proxy, _ = self._fresh(monkeypatch, tmp_path)
        # A fresh deploy must not cost a silent hour.
        assert proxy._digest_due(time.time()) is True

    def test_corrupt_state_file_does_not_raise(self, monkeypatch, tmp_path):
        proxy, state_file = self._fresh(monkeypatch, tmp_path)
        state_file.write_text("{not json")
        proxy._digest_load()
        assert proxy._digest_state["lines"] == []

    def test_digest_caps_at_telegram_limit(self, monkeypatch, tmp_path):
        proxy, _ = self._fresh(monkeypatch, tmp_path)
        # 40 lines x ~550 chars is far past Telegram's 4096-char sendMessage
        # limit. Items must never be dropped to fit: each gets its own
        # message, and the relayed summary keeps the actionable footer.
        for i in range(40):
            proxy._digest_record(f"[SYSTEM] #{i}: " + "x" * 540)

        messages, summary = _take_digest(proxy, time.time(), 28.65, [])

        assert messages
        assert all(len(m) <= telegram.MAX_CHARS for m in messages)
        # The overflow pointer names what was deferred — nothing silently lost.
        assert "more item" in messages[-1]
        # The balance footer must survive — it is the actionable bit.
        assert "$28.65" in summary


class TestDigestMultiMessageFlush:
    """The digest rode the heartbeat response, which OpenClaw relays as a
    single Telegram message — so a busy hour truncated results to 500 chars
    and dropped whole items to fit 4096.

    The flush now sends one message per item straight to Telegram and leaves
    the heartbeat response as a short closing summary. Long items chunk
    across messages instead of being clipped; a runaway hour caps the direct
    sends (Telegram rate-limits bots to ~20 messages/minute per chat).
    """

    def _fresh(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HEARTBEAT_DIGEST_STATE", str(tmp_path / "digest.json"))
        monkeypatch.setattr(digest, "_digest_state", {"lines": [], "last_flush": None})
        return digest

    def test_one_message_per_item_with_header_on_first(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)
        proxy._digest_record("[SYSTEM] #1: first")
        proxy._digest_record("[SYSTEM] #2: second")

        messages, summary = _take_digest(proxy, time.time(), 28.65, [])

        assert messages == [
            "Heartbeat digest (2 items this hour):\n\n[SYSTEM] #1: first",
            "[SYSTEM] #2: second",
        ]
        assert all(len(m) <= telegram.MAX_CHARS for m in messages)
        # The relayed summary carries the actionable footer.
        assert "2 item(s) this hour" in summary
        assert "$28.65" in summary

    def test_single_item_uses_singular_header(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)
        proxy._digest_record("[SYSTEM] #1: only")

        messages, summary = _take_digest(proxy, time.time(), None, [])

        assert messages[0].startswith("Heartbeat digest (1 item this hour):")
        assert "1 item(s) this hour" in summary

    def test_oversized_item_chunks_instead_of_dropping(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)
        # No sentence or word boundaries: forces hard cuts at the limit.
        proxy._digest_record("[SYSTEM] #1: " + "x" * 9000 + " TAIL-SENTINEL")

        messages, _ = _take_digest(proxy, time.time(), None, [])

        assert len(messages) >= 3
        assert all(len(m) <= telegram.MAX_CHARS for m in messages)
        # The tail survives — the old single-message format dropped it.
        assert messages[-1].endswith("TAIL-SENTINEL")
        # Continuations are marked so the burst reads as one item.
        assert all(m.startswith("…") for m in messages[1:])

    def test_runaway_hour_caps_direct_messages(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)
        for i in range(30):
            proxy._digest_record(f"[SYSTEM] #{i}: item")

        messages, summary = _take_digest(proxy, time.time(), None, [])

        # 19 item messages + 1 pointer = the 20-send cap, pointer names the 11
        # deferred items — nothing silently lost.
        assert len(messages) == proxy.HEARTBEAT_DIGEST_MAX_MESSAGES
        assert "11 more item" in messages[-1]
        assert "30 item(s) this hour" in summary

    def test_silent_hour_with_no_footer_stays_silent(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)

        messages, summary = _take_digest(proxy, time.time(), None, [])

        assert messages == []
        assert summary == ""

    def test_waiting_questions_surface_without_items(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)

        messages, summary = _take_digest(
            proxy, time.time(), None, [], waiting=["Waiting on you: #12 — domain?"]
        )

        assert messages == []
        assert "nothing new this hour" in summary
        assert "domain?" in summary


BLOCKED_RESULT = """# Task: Record Alex's Focus Areas & Priorities

Status: BLOCKED — Need Input

## What I Found
- Brain is empty (no prior context on Alex's focus areas or clients)

## What I Need From You
To populate the brain accurately, I need you to tell me:
1. Client segments — Who do you work with?
2. Outreach priorities — What matters most this quarter?
"""

WORKED_RESULT = """Scanned r/msp for ticketing-automation threads over the last
24h. Three relevant posts, links below, each with a suggested comment.
"""


class TestNeedsInput:
    """A self-directed task that turns out to need Alex's private knowledge
    must not be dumped into Telegram as a 500-char non-answer.

    _is_non_answer() only fires under 300 chars, so a long, well-formatted
    "BLOCKED -- Need Input" essay sailed through and was relayed in full.
    """

    def test_detects_blocked_result(self):

        assert heartbeat._needs_input(BLOCKED_RESULT) is True

    def test_detects_hold_for_now_result(self):

        text = (
            "## Recommendation\n**Hold for now.** Before running this canary, "
            "we need:\n1. Confirmation the fix shipped\n\n## What I Need From You\n"
            "Should I wait for this info, or do you want me to dig in first?"
        )
        assert heartbeat._needs_input(text) is True

    def test_real_work_is_not_flagged(self):

        assert heartbeat._needs_input(WORKED_RESULT) is False

    def test_empty_result_is_not_flagged(self):

        assert heartbeat._needs_input("") is False

    def test_blocked_task_gets_compact_digest_line(self, monkeypatch):
        """The digest carries one line naming what stalled, not the essay."""

        line = heartbeat._blocked_digest_line(
            {
                "id": "73d5f106",
                "description": (
                    "Record Alex's current focus areas, client segments, and "
                    "outreach priorities into the brain"
                ),
            }
        )
        assert line.startswith("Blocked #73d5f106: needs your input —")
        assert len(line) <= 120
        assert "What I Need From You" not in line


class TestThinkingPromptDoesNotAskAlex:
    """An empty brain used to instruct the model to suggest knowledge-building
    tasks ("record a client"), which only Alex can answer. That closed a loop:
    empty brain -> suggest -> blocked -> brain still empty -> repeat hourly.
    """

    def _prompt(self, monkeypatch):
        # _load_soul() reads /app/soul.md, absent under test -- without a soul
        # _build_thinking_prompt falls back to a short branch that carries
        # neither rule, so the assertions would pass vacuously.

        monkeypatch.setattr(heartbeat, "_load_soul", lambda: "You are Max.")
        return heartbeat._build_thinking_prompt(["skrills"], brain_db=brain_db)

    def test_prompt_does_not_ask_for_knowledge_building(self, monkeypatch):
        assert "suggest tasks that BUILD knowledge" not in self._prompt(monkeypatch)

    @pytest.mark.usefixtures("marketing_persona")
    def test_prompt_forbids_tasks_needing_alex(self, monkeypatch):
        assert "only Alex can supply" in self._prompt(monkeypatch)


class TestBlockedTaskStatus:
    def test_complete_task_accepts_blocked(self):

        t = brain_db.create_task("needs alex input", priority=3)
        brain_db.complete_task(t["id"], "BLOCKED — Need Input", "blocked")
        got = brain_db.get_task(t["id"])
        assert got["status"] == "blocked"

    def test_blocked_task_is_not_pending(self):
        """A blocked task must leave the pending queue or the heartbeat
        reworks it every cycle -- the loop this fix exists to break."""

        t = brain_db.create_task("needs alex input", priority=3)
        brain_db.complete_task(t["id"], "BLOCKED", "blocked")
        pending_ids = {x["id"] for x in brain_db.list_tasks(status="pending")}
        assert t["id"] not in pending_ids


class TestNeedsInputPrecision:
    """Guards found by running the detector over all 846 stored results.

    A first cut counted request-for-input phrases anywhere in the body and
    flagged 232 of 846 (27%) -- real deliverables, silently binned.
    """

    def test_prepared_work_naming_next_steps_is_not_blocked(self):
        """The work prompt asks the model to say "what Alex needs to do to
        finish it", so that phrasing appears in 208 real results."""

        text = (
            "# Draft comment for r/msp\n"
            "**Status:** Ready to post. Draft below.\n\n"
            "## What I Need From You\n"
            "Post it to the thread; I can't post on your behalf.\n"
        )
        assert heartbeat._needs_input(text) is False

    def test_blocked_subsection_does_not_bin_the_deliverable(self):
        """One blocked angle inside finished work is still finished work."""

        text = (
            "# Draft: Content Package\n"
            "**Status:** Ready for review. Three angles structured below.\n\n"
            + "Angle 1 detail. "
            * 40
            + "\n## Angle 2: Reddit comment (BLOCKED—needs live data)\n"
            "**What I need from you:** the positioning.\n"
        )
        assert heartbeat._needs_input(text) is False


# ─── Waiting-on-you digest footer ─────────────────────────────────
# A task that came back BLOCKED asked Alex a question. The ask-once
# design (blocked tasks leave the pending queue and dedup stops the
# LLM re-suggesting them) made the system go mute while waiting on an
# answer. The digest footer re-surfaces those questions until a later
# task answers them.


def _wtask(
    tid: str,
    desc: str,
    result: str = "",
    status: str = "completed",
    completed_at: datetime | None = None,
) -> dict:
    return {
        "id": tid,
        "description": desc,
        "result": result,
        "status": status,
        "completed_at": (completed_at or datetime.now(UTC)).isoformat(),
        "created_at": (completed_at or datetime.now(UTC)).isoformat(),
    }


class TestWaitingOnYouLines:
    def _lines(self, *tasks, now=None):

        return heartbeat._waiting_on_you_lines(list(tasks), now=now)

    def test_blocked_result_surfaces(self):
        """A recently completed task whose result declares BLOCKED gets a
        waiting line naming the task."""
        hour_ago = datetime.now(UTC) - timedelta(hours=1)
        lines = self._lines(
            _wtask(
                "abc12345",
                "Record Alex's focus areas",
                BLOCKED_RESULT,
                completed_at=hour_ago,
            )
        )
        assert len(lines) == 1
        assert "abc12345" in lines[0]
        assert "Record Alex's focus areas" in lines[0]
        assert lines[0].startswith("Waiting on you")

    def test_blocked_status_surfaces_even_without_signal_text(self):
        """Status='blocked' counts even if the result text lacks a
        detector signal (defense in depth: the detector is heuristic)."""
        hour_ago = datetime.now(UTC) - timedelta(hours=1)
        lines = self._lines(
            _wtask(
                "bbb22222",
                "Draft the launch email",
                "no signal here",
                status="blocked",
                completed_at=hour_ago,
            )
        )
        assert len(lines) == 1

    def test_superseded_by_later_success(self):
        """Once a later task with overlapping keywords completes
        successfully, the older blocked question is answered: drop it."""
        two_days_ago = datetime.now(UTC) - timedelta(days=2)
        one_day_ago = datetime.now(UTC) - timedelta(days=1)
        lines = self._lines(
            _wtask(
                "ccc33333",
                "Record Alex's focus areas and client segments",
                BLOCKED_RESULT,
                completed_at=two_days_ago,
            ),
            _wtask(
                "ddd44444",
                "Record Alex's focus areas and client segments",
                "Stored: focus areas, segments, priorities in the brain.",
                completed_at=one_day_ago,
            ),
        )
        assert lines == []

    def test_expired_after_window(self):
        """Blocked questions older than the 7-day window stop nagging."""
        eight_days_ago = datetime.now(UTC) - timedelta(days=8)
        lines = self._lines(
            _wtask(
                "eee55555", "Old question", BLOCKED_RESULT, completed_at=eight_days_ago
            )
        )
        assert lines == []

    def test_near_duplicates_keep_only_newest(self):
        """The LLM re-suggests the same blocked task after the dedup
        window expires; the footer must not stack both."""
        old = datetime.now(UTC) - timedelta(days=3)
        new = datetime.now(UTC) - timedelta(hours=2)
        lines = self._lines(
            _wtask(
                "fff66666",
                "Record Alex's current focus areas and segments",
                BLOCKED_RESULT,
                completed_at=old,
            ),
            _wtask(
                "7777777g",
                "Record Alex's current focus areas and segments",
                BLOCKED_RESULT,
                completed_at=new,
            ),
        )
        assert len(lines) == 1
        assert "7777777g" in lines[0]

    def test_caps_at_three(self):
        now = datetime.now(UTC)
        descs = [
            "Verify stripe billing handles usage overage",
            "Draft the github auth migration runbook",
            "Scan r/sideproject for launch feedback",
            "Export the CRM leads into notion",
            "Triage linear backlog by customer impact",
        ]
        tasks = [
            _wtask(
                f"t{i:08x}",
                desc,
                BLOCKED_RESULT,
                completed_at=now - timedelta(hours=i + 1),
            )
            for i, desc in enumerate(descs)
        ]
        assert len(self._lines(*tasks)) == 3


class TestDigestTakeWaiting:
    """The digest must be non-empty when the only content is unanswered
    questions — that is exactly the silence that looked like breakage."""

    def _fresh(self, monkeypatch, tmp_path):
        state_file = tmp_path / "digest.json"
        monkeypatch.setenv("HEARTBEAT_DIGEST_STATE", str(state_file))
        monkeypatch.setattr(digest, "_digest_state", {"lines": [], "last_flush": None})
        return digest

    def test_waiting_only_makes_digest_nonempty(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)
        messages, summary = _take_digest(
            proxy, time.time(), 28.64, [], waiting=["Waiting on you: #abc focus areas"]
        )
        assert messages == []
        assert summary != ""
        assert "nothing new" in summary
        assert "#abc" in summary
        assert "$28.64" in summary

    def test_waiting_appended_after_buffered_lines(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)
        proxy._digest_record("[SYSTEM] #1: did a thing")
        messages, summary = _take_digest(
            proxy, time.time(), None, [], waiting=["Waiting on you: #abc focus areas"]
        )
        assert any("did a thing" in m for m in messages)
        assert "#abc" in summary

    def test_empty_and_no_waiting_stays_silent(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)
        assert _take_digest(proxy, time.time(), None, []) == ([], "")

    def test_digest_header_pluralizes(self, monkeypatch, tmp_path):
        proxy = self._fresh(monkeypatch, tmp_path)
        messages = _plan_messages(proxy, ["[SYSTEM] #1: did a thing"])
        assert "1 item this hour" in messages[0]
        assert "item(s)" not in messages[0]
        messages = _plan_messages(proxy, ["a", "b"])
        assert "2 items this hour" in messages[0]


class TestClipResult:
    def test_short_result_unchanged(self):

        assert digest._clip_result("done") == "done"

    def test_long_result_clips_at_sentence_boundary(self):

        long = "word " * 80 + "Final sentence. " + "word " * 60
        clipped = digest._clip_result(long, limit=500)
        assert len(clipped) <= 500
        assert clipped.endswith("…")
        # The last whole sentence inside the budget survives.
        assert clipped.rstrip("…").endswith("Final sentence.")

    def test_early_sentence_mark_prefers_word_boundary(self):
        """A lone terminator near the start must not waste the budget."""

        long = "No. " + "word " * 120
        clipped = digest._clip_result(long, limit=500)
        assert len(clipped) > 100
        assert clipped.endswith("…")
        assert "word" in clipped

    def test_no_sentence_mark_falls_back_to_word_boundary(self):

        long = " ".join(f"w{i}" for i in range(200))
        clipped = digest._clip_result(long, limit=500)
        assert len(clipped) <= 500
        assert clipped.endswith("…")
        head = clipped.rstrip("…")
        # The clipped head is a prefix of the original at a word break.
        assert long.startswith(head)
        assert long[len(head) : len(head) + 1] == " "


class TestGroundedThinking:
    """Self-invented work is welcome when the worker can do it: research on
    Reddit/GitHub/web about a subject that exists (a tracked project or a
    homelab deal target). INVENTED are real system tasks from 2026-09 that
    needed a CRM, billing data, a calendar or internal docs."""

    SUBJECTS = [
        "claude-night-market",
        "skrills",
        "simple-resume",
        "clawrange",
        "Strix Halo 128GB",
        "RTX 3090 24GB",
    ]
    INVENTED = [
        "Query ClawRange FastAPI workflows documentation and best practices "
        "to identify optimization patterns that could reduce Alex's task "
        "queue processing latency and LLM proxy call overhead.",
        "Query billing records and usage logs to calculate LLM proxy "
        "cost-per-transaction and identify which client segments",
        "Query CRM to identify which clients in the active segment haven't "
        "had contact in 60+ days, then surface their last interaction type",
        "Query ClawRange infrastructure costs (compute, storage, LLM proxy "
        "calls) against monthly budget targets",
        "Create a personal wellness check-in template (weekly cadence: sleep, "
        "exercise, stress, focus) and schedule it",
        "Audit ClawRange infrastructure for secret exposure in logs, "
        "environment variables, and API responses",
        "Search the web for the top 3 inactive clients from Q3",
        "I need more context to suggest a meaningful task. What's Alex "
        "currently working on?",
    ]
    LEGIT = [
        "Research recent Reddit threads on Strix Halo 128GB local inference "
        "speed with 70B models",
        "Search GitHub for resume generators similar to simple-resume and "
        "note features it lacks",
        "Scan r/ClaudeAI posts asking for plugin marketplaces where "
        "claude-night-market would be a useful answer",
    ]

    def test_drops_invented_tasks(self):
        from heartbeat import _is_grounded_suggestion

        for text in self.INVENTED:
            assert not _is_grounded_suggestion(text, self.SUBJECTS), text

    def test_keeps_runnable_research_on_real_subjects(self):
        from heartbeat import _is_grounded_suggestion

        for text in self.LEGIT:
            assert _is_grounded_suggestion(text, self.SUBJECTS), text

    def test_subjects_from_projects_and_deal_targets(self):
        from heartbeat import _thinking_subjects

        subjects = _thinking_subjects(
            [{"slug": "skrills"}, {"slug": "simple-resume"}],
            {
                "targets": [
                    {
                        "name": "Strix Halo 128GB (Ryzen AI Max+ 395)",
                        "query": "x",
                        "max_price_great": 1,
                    },
                    {
                        "name": "RTX 3090 24GB",
                        "query": "rtx 3090",
                        "max_price_great": 1,
                    },
                ]
            },
        )
        assert subjects == [
            "skrills",
            "simple-resume",
            "Strix Halo 128GB",
            "RTX 3090 24GB",
        ]

    def test_subjects_without_deals_block(self):
        from heartbeat import _thinking_subjects

        assert _thinking_subjects([{"slug": "skrills"}], None) == ["skrills"]

    def test_prompt_lists_subjects_and_capabilities(self, monkeypatch):

        monkeypatch.setattr(heartbeat, "_load_soul", lambda: "You are Max.")
        prompt = heartbeat._build_thinking_prompt(
            self.SUBJECTS, "Stack gaps: vram: nothing with 24 GB+", brain_db=brain_db
        )
        for subject in self.SUBJECTS:
            assert subject in prompt
        assert "Reddit" in prompt and "GitHub" in prompt
        assert "Stack gaps: vram" in prompt
        assert "Focus area" not in prompt
        assert "client outreach" not in prompt


class TestGenericOperatorWording:
    """Without a configured name, prompts never fall back to a real person."""

    @pytest.fixture(autouse=True)
    def _starter(self, monkeypatch):
        import tenant_profile

        monkeypatch.delenv("CLAWRANGE_PROFILE", raising=False)
        monkeypatch.delenv("OPERATOR_NAME", raising=False)
        tenant_profile._ACTIVE.clear()
        yield
        tenant_profile._ACTIVE.clear()

    def test_fallback_names_the_operator_generically(self, monkeypatch):
        """The give-up note after the LLM retry limit names the operator."""

        task = brain_db.create_task("Send weekly invoice")
        monkeypatch.setattr(
            heartbeat, "_llm_retry_counts", {task["id"]: heartbeat.LLM_RETRY_LIMIT - 1}
        )
        monkeypatch.setattr(
            llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=50.0)
        )
        with (
            patch.object(llm_proxy, "_llm_call", AsyncMock(return_value="")),
            patch.object(heartbeat, "_build_work_prompt", AsyncMock(return_value="P")),
        ):
            asyncio.run(heartbeat._handle_heartbeat(False, brain_db=brain_db))
        result = brain_db.get_task(task["id"])["result"]
        # The starter profile's placeholder owner.name is "Operator".
        assert "Operator should handle it manually" in result
        assert "Alex" not in result

    def test_no_persona_hygiene_without_config(self):
        soul = "## Who You Are\nlore\n## What You Can Do\njob"
        assert heartbeat._strip_persona_backstory(soul) == soul
        assert not llm_proxy._is_persona_citation("https://halopedia.test", "x")
        assert heartbeat._operator_label() == "OPERATOR"

    def test_profile_without_owner_says_the_operator(self):
        import tenant_profile

        profile = tenant_profile.Profile(name="x", raw={"assistant": {}})
        assert tenant_profile.operator_name(profile) == "the operator"


def test_sentence_start_keeps_multi_word_names():
    # str.capitalize would turn "Mary Ann" into "Mary ann".
    assert heartbeat._sentence_start("Mary Ann") == "Mary Ann"
    assert heartbeat._sentence_start("the operator") == "The operator"
    assert heartbeat._sentence_start("") == ""


class TestLoadSoulPrefersRenderedWorkspace:
    """The proxy's own soul.md is a read-only bind of the repo template, so
    approved learnings (rendered into the OpenClaw workspace) never reached
    heartbeat prompts. Prefer the rendered workspace copy when present."""

    def test_workspace_soul_wins(self, tmp_path, monkeypatch):

        (tmp_path / "workspace").mkdir()
        (tmp_path / "workspace" / "SOUL.md").write_text("rendered soul\n")
        monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
        assert heartbeat._load_soul() == "rendered soul"

    def test_falls_back_to_bundled_soul(self, tmp_path, monkeypatch):

        bundled = tmp_path / "soul.md"
        bundled.write_text("bundled soul\n")
        monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "missing"))
        monkeypatch.setattr(heartbeat, "_SOUL_MD_PATH", bundled)
        assert heartbeat._load_soul() == "bundled soul"


class TestNextHeartbeatTask:
    """The heartbeat must only claim tasks it owns. `research:tome:` tasks
    belong to the local tome bridge; persona and income `[DRAFT]` tasks wait
    for the operator. Claiming them answered them with the free-tier LLM
    before their real consumer ever saw them."""

    def test_skips_tome_and_draft_tasks(self):

        pending = [
            {"id": "a", "description": "research:tome: agent platforms"},
            {"id": "b", "description": "[research:tome] x"},
            {"id": "c", "description": "Research via tome: y"},
            {"id": "d", "description": "[DRAFT] persona persona enhancement (Tone): x"},
            {"id": "e", "description": "[DRAFT] income: weekly strategy review"},
            {"id": "f", "description": "check disk usage"},
        ]
        assert heartbeat._next_heartbeat_task(pending)["id"] == "f"

    def test_comment_drafts_stay_heartbeat_owned(self):
        """Comment-draft tasks instruct the LLM worker to write the reply."""

        pending = [{"id": "a", "description": "[DRAFT] Comment draft for x on y."}]
        assert heartbeat._next_heartbeat_task(pending)["id"] == "a"

    def test_none_when_only_foreign_tasks(self):

        pending = [{"id": "a", "description": "[DRAFT] income: weekly review"}]
        assert heartbeat._next_heartbeat_task(pending) is None


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestHeartbeatLeavesForeignTasks:
    def setup_method(self):
        _reset_state()

        brain_db._conn.execute("DELETE FROM tasks")
        brain_db._conn.commit()

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_tome_task_stays_pending(self, _notify):
        import time as _time

        heartbeat._proactive_state["stale_tasks"] = _time.monotonic()
        heartbeat._proactive_state["llm_thinking"] = _time.monotonic()
        heartbeat._proactive_state["pending_reminder"] = _time.monotonic()
        t = brain_db.create_task("research:tome: agent platforms")
        caller = AsyncMock(return_value=_mock_response(200))
        with patch("llm_proxy._call_provider", caller):
            client.post(
                "/v1/chat/completions",
                json=TestHeartbeatInterceptor._heartbeat_body(None),
                headers=AUTH_HEADER,
            )
        assert brain_db.get_task(t["id"])["status"] == "pending"


class TestBalanceFailureBackoff:
    """A failed credits lookup used to leave checked_at untouched, so during
    an OpenRouter outage every request re-ran a 10s-timeout HTTP call (twice
    per chat request, before any tier started). Back off after a failure."""

    def test_failure_not_retried_immediately(self, monkeypatch):
        import asyncio

        import llm_proxy

        calls = {"n": 0}

        class FailingClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                calls["n"] += 1
                raise httpx.ConnectError("down")

        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", FailingClient)
        monkeypatch.setattr(
            llm_proxy, "_balance_cache", {"remaining": 42.0, "checked_at": None}
        )
        assert asyncio.run(llm_proxy._check_openrouter_balance()) == 42.0
        assert asyncio.run(llm_proxy._check_openrouter_balance()) == 42.0
        assert calls["n"] == 1


# ─── Review-finding regressions ──────────────────────────────────


def _collect_sse(sse_resp) -> list[dict]:
    async def _run():
        return [c async for c in sse_resp.body_iterator]

    raw = "".join(asyncio.run(_run()))
    return [
        json.loads(ln.removeprefix("data: "))
        for ln in raw.split("\n")
        if ln.startswith("data: ") and "[DONE]" not in ln
    ]


class TestStreamToolCalls:
    def test_null_content_tool_calls_stream(self):
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "exec", "arguments": '{"a": 1}'},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            },
        ]
        resp = JSONResponse(
            content={
                "id": "x",
                "model": "m",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": calls,
                        }
                    }
                ],
            }
        )
        chunks = _collect_sse(llm_proxy._wrap_json_as_sse(resp))
        tool_chunks = [c for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
        assert tool_chunks
        streamed = tool_chunks[0]["choices"][0]["delta"]["tool_calls"]
        assert [t["index"] for t in streamed] == [0, 1]
        assert streamed[0]["function"]["name"] == "exec"
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_null_content_without_tool_calls_does_not_crash(self):
        resp = JSONResponse(
            content={"choices": [{"message": {"role": "assistant", "content": None}}]}
        )
        chunks = _collect_sse(llm_proxy._wrap_json_as_sse(resp))
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


class TestBackgroundLlmCallFreeTiersOnly:
    @patch.dict("os.environ", FAKE_ENV)
    def test_llm_call_never_uses_paid_tier(self):
        paid = next(
            t for t in llm_proxy.CONFIG["tiers"] if t["name"] == "openrouter-paid"
        )
        seen: list[list[str]] = []

        async def fail(provider, models, body, api_key):
            seen.append(list(models))
            return _mock_response(500, {"error": "boom"})

        with patch("llm_proxy._call_provider", side_effect=fail):
            assert asyncio.run(llm_proxy._llm_call("hi")) is None
        assert seen
        assert paid["models"] not in seen

    @patch.dict("os.environ", FAKE_ENV)
    def test_llm_call_skips_tier_missing_from_provider_urls(self, monkeypatch):
        monkeypatch.delitem(llm_proxy.PROVIDER_URLS, "zai")
        providers: list[str] = []

        async def fail(provider, models, body, api_key):
            providers.append(provider)
            return _mock_response(500, {"error": "boom"})

        with patch("llm_proxy._call_provider", side_effect=fail):
            asyncio.run(llm_proxy._llm_call("hi"))
        assert "zai" not in providers


class TestLlmCallExceptionHandling:
    @patch.dict("os.environ", FAKE_ENV)
    def test_exception_logged_and_recorded(self, caplog):
        async def boom(provider, models, body, api_key):
            raise RuntimeError("kaput")

        with patch("llm_proxy._call_provider", side_effect=boom):
            with caplog.at_level("WARNING", logger=llm_proxy.logger.name):
                assert asyncio.run(llm_proxy._llm_call("hi")) is None
        assert "kaput" in caplog.text
        assert any(s["failures"] >= 1 for s in llm_proxy._circuit_state.values())

    @patch.dict("os.environ", FAKE_ENV)
    def test_429_in_background_call_trips_breaker(self):
        async def limited(provider, models, body, api_key):
            return _mock_response(429, {"error": "rate limited"})

        with patch("llm_proxy._call_provider", side_effect=limited):
            asyncio.run(llm_proxy._llm_call("hi"))
        assert llm_proxy._circuit_open("openrouter-free", probe=False)


# Z.AI's reply when the account has no credit left: HTTP 429, code 1113
# ("insufficient balance or no resource package, please recharge"),
# captured from the live proxy log.
ZAI_NO_CREDIT = {
    "error": {"code": "1113", "message": "余额不足或无可用资源包,请充值。"}
}


class TestFailureReasons:
    """An outage must say why each tier failed, not just "unreachable"."""

    def setup_method(self):
        _reset_state()
        llm_proxy._tier_failure_reason.clear()

    @pytest.mark.parametrize(
        ("status", "body", "want"),
        [
            (429, ZAI_NO_CREDIT, "out of credit"),
            # The code alone decides when Z.AI rewords the message.
            (
                429,
                {"error": {"code": "1113", "message": "Please recharge"}},
                "out of credit",
            ),
            (402, {"error": {"message": "Insufficient credits"}}, "out of credit"),
            (429, {"error": {"message": "insufficient_quota"}}, "out of credit"),
            (429, {"error": "rate limited"}, "rate-limited"),
            (401, {"error": {"message": "invalid api key"}}, "API key rejected"),
            (503, {"error": "upstream"}, "HTTP 503"),
        ],
    )
    def test_classifies_http_failures(self, status, body, want):
        assert llm_proxy._classify_failure(_mock_response(status, body)) == want

    @patch.dict("os.environ", FAKE_ENV)
    def test_summary_names_each_tier_and_its_cause(self):
        """GIVEN openrouter-free answers empty and Z.AI is out of credit
        WHEN a background call fails on every tier
        THEN the summary says so per tier, and that paid was not tried."""

        async def fake(provider, models, body, api_key):
            if provider == "zai":
                return _mock_response(429, ZAI_NO_CREDIT)
            return _mock_response(200, {"choices": [{"message": {"content": ""}}]})

        with (
            patch("llm_proxy._call_provider", side_effect=fake),
            patch("llm_proxy._check_openrouter_balance", AsyncMock(return_value=17.0)),
        ):
            assert asyncio.run(llm_proxy._llm_call("hi")) is None
        summary = llm_proxy.describe_last_failure()
        assert "openrouter-free: empty reply" in summary
        assert "zai-direct: out of credit" in summary
        assert "openrouter-paid: not tried (paid is opt-in)" in summary

    @patch.dict("os.environ", FAKE_ENV)
    def test_open_breaker_keeps_the_cause_that_tripped_it(self):
        """GIVEN Z.AI tripped its breaker by running out of credit
        WHEN the next call skips it
        THEN the summary still says out of credit, not just breaker open."""

        async def fake(provider, models, body, api_key):
            if provider == "zai":
                return _mock_response(429, ZAI_NO_CREDIT)
            return _mock_response(503, {"error": "upstream"})

        with (
            patch("llm_proxy._call_provider", side_effect=fake),
            patch("llm_proxy._check_openrouter_balance", AsyncMock(return_value=17.0)),
        ):
            asyncio.run(llm_proxy._llm_call("hi"))
            asyncio.run(llm_proxy._llm_call("hi"))
        summary = llm_proxy.describe_last_failure()
        assert "zai-direct: out of credit (breaker open)" in summary

    @patch.dict("os.environ", FAKE_ENV)
    def test_depleted_openrouter_is_named_out_of_credit(self):
        """GIVEN the OpenRouter balance is at zero and Z.AI answers malformed
        WHEN a background call fails
        THEN OpenRouter reads as out of credit without being called, and
        Z.AI's reply is named malformed."""
        called = []

        async def fake(provider, models, body, api_key):
            called.append(provider)
            return httpx.Response(200, content=b"not json")

        with (
            patch("llm_proxy._call_provider", side_effect=fake),
            patch("llm_proxy._check_openrouter_balance", AsyncMock(return_value=0.0)),
        ):
            assert asyncio.run(llm_proxy._llm_call("hi")) is None
        summary = llm_proxy.describe_last_failure()
        assert "openrouter-free: out of credit (balance" in summary
        assert "zai-direct: malformed reply" in summary
        assert called == ["zai"]

    @patch.dict("os.environ", FAKE_ENV)
    def test_success_clears_the_summary(self):
        async def ok(provider, models, body, api_key):
            return _mock_response(200)

        llm_proxy._last_call_failures["zai-direct"] = "out of credit"
        with (
            patch("llm_proxy._call_provider", side_effect=ok),
            patch("llm_proxy._check_openrouter_balance", AsyncMock(return_value=17.0)),
        ):
            assert asyncio.run(llm_proxy._llm_call("hi"))
        assert llm_proxy.describe_last_failure() == ""

    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_requeue_digest_line_says_why(self, _n):
        """GIVEN every tier fails with known causes
        WHEN the heartbeat re-queues a task
        THEN its digest line names those causes instead of "LLM unreachable"."""
        brain_db._conn.execute("DELETE FROM tasks")
        brain_db._conn.commit()
        for key in ("stale_tasks", "llm_thinking", "pending_reminder"):
            heartbeat._proactive_state[key] = time.monotonic()
        t = brain_db.create_task("Summarize the open deals")

        async def fake(provider, models, body, api_key):
            if provider == "zai":
                return _mock_response(429, ZAI_NO_CREDIT)
            return _mock_response(200, {"choices": [{"message": {"content": ""}}]})

        with (
            patch("llm_proxy._call_provider", side_effect=fake),
            patch("llm_proxy._check_openrouter_balance", AsyncMock(return_value=17.0)),
        ):
            client.post(
                "/v1/chat/completions",
                json=TestHeartbeatInterceptor._heartbeat_body(None),
                headers=AUTH_HEADER,
            )
        assert brain_db.get_task(t["id"])["status"] == "pending"
        line = next(ln for ln in digest._digest_state["lines"] if t["id"] in ln)
        assert "zai-direct: out of credit" in line
        assert "openrouter-free: empty reply" in line
        assert "unreachable" not in line.lower()
        heartbeat._llm_retry_counts.clear()


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestHeartbeatRobustness:
    def setup_method(self):
        _reset_state()

        brain_db._conn.execute("DELETE FROM tasks")
        brain_db._conn.commit()
        for key in ("stale_tasks", "llm_thinking", "pending_reminder"):
            heartbeat._proactive_state[key] = time.monotonic()

    def _beat(self):
        return client.post(
            "/v1/chat/completions",
            json=TestHeartbeatInterceptor._heartbeat_body(None),
            headers=AUTH_HEADER,
        )

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_work_exception_fails_task_and_beat_continues(self, _n):

        t = brain_db.create_task("Test task for heartbeat")
        with patch(
            "heartbeat._llm_work_task", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            r = self._beat()
        assert r.status_code == 200
        got = brain_db.get_task(t["id"])
        assert got["status"] == "failed"
        assert "boom" in got["result"]

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_claim_valueerror_skips_task_without_failing_it(self, _n):

        t = brain_db.create_task("Test task for heartbeat")
        work = AsyncMock(return_value="done")
        with (
            patch.object(brain_db, "claim_task", side_effect=ValueError("claimed")),
            patch("heartbeat._llm_work_task", work),
        ):
            r = self._beat()
        assert r.status_code == 200
        work.assert_not_called()
        assert brain_db.get_task(t["id"])["status"] == "pending"

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_claim_falsy_skips_task(self, _n):

        brain_db.create_task("Test task for heartbeat")
        work = AsyncMock(return_value="done")
        with (
            patch.object(brain_db, "claim_task", return_value=None),
            patch("heartbeat._llm_work_task", work),
        ):
            assert self._beat().status_code == 200
        work.assert_not_called()

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_complete_valueerror_tolerated(self, _n):

        brain_db.create_task("Test task for heartbeat")
        with (
            patch("heartbeat._llm_work_task", AsyncMock(return_value="ok")),
            patch.object(brain_db, "complete_task", side_effect=ValueError("gone")),
        ):
            assert self._beat().status_code == 200

    @patch("telegram.notify", new_callable=AsyncMock, return_value=True)
    def test_overall_work_deadline(self, _n, monkeypatch):

        monkeypatch.setenv("HEARTBEAT_WORK_DEADLINE", "0.05")
        t = brain_db.create_task("Test task for heartbeat")

        async def hang(_desc):
            await asyncio.sleep(10)

        with patch("heartbeat._llm_work_task", hang):
            r = self._beat()
        assert r.status_code == 200
        assert brain_db.get_task(t["id"])["status"] == "failed"


class TestSearchPathDeadline:
    @patch.dict("os.environ", FAKE_ENV)
    # Search is paid: without a known balance above the floor it never
    # reaches the hung provider and the deadline would go untested (#23).
    @patch("llm_proxy._check_openrouter_balance", AsyncMock(return_value=50.0))
    def test_search_call_honours_deadline(self, monkeypatch):
        monkeypatch.setattr(llm_proxy, "LLM_CALL_DEADLINE", 0.05)

        async def hang(provider, models, body, api_key):
            await asyncio.sleep(10)

        async def run():
            return await asyncio.wait_for(
                llm_proxy._llm_call("q", web_search=True), timeout=2
            )

        with patch("llm_proxy._call_provider", side_effect=hang) as call:
            assert asyncio.run(run()) is None
        assert call.called


class TestAutoRetryPopsOnlyElapsedTiers:
    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
    @patch("llm_proxy._background_notify")
    def test_long_reset_tier_stays_tripped(self, _bg):
        _reset_state()
        phase = {"retry": False}
        retry_providers: list[str] = []

        async def fake_sleep(_s):
            phase["retry"] = True

        async def mock_call(provider, models, body, api_key):
            if phase["retry"]:
                retry_providers.append(provider)
                return _mock_response(200)
            secs = "5" if provider == "openrouter" else "25"
            return _mock_response(
                429, {"error": "rate limited"}, headers={"retry-after": secs}
            )

        with (
            patch("llm_proxy._call_provider", side_effect=mock_call),
            patch("llm_proxy.asyncio.sleep", side_effect=fake_sleep),
        ):
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert "_clawrange_tier" in r.json()
        assert retry_providers
        assert "zai" not in retry_providers


class TestTrySingleTierGarbage:
    @pytest.mark.parametrize(
        "resp",
        [
            httpx.Response(200, content=b"<html>not json</html>"),
            httpx.Response(200, json={"choices": []}),
            httpx.Response(200, json={"choices": [{"nomessage": 1}]}),
            httpx.Response(200, json=["list"]),
        ],
    )
    @patch.dict("os.environ", FAKE_ENV)
    def test_garbage_records_failure(self, resp):
        tier = llm_proxy.CONFIG["tiers"][0]
        with patch("llm_proxy._call_provider", AsyncMock(return_value=resp)):
            out = asyncio.run(llm_proxy._try_single_tier(tier, {"messages": []}))
        assert out == (None, None)
        assert llm_proxy._circuit_state[tier["name"]]["failures"] == 1


class TestBackgroundTasksHeld:
    def test_spawn_keeps_reference_until_done(self):
        async def run():
            gate = asyncio.Event()

            async def work():
                await gate.wait()

            task = llm_proxy._spawn_background(work())
            assert task in llm_proxy._background_tasks
            gate.set()
            await task
            await asyncio.sleep(0)
            assert task not in llm_proxy._background_tasks

        asyncio.run(run())

    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
    def test_status_message_deleted_even_on_error(self):
        delete = AsyncMock()
        with (
            patch("telegram.send_status", AsyncMock(return_value=42)),
            patch("telegram.send_typing", AsyncMock()),
            patch("telegram.delete_message", delete),
            patch(
                "llm_proxy._handle_non_streaming",
                AsyncMock(side_effect=RuntimeError("x")),
            ),
        ):
            with pytest.raises(RuntimeError):
                client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        delete.assert_awaited_once_with(42)


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestDigestNotLostOnFailure:
    def setup_method(self):
        _reset_state()

        brain_db._conn.execute("DELETE FROM tasks")
        brain_db._conn.commit()
        for key in ("stale_tasks", "llm_thinking", "pending_reminder"):
            heartbeat._proactive_state[key] = time.monotonic()
        digest._digest_state["last_flush"] = (
            time.time() - digest.HEARTBEAT_DIGEST_INTERVAL - 1
        )

    def _beat(self):
        return client.post(
            "/v1/chat/completions",
            json=TestHeartbeatInterceptor._heartbeat_body(None),
            headers=AUTH_HEADER,
        )

    def test_failed_send_rebuffers_line(self):
        digest._digest_record("[SYSTEM] #1: important")
        with patch("telegram.notify", AsyncMock(return_value=False)):
            assert self._beat().status_code == 200
        assert digest._digest_state["lines"] == ["[SYSTEM] #1: important"]

    def test_successful_send_clears_buffer(self):
        digest._digest_record("[SYSTEM] #1: important")
        with patch("telegram.notify", AsyncMock(return_value=True)):
            self._beat()
        assert digest._digest_state["lines"] == []

    def test_overflow_items_carry_over(self):
        for i in range(30):
            digest._digest_record(f"[SYSTEM] #{i}: item")
        with patch("telegram.notify", AsyncMock(return_value=True)):
            self._beat()
        assert len(digest._digest_state["lines"]) == 11
        assert digest._digest_state["lines"][0] == "[SYSTEM] #19: item"


class TestRequestValidation:
    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"content": b"not json"},
            {"json": [1, 2]},
            {"json": {"messages": "hello"}},
            {"json": {"messages": [1, "a"]}},
        ],
    )
    def test_chat_bad_body_is_400(self, kwargs):
        r = client.post(
            "/v1/chat/completions",
            headers={**AUTH_HEADER, "Content-Type": "application/json"},
            **kwargs,
        )
        assert r.status_code == 400

    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
    @pytest.mark.parametrize("kwargs", [{"content": b"nope"}, {"json": ["x"]}])
    def test_embeddings_bad_body_is_400(self, kwargs):
        r = client.post(
            "/v1/embeddings",
            headers={**AUTH_HEADER, "Content-Type": "application/json"},
            **kwargs,
        )
        assert r.status_code == 400


class TestHalfOpenBreaker:
    def _trip(self, name="t"):
        for _ in range(llm_proxy.CIRCUIT_FAILURE_THRESHOLD):
            llm_proxy._record_failure(name)
        llm_proxy._circuit_state[name]["last_failure"] = (
            time.monotonic() - llm_proxy.CIRCUIT_COOLDOWN_SECONDS - 1
        )

    def test_probe_failure_retrips_immediately(self):
        self._trip()
        assert llm_proxy._circuit_open("t") is False  # the probe
        llm_proxy._record_failure("t")
        assert llm_proxy._circuit_open("t") is True

    def test_only_one_probe_at_a_time(self):
        self._trip()
        assert llm_proxy._circuit_open("t") is False
        assert llm_proxy._circuit_open("t") is True

    def test_probe_success_closes(self):
        self._trip()
        assert llm_proxy._circuit_open("t") is False
        llm_proxy._record_success("t")
        assert llm_proxy._circuit_open("t") is False
        llm_proxy._record_failure("t")
        assert llm_proxy._circuit_open("t") is False  # needs N fresh failures


class TestTierHintBoundary:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("please use !claude for this", "openrouter-paid"),
            ("!paid summarize", "openrouter-paid"),
            ("look at foo!claude bar", None),
            ("see https://x.io/a!paid", None),
            ("!claudette is a name", None),
        ],
    )
    def test_boundaries(self, text, expected):
        got = llm_proxy._detect_tier_hint([{"role": "user", "content": text}])
        assert got == expected


class TestDigestContinuationPrefix:
    def test_exactly_one_prefix_per_continuation(self):
        messages = _plan_messages(digest, ["[SYSTEM] #1: " + "x" * 9000])
        assert len(messages) >= 3
        assert not messages[0].startswith("…")
        for m in messages[1:]:
            assert m.startswith("… ")
            assert not m.startswith("… …")

    def test_split_parts_not_double_prefixed(self):
        text = ("word " * 2000).strip()
        parts = telegram.split_sentences(text, limit=500)
        assert all(not p.startswith("… …") for p in parts)


class TestLowUptimeClock:
    def _fake_time(self, monkeypatch):
        from types import SimpleNamespace

        fake = SimpleNamespace(monotonic=lambda: 5.0, time=time.time)
        monkeypatch.setattr(llm_proxy, "time", fake)
        monkeypatch.setattr(heartbeat, "time", fake)

    def test_proactive_due_on_fresh_host(self, monkeypatch):
        self._fake_time(monkeypatch)
        assert heartbeat._proactive_ready("never_ran") is True

    def test_notification_not_debounced_on_fresh_host(self, monkeypatch):
        self._fake_time(monkeypatch)
        assert llm_proxy._should_notify("fresh-tier") is True

    @patch.dict("os.environ", FAKE_ENV)
    def test_balance_checked_on_fresh_host(self, monkeypatch):
        self._fake_time(monkeypatch)
        calls = {"n": 0}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                calls["n"] += 1
                return httpx.Response(
                    200, json={"data": {"total_credits": 20, "total_usage": 5}}
                )

        monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", FakeClient)
        assert asyncio.run(llm_proxy._check_openrouter_balance()) == 15.0
        assert calls["n"] == 1


class TestDeadCodeRemoved:
    @pytest.mark.parametrize(
        "name", ["_safe_notify", "_TASK_TRIGGERS", "_strip_prefix"]
    )
    def test_removed(self, name):
        assert not hasattr(llm_proxy, name)


class TestTierSnapshot:
    """Status displays must never claim the half-open probe. GET /tier is
    polled by a dashboard; claiming the probe there kept a tripped tier from
    ever getting the real request that would close it."""

    def _trip_past_cooldown(self, llm_proxy, name):
        llm_proxy._circuit_state[name] = {
            "failures": llm_proxy.CIRCUIT_FAILURE_THRESHOLD,
            "last_failure": time.monotonic() - llm_proxy.CIRCUIT_COOLDOWN_SECONDS - 1,
        }

    def test_snapshot_does_not_claim_probe(self):
        import llm_proxy

        name = llm_proxy.CONFIG["tiers"][0]["name"]
        self._trip_past_cooldown(llm_proxy, name)
        llm_proxy.tier_snapshot()
        assert "probe_at" not in llm_proxy._circuit_state[name]

    def test_snapshot_markers(self):
        import llm_proxy

        tiers = llm_proxy.CONFIG["tiers"]
        llm_proxy._circuit_state[tiers[0]["name"]] = {
            "failures": llm_proxy.CIRCUIT_FAILURE_THRESHOLD,
            "last_failure": time.monotonic(),
        }
        llm_proxy._last_tier_used = tiers[1]["name"]
        snap = {t["name"]: t["status"] for t in llm_proxy.tier_snapshot()}
        assert snap[tiers[0]["name"]] == "TRIPPED"
        assert snap[tiers[1]["name"]] == "ACTIVE"
        assert all(t["description"] for t in llm_proxy.tier_snapshot())

    def test_get_tier_route_does_not_claim_probe(self):
        import llm_proxy

        name = llm_proxy.CONFIG["tiers"][0]["name"]
        self._trip_past_cooldown(llm_proxy, name)
        with patch("llm_proxy._check_openrouter_balance", return_value=None):
            assert client.get("/tier").status_code == 200
        assert "probe_at" not in llm_proxy._circuit_state[name]
