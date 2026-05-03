"""Tests for the LLM proxy with three-tier fallback."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import app

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


def _reset_state():
    """Reset all mutable proxy state between tests."""
    import llm_proxy

    llm_proxy._circuit_state.clear()
    llm_proxy._notification_last_sent.clear()


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
    @patch("llm_proxy._call_provider", side_effect=_all_rate_limited)
    def test_all_tiers_exhausted_returns_synthetic(self, mock_call, mock_bg):
        """When all tiers are rate limited, return a friendly synthetic response."""
        r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert "temporarily on pause" in content

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
                    "I'm temporarily on pause \u2014 the free API tiers are rate-limited "
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
            # 2 user messages (assistant stripped) + trailing system (anti-hallucination)
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
        """GLM 5.1 reasoning models put output in reasoning_content — proxy merges it."""
        reasoning_body = {
            "id": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "The user said hello, so I should greet them.",
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
    def test_garbled_response_falls_to_next_tier(self, _mock_bg):
        """If a tier returns garbled (no-space) text, skip it and try next."""
        garbled_body = {
            "id": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Alex,Idon'tseeanyactiveresearchsub-agentsrunningrightnow.Letmecheckifthere'saresearchdocumentthatwascreated:",
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

        original = llm_proxy.OPENROUTER_CREDIT_BALANCE
        llm_proxy.OPENROUTER_CREDIT_BALANCE = 0
        llm_proxy._balance_cache.clear()
        try:
            result = asyncio.run(llm_proxy._check_openrouter_balance())
            assert result is None
        finally:
            llm_proxy.OPENROUTER_CREDIT_BALANCE = original

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"})
    def test_balance_calculation(self):
        """Verify remaining = credit_balance - usage."""
        import asyncio

        import llm_proxy

        original_balance = llm_proxy.OPENROUTER_CREDIT_BALANCE
        llm_proxy.OPENROUTER_CREDIT_BALANCE = 15.0
        llm_proxy._balance_cache.clear()

        mock_resp = httpx.Response(
            200,
            content=json.dumps({"data": {"usage": 5.50}}).encode(),
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        try:
            with patch("llm_proxy.httpx.AsyncClient", return_value=mock_client):
                result = asyncio.run(llm_proxy._check_openrouter_balance())
                assert result == 9.50  # 15.0 - 5.50
        finally:
            llm_proxy.OPENROUTER_CREDIT_BALANCE = original_balance
            llm_proxy._balance_cache.clear()


# ─── Tier Command Interception ───────────────────────────────────


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
        """Status command matches via last-line fallback even if metadata regex fails."""
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
        assert "!recall" in content
        assert "!remember" in content
        assert "!tier" in content
        assert "/brain/search" in content

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

        llm_proxy._notification_last_sent["test-tier"] = 0  # long ago
        assert llm_proxy._should_notify("test-tier") is True


# ─── Auto-Retry ──────────────────────────────────────────────────


class TestResetSecondsParsing:
    """Verify numeric reset time extraction for auto-retry decisions."""

    def test_parse_seconds_from_message(self):
        from llm_proxy import _parse_reset_seconds_from_message

        assert _parse_reset_seconds_from_message("try again in 30 seconds") == 30

    def test_parse_minutes_from_message(self):
        from llm_proxy import _parse_reset_seconds_from_message

        assert _parse_reset_seconds_from_message("resets in 5 minutes") == 300

    def test_parse_upstream_throttle(self):
        from llm_proxy import _parse_reset_seconds_from_message

        result = _parse_reset_seconds_from_message("upstream provider throttled")
        assert result == 20

    def test_parse_daily_limit(self):
        from llm_proxy import _parse_reset_seconds_from_message

        assert _parse_reset_seconds_from_message("exceeded daily limit") == 86400

    def test_parse_unknown_returns_none(self):
        from llm_proxy import _parse_reset_seconds_from_message

        assert _parse_reset_seconds_from_message("something went wrong") is None

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
            assert "temporarily on pause" in content

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
            assert "temporarily on pause" in content
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


class TestResetMessageParsing:
    """Verify reset time extraction from error message text."""

    def test_parse_seconds(self):
        from llm_proxy import _parse_reset_from_message

        assert _parse_reset_from_message("try again in 30 seconds") == "in 30s"

    def test_parse_minutes(self):
        from llm_proxy import _parse_reset_from_message

        assert _parse_reset_from_message("Rate limit resets in 5 minutes") == "in ~5min"

    def test_parse_daily(self):
        from llm_proxy import _parse_reset_from_message

        result = _parse_reset_from_message("exceeded daily free limit")
        assert "24h" in result

    def test_parse_empty(self):
        from llm_proxy import _parse_reset_from_message

        assert _parse_reset_from_message("unknown error") == ""


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
                                    "content": "Let me do my full startup sequence first.",
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

        garbled = "Alex,Idon'tseeanyactiveresearchsub-agentsrunningrightnow.Letmecheckifthere'saresearchdocumentthatwascreated:"
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
        assert llm_proxy._circuit_open("test-tier") is False
        # State should be cleaned up
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

        original = llm_proxy.OPENROUTER_CREDIT_BALANCE
        llm_proxy.OPENROUTER_CREDIT_BALANCE = 20.0
        llm_proxy._balance_cache.clear()
        # Pre-populate cache
        llm_proxy._balance_cache["remaining"] = 15.0
        llm_proxy._balance_cache["checked_at"] = time.monotonic()

        try:
            result = asyncio.run(llm_proxy._check_openrouter_balance())
            assert result == 15.0  # Returned cached, no HTTP call needed
        finally:
            llm_proxy.OPENROUTER_CREDIT_BALANCE = original
            llm_proxy._balance_cache.clear()

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"})
    def test_refetches_after_interval(self):
        """After cache expires, should make a new HTTP call."""
        import asyncio
        import time

        import llm_proxy

        original = llm_proxy.OPENROUTER_CREDIT_BALANCE
        llm_proxy.OPENROUTER_CREDIT_BALANCE = 20.0
        llm_proxy._balance_cache.clear()
        # Set cache as expired
        llm_proxy._balance_cache["remaining"] = 15.0
        llm_proxy._balance_cache["checked_at"] = (
            time.monotonic() - llm_proxy._BALANCE_CHECK_INTERVAL - 1
        )

        mock_resp = httpx.Response(
            200, content=json.dumps({"data": {"usage": 8.0}}).encode()
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
            llm_proxy.OPENROUTER_CREDIT_BALANCE = original
            llm_proxy._balance_cache.clear()

    def test_returns_none_when_api_key_missing(self):
        import asyncio

        import llm_proxy

        original = llm_proxy.OPENROUTER_CREDIT_BALANCE
        llm_proxy.OPENROUTER_CREDIT_BALANCE = 20.0
        llm_proxy._balance_cache.clear()

        try:
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}):
                result = asyncio.run(llm_proxy._check_openrouter_balance())
            assert result is None
        finally:
            llm_proxy.OPENROUTER_CREDIT_BALANCE = original
            llm_proxy._balance_cache.clear()

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"})
    def test_returns_stale_cache_on_http_error(self):
        """If the API call fails, return the last known value."""
        import asyncio
        import time

        import llm_proxy

        original = llm_proxy.OPENROUTER_CREDIT_BALANCE
        llm_proxy.OPENROUTER_CREDIT_BALANCE = 20.0
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
            llm_proxy.OPENROUTER_CREDIT_BALANCE = original
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


# ─── Heartbeat Interceptor ──────────────────────────────────────


@patch.dict("os.environ", FAKE_ENV)
@patch("llm_proxy.PROXY_AUTH_TOKEN", "test-token")
class TestHeartbeatInterceptor:
    """Verify heartbeat messages are handled in Python, not forwarded to LLM."""

    def setup_method(self):
        _reset_state()
        # Clear persistent task queue via brain_db
        from app import brain_db

        for table in ("tasks",):
            brain_db._conn.execute(f"DELETE FROM {table}")
        brain_db._conn.commit()

    def _heartbeat_body(self):
        return {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "read heartbeat.md if it exists (workspace context). "
                        "follow it strictly. do not infer or repeat old tasks "
                        "from prior chats. if nothing needs attention, reply heartbeat_ok."
                    ),
                }
            ],
        }

    def test_heartbeat_intercepted_no_llm_call(self):
        """Heartbeat message should NOT call the LLM provider for basic checks."""
        import llm_proxy
        import time as _time

        # Suppress proactive LLM thinking so we only test deterministic path
        llm_proxy._proactive_state["stale_tasks"] = _time.monotonic()
        llm_proxy._proactive_state["llm_thinking"] = _time.monotonic()

        mock_caller = AsyncMock(return_value=_mock_response(200))
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
            assert r.status_code == 200
            mock_caller.assert_not_called()

    @patch("llm_proxy.notify", new_callable=AsyncMock, return_value=True)
    def test_heartbeat_processes_pending_task(self, mock_notify):
        """When pending tasks exist, heartbeat sends them to the LLM for work."""
        from app import TaskCreate as TC, create_task

        create_task(TC(description="Test task for heartbeat", priority=2))

        mock_resp = _mock_response(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Checked logs — no anomalies found in the last 24h.",
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
            assert "ALEX" in content or "SYSTEM" in content
            assert "no anomalies" in content
            mock_caller.assert_called_once()

        # Verify Telegram notification was sent
        mock_notify.assert_called_once()
        notify_text = mock_notify.call_args[0][0]
        assert "Task completed" in notify_text

        from app import brain_db

        tasks = brain_db.list_tasks()
        completed = [t for t in tasks if t["status"] == "completed"]
        assert len(completed) >= 1
        assert "no anomalies" in completed[0]["result"]

    def test_heartbeat_silent_when_no_issues(self):
        """With no pending tasks, no infra issues, and proactive checks
        not yet due, return empty response (silent heartbeat)."""
        import llm_proxy

        # Mark all proactive checks as just-ran so they don't fire
        import time as _time

        llm_proxy._proactive_state["stale_tasks"] = _time.monotonic()
        llm_proxy._proactive_state["llm_thinking"] = _time.monotonic()

        r = client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )
        content = r.json()["choices"][0]["message"]["content"]
        assert content == ""

    def test_heartbeat_stale_task_detection(self):
        """Heartbeat processes stale pending tasks and creates nudge tasks."""
        import llm_proxy

        # Ensure stale_tasks check is ready to fire
        llm_proxy._proactive_state.pop("stale_tasks", None)
        # Suppress LLM thinking
        llm_proxy._proactive_state["llm_thinking"] = __import__("time").monotonic()

        from app import brain_db
        from datetime import timedelta

        # Create a task and backdate its created_at to 5 hours ago
        old_task = brain_db.create_task("old task from earlier")
        old_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        brain_db._conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (old_time, old_task["id"]),
        )
        brain_db._conn.commit()

        r = client.post(
            "/v1/chat/completions",
            json=self._heartbeat_body(),
            headers=AUTH_HEADER,
        )
        content = r.json()["choices"][0]["message"]["content"]
        # Should process the old task (it's pending)
        assert "SYSTEM" in content or "[TASK]" in content

    def test_heartbeat_llm_thinking(self):
        """Heartbeat asks the LLM for a task suggestion when due."""
        import llm_proxy

        # Ensure LLM thinking is ready to fire, stale tasks is not
        llm_proxy._proactive_state.pop("llm_thinking", None)
        llm_proxy._proactive_state["stale_tasks"] = __import__("time").monotonic()

        mock_resp = _mock_response(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Review OpenRouter spending trends for the past week",
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
            assert "Created" in content
            assert "OpenRouter spending" in content

        from app import brain_db

        all_tasks = brain_db.list_tasks()
        llm_tasks = [t for t in all_tasks if "OpenRouter spending" in t["description"]]
        assert len(llm_tasks) == 1

    def test_heartbeat_llm_thinking_graceful_failure(self):
        """LLM failure during thinking doesn't break the heartbeat."""
        import llm_proxy

        llm_proxy._proactive_state.pop("llm_thinking", None)
        llm_proxy._proactive_state["stale_tasks"] = __import__("time").monotonic()

        mock_caller = AsyncMock(side_effect=Exception("network error"))
        with patch("llm_proxy._call_provider", mock_caller):
            r = client.post(
                "/v1/chat/completions",
                json=self._heartbeat_body(),
                headers=AUTH_HEADER,
            )
            # Should still return silent — LLM failure doesn't break heartbeat
            content = r.json()["choices"][0]["message"]["content"]
            assert content == ""


