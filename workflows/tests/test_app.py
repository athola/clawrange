"""Tests for the ClawRange workflow service."""

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


class TestHealthz:
    def test_returns_ok(self):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestWebhook:
    def test_echo_contains_received(self):
        r = client.post("/webhook/test", json={"message": "ping"})
        assert r.status_code == 200
        assert "received" in r.json()["message"]

    def test_echo_returns_keys(self):
        r = client.post("/webhook/test", json={"a": 1, "b": 2})
        assert set(r.json()["payloadKeys"]) == {"a", "b"}

    def test_empty_payload(self):
        r = client.post("/webhook/test", json={})
        assert "empty payload" in r.json()["message"]

    def test_webhook_test_path(self):
        """The /webhook-test/ path works identically for backward compat."""
        r = client.post("/webhook-test/test", json={"source": "test"})
        assert r.status_code == 200
        assert "received" in r.json()["message"]


class TestLeadLookup:
    def test_find_by_name(self):
        r = client.post("/webhook/lead-status", json={"name": "John Smith"})
        data = r.json()
        assert data["status"] == "found"
        assert data["lead"]["name"] == "John Smith"
        assert "John Smith" in data["message"]

    def test_find_by_phone(self):
        r = client.post("/webhook/lead-status", json={"phone": "903-555-0200"})
        data = r.json()
        assert data["status"] == "found"
        assert data["lead"]["name"] == "Maria Garcia"

    def test_partial_name_match(self):
        r = client.post("/webhook/lead-status", json={"name": "robert"})
        assert r.json()["lead"]["name"] == "Robert Johnson"

    def test_not_found(self):
        r = client.post("/webhook/lead-status", json={"name": "Nobody"})
        data = r.json()
        assert data["status"] == "not_found"
        assert "No lead found" in data["message"]

    def test_webhook_test_path(self):
        r = client.post(
            "/webhook-test/lead-status",
            json={"name": "John Smith", "phone": "903-555-0100"},
        )
        assert r.status_code == 200
        assert "John Smith" in r.json()["message"]


class TestMorningBriefing:
    def test_returns_briefing(self):
        r = client.get("/webhook/morning-briefing")
        data = r.json()
        assert "briefing" in data
        assert data["leadCount"] == 4

    def test_briefing_contains_all_leads(self):
        briefing = client.get("/webhook/morning-briefing").json()["briefing"]
        assert "John Smith" in briefing
        assert "Maria Garcia" in briefing
        assert "Robert Johnson" in briefing
        assert "Ashley Williams" in briefing

    def test_briefing_header(self):
        briefing = client.get("/webhook/morning-briefing").json()["briefing"]
        assert "MORNING BRIEFING" in briefing
        assert "Longview Home Center" in briefing

    def test_post_also_works(self):
        r = client.post("/webhook/morning-briefing")
        assert r.status_code == 200
        assert r.json()["leadCount"] == 4
