"""API-wide bearer auth: every route needs PROXY_AUTH_TOKEN except a small
read-only allowlist (health checks plus the tier/schedule GETs a
monitoring dashboard polls). Fails closed when the token is unset."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import auth


@pytest.fixture()
def real_auth():
    """Undo conftest's global auth bypass for this module's tests."""
    from app import app

    saved = app.dependency_overrides.pop(auth.require_token, None)
    yield TestClient(app)
    if saved is not None:
        app.dependency_overrides[auth.require_token] = saved


GOOD = {"Authorization": "Bearer s3cret"}


class TestIsPublic:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/healthz"),
            ("GET", "/healthz/research"),
            ("GET", "/healthz/crm"),
            ("GET", "/tier"),
            ("GET", "/sched"),
            ("GET", "/sched/morning_digest"),
        ],
    )
    def test_allowlisted(self, method, path):
        assert auth.is_public(method, path) is True

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/tier/notify"),
            ("POST", "/sched"),
            ("POST", "/sched/x/run"),
            ("GET", "/sched/x/run"),
            ("DELETE", "/sched/x"),
            ("GET", "/task"),
            ("POST", "/task"),
            ("GET", "/brain/pages"),
            ("POST", "/persona/propose"),
            ("GET", "/healthzz"),
            ("POST", "/v1/chat/completions"),
        ],
    )
    def test_everything_else_protected(self, method, path):
        assert auth.is_public(method, path) is False


class TestBearerOk:
    def test_matching_token(self):
        assert auth.bearer_ok("Bearer s3cret", "s3cret") is True

    def test_wrong_token(self):
        assert auth.bearer_ok("Bearer nope", "s3cret") is False

    def test_missing_header(self):
        assert auth.bearer_ok(None, "s3cret") is False

    def test_not_bearer_scheme(self):
        assert auth.bearer_ok("s3cret", "s3cret") is False

    def test_empty_expected_fails_closed(self):
        """An unset token must never mean 'open': empty == empty is a deny."""
        assert auth.bearer_ok("Bearer ", "") is False
        assert auth.bearer_ok(None, "") is False


@patch("llm_proxy.PROXY_AUTH_TOKEN", "s3cret")
class TestRoutes:
    def test_health_open(self, real_auth):
        assert real_auth.get("/healthz").status_code == 200

    def test_tier_get_open(self, real_auth):
        with patch("llm_proxy._check_openrouter_balance", return_value=None):
            assert real_auth.get("/tier").status_code == 200

    def test_sched_list_open(self, real_auth):
        assert real_auth.get("/sched").status_code == 200

    def test_task_create_requires_token(self, real_auth):
        r = real_auth.post("/task", json={"description": "x"})
        assert r.status_code == 401

    def test_task_create_with_token(self, real_auth):
        r = real_auth.post("/task", json={"description": "x"}, headers=GOOD)
        assert r.status_code == 200

    def test_brain_router_protected(self, real_auth):
        assert real_auth.get("/brain/pages").status_code == 401

    def test_persona_router_protected(self, real_auth):
        r = real_auth.post(
            "/persona/propose",
            json={"kind": "soul", "target": "t", "content": "c", "source": "x"},
        )
        assert r.status_code == 401

    def test_delete_project_protected(self, real_auth):
        assert real_auth.delete("/projects/anything").status_code == 401

    def test_wrong_token_rejected(self, real_auth):
        r = real_auth.get("/task", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


@patch("llm_proxy.PROXY_AUTH_TOKEN", "")
class TestUnsetTokenFailsClosed:
    def test_protected_route_denied(self, real_auth):
        assert real_auth.get("/task").status_code == 401

    def test_health_still_open(self, real_auth):
        assert real_auth.get("/healthz").status_code == 200

    def test_chat_completions_denied(self, real_auth):
        r = real_auth.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 401


class TestStartupWarning:
    def test_warns_when_token_unset(self, caplog):
        with caplog.at_level("ERROR", logger="clawrange.auth"):
            assert auth.warn_if_unset("") is True
        assert "PROXY_AUTH_TOKEN" in caplog.text

    def test_silent_when_token_set(self, caplog):
        with caplog.at_level("ERROR", logger="clawrange.auth"):
            assert auth.warn_if_unset("tok") is False
        assert caplog.text == ""