class TestSemanticDedup:
    """Tests for keyword-overlap task deduplication."""

    def test_extract_keywords_filters_stop_words(self):
        from llm_proxy import _extract_keywords

        kw = _extract_keywords(
            "Review the ClawRange tier allocation for current balance"
        )
        assert "the" not in kw
        assert "for" not in kw
        assert "clawrange" in kw
        assert "tier" in kw
        assert "allocation" in kw
        assert "balance" in kw

    def test_extract_keywords_skips_short_words(self):
        from llm_proxy import _extract_keywords

        kw = _extract_keywords("go do it now")
        assert "go" not in kw
        assert "do" not in kw
        assert "it" not in kw
        assert "now" in kw

    def test_semantic_dedup_catches_rephrased_tasks(self):
        """Two differently worded tier-review tasks should be flagged as duplicates."""
        from llm_proxy import _has_recent_task

        queue = [
            {
                "id": "abc123",
                "description": "Review ClawRange tier allocation against current MSP client load",
                "status": "pending",
                "priority": 3,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        # Rephrased version of the same task
        similar = "Review ClawRange service tier balance and flag approaching limits"
        assert _has_recent_task(queue, similar) is True

    def test_semantic_dedup_allows_different_tasks(self):
        """Genuinely different tasks should not be flagged as duplicates."""
        from llm_proxy import _has_recent_task

        queue = [
            {
                "id": "abc123",
                "description": "Review ClawRange tier allocation against MSP client load",
                "status": "pending",
                "priority": 3,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        different = "Send weekly invoice summary to accounting team"
        assert _has_recent_task(queue, different) is False

    def test_semantic_dedup_ignores_expired_tasks(self):
        """Tasks older than the cutoff should not block new ones."""
        from llm_proxy import _has_recent_task

        old_time = (
            datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=25)
        ).isoformat()
        queue = [
            {
                "id": "abc123",
                "description": "Review ClawRange tier allocation against MSP client load",
                "status": "pending",
                "priority": 3,
                "created_at": old_time,
            }
        ]
        similar = "Review ClawRange service tier balance and limits"
        assert _has_recent_task(queue, similar) is False

    def test_llm_thinking_interval_is_fifteen_minutes(self):
        """Verify LLM thinking interval is 900s (15 min), not 300s."""
        from llm_proxy import _PROACTIVE_INTERVALS

        assert _PROACTIVE_INTERVALS["llm_thinking"] == 900


class TestWebSearchTierSkipping:
    """Verify web search tasks only use Z.AI (the one tier with web access)."""

    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy._call_provider", new_callable=AsyncMock)
    @patch("llm_proxy._circuit_open", return_value=False)
    def test_web_search_skips_non_zai_tiers(self, _cb, mock_call):
        """When web_search=True, openrouter tiers should be skipped entirely."""
        import asyncio

        from llm_proxy import _llm_call

        mock_call.return_value = httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "search results"}}
                ]
            },
        )

        result = asyncio.run(
            _llm_call("find reddit posts", max_tokens=500, web_search=True)
        )

        assert result == "search results"
        # Should have been called once with zai-search, NOT with openrouter
        assert mock_call.call_count == 1
        call_args = mock_call.call_args
        assert call_args[0][0] == "zai-search"  # provider arg
        assert call_args[0][1] == ["glm-5.1"]  # zai-direct models

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


