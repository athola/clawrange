"""The OpenClaw config template must keep feeding the heartbeat interceptor.

workflows/heartbeat.py recognises a heartbeat by the polled message starting
with _HEARTBEAT_MARKER. OpenClaw 2026.8+ changed its default heartbeat prompt
(it no longer mentions HEARTBEAT.md), so every heartbeating agent pins the
prompt explicitly; without it heartbeats reach the model unintercepted.
"""

import json
from pathlib import Path

import heartbeat

TEMPLATE = Path(__file__).resolve().parents[2] / "openclaw" / "config" / "openclaw.json"


def _heartbeats():
    agents = json.loads(TEMPLATE.read_text())["agents"]
    for agent_id, entry in agents["entries"].items():
        hb = entry.get("heartbeat")
        if hb and hb.get("every") not in (None, "0m"):
            yield agent_id, hb


def test_some_agent_heartbeats():
    assert list(_heartbeats()), "no agent has a recurring heartbeat"


def test_every_heartbeat_prompt_is_intercepted():
    for agent_id, hb in _heartbeats():
        prompt = hb.get("prompt", "")
        assert heartbeat._is_heartbeat(prompt), (
            f"{agent_id} heartbeat prompt would bypass the interceptor: {prompt!r}"
        )


def test_default_heartbeat_is_off():
    # 2026.8+ otherwise gives every agent a 30m owner-DM heartbeat.
    defaults = json.loads(TEMPLATE.read_text())["agents"]["defaults"]
    assert defaults["heartbeat"]["every"] == "0m"
