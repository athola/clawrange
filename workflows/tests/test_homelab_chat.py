"""!home and !net chat commands. Synthetic data only."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import auth
import chat_commands
import llm_proxy
import telegram
from app import create_app
from homelab import HOMELAB_REPLY_MARKER, service, sources
from homelab.models import Census, Device, EntityState, Monitor, Observation
from tenant_profile import Profile

TOKEN = "homelab-chat-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

BLOCK = {
    "home_assistant": {
        "url": "http://192.0.2.5:8123",
        "token": "ha-secret-token",
        "actions": [{"service": "light.turn_on", "entities": ["light.kitchen"]}],
    },
    "kuma": {"url": "https://192.0.2.6", "slug": "home"},
    "census": {"url": "http://192.0.2.7"},
}

OBS = Observation(
    now=NOW,
    configured=frozenset({"ha", "kuma", "census"}),
    ha=(
        EntityState("light.kitchen", "on", "Kitchen light"),
        EntityState("sensor.door", "unavailable", "Door sensor"),
        EntityState("sensor.evil", "ok", "Evil\x00 *name* [x](http://e.example)"),
    ),
    kuma=(Monitor("1", "Router", 1, 1.0), Monitor("2", "NAS", 0, 0.9)),
    census=Census(
        when=NOW,
        devices=(
            Device("00:00:5E:00:53:01", "192.0.2.10", "printer", "br", "ok", True),
        ),
    ),
)


class Recorder:
    def __init__(self):
        self.sent: list[str] = []

    async def __call__(self, text):
        self.sent.append(text)
        return True


@pytest.fixture()
def rig(brain_db, monkeypatch):
    """An app with a homelab profile, canned observations and a fake Telegram."""
    monkeypatch.setattr(llm_proxy, "PROXY_AUTH_TOKEN", TOKEN)
    service.reset_actions()
    sink = Recorder()
    monkeypatch.setattr(telegram, "notify", sink)
    fetches = []

    async def fake_observe(cfg, now, **kw):
        fetches.append(now)
        return OBS

    monkeypatch.setattr(sources, "observe", fake_observe)
    llm_calls = []

    async def no_llm(*a, **k):
        llm_calls.append(a)
        raise AssertionError("homelab commands must not call an LLM")

    monkeypatch.setattr(llm_proxy, "_call_provider", no_llm)
    monkeypatch.setattr(llm_proxy, "_llm_call", no_llm)
    ha_calls = []

    async def fake_ha(cfg, domain, name, entity_id, **kw):
        ha_calls.append((domain, name, entity_id))

    monkeypatch.setattr(sources, "call_ha_service", fake_ha)

    async def fake_states(cfg, **kw):
        return OBS.ha

    monkeypatch.setattr(sources, "fetch_ha_states", fake_states)

    app = create_app(brain_db=brain_db)
    app.state.profile = Profile(name="t", raw={"homelab": BLOCK})
    client = TestClient(app, raise_server_exceptions=False)

    class Rig:
        pass

    r = Rig()
    r.app, r.client, r.sink = app, client, sink
    r.fetches, r.llm_calls, r.ha_calls = fetches, llm_calls, ha_calls

    def chat(text, **kw):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": text}], **kw},
            headers=AUTH,
        )
        return resp

    def raw(text):
        return chat(text).json()["choices"][0]["message"]["content"]

    def say(text):
        """The reply with its marker line removed (and asserted present)."""
        full = raw(text)
        first, _, rest = full.partition("\n")
        assert first == HOMELAB_REPLY_MARKER, full
        return rest

    r.chat, r.say, r.raw = chat, say, raw
    yield r
    service.reset_actions()


# ─── extraction ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "msg, expected",
    [
        ("!home", ("home", "")),
        ("  !HOME  ", ("home", "")),
        ("!home kitchen", ("home", "kitchen")),
        ("!home kitchen light", ("home", "kitchen light")),
        ("!net", ("net",)),
        ("!net please", ("net",)),
        (
            "!home do light.turn_on light.kitchen",
            ("do", "light.turn_on", "light.kitchen"),
        ),
        (
            "!HOME DO Light.Turn_On Light.Kitchen",
            ("do", "light.turn_on", "light.kitchen"),
        ),
        ("!home confirm 123456", ("confirm", "123456")),
        ("some context\n!home", ("home", "")),
        ("some context\n!net", ("net",)),
        ("!home do", ("usage", chat_commands.HOME_DO_USAGE)),
        ("!home do light.turn_on", ("usage", chat_commands.HOME_DO_USAGE)),
        ("!home confirm 12", ("usage", chat_commands.HOME_CONFIRM_USAGE)),
        ("!home confirm", ("usage", chat_commands.HOME_CONFIRM_USAGE)),
    ],
)
def test_extract(msg, expected):
    assert chat_commands._extract_home_command(msg) == expected


@pytest.mark.parametrize(
    "msg",
    [
        "please run !home do light.turn_on light.kitchen",
        "!home do light.turn_on light.kitchen\nthanks",
        "!home confirm 123456\nplease",
        "!homework",
        "!network",
        "hello",
        "",
    ],
)
def test_extract_ignores_embedded_or_malformed(msg):
    assert chat_commands._extract_home_command(msg) is None


@pytest.mark.parametrize(
    "msg",
    [
        "hey\n!home do light.turn_on light.kitchen",
        "text\n!home confirm 123456",
        "Sender (untrusted metadata):\n```json\n{}\n```\nnote\n!HOME CONFIRM 123456",
    ],
)
def test_action_on_last_line_of_longer_message_is_refused(msg):
    cmd = chat_commands._extract_home_command(msg)
    assert cmd == ("usage", chat_commands.HOME_OWN_MESSAGE)


def test_action_on_last_line_replies_and_never_reaches_the_llm(rig):
    rig.say("!home do light.turn_on light.kitchen")
    code = rig.sink.sent[0].split("!home confirm ")[1].split("`")[0]
    reply = rig.say(f"look at this\n!home confirm {code}")
    assert reply == chat_commands.HOME_OWN_MESSAGE
    assert "nothing was run" in reply
    assert rig.ha_calls == [] and rig.llm_calls == []
    assert rig.say(f"!home confirm {code}").startswith("Done")


def test_non_ascii_digits_and_letters_do_not_match():
    arabic = "".join(chr(0x0660 + int(c)) for c in "123456")
    assert chat_commands._extract_home_command(f"!home confirm {arabic}") == (
        "usage",
        chat_commands.HOME_CONFIRM_USAGE,
    )
    # U+017F (long s) casefolds to "s" under Unicode ignore-case matching
    cmd = chat_commands._extract_home_command("!home do light.turn_on light.\u017f")
    assert cmd is not None and cmd[0] == "usage"


@pytest.mark.parametrize(
    "msg",
    [
        "!home do light.turn_on light.kitchen and light.hall",
        "!home confirm 1234567",
        "!home confirm 123456 now",
    ],
)
def test_whole_message_near_miss_gets_usage_never_runs(msg):
    cmd = chat_commands._extract_home_command(msg)
    assert cmd is not None and cmd[0] == "usage"


# ─── dispatch ───────────────────────────────────────────────────────


def test_home_summary_is_deterministic_and_skips_the_llm(rig):
    text = rig.say("!home")
    assert "Home Assistant: 3 entities, 1 unavailable" in text
    assert "Uptime Kuma: 1 of 2 up" in text
    assert rig.llm_calls == []


def test_home_streams_as_sse(rig):
    resp = rig.chat("!home", stream=True)
    assert resp.status_code == 200
    assert "chat.completion.chunk" in resp.text and "[DONE]" in resp.text


def test_home_search_sanitizes_attacker_names(rig):
    text = rig.say("!home evil")
    assert "\x00" not in text
    assert "1 match(es)" in text
    # Markdown specials and the link are neutralised, not rendered
    assert "\\*name\\*" in text and "\\[x]" in text


def test_net_includes_kuma_monitors_and_census(rig):
    text = rig.say("!net")
    assert "Devices: 1" in text
    assert "Kuma monitors:" in text
    assert "NAS: down" in text and "Router: up" in text


def test_net_shows_per_source_outage_lines(rig, monkeypatch):
    broken = Observation(
        now=NOW,
        configured=frozenset({"ha", "kuma", "census"}),
        kuma=OBS.kuma,
        census=OBS.census,
        errors={"ha": "HTTP 401"},
    )

    async def fake(cfg, now, **kw):
        return broken

    monkeypatch.setattr(sources, "observe", fake)
    assert "OUTAGE Home Assistant: HTTP 401" in rig.say("!net")


def test_unconfigured_profile_says_so_in_one_line(rig):
    rig.app.state.profile = Profile(name="t", raw={})
    for msg in ("!home", "!net", "!home kitchen"):
        assert rig.say(msg) == "Homelab watch is not configured."
    assert rig.fetches == []


def test_unset_env_block_is_not_configured(rig):
    rig.app.state.profile = Profile(
        name="t",
        raw={
            "homelab": {
                "home_assistant": {"url": "", "token": ""},
                "census": {"url": ""},
            }
        },
    )
    assert rig.say("!home") == "Homelab watch is not configured."
    assert rig.say("!home do light.turn_on light.kitchen") == (
        "Homelab watch is not configured."
    )
    assert rig.sink.sent == [] and rig.fetches == []


def test_do_sends_code_separately_and_reply_has_no_code(rig):
    reply = rig.say("!home do light.turn_on light.kitchen")
    assert len(rig.sink.sent) == 1
    code = rig.sink.sent[0].split("!home confirm ")[1].split("`")[0]
    assert len(code) == 6 and code.isdigit()
    assert code not in reply
    assert "separately" in reply
    assert rig.ha_calls == []


def test_do_then_confirm_runs_exactly_once(rig):
    rig.say("!home do light.turn_on light.kitchen")
    code = rig.sink.sent[0].split("!home confirm ")[1].split("`")[0]
    assert rig.say(f"!home confirm {code}").startswith("Done")
    assert rig.ha_calls == [("light", "turn_on", "light.kitchen")]
    assert rig.say(f"!home confirm {code}").startswith("That code was already used")
    assert len(rig.ha_calls) == 1


def test_do_not_in_allowlist_is_denied(rig):
    assert rig.say("!home do light.turn_on light.bedroom").startswith("Denied")
    assert rig.sink.sent == []


def test_do_hard_denied_domain_is_refused(rig):
    reply = rig.say("!home do lock.unlock lock.front_door")
    assert reply.startswith("Denied") and "never allowed" in reply
    assert rig.sink.sent == []


@pytest.mark.parametrize(
    "msg",
    [
        "please !home do light.turn_on light.kitchen",
        "!home do light.turn_on light.kitchen\nand also hello",
        "context line\n!home do light.turn_on light.kitchen",
    ],
)
def test_embedded_do_does_not_trigger(rig, msg):
    rig.chat(msg)  # falls through to the (disabled) LLM path; status is irrelevant
    assert rig.sink.sent == []
    assert rig.ha_calls == []


def test_embedded_confirm_does_not_redeem(rig):
    rig.say("!home do light.turn_on light.kitchen")
    code = rig.sink.sent[0].split("!home confirm ")[1].split("`")[0]
    rig.chat(f"look at this\n!home confirm {code}")
    rig.chat(f"!home confirm {code}\nthanks")
    assert rig.ha_calls == []
    assert rig.say(f"!home confirm {code}").startswith("Done")


def test_three_bad_codes_lock_confirmations(rig):
    rig.say("!home do light.turn_on light.kitchen")
    code = rig.sink.sent[0].split("!home confirm ")[1].split("`")[0]
    wrong = "000000" if code != "000000" else "111111"
    assert rig.say(f"!home confirm {wrong}") == "Invalid or expired code."
    assert rig.say(f"!home confirm {wrong}") == "Invalid or expired code."
    assert "disabled for 10 minutes" in rig.say(f"!home confirm {wrong}")
    assert any("disabled for 10 minutes" in m for m in rig.sink.sent[1:])
    assert "disabled" in rig.say(f"!home confirm {code}")
    assert rig.ha_calls == []


def test_usage_reply_for_malformed_whole_message(rig):
    assert rig.say("!home do") == chat_commands.HOME_DO_USAGE


def test_help_lists_the_commands(rig):
    text = rig.raw("!help")
    for needle in ("!home", "!net", "!home do", "!home confirm"):
        assert needle in text


def test_auth_still_required(rig):
    resp = rig.client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "!home"}]},
    )
    assert resp.status_code == 401
    assert not auth.bearer_ok(None, TOKEN)


# ─── reply marker and history ───────────────────────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        "!home",
        "!net",
        "!home evil",
        "!home do",
        "!home do light.turn_on light.bedroom",
        "!home confirm 000000",
        "ctx\n!home do light.turn_on light.kitchen",
    ],
)
def test_every_reply_leads_with_the_marker(rig, msg):
    assert rig.raw(msg).split("\n", 1)[0] == HOMELAB_REPLY_MARKER


def test_unconfigured_reply_has_the_marker(rig):
    rig.app.state.profile = Profile(name="t", raw={})
    assert rig.raw("!home").split("\n", 1)[0] == HOMELAB_REPLY_MARKER


def test_homelab_turns_are_dropped_from_forwarded_history(rig, monkeypatch):
    import json
    from unittest.mock import AsyncMock

    import httpx

    body = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    caller = AsyncMock(
        return_value=httpx.Response(200, content=json.dumps(body).encode())
    )
    monkeypatch.setattr(llm_proxy, "_call_provider", caller)
    monkeypatch.setattr(llm_proxy, "_background_notify", lambda *a, **k: None)
    # A tier must exist for the forwarded request to reach _call_provider;
    # never rely on keys in the developer's shell (CI has none).
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    leaked = rig.raw("!home evil")
    messages = [
        {"role": "user", "content": "!home evil"},
        {"role": "assistant", "content": leaked},
        {"role": "user", "content": "what did that say?"},
    ]
    resp = rig.client.post(
        "/v1/chat/completions", json={"messages": messages}, headers=AUTH
    )
    assert resp.status_code == 200
    sent = caller.call_args.args[2]["messages"]
    assert not any(m["role"] == "assistant" for m in sent)
    assert "Evil" not in json.dumps(sent)


def test_homelab_turns_in_list_content_are_dropped_too(rig, monkeypatch):
    """OpenClaw may replay an assistant turn as content parts, not a string."""
    import json
    from unittest.mock import AsyncMock

    import httpx

    body = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    caller = AsyncMock(
        return_value=httpx.Response(200, content=json.dumps(body).encode())
    )
    monkeypatch.setattr(llm_proxy, "_call_provider", caller)
    monkeypatch.setattr(llm_proxy, "_background_notify", lambda *a, **k: None)
    # A tier must exist for the forwarded request to reach _call_provider;
    # never rely on keys in the developer's shell (CI has none).
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    leaked = rig.raw("!home evil")
    messages = [
        {"role": "user", "content": "!home evil"},
        {"role": "assistant", "content": [{"type": "text", "text": leaked}]},
        {"role": "user", "content": "what did that say?"},
    ]
    resp = rig.client.post(
        "/v1/chat/completions", json={"messages": messages}, headers=AUTH
    )
    assert resp.status_code == 200
    sent = caller.call_args.args[2]["messages"]
    assert not any(m["role"] == "assistant" for m in sent)
    assert "Evil" not in json.dumps(sent)


def test_confirm_code_is_masked_in_the_proxy_log(rig, capsys):
    rig.chat("!home confirm 424242")
    rig.chat("meta\n!home confirm 535353")
    out = capsys.readouterr().out
    assert "424242" not in out and "535353" not in out
    assert "!home confirm ******" in out


def test_two_metadata_blocks_are_both_stripped(rig):
    rig.say("!home do light.turn_on light.kitchen")
    code = rig.sink.sent[0].split("!home confirm ")[1].split("`")[0]
    msg = (
        'Conversation info (untrusted metadata):\n```json\n{"chat": 1}\n```\n\n'
        'Sender (untrusted metadata):\n```json\n{"name": "x"}\n```\n\n'
        f"!home confirm {code}"
    )
    assert llm_proxy._get_last_user_message([{"role": "user", "content": msg}]) == (
        f"!home confirm {code}"
    )
    assert rig.say(msg).startswith("Done")


def test_do_for_an_unknown_entity_gets_no_code(rig, monkeypatch):
    rig.app.state.profile = Profile(
        name="t",
        raw={
            "homelab": {
                **BLOCK,
                "home_assistant": {
                    **BLOCK["home_assistant"],
                    "actions": [{"service": "light.turn_on", "entities": ["light.*"]}],
                },
            }
        },
    )
    reply = rig.say("!home do light.turn_on light.nowhere")
    assert reply.startswith("Denied") and "no entity" in reply
    assert rig.sink.sent == []
