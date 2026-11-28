"""Action policy: allowlist, hard denies and confirmation codes."""

from datetime import UTC, datetime, timedelta

import pytest

from homelab.actions import (
    HARD_DENY_DOMAINS,
    ActionRule,
    PendingActions,
    authorize,
    parse_rules,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

RULES = parse_rules(
    [
        {"service": "light.turn_on", "entities": ["light.lamp", "light.*"]},
        {"service": "light.turn_off", "entities": ["light.lamp"]},
        {"service": "script.turn_on", "entities": ["script.goodnight"]},
    ]
)


# ─── parse_rules ────────────────────────────────────────────────────


def test_parse_rules_valid():
    assert RULES[0] == ActionRule("light.turn_on", ("light.lamp", "light.*"))
    assert len(RULES) == 3


def test_parse_rules_empty_and_none():
    assert parse_rules([]) == ()
    assert parse_rules(None) == ()


@pytest.mark.parametrize(
    "bad",
    [
        "light.turn_on",
        [{"service": "light.turn_on"}],
        [{"entities": ["light.a"]}],
        [{"service": "turn_on", "entities": ["light.a"]}],
        [{"service": "light.turn_on", "entities": []}],
        [{"service": "light.turn_on", "entities": "light.a"}],
        [{"service": "light.turn_on", "entities": ["light.a"], "extra": 1}],
        [{"service": "light.turn_on", "entities": ["*"]}],
        [{"service": "light.turn_on", "entities": ["*.*"]}],
        [{"service": "light.turn_on", "entities": ["light.k*"]}],
        [{"service": "light.turn_on", "entities": ["script.*"]}],
        [{"service": "light.turn_on", "entities": ["automation.*"]}],
        [{"service": "script.turn_on", "entities": ["script.*"]}],
        [{"service": "light.turn_on", "entities": ["Light.A"]}],
        ["not a dict"],
    ],
)
def test_parse_rules_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_rules(bad)


@pytest.mark.parametrize("domain", sorted(HARD_DENY_DOMAINS))
def test_parse_rules_rejects_hard_denied_domains(domain):
    with pytest.raises(ValueError):
        parse_rules([{"service": f"{domain}.do_it", "entities": ["light.a"]}])
    with pytest.raises(ValueError):
        parse_rules([{"service": "light.turn_on", "entities": [f"{domain}.x"]}])


def test_hard_deny_list_matches_spec():
    assert HARD_DENY_DOMAINS == frozenset(
        {
            "lock",
            "cover",
            "alarm_control_panel",
            "valve",
            "siren",
            "camera",
            "homeassistant",
            "hassio",
            "shell_command",
            "rest_command",
            "python_script",
            "pyscript",
            "recorder",
            "system_log",
        }
    )


def test_script_and_automation_exact_ids_are_allowed():
    rules = parse_rules(
        [
            {"service": "script.turn_on", "entities": ["script.goodnight"]},
            {"service": "automation.trigger", "entities": ["automation.porch"]},
        ]
    )
    assert authorize(rules, "automation.trigger", "automation.porch") is None


# ─── authorize ──────────────────────────────────────────────────────


def test_authorize_exact_and_domain_glob():
    assert authorize(RULES, "light.turn_on", "light.lamp") is None
    assert authorize(RULES, "light.turn_on", "light.kitchen") is None
    assert authorize(RULES, "script.turn_on", "script.goodnight") is None


def test_authorize_denies_unlisted_service_or_entity():
    assert authorize(RULES, "light.toggle", "light.lamp")
    assert authorize(RULES, "light.turn_off", "light.kitchen")
    assert authorize(RULES, "script.turn_on", "script.other")
    assert authorize(RULES, "switch.turn_on", "light.lamp")


def test_authorize_empty_rules_deny_everything():
    assert authorize((), "light.turn_on", "light.lamp")


@pytest.mark.parametrize("domain", sorted(HARD_DENY_DOMAINS))
def test_authorize_hard_deny_even_if_rule_matches(domain):
    forced = (ActionRule(f"{domain}.go", (f"{domain}.x",)),)
    assert authorize(forced, f"{domain}.go", f"{domain}.x")
    mixed = (ActionRule("light.turn_on", (f"{domain}.x",)),)
    assert authorize(mixed, "light.turn_on", f"{domain}.x")


def test_authorize_glob_on_script_in_hand_built_rule_is_ignored():
    forced = (ActionRule("script.turn_on", ("script.*",)),)
    assert authorize(forced, "script.turn_on", "script.anything")


@pytest.mark.parametrize(
    ("service", "entity"),
    [
        ("light.turn_on", "light.lamp\nlock.front"),
        ("light", "light.lamp"),
        ("light.turn_on", "lamp"),
        ("", ""),
        ("light.turn_on", "LIGHT.lamp"),
    ],
)
def test_authorize_rejects_malformed_ids(service, entity):
    assert authorize(RULES, service, entity)


def test_denial_reason_does_not_echo_markdown_specials():
    reason = authorize(RULES, "shell_command.run", "light.lamp")
    assert reason
    assert "_" not in reason.replace("\\_", "")


# ─── PendingActions ─────────────────────────────────────────────────


def test_issue_returns_six_digit_code_and_redeem_is_single_use():
    pending = PendingActions()
    code = pending.issue("light.turn_on", "light.lamp", T0)
    assert len(code) == 6
    assert code.isdigit()
    assert pending.redeem(code, T0 + timedelta(seconds=5)) == (
        "light.turn_on",
        "light.lamp",
    )
    assert pending.redeem(code, T0 + timedelta(seconds=6)) is None


def test_redeem_expires_after_ttl():
    pending = PendingActions()
    code = pending.issue("light.turn_on", "light.lamp", T0)
    assert pending.redeem(code, T0 + timedelta(seconds=121)) is None
    # expired codes are gone, not merely refused once
    assert pending.redeem(code, T0 + timedelta(seconds=1)) is None


def test_redeem_at_ttl_boundary_still_valid():
    pending = PendingActions()
    code = pending.issue("light.turn_on", "light.lamp", T0)
    assert pending.redeem(code, T0 + timedelta(seconds=120)) is not None


def test_redeem_unknown_or_malformed_code():
    pending = PendingActions()
    pending.issue("light.turn_on", "light.lamp", T0)
    assert pending.redeem("000000x", T0) is None
    assert pending.redeem("", T0) is None
    assert pending.redeem("abc", T0) is None


def test_at_most_five_pending_oldest_evicted():
    pending = PendingActions()
    codes = [
        pending.issue("light.turn_on", f"light.l{i}", T0 + timedelta(seconds=i))
        for i in range(6)
    ]
    assert len(set(codes)) == 6
    later = T0 + timedelta(seconds=10)
    assert pending.redeem(codes[0], later) is None
    assert pending.redeem(codes[5], later) == ("light.turn_on", "light.l5")
    assert pending.redeem(codes[1], later) == ("light.turn_on", "light.l1")


def test_issue_purges_expired_before_counting():
    pending = PendingActions()
    old = [pending.issue("light.turn_on", f"light.l{i}", T0) for i in range(5)]
    later = T0 + timedelta(seconds=500)
    fresh = pending.issue("light.turn_on", "light.new", later)
    assert pending.redeem(fresh, later) == ("light.turn_on", "light.new")
    assert all(pending.redeem(c, later) is None for c in old)


def test_codes_use_secrets(monkeypatch):
    calls = []

    def fake_randbelow(n):
        calls.append(n)
        return 42

    monkeypatch.setattr("homelab.actions.secrets.randbelow", fake_randbelow)
    pending = PendingActions()
    assert pending.issue("light.turn_on", "light.a", T0) == "000042"
    assert calls == [1_000_000]
