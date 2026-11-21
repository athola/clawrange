"""check_models finds configured OpenRouter model ids the catalog no longer has."""

import os
import sys

import pytest

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


def test_configured_ids_skips_non_openrouter_search_and_missing_base_url():
    """GIVEN search routed via a non-OpenRouter provider and a deerflow model
    with no base_url
    WHEN configured_ids runs
    THEN neither contributes an id."""
    tiers = {"search": {"provider": "zai", "models": ["glm-search"]}}
    deerflow = {"models": [{"name": "n", "model": "m"}]}
    assert check_models.configured_ids(tiers, deerflow) == {}


def test_configured_ids_tolerates_empty_configs():
    """GIVEN empty tier and deerflow configs
    WHEN configured_ids runs
    THEN it returns an empty mapping."""
    assert check_models.configured_ids({}, {}) == {}


# ─── main(): config files on disk, catalog fetch mocked ───────────


def _setup_root(tmp_path, monkeypatch, tiers, deerflow):
    import json

    import yaml

    (tmp_path / "workflows").mkdir()
    (tmp_path / "deerflow").mkdir()
    (tmp_path / "workflows/models.json").write_text(json.dumps(tiers))
    (tmp_path / "deerflow/config.yaml").write_text(yaml.safe_dump(deerflow))
    monkeypatch.setattr(check_models, "ROOT", tmp_path)


def _fake_catalog(monkeypatch, ids):
    import io
    import json

    seen = {}

    def fake_urlopen(url, timeout=None):
        seen["url"], seen["timeout"] = url, timeout
        payload = {"data": [{"id": i} for i in ids]}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(check_models.urllib.request, "urlopen", fake_urlopen)
    return seen


class TestMain:
    def test_returns_zero_when_every_model_is_live(self, tmp_path, monkeypatch, capsys):
        """GIVEN every configured id is in the catalog
        WHEN main runs
        THEN it returns 0 and reports N/N live."""
        _setup_root(tmp_path, monkeypatch, TIERS, DEERFLOW)
        seen = _fake_catalog(monkeypatch, CATALOG | {"a/gone", "b/gone"})
        assert check_models.main() == 0
        out = capsys.readouterr().out
        assert "5/5 configured OpenRouter models are live" in out
        assert "GONE" not in out
        assert seen["url"] == check_models.CATALOG_URL

    def test_returns_one_and_lists_retired_models(self, tmp_path, monkeypatch, capsys):
        """GIVEN two configured ids are missing from the catalog
        WHEN main runs
        THEN it returns 1 and prints each GONE id with where it is set."""
        _setup_root(tmp_path, monkeypatch, TIERS, DEERFLOW)
        _fake_catalog(monkeypatch, CATALOG)
        assert check_models.main() == 1
        out = capsys.readouterr().out
        assert "GONE  a/gone  (models.json tier free)" in out
        assert "GONE  b/gone  (deerflow model x)" in out
        assert "3/5 configured OpenRouter models are live" in out

    def test_catalog_outage_propagates_instead_of_passing(self, tmp_path, monkeypatch):
        """GIVEN the catalog request fails
        WHEN main runs
        THEN the error propagates, so an outage is never read as 'all live'."""
        import urllib.error

        _setup_root(tmp_path, monkeypatch, TIERS, DEERFLOW)

        def boom(url, timeout=None):
            raise urllib.error.URLError("down")

        monkeypatch.setattr(check_models.urllib.request, "urlopen", boom)
        with pytest.raises(urllib.error.URLError):
            check_models.main()
