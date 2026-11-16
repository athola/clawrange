"""describe_balance(): one display string for the OpenRouter balance (#30).

An unknown balance must never read as a number: callers that showed
``$None`` or silently dropped the line hid a failing credits lookup.
"""

import asyncio

import httpx
import pytest

import llm_proxy


@pytest.fixture(autouse=True)
def _reset_balance(monkeypatch):
    monkeypatch.setitem(llm_proxy._balance_cache, "remaining", None)
    monkeypatch.setitem(llm_proxy._balance_cache, "checked_at", None)
    monkeypatch.setitem(llm_proxy._balance_status, "ok_at", None)
    monkeypatch.setitem(llm_proxy._balance_status, "error", None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")


def test_no_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert llm_proxy.describe_balance() == "not configured"


def test_never_checked_is_unknown():
    assert llm_proxy.describe_balance() == "unknown (not checked yet)"


def test_fresh_lookup_shows_amount(monkeypatch):
    monkeypatch.setattr(llm_proxy.time, "monotonic", lambda: 1000.0)
    llm_proxy._balance_cache["remaining"] = 12.5
    llm_proxy._balance_status["ok_at"] = 1000.0
    assert llm_proxy.describe_balance() == "$12.50"


def test_failed_lookup_without_cache_says_why():
    llm_proxy._balance_status["error"] = "HTTP 503"
    assert llm_proxy.describe_balance() == "unknown (lookup failed: HTTP 503)"


def test_cached_value_after_failure_shows_age(monkeypatch):
    monkeypatch.setattr(llm_proxy.time, "monotonic", lambda: 1000.0 + 600)
    llm_proxy._balance_cache["remaining"] = 7.0
    llm_proxy._balance_status["ok_at"] = 1000.0
    llm_proxy._balance_status["error"] = "timeout"
    assert llm_proxy.describe_balance() == "$7.00 (10 min old; lookup failing: timeout)"


def test_lookup_records_success_and_failure(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json={"data": {"total_credits": 20, "total_usage": 5}}
            )
        return httpx.Response(503)

    real_client = httpx.AsyncClient

    def client(**kw):
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(llm_proxy.httpx, "AsyncClient", client)
    assert asyncio.run(llm_proxy._check_openrouter_balance()) == 15.0
    assert llm_proxy._balance_status["error"] is None
    assert llm_proxy._balance_status["ok_at"] is not None

    llm_proxy._balance_cache["checked_at"] = None  # force a re-check
    assert asyncio.run(llm_proxy._check_openrouter_balance()) == 15.0  # cached
    assert llm_proxy._balance_status["error"] == "HTTP 503"
    assert "lookup failing: HTTP 503" in llm_proxy.describe_balance()
