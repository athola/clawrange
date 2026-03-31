"""Tests for the LLM proxy with three-tier fallback."""

import json
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
    def test_strips_dangerous_fields(self, _mock_bg):
        mock_caller = AsyncMock(return_value=_mock_response(200))
        with patch("llm_proxy._call_provider", mock_caller):
            body = {
                **CHAT_BODY,
                "model": "gpt-4o",
                "tools": [{"type": "function"}],
                "route": "fallback",
            }
            client.post("/v1/chat/completions", json=body, headers=AUTH_HEADER)
            # _call_provider(provider, models, body, api_key) — body is arg[2]
            call_body = mock_caller.call_args.args[2]
            assert "tools" not in call_body
            assert "route" not in call_body
            assert "model" not in call_body
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
        """A single 429 should trip the circuit — no need for 3 failures."""
        call_count = {"zai": 0}

        async def mock_call(provider, models, body, api_key):
            if provider == "zai":
                call_count["zai"] += 1
                return _mock_response(429, {"error": "rate limited"})
            return _mock_response(200)

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            # First request: zai-direct 429 → circuit trips
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            assert call_count["zai"] == 1

            # Second request: zai-direct skipped (circuit open) → no tiers
            r = client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)
            content = r.json()["choices"][0]["message"]["content"]
            assert "temporarily on pause" in content
            assert call_count["zai"] == 1  # NOT called again

    @patch("llm_proxy._background_notify")
    def test_transient_errors_still_need_threshold(self, _mock_bg):
        """Non-rate-limit errors should require CIRCUIT_FAILURE_THRESHOLD failures."""
        call_count = {"zai": 0}

        async def mock_call(provider, models, body, api_key):
            if provider == "zai":
                call_count["zai"] += 1
                return _mock_response(502, {"error": "bad gateway"})
            return _mock_response(200)

        with patch("llm_proxy._call_provider", side_effect=mock_call):
            # First request: zai-direct 502 (circuit NOT tripped yet)
            client.post("/v1/chat/completions", json=CHAT_BODY, headers=AUTH_HEADER)

            # Second request: zai-direct still tried (circuit not yet open)
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
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(llm_proxy._check_openrouter_balance())
            loop.close()
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
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(llm_proxy._check_openrouter_balance())
                loop.close()
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
        """When default tier 429s with short reset, proxy waits and retries."""
        call_count = {"total": 0}

        async def mock_call(provider, models, body, api_key):
            call_count["total"] += 1
            if call_count["total"] <= 1:
                # First pass: zai-direct rate-limited with 5s reset
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

        chunks = asyncio.get_event_loop().run_until_complete(_collect())
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

        raw = asyncio.get_event_loop().run_until_complete(_collect())
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
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(telegram.notify("test"))
            loop.close()
            assert result is False
        finally:
            telegram.BOT_TOKEN = original_token
