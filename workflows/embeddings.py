"""Brain embeddings via OpenRouter's OpenAI-compatible /api/v1/embeddings.

Per project convention every model call goes through OpenRouter or Z.AI.
Z.AI is not an option here: a GLM Coding Plan key cannot call embedding
models (error 1211), so embeddings use OpenRouter only.

Vectors from different models (or dimensions) are not comparable, so each
model+dimension pair gets its own sqlite-vec table (``EmbedConfig.table``)
and search reads only the configured model's table. Changing ``EMBED_MODEL``
or ``EMBED_DIM`` starts a fresh table that the backfill job fills; the old
one is left untouched.

Env: OPENROUTER_API_KEY (required), EMBED_MODEL
(default openai/text-embedding-3-small), EMBED_DIM (default 1536),
EMBED_URL (default the OpenRouter endpoint).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

logger = logging.getLogger("clawrange.embeddings")

DEFAULT_URL = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_MODEL = "openai/text-embedding-3-small"
DEFAULT_DIM = 1536
BATCH = 96  # inputs per request
TIMEOUT = 45.0


@dataclass(frozen=True)
class EmbedConfig:
    url: str
    model: str
    dim: int
    api_key: str

    @property
    def table(self) -> str:
        """sqlite-vec table for this model+dimension (one table per model)."""
        slug = re.sub(r"[^a-z0-9]+", "_", f"{self.model}_{self.dim}".lower())
        return "vec_" + slug.strip("_")


def config(env: Mapping[str, str] | None = None) -> EmbedConfig:
    env = os.environ if env is None else env
    return EmbedConfig(
        url=env.get("EMBED_URL") or DEFAULT_URL,
        model=env.get("EMBED_MODEL") or DEFAULT_MODEL,
        dim=int(env.get("EMBED_DIM") or DEFAULT_DIM),
        api_key=env.get("OPENROUTER_API_KEY", ""),
    )


def request_body(inputs: list[str], cfg: EmbedConfig) -> dict:
    # `dimensions` pins the output size (Matryoshka truncation for models
    # that support it), so a stored vector always matches the table.
    return {"model": cfg.model, "input": inputs, "dimensions": cfg.dim}


async def request(inputs: list[str], cfg: EmbedConfig) -> httpx.Response:
    """POST one embeddings request. Returns the raw response."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        return await client.post(
            cfg.url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json=request_body(inputs, cfg),
        )


async def embed_texts(
    texts: list[str], cfg: EmbedConfig | None = None, batch: int = BATCH
) -> list[list[float]] | None:
    """Vectors for ``texts`` in order, or None if any batch fails.

    A vector of the wrong length is a failure, never stored: it would not fit
    the model's table.
    """
    cfg = cfg or config()
    if not cfg.api_key:
        return None
    out: list[list[float]] = []
    for start in range(0, len(texts), batch):
        chunk = texts[start : start + batch]
        try:
            resp = await request(chunk, cfg)
        except httpx.HTTPError as exc:
            logger.warning("embeddings request failed: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning("embeddings HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        try:
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors = [list(map(float, d["embedding"])) for d in data]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("embeddings response unreadable: %s", exc)
            return None
        if len(vectors) != len(chunk) or any(len(v) != cfg.dim for v in vectors):
            logger.warning(
                "embeddings: expected %d x %d-dim vectors", len(chunk), cfg.dim
            )
            return None
        out.extend(vectors)
    return out


async def backfill(brain_db, cfg: EmbedConfig | None = None, limit: int = 256) -> dict:
    """Embed chunks that have no vector in the configured model's table.

    Also drops vectors whose chunk was rewritten (re-chunking replaces chunk
    rows, so the old vector no longer points at anything).
    """
    cfg = cfg or config()
    if not cfg.api_key:
        return {"skipped": "no api key"}
    pruned = brain_db.prune_orphan_embeddings()
    missing = brain_db.missing_embeddings(limit)
    if not missing:
        return {"stored": 0, "pruned": pruned}
    vectors = await embed_texts([m["content"] for m in missing], cfg)
    if vectors is None:
        return {"stored": 0, "pruned": pruned, "failed": len(missing)}
    brain_db.store_embeddings([(m["id"], v) for m, v in zip(missing, vectors)])
    logger.info("embeddings: stored %d vectors in %s", len(vectors), cfg.table)
    return {"stored": len(vectors), "pruned": pruned}
