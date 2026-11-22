"""Regression tests for the PR #11 review fixes in the LLM proxy (cluster K1)."""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import chat_commands
import llm_proxy
import persona_api
from persona_api import Proposal

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

        kwargs["transport"] = httpx.ASGITransport(app=app_module.app)
        super().__init__(*args, **kwargs)


@pytest.fixture()
def authed_app(monkeypatch):
    """The real app with the real bearer check (conftest's bypass undone).

    Plain httpx clients are pointed at the same app in-process, so a
    loopback HTTP self-call (the old implementation) reaches the real auth
    dependency instead of a closed port: reverting the fix fails with 401.
    """

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

    @pytest.mark.parametrize(
        ("text", "args"),
        [
            ("!persona Be Brief", " Be Brief"),
            ("  !LEARN keep it short  ", " keep it short"),
            ("!persona", ""),
            ("!personality test", None),
            ("!learning plan", None),
            ("tell me about !persona", None),
            ("", None),
        ],
    )
    def test_extract_persona_command(self, text, args):
        """Whole first word, case-insensitive; args keep their casing."""
        assert chat_commands._extract_persona_command(text) == args

    @pytest.mark.parametrize("text", ["!personality test", "!learning plan"])
    def test_longer_word_is_not_the_command(self, authed_app, text):
        """Only the whole first word triggers the command (K2 S6/S7 class)."""
        client, db = authed_app
        before = len(db.list_learnings())
        with (
            patch.object(
                chat_commands, "_handle_persona_command", AsyncMock()
            ) as handler,
            _balance(50.0),
        ):
            with patch.object(
                llm_proxy, "_call_provider", AsyncMock(return_value=_ok())
            ):
                _chat(client, text)
        assert not handler.called
        assert len(db.list_learnings()) == before

    def test_propose_storage_error_is_a_friendly_reply(self, authed_app):

        client, _db = authed_app
        with patch(
            "persona_learning.propose", side_effect=sqlite3.OperationalError("locked")
        ):
            r = _chat(client, "!persona Be brief.")
        assert r.status_code == 200
        assert "not queued" in _reply(r).lower()

    def test_reflect_prefix_is_feedback_not_reflection(self, authed_app):
        """'!persona reflective tone' is feedback, not the reflect subcommand."""
        client, db = authed_app
        r = _chat(client, "!persona reflective tone in weekly reviews")
        assert r.status_code == 200
        assert _learnings(db, "reflective tone in weekly reviews"), _reply(r)

    def test_reflect_failure_is_reported(self, authed_app, monkeypatch):

        def boom(profile_name):
            raise RuntimeError("generator exploded")

        monkeypatch.setattr(persona_api, "_run_reflection", boom)
        client, _db = authed_app
        r = _chat(client, "!persona reflect")
        assert r.status_code == 200
        assert "failed" in _reply(r).lower()


# ─── Shared helpers for the tier tests ───────────────────────────────

BODY = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(llm_proxy, "_balance_status", {"ok_at": None, "error": None})


def _ok(text: str = "Here is a proper answer to the question.") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


def _balance(value):
    return patch.object(
        llm_proxy, "_check_openrouter_balance", AsyncMock(return_value=value)
    )


def _text(resp) -> str:
    return json.loads(bytes(resp.body))["choices"][0]["message"]["content"]


# ─── #30: an unknown balance must not unlock !paid ───────────────────

FLOOR = llm_proxy.OPENROUTER_BALANCE_FLOOR


class TestPaidFloor:
    @pytest.mark.parametrize("balance", [None, 4.0, FLOOR])
    def test_paid_denied_when_unknown_or_at_floor(self, keys, balance):
        call = AsyncMock(return_value=_ok())
        with _balance(balance), patch.object(llm_proxy, "_call_provider", call):
            resp = asyncio.run(llm_proxy._handle_non_streaming(BODY, "openrouter-paid"))
        assert not call.called
        text = _text(resp)
        assert "balance" in text.lower()
        # The user already asked for paid: don't tell them to send !paid.
        assert "!paid" not in text

    def test_paid_allowed_above_floor(self, keys):
        call = AsyncMock(return_value=_ok())
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            resp = asyncio.run(llm_proxy._handle_non_streaming(BODY, "openrouter-paid"))
        assert call.called
        assert json.loads(bytes(resp.body))["_clawrange_tier"] == "openrouter-paid"


