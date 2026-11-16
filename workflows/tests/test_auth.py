"""API-wide bearer auth: every route needs PROXY_AUTH_TOKEN except a small
read-only allowlist (health checks plus the tier/schedule GETs a
monitoring dashboard polls). Fails closed when the token is unset."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import auth
from app import app, brain_db


@pytest.fixture()
def real_auth():
    """Undo conftest's global auth bypass for this module's tests."""
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


@patch("llm_proxy.PROXY_AUTH_TOKEN", "s3cret")
class TestPublicScheduleView:
    """The dashboard polls GET /sched without a token. It gets the shape it
    needs (id, name, kind, cron, paused, next fire, ok/failed) but never a
    schedule's kwargs or the raw status text, which can carry queries and
    error detail. Authenticated callers still see full rows."""

    @pytest.fixture(autouse=True)
    def _sched(self):
        brain_db.upsert_schedule(
            "pub_view",
            "pub view",
            "reddit_scan",
            "0 9 * * *",
            {"query": "private search terms"},
        )
        brain_db.update_schedule_status(
            "pub_view", "2026-11-15T09:00:00", "FAILED telegram 1/3 msgs (x)"
        )

    def _row(self, body):
        return next(s for s in body["schedules"] if s["id"] == "pub_view")

    def test_list_without_token_hides_kwargs(self, real_auth):
        row = self._row(real_auth.get("/sched").json())
        assert "kwargs" not in row
        assert row["last_status"] == "failed"
        assert row["cron"] == "0 9 * * *"

    def test_get_without_token_hides_kwargs(self, real_auth):
        row = real_auth.get("/sched/pub_view").json()
        assert "kwargs" not in row
        assert row["last_status"] == "failed"

    def test_list_with_token_keeps_full_row(self, real_auth):
        row = self._row(real_auth.get("/sched", headers=GOOD).json())
        assert "private search terms" in str(row["kwargs"])
        assert row["last_status"].startswith("FAILED telegram")


@patch("llm_proxy.PROXY_AUTH_TOKEN", "s3cret")
class TestPublicHealthzView:
    """/healthz is public. Anonymous callers get the status, the problem
    names and the counts, never error text: a profile, scheduler or
    sqlite-vec failure message can carry file paths and config values."""

    SECRET = "/home/operator/private/profile.yaml: bad key"

    @pytest.fixture(autouse=True)
    def _broken(self, monkeypatch):
        monkeypatch.setattr(app.state, "profile_error", self.SECRET, raising=False)
        monkeypatch.setattr(
            brain_db, "vec_status", lambda: {"enabled": False, "reason": self.SECRET}
        )

    def test_without_token_hides_error_text(self, real_auth):
        body = real_auth.get("/healthz").json()
        assert body["status"] == "degraded"
        assert "profile" in body["problems"]
        assert "vector_search" in body["problems"]
        assert self.SECRET not in str(body)
        assert body["profile"]["error"] is True
        assert body["brain"]["vector_search"] == {"enabled": False}
        assert set(body["scheduler"]) == {"status"}

    def test_with_token_keeps_detail(self, real_auth):
        body = real_auth.get("/healthz", headers=GOOD).json()
        assert body["profile"]["error"] == self.SECRET
        assert body["brain"]["vector_search"]["reason"] == self.SECRET
        assert "errors" in body["scheduler"]


class TestHealthzAllowlistIsExact:
    @pytest.mark.parametrize(
        "path", ["/healthz", "/healthz/research", "/healthz/crm", "/healthz/persona"]
    )
    def test_known_health_routes_public(self, path):
        assert auth.is_public("GET", path)

    def test_unknown_health_subroute_not_public(self):
        assert not auth.is_public("GET", "/healthz/anything-new")
