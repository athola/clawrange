"""HomelabConfig: built from the env-resolved profile block."""

import pytest

from homelab.config import HomelabConfig
from homelab.models import WatchConfig


def test_empty_block_configures_nothing():
    cfg = HomelabConfig.from_block(None)
    assert cfg.configured == frozenset()
    assert cfg.watch == WatchConfig()
    assert cfg.rules == ()


def test_unset_env_resolves_to_empty_and_disables_silently():
    cfg = HomelabConfig.from_block(
        {
            "home_assistant": {"url": "", "token": ""},
            "kuma": {"url": "", "slug": "home", "ca_cert": ""},
            "census": {"url": ""},
        }
    )
    assert cfg.configured == frozenset()


def test_ha_needs_url_and_token():
    only_url = HomelabConfig.from_block({"home_assistant": {"url": "http://192.0.2.5"}})
    assert only_url.configured == frozenset()
    both = HomelabConfig.from_block(
        {"home_assistant": {"url": "http://192.0.2.5/", "token": "t"}}
    )
    assert both.configured == frozenset({"ha"})
    assert both.ha_url == "http://192.0.2.5"


def test_full_block():
    cfg = HomelabConfig.from_block(
        {
            "home_assistant": {
                "url": "http://192.0.2.5:8123",
                "token": "tok",
                "battery_low": 15,
                "unavailable_after_minutes": 45,
                "flood_threshold": 7,
                "ignore_entities": ["sensor.noisy_*"],
                "actions": [{"service": "light.turn_on", "entities": ["light.lamp"]}],
            },
            "kuma": {"url": "https://192.0.2.6", "ca_cert": "/config/homelab/ca.pem"},
            "census": {"url": "http://192.0.2.7", "max_age_hours": 20},
            "source_down_after": 5,
        }
    )
    assert cfg.configured == frozenset({"ha", "kuma", "census"})
    assert cfg.kuma_slug == "home"
    assert cfg.kuma_ca_cert == "/config/homelab/ca.pem"
    assert cfg.watch == WatchConfig(
        battery_low=15,
        unavailable_after_minutes=45,
        flood_threshold=7,
        ignore_entities=("sensor.noisy_*",),
        census_max_age_hours=20,
        source_down_after=5,
    )
    assert cfg.rules[0].service == "light.turn_on"


def test_bad_actions_fail_closed_to_no_rules():
    cfg = HomelabConfig.from_block(
        {
            "home_assistant": {
                "actions": [{"service": "lock.unlock", "entities": ["lock.door"]}]
            }
        }
    )
    assert cfg.rules == ()


def test_half_set_sources_are_recorded_as_incomplete():
    only_url = HomelabConfig.from_block({"home_assistant": {"url": "http://192.0.2.5"}})
    only_token = HomelabConfig.from_block({"home_assistant": {"token": "t"}})
    cert = HomelabConfig.from_block({"kuma": {"url": "", "ca_cert": "/x/ca.pem"}})
    for cfg, name in ((only_url, "ha"), (only_token, "ha"), (cert, "kuma")):
        assert cfg.incomplete == frozenset({name})
        assert name not in cfg.configured


def test_complete_or_untouched_sources_are_not_incomplete():
    assert HomelabConfig.from_block(None).incomplete == frozenset()
    both = HomelabConfig.from_block(
        {"home_assistant": {"url": "http://x", "token": "t"}}
    )
    assert both.incomplete == frozenset()
    assert HomelabConfig.from_block({"census": {"url": ""}}).incomplete == frozenset()


# ─── local_file: operator-private ignore list and action rules ──────


def _local(tmp_path, text: str) -> str:
    path = tmp_path / "ha-local.yaml"
    path.write_text(text)
    return str(path)


def _with_local(path: str, **ha) -> HomelabConfig:
    return HomelabConfig.from_block({"home_assistant": {**ha, "local_file": path}})


def test_local_file_adds_ignores_and_rules_to_the_profile(tmp_path):
    path = _local(
        tmp_path,
        "ignore_entities: [sensor.dead_one, 'binary_sensor.old_*']\n"
        "actions:\n"
        "  - {service: light.turn_on, entities: [light.lamp]}\n",
    )
    cfg = _with_local(
        path,
        ignore_entities=["sensor.noisy_*"],
        actions=[{"service": "light.turn_off", "entities": ["light.*"]}],
    )
    assert cfg.watch.ignore_entities == (
        "sensor.noisy_*",
        "sensor.dead_one",
        "binary_sensor.old_*",
    )
    assert [r.service for r in cfg.rules] == ["light.turn_off", "light.turn_on"]
    assert cfg.local_error == ""


def test_no_local_file_is_not_an_error():
    cfg = HomelabConfig.from_block({"home_assistant": {"local_file": ""}})
    assert cfg.local_error == ""
    assert cfg.rules == ()


@pytest.mark.parametrize(
    "text, match",
    [
        (None, "cannot be read"),
        ("ignore_entities: [a\n", "not valid YAML"),
        ("- just a list\n", "mapping"),
        ("ignore_entities: [sensor.a]\nextra: 1\n", "unknown keys"),
        ("ignore_entities: sensor.a\n", "ignore_entities"),
        ("actions: [{service: lock.unlock, entities: [lock.door]}]\n", "denied"),
    ],
)
def test_bad_local_file_fails_closed_and_says_why(tmp_path, text, match):
    path = str(tmp_path / "missing.yaml") if text is None else _local(tmp_path, text)
    cfg = _with_local(
        path,
        ignore_entities=["sensor.noisy_*"],
        actions=[{"service": "light.turn_off", "entities": ["light.*"]}],
    )
    assert match in cfg.local_error
    # Nothing from the file applies, but the profile's own settings still do.
    assert cfg.watch.ignore_entities == ("sensor.noisy_*",)
    assert [r.service for r in cfg.rules] == ["light.turn_off"]


def test_local_error_never_echoes_file_contents(tmp_path):
    path = _local(
        tmp_path, "actions: [{service: lock.unlock, entities: [lock.secret_door]}]\n"
    )
    assert "secret_door" not in _with_local(path).local_error
