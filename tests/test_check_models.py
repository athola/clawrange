"""check_models finds configured OpenRouter model ids the catalog no longer has."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

import check_models

TIERS = {
    "tiers": [
        {"name": "free", "provider": "openrouter", "models": ["a/live:free", "a/gone"]},
        {"name": "direct", "provider": "zai", "models": ["glm-x"]},
    ],
    "search": {"provider": "openrouter", "models": ["a/live:online"]},
}
DEERFLOW = {
    "models": [
        {"name": "r", "model": "b/live", "base_url": "https://openrouter.ai/api/v1"},
        {"name": "x", "model": "b/gone", "base_url": "https://openrouter.ai/api/v1"},
        {"name": "y", "model": "local", "base_url": "http://localhost:11434"},
    ]
}
CATALOG = {"a/live:free", "a/live", "b/live"}


def test_configured_ids_cover_tiers_search_and_deerflow():
    ids = check_models.configured_ids(TIERS, DEERFLOW)
    assert ids == {
        "a/live:free": "models.json tier free",
        "a/gone": "models.json tier free",
        "a/live:online": "models.json search",
        "b/live": "deerflow model r",
        "b/gone": "deerflow model x",
    }


def test_missing_ignores_online_suffix_and_non_openrouter():
    ids = check_models.configured_ids(TIERS, DEERFLOW)
    assert check_models.missing(ids, CATALOG) == {
        "a/gone": "models.json tier free",
        "b/gone": "deerflow model x",
    }
