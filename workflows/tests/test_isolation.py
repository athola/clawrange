"""Guards that keep the unit suite hermetic. A real HTTPS call to
openrouter.ai inside a deadline-timed test was the cause of the
intermittent pre-push failure (slow DNS can't be cancelled by wait_for)."""

import socket

import pytest

import heartbeat
import llm_proxy


def test_external_dns_is_blocked():
    with pytest.raises(OSError, match="network disabled"):
        socket.getaddrinfo("openrouter.ai", 443)


def test_local_names_still_resolve():
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_proxy_globals_start_clean():
    assert llm_proxy._circuit_state == {}
    assert heartbeat._proactive_state == {}
    assert heartbeat._thinking_focus_index == 0
    assert llm_proxy._notification_last_sent == {}
    assert llm_proxy._balance_cache["checked_at"] is None
    assert llm_proxy._last_tier_used is None
