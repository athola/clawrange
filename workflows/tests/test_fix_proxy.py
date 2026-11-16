"""Regression tests for the PR #11 review fixes in the LLM proxy (cluster K1)."""

from unittest.mock import patch

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

    def test_reflect_failure_is_reported(self, authed_app, monkeypatch):
        import persona_api

        def boom(profile_name):
            raise RuntimeError("generator exploded")

        monkeypatch.setattr(persona_api, "_run_reflection", boom)
        client, _db = authed_app
        r = _chat(client, "!persona reflect")
        assert r.status_code == 200
        assert "failed" in _reply(r).lower()
