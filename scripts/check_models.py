"""Check every configured OpenRouter model id against the live catalog.

OpenRouter retires model ids without notice, and the proxy only finds out
as a 404 at call time (it then falls through to the next tier, so nothing
looks broken). Run `make models-check` after a dependency update.

Covers workflows/models.json (tiers + web-search routing) and the
OpenRouter models in deerflow/config.yaml. Exits 1 when any id is gone.
"""

import json
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG_URL = "https://openrouter.ai/api/v1/models"
# Route variants, not separate catalog entries.
VARIANT_SUFFIXES = (":online",)


def configured_ids(tiers: dict, deerflow: dict) -> dict[str, str]:
    """Map each OpenRouter model id to where it is configured."""
    ids: dict[str, str] = {}
    for tier in tiers.get("tiers", []):
        if tier.get("provider") == "openrouter":
            for model in tier.get("models", []):
                ids[model] = f"models.json tier {tier['name']}"
    search = tiers.get("search", {})
    if search.get("provider") == "openrouter":
        for model in search.get("models", []):
            ids[model] = "models.json search"
    for model in deerflow.get("models", []):
        if "openrouter.ai" in (model.get("base_url") or ""):
            ids[model["model"]] = f"deerflow model {model['name']}"
    return ids


def missing(ids: dict[str, str], catalog: set[str]) -> dict[str, str]:
    """The configured ids whose base model is not in the catalog."""
    out = {}
    for model, where in ids.items():
        base = model
        for suffix in VARIANT_SUFFIXES:
            base = base.removesuffix(suffix)
        if base not in catalog:
            out[model] = where
    return out


def main() -> int:
    tiers = json.loads((ROOT / "workflows/models.json").read_text())
    deerflow = yaml.safe_load((ROOT / "deerflow/config.yaml").read_text())
    with urllib.request.urlopen(CATALOG_URL, timeout=30) as resp:
        catalog = {m["id"] for m in json.load(resp)["data"]}
    ids = configured_ids(tiers, deerflow)
    gone = missing(ids, catalog)
    for model, where in sorted(gone.items()):
        print(f"GONE  {model}  ({where})")
    print(f"{len(ids) - len(gone)}/{len(ids)} configured OpenRouter models are live")
    return 1 if gone else 0


if __name__ == "__main__":
    sys.exit(main())
