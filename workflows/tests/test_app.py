"""Tests for the ClawRange workflow service."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


class TestHealthz:
    def test_returns_ok(self):
        r = client.get("/healthz")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "brain" in data
        assert data["brain"]["db"] == "ok"
        assert data["brain"]["pages"] == 0
        assert "embeddings" in data["brain"]


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


# ─── Tier Status ─────────────────────────────────────────────────


class TestTierStatus:
    """GIVEN the /tier endpoint
    WHEN queried
    THEN it returns tier list with status markers and balance info."""

    @patch(
        "llm_proxy._check_openrouter_balance",
        new_callable=AsyncMock,
        return_value=12.50,
    )
    @patch("llm_proxy._last_tier_used", "zai-direct")
    def test_returns_tier_list(self, _mock_balance):
        r = client.get("/tier")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["tiers"], list)
        assert len(data["tiers"]) >= 2
        assert data["last_used"] == "zai-direct"
        assert data["balance_remaining"] == "$12.50"
        assert data["paid_auto_fallback"] == "off"
        # Each tier has name, status, description
        for tier in data["tiers"]:
            assert "name" in tier
            assert "status" in tier
            assert "description" in tier

    @patch(
        "llm_proxy._check_openrouter_balance", new_callable=AsyncMock, return_value=None
    )
    @patch("llm_proxy._last_tier_used", None)
    def test_handles_unconfigured_balance(self, _mock_balance):
        r = client.get("/tier")
        data = r.json()
        assert data["balance_remaining"] == "not configured"
        assert data["last_used"] == "none"

    @patch(
        "llm_proxy._check_openrouter_balance", new_callable=AsyncMock, return_value=5.0
    )
    @patch("llm_proxy._last_tier_used", None)
    def test_tripped_tier_shows_tripped(self, _mock_balance):
        import llm_proxy

        # Trip the circuit breaker for a tier
        llm_proxy._circuit_state["openrouter-free"] = {
            "failures": llm_proxy.CIRCUIT_FAILURE_THRESHOLD,
            "last_failure": __import__("time").monotonic(),
        }
        try:
            r = client.get("/tier")
            tiers = {t["name"]: t["status"] for t in r.json()["tiers"]}
            assert tiers.get("openrouter-free") == "TRIPPED"
        finally:
            llm_proxy._circuit_state.clear()


# ─── Tier Notify ─────────────────────────────────────────────────


class TestTierNotify:
    """GIVEN the /tier/notify endpoint
    WHEN called
    THEN it formats a status message and sends it via Telegram."""

    @patch("app.notify", new_callable=AsyncMock, return_value=True)
    @patch(
        "llm_proxy._check_openrouter_balance", new_callable=AsyncMock, return_value=8.00
    )
    @patch("llm_proxy._last_tier_used", "zai-direct")
    def test_sends_notification(self, _mock_balance, mock_notify):
        r = client.post("/tier/notify")
        assert r.status_code == 200
        data = r.json()
        assert data["sent"] is True
        assert "Tier Status" in data["message"]
        assert "zai-direct" in data["message"]
        mock_notify.assert_called_once()

    @patch("app.notify", new_callable=AsyncMock, return_value=False)
    @patch(
        "llm_proxy._check_openrouter_balance", new_callable=AsyncMock, return_value=None
    )
    @patch("llm_proxy._last_tier_used", None)
    def test_reports_failure_when_telegram_down(self, _mock_balance, mock_notify):
        r = client.post("/tier/notify")
        data = r.json()
        assert data["sent"] is False
        assert "not configured" in data["message"]


# ─── Task Queue (Persistent Backend) ──────────────────────────────


class TestTaskQueue:
    """GIVEN the /task endpoints backed by SQLite
    WHEN tasks are created, listed, claimed, completed, and cancelled
    THEN the queue manages state transitions correctly with persistence."""

    def test_create_task(self):
        r = client.post("/task", json={"description": "check z.ai models"})
        assert r.status_code == 200
        task = r.json()
        assert task["status"] == "pending"
        assert task["description"] == "check z.ai models"
        assert task["priority"] == 3
        assert task["id"]

    def test_list_empty_queue(self):
        r = client.get("/task")
        assert r.json() == {"tasks": [], "total": 0}

    def test_list_with_filter(self):
        client.post("/task", json={"description": "task a"})
        client.post("/task", json={"description": "task b"})
        r = client.get("/task?status=pending")
        assert r.json()["total"] == 2
        r = client.get("/task?status=completed")
        assert r.json()["total"] == 0

    def test_get_task_by_id(self):
        task = client.post("/task", json={"description": "lookup"}).json()
        r = client.get(f"/task/{task['id']}")
        assert r.status_code == 200
        assert r.json()["description"] == "lookup"

    def test_get_nonexistent_task(self):
        r = client.get("/task/nope")
        assert r.status_code == 404

    def test_claim_task(self):
        task = client.post("/task", json={"description": "work"}).json()
        r = client.post(f"/task/{task['id']}/claim")
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_claim_non_pending_task_fails(self):
        task = client.post("/task", json={"description": "work"}).json()
        client.post(f"/task/{task['id']}/claim")
        r = client.post(f"/task/{task['id']}/claim")
        assert r.status_code == 409

    def test_complete_task_with_result(self):
        task = client.post("/task", json={"description": "research"}).json()
        client.post(f"/task/{task['id']}/claim")
        r = client.post(
            f"/task/{task['id']}/result",
            json={"result": "found 3 new models", "status": "completed"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert r.json()["result"] == "found 3 new models"
        assert r.json()["completed_at"] is not None

    def test_fail_task(self):
        task = client.post("/task", json={"description": "impossible"}).json()
        client.post(f"/task/{task['id']}/claim")
        r = client.post(
            f"/task/{task['id']}/result",
            json={"result": "tools insufficient", "status": "failed"},
        )
        assert r.json()["status"] == "failed"

    def test_cancel_task(self):
        task = client.post("/task", json={"description": "nevermind"}).json()
        r = client.delete(f"/task/{task['id']}")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_cancel_completed_task_fails(self):
        task = client.post("/task", json={"description": "done"}).json()
        client.post(f"/task/{task['id']}/claim")
        client.post(
            f"/task/{task['id']}/result",
            json={"result": "done", "status": "completed"},
        )
        r = client.delete(f"/task/{task['id']}")
        assert r.status_code == 409

    def test_priority_ordering(self):
        client.post("/task", json={"description": "low", "priority": 5})
        client.post("/task", json={"description": "urgent", "priority": 1})
        client.post("/task", json={"description": "normal", "priority": 3})
        tasks = client.get("/task").json()["tasks"]
        descriptions = [t["description"] for t in tasks]
        assert descriptions == ["urgent", "normal", "low"]

    def test_priority_clamped(self):
        task = client.post("/task", json={"description": "x", "priority": 99}).json()
        assert task["priority"] == 5


# ─── Task Command Interception ───────────────────────────────────


class TestTaskCommandDetection:
    """GIVEN the _extract_task_command parser
    WHEN various !task commands are sent
    THEN it correctly classifies them."""

    def test_create_task(self):
        from llm_proxy import _extract_task_command

        result = _extract_task_command("!task check z.ai for new models")
        assert result == {"type": "create", "description": "check z.ai for new models"}

    def test_list_tasks(self):
        from llm_proxy import _extract_task_command

        assert _extract_task_command("!tasks")["type"] == "list"
        assert _extract_task_command("/tasks")["type"] == "list"
        assert _extract_task_command("!task")["type"] == "list"
        assert _extract_task_command("!task list")["type"] == "list"

    def test_cancel_task(self):
        from llm_proxy import _extract_task_command

        result = _extract_task_command("!task cancel abc123")
        assert result == {"type": "action", "action": "cancel", "args": "abc123"}

    def test_priority_task(self):
        from llm_proxy import _extract_task_command

        result = _extract_task_command("!task priority abc123 1")
        assert result == {"type": "action", "action": "priority", "args": "abc123 1"}

    def test_non_task_message(self):
        from llm_proxy import _extract_task_command

        assert _extract_task_command("hello world") is None
        assert _extract_task_command("what tasks do I have") is None