class TestWebCitationExtraction:
    """Verify GLM web_search tool_call citations are extracted."""

    def test_extracts_citations_from_tool_calls(self):
        from llm_proxy import _extract_web_citations

        tool_calls = [
            {
                "type": "web_search",
                "web_search": {
                    "search_result": [
                        {
                            "title": "MSP Marketing on Reddit",
                            "link": "https://reddit.com/r/msp/post1",
                            "content": "Great tips for MSP lead generation",
                        },
                        {
                            "title": "Another Post",
                            "link": "https://reddit.com/r/msp/post2",
                            "content": "Discussion about pricing",
                        },
                    ]
                },
            }
        ]

        result = _extract_web_citations(tool_calls)
        assert "https://reddit.com/r/msp/post1" in result
        assert "https://reddit.com/r/msp/post2" in result
        assert "MSP Marketing on Reddit" in result
        assert "1." in result
        assert "2." in result

    def test_empty_tool_calls_returns_empty(self):
        from llm_proxy import _extract_web_citations

        assert _extract_web_citations([]) == ""

    def test_non_web_search_tool_calls_ignored(self):
        from llm_proxy import _extract_web_citations

        tool_calls = [{"type": "function", "function": {"name": "foo"}}]
        assert _extract_web_citations(tool_calls) == ""

    @patch.dict("os.environ", FAKE_ENV)
    @patch("llm_proxy._call_provider", new_callable=AsyncMock)
    @patch("llm_proxy._circuit_open", return_value=False)
    def test_citations_appended_to_content(self, _cb, mock_call):
        """GLM response with both content and tool_calls merges them."""
        import asyncio

        from llm_proxy import _llm_call

        mock_call.return_value = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Here are the results.",
                            "tool_calls": [
                                {
                                    "type": "web_search",
                                    "web_search": {
                                        "search_result": [
                                            {
                                                "title": "A Post",
                                                "link": "https://example.com",
                                                "content": "Relevant info",
                                            }
                                        ]
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
        assert "Here are the results." in result
        assert "https://example.com" in result
        assert "A Post" in result