# ─── #23: background :online search obeys the floor and the breaker ──


class TestSearchGuard:
    @pytest.mark.parametrize("balance", [None, 3.0, FLOOR])
    def test_search_denied_when_unknown_or_at_floor(self, keys, balance):
        call = AsyncMock(return_value=_ok())
        with _balance(balance), patch.object(llm_proxy, "_call_provider", call):
            assert asyncio.run(llm_proxy._llm_call("q", web_search=True)) is None
        assert not call.called

    def test_search_allowed_above_floor(self, keys):
        call = AsyncMock(return_value=_ok("Found it."))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            assert asyncio.run(llm_proxy._llm_call("q", web_search=True)) == "Found it."

    def test_search_failures_trip_the_breaker(self, keys):
        call = AsyncMock(return_value=httpx.Response(500, text="boom"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            for _ in range(llm_proxy.CIRCUIT_FAILURE_THRESHOLD + 2):
                asyncio.run(llm_proxy._llm_call("q", web_search=True))
        assert call.call_count == llm_proxy.CIRCUIT_FAILURE_THRESHOLD

    def test_search_timeout_counts_as_failure(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "LLM_CALL_DEADLINE", 0.1)

        async def hang(*args):
            await asyncio.sleep(5)

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", hang):
            assert asyncio.run(llm_proxy._llm_call("q", web_search=True)) is None
        assert llm_proxy._circuit_state[llm_proxy.SEARCH_TIER]["failures"] == 1


# ─── #32: a hung tier must not use up the whole background deadline ──


def _free_tiers() -> list[dict]:
    return [t for t in llm_proxy.CONFIG["tiers"] if not t.get("paid")]


class TestBackgroundAttemptTimeout:
    def test_attempt_timeout_leaves_room_for_every_free_tier(self):
        assert (
            llm_proxy.BACKGROUND_ATTEMPT_TIMEOUT * len(_free_tiers())
            < llm_proxy.LLM_CALL_DEADLINE
        )

    def test_hung_first_tier_fails_over(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "LLM_CALL_DEADLINE", 2.0)
        monkeypatch.setattr(llm_proxy, "BACKGROUND_ATTEMPT_TIMEOUT", 0.2)

        async def first_hangs(provider, models, body, api_key):
            if provider == "openrouter":
                await asyncio.sleep(5)
            return _ok("from zai")

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", first_hangs):
            assert asyncio.run(llm_proxy._llm_call("q")) == "from zai"
        assert llm_proxy._circuit_state["openrouter-free"]["failures"] == 1

    def test_overall_deadline_records_the_inflight_tier(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "LLM_CALL_DEADLINE", 0.2)

        async def hang(*args):
            await asyncio.sleep(5)

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", hang):
            assert asyncio.run(llm_proxy._llm_call("q")) is None
        assert llm_proxy._circuit_state["openrouter-free"]["failures"] == 1

    def test_chat_tier_timeout_records_failure(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "PROVIDER_TIMEOUT", 0.1)

        async def hang(*args):
            await asyncio.sleep(2)
            return _ok()

        tier = llm_proxy.CONFIG["tiers"][1]
        with patch.object(llm_proxy, "_call_provider", hang):
            resp, rl = asyncio.run(llm_proxy._try_single_tier(tier, BODY))
        assert resp is None and rl is None
        assert llm_proxy._circuit_state[tier["name"]]["failures"] == 1


# ─── #26: failures are logged and reported as what they were ─────────


class TestFailureReporting:
    def test_llm_call_logs_tier_status_and_body(self, keys, caplog):
        caplog.set_level(logging.INFO, logger="clawrange.llm_proxy")
        call = AsyncMock(return_value=httpx.Response(503, text="upstream overloaded"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            assert asyncio.run(llm_proxy._llm_call("q")) is None
        log = caplog.text
        assert "openrouter-free" in log and "zai-direct" in log
        assert "503" in log and "upstream overloaded" in log

    def test_llm_call_logs_empty_reply(self, keys, caplog):
        caplog.set_level(logging.INFO, logger="clawrange.llm_proxy")
        call = AsyncMock(return_value=_ok(""))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            assert asyncio.run(llm_proxy._llm_call("q")) is None
        assert "empty" in caplog.text

    def test_llm_call_logs_skipped_tier(self, monkeypatch, caplog):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        caplog.set_level(logging.INFO, logger="clawrange.llm_proxy")
        assert asyncio.run(llm_proxy._llm_call("q")) is None
        assert "zai-direct" in caplog.text and "ZAI_API_KEY" in caplog.text

    def test_exhausted_reply_names_the_real_failures(self, keys):
        call = AsyncMock(return_value=httpx.Response(500, text="boom"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            text = _text(asyncio.run(llm_proxy._handle_non_streaming(BODY)))
        assert "HTTP 500" in text
        assert "rate-limited" not in text
        # Still a history poison marker, so the reply is never fed back.
        assert "temporarily on pause" in text

    def test_exhausted_reply_when_rate_limited(self, keys):
        call = AsyncMock(return_value=httpx.Response(429, text="slow down"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            text = _text(asyncio.run(llm_proxy._handle_non_streaming(BODY)))
        assert "rate-limited" in text

    def test_chat_reply_lists_reasons_end_to_end(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
        call = AsyncMock(return_value=httpx.Response(404, text="no such model"))
        with _balance(50.0), patch.object(llm_proxy, "_call_provider", call):
            r = _chat(TestClient(app_module.app), "hello")
        assert "HTTP 404" in _reply(r)

    def test_background_notify_logs_every_event(self, caplog):
        caplog.set_level(logging.INFO, logger="clawrange.llm_proxy")
        llm_proxy._background_notify("zai-direct", "*Error* on `zai-direct` (HTTP 500)")
        llm_proxy._background_notify("zai-direct", "*Error* on `zai-direct` (HTTP 502)")
        assert "HTTP 500" in caplog.text and "HTTP 502" in caplog.text


# ─── #31: history cleanup never orphans a tool result ────────────────


class TestToolCallHistory:
    def test_assistant_tool_call_turn_survives_cleanup(self, keys, monkeypatch):
        monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
        preamble = "Let me search the web for that first, then I'll gather context."
        assert llm_proxy._is_non_answer(preamble)  # would be stripped as a deferral
        messages = [
            {"role": "user", "content": "What changed in the release?"},
            {
                "role": "assistant",
                "content": preamble,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "release notes"},
            {"role": "user", "content": "So?"},
        ]
        sent: list[list[dict]] = []

        async def capture(provider, models, body, api_key):
            sent.append(body["messages"])
            return _ok()

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", capture):
            r = TestClient(app_module.app).post(
                "/v1/chat/completions", json={"messages": messages}, headers=GOOD
            )
        assert r.status_code == 200
        assert sent
        roles = [m["role"] for m in sent[0]]
        assert roles[:4] == ["user", "assistant", "tool", "user"]
        assert sent[0][1].get("tool_calls")


# ─── #45: the paid flag and tier config validation ───────────────────


def _config(**overrides) -> dict:
    cfg = json.loads(json.dumps(llm_proxy.CONFIG))
    cfg.update(overrides)
    return cfg


class TestPaidFlag:
    def test_shipped_paid_tier_is_flagged(self):
        paid = [t["name"] for t in llm_proxy.CONFIG["tiers"] if t.get("paid")]
        assert paid == ["openrouter-paid"]

    def test_renamed_paid_tier_is_still_never_used_in_background(
        self, keys, monkeypatch
    ):
        cfg = _config()
        for tier in cfg["tiers"]:
            if tier.get("paid"):
                tier["name"] = "premium"
        monkeypatch.setattr(llm_proxy, "CONFIG", cfg)
        paid_models = next(t["models"] for t in cfg["tiers"] if t.get("paid"))
        seen: list[list[str]] = []

        async def fail(provider, models, body, api_key):
            seen.append(list(models))
            return httpx.Response(500, text="boom")

        with _balance(50.0), patch.object(llm_proxy, "_call_provider", fail):
            assert asyncio.run(llm_proxy._llm_call("q")) is None
            asyncio.run(llm_proxy._handle_non_streaming(BODY))
        assert seen
        assert paid_models not in seen


class TestConfigValidation:
    def test_shipped_config_is_valid(self):
        llm_proxy._validate_config(llm_proxy.CONFIG, llm_proxy.TIER_HINTS)

    def test_zai_search_provider_is_gone(self):
        assert "zai-search" not in llm_proxy.CONFIG["providers"]
        assert set(llm_proxy.PROVIDER_URLS) == set(llm_proxy.CONFIG["providers"])

    def test_duplicate_tier_names_rejected(self):
        cfg = _config()
        cfg["tiers"].append(dict(cfg["tiers"][0]))
        with pytest.raises(ValueError, match="duplicate"):
            llm_proxy._validate_config(cfg, {})

    def test_unknown_provider_rejected(self):
        cfg = _config()
        cfg["tiers"][0]["provider"] = "nowhere"
        with pytest.raises(ValueError, match="nowhere"):
            llm_proxy._validate_config(cfg, {})

    def test_provider_without_url_rejected(self):
        cfg = _config()
        del cfg["providers"]["zai"]["base_url"]
        with pytest.raises(ValueError, match="base_url"):
            llm_proxy._validate_config(cfg, {})

    def test_hint_to_missing_tier_rejected(self):
        with pytest.raises(ValueError, match="ghost"):
            llm_proxy._validate_config(_config(), {"boo": "ghost"})

    def test_unknown_search_provider_rejected(self):
        cfg = _config(search={"provider": "nowhere", "models": ["m"]})
        with pytest.raises(ValueError, match="search"):
            llm_proxy._validate_config(cfg, {})


# ─── G1 / S2: auth allowlist and the shipped placeholder token ───────

REPO = Path(__file__).resolve().parents[2]
PLACEHOLDER = "generate-a-token-here"


class TestAuthHardening:
    @pytest.mark.parametrize("path", ["/tier\n", "/healthz\n", "/sched/x\n"])
    def test_trailing_newline_is_not_public(self, path):
        assert auth.is_public("GET", path) is False

    def test_placeholder_token_never_authenticates(self):
        assert auth.bearer_ok(f"Bearer {PLACEHOLDER}", PLACEHOLDER) is False

    def test_placeholder_token_is_reported_at_startup(self, caplog):
        caplog.set_level(logging.ERROR, logger="clawrange.auth")
        assert auth.warn_if_unset(PLACEHOLDER) is True
        assert "placeholder" in caplog.text

    def test_placeholder_rejected_through_the_app(self, monkeypatch):

        monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", PLACEHOLDER)
        saved = app_module.app.dependency_overrides.pop(auth.require_token, None)
        try:
            r = TestClient(app_module.app).get(
                "/task", headers={"Authorization": f"Bearer {PLACEHOLDER}"}
            )
        finally:
            if saved is not None:
                app_module.app.dependency_overrides[auth.require_token] = saved
        assert r.status_code == 401

    def test_env_example_ships_no_token_value(self):
        lines = (REPO / ".env.example").read_text().splitlines()
        token = [ln for ln in lines if ln.startswith("PROXY_AUTH_TOKEN=")]
        assert token == ["PROXY_AUTH_TOKEN="]

    @pytest.mark.parametrize("value", ["", PLACEHOLDER])
    def test_make_tome_bridge_refuses_unset_or_placeholder(self, tmp_path, value):
        stub = tmp_path / "bin"
        stub.mkdir()
        marker = tmp_path / "ran"
        uv = stub / "uv"
        uv.write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\n')
        uv.chmod(0o755)
        env = {
            "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
            "PROXY_AUTH_TOKEN": value,
        }
        proc = subprocess.run(
            ["make", "-s", "-f", str(REPO / "Makefile"), "tome-bridge"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert proc.returncode != 0
        assert "PROXY_AUTH_TOKEN" in proc.stderr
        assert not marker.exists()

    def test_make_tome_bridge_runs_with_a_real_token(self, tmp_path):
        stub = tmp_path / "bin"
        stub.mkdir()
        marker = tmp_path / "ran"
        uv = stub / "uv"
        uv.write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\n')
        uv.chmod(0o755)
        env = {
            "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
            "PROXY_AUTH_TOKEN": "a-real-token",
        }
        proc = subprocess.run(
            ["make", "-s", "-f", str(REPO / "Makefile"), "tome-bridge"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert marker.exists()

    def test_make_tome_bridge_passes_only_the_token_from_env_file(self, tmp_path):
        """GIVEN a .env holding the queue token beside provider secrets
        WHEN make tome-bridge starts the bridge
        THEN the bridge gets the token but none of the other secrets, which
        its claude session (driven by untrusted queue text) would inherit."""
        stub = tmp_path / "bin"
        stub.mkdir()
        seen = tmp_path / "env.txt"
        uv = stub / "uv"
        uv.write_text(f'#!/usr/bin/env bash\nenv > "{seen}"\n')
        uv.chmod(0o755)
        (tmp_path / ".env").write_text(
            "PROXY_AUTH_TOKEN=from-dotenv\n"
            "OPENROUTER_API_KEY=sk-or-secret\n"
            "TELEGRAM_BOT_TOKEN=123:secret\n"
        )
        proc = subprocess.run(
            ["make", "-s", "-f", str(REPO / "Makefile"), "tome-bridge"],
            cwd=tmp_path,
            env={"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        env = seen.read_text().splitlines()
        assert "PROXY_AUTH_TOKEN=from-dotenv" in env
        assert not [ln for ln in env if ln.startswith(("OPENROUTER", "TELEGRAM"))]


# ─── K8 follow-up: make start-prod runs the soul.md render guard ─────


class TestStartProdSoulGuard:
    @pytest.fixture
    def repo(self, tmp_path):

        root = tmp_path / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / "openclaw").mkdir()
        shutil.copy(REPO / "Makefile", root / "Makefile")
        shutil.copy(REPO / "scripts" / "start.sh", root / "scripts" / "start.sh")
        (root / ".env").write_text(
            "OPENROUTER_API_KEY=sk-test\nPROXY_AUTH_TOKEN=a-real-token\n"
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "calls.log"
        stubs = {
            "docker": f'echo "docker $*" >> "{log}"\n',
            "curl": "exit 0\n",
            "uv": "exit 0\n",
            # The inner `make persona` the guard runs; the outer make is real.
            "make": (
                f'echo "make $*" >> "{log}"; echo soul > "{root}/openclaw/soul.md"\n'
            ),
        }
        for name, body in stubs.items():
            path = bin_dir / name
            path.write_text("#!/usr/bin/env bash\n" + body)
            path.chmod(0o755)
        return root, bin_dir, log

    def test_full_target_runs_the_guard_first(self):
        """Dry run only: the real target would clone DeerFlow."""
        proc = subprocess.run(
            ["make", "-n", "-f", str(REPO / "Makefile"), "start-prod-full"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        lines = proc.stdout.splitlines()
        guard = lines.index("./scripts/start.sh --ensure-soul")
        up = next(i for i, ln in enumerate(lines) if "compose" in ln and " up " in ln)
        assert guard < up

    def test_soul_rendered_before_prod_compose_up(self, repo):

        root, bin_dir, log = repo
        real_make = shutil.which("make")
        assert real_make
        proc = subprocess.run(
            [real_make, "-s", "start-prod"],
            cwd=root,
            env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        calls = log.read_text() if log.exists() else ""
        assert "make persona PROFILE=starter" in calls, proc.stdout + proc.stderr
        up = calls.index("docker compose -f docker-compose.yml")
        assert calls.index("make persona") < up
        assert (root / "openclaw" / "soul.md").is_file()

    def test_ensure_soul_flag_never_starts_compose(self, repo):
        root, bin_dir, log = repo
        proc = subprocess.run(
            ["bash", str(root / "scripts" / "start.sh"), "--ensure-soul"],
            env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        calls = log.read_text()
        assert "make persona" in calls
        assert "compose up" not in calls
