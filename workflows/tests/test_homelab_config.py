"""HomelabConfig: built from the env-resolved profile block."""

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
