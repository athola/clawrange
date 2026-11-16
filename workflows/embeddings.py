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
from dataclasses import dataclass, field, replace

import httpx

logger = logging.getLogger("clawrange.embeddings")

DEFAULT_URL = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_MODEL = "openai/text-embedding-3-small"
DEFAULT_DIM = 1536
BATCH = 96  # inputs per request
TIMEOUT = 45.0  # backfill batches: up to BATCH inputs per request
# A search waits on its query embedding before it can answer, and falls back
# to keyword search when the embed fails: a slow provider must not stall it.
QUERY_TIMEOUT = 4.0
# Statuses that blame the input itself (too long, malformed). Only these mark
# a chunk as rejected; anything else is an outage and is retried next run.
REJECTED_STATUSES = frozenset({400, 413, 422})


@dataclass(frozen=True)
class EmbedConfig:
    url: str
    model: str
    dim: int
    api_key: str = field(repr=False)
    timeout: float = TIMEOUT

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
    async with httpx.AsyncClient(timeout=cfg.timeout) as client:
        return await client.post(
            cfg.url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json=request_body(inputs, cfg),
        )


async def _embed_batch(
    inputs: list[str], cfg: EmbedConfig
) -> tuple[list[list[float]] | None, bool]:
    """One request, as (vectors, refused).

    (vectors, False) on success; (None, True) when the provider refused this
    input; (None, False) on any other failure (network, 5xx, 429, auth, bad
    dimensions), which says nothing about the input.
    """
    try:
        resp = await request(inputs, cfg)
    except httpx.HTTPError as exc:
        logger.warning("embeddings request failed: %s", exc)
        return None, False
    if resp.status_code != 200:
        logger.warning("embeddings HTTP %s: %s", resp.status_code, resp.text[:200])
        return None, resp.status_code in REJECTED_STATUSES
    try:
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        vectors = [list(map(float, d["embedding"])) for d in data]
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("embeddings response unreadable: %s", exc)
        return None, False
    if len(vectors) != len(inputs) or any(len(v) != cfg.dim for v in vectors):
        logger.warning("embeddings: expected %d x %d-dim vectors", len(inputs), cfg.dim)
        return None, False
    return vectors, False


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
        vectors, _ = await _embed_batch(texts[start : start + batch], cfg)
        if vectors is None:
            return None
        out.extend(vectors)
    return out


async def embed_query(text: str, cfg: EmbedConfig | None = None) -> list[float] | None:
    """One search query's vector, or None. Uses the short QUERY_TIMEOUT."""
    cfg = replace(cfg or config(), timeout=QUERY_TIMEOUT)
    vectors = await embed_texts([text], cfg)
    return vectors[0] if vectors else None


async def backfill(brain_db, cfg: EmbedConfig | None = None, limit: int = 256) -> dict:
    """Embed chunks that have no vector in the configured model's table.

    Each batch is stored as soon as it succeeds. When the provider refuses a
    batch's input, the batch is bisected down to the chunks it refuses, and
    those are marked so ``missing_embeddings`` stops returning them. Any
    other failure (an outage) ends the run; the rest is retried next time.

    Marks are written only when the same run stored at least one vector: if
    every request is refused, the fault is the request (a wrong EMBED_URL,
    a bad model), not the chunks, and nothing is marked.

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
    stored = 0
    refused: list[int] = []
    out: dict = {"pruned": pruned}
    for start in range(0, len(missing), BATCH):
        s, r, ok = await _store_or_isolate(
            brain_db, missing[start : start + BATCH], cfg
        )
        stored += s
        refused += r
        if not ok:
            out["failed"] = len(missing) - start - s - len(r)
            break
    if refused and stored:
        brain_db.mark_embedding_rejected(refused)
        logger.warning(
            "embeddings: provider refused %d chunk(s) %s; skipping them",
            len(refused),
            refused[:10],
        )
        out["rejected"] = len(refused)
    elif refused:
        logger.error(
            "embeddings: provider refused every input this run (%d chunks); "
            "check EMBED_URL / EMBED_MODEL. Nothing marked.",
            len(refused),
        )
        out["failed"] = out.get("failed", 0) + len(refused)
    out["stored"] = stored
    if stored:
        logger.info("embeddings: stored %d vectors in %s", stored, cfg.table)
    return out


async def _store_or_isolate(
    brain_db, rows: list[dict], cfg: EmbedConfig
) -> tuple[int, list[int], bool]:
    """Embed and store ``rows``; isolate the ones the provider refuses.

    Returns (stored, refused chunk ids, ok); ok is False on a non-input
    failure, which stops the caller rather than bisecting during an outage.
    """
    vectors, refused = await _embed_batch([r["content"] for r in rows], cfg)
    if vectors is not None:
        brain_db.store_embeddings([(r["id"], v) for r, v in zip(rows, vectors)])
        return len(rows), [], True
    if not refused:
        return 0, [], False
    if len(rows) == 1:
        return 0, [rows[0]["id"]], True
    mid = len(rows) // 2
    s1, r1, ok = await _store_or_isolate(brain_db, rows[:mid], cfg)
    if not ok:
        return s1, r1, False
    s2, r2, ok = await _store_or_isolate(brain_db, rows[mid:], cfg)
    return s1 + s2, r1 + r2, ok
