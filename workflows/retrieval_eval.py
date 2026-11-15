"""Offline retrieval evaluation for the brain's hybrid search.

Measures how well search ranks the right page for a set of labelled queries,
and compares search configurations (RRF k and weights, candidate depth,
embedding dimensions, chunk size, contextual headers) on the same queries.

Runs offline: every vector comes from a committed cache keyed by
sha256(model, dim, text), so CI needs no network and results are
deterministic. ``--refresh`` embeds whatever the cache is missing through
``embeddings.embed_texts`` (OpenRouter), the same path production uses.

Every configuration is measured through a real temporary BrainDB and its
search methods, so the eval scores the code that serves queries, not a copy.

    uv run python workflows/retrieval_eval.py            # offline matrix
    uv run python workflows/retrieval_eval.py --refresh  # embed cache misses
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import struct
import sys
import tempfile
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from brain_db import BrainDB

FIXTURES = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "retrieval")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Real-brain mode keeps its golden set and vectors under data/ (gitignored):
# real queries and page contents are operator-private.
LOCAL_DIR = os.path.join(_REPO, "data", "retrieval")
SESSIONS_DIR = os.path.join(_REPO, "data", "openclaw-state", "agents")
METRICS = ("hit@1", "hit@3", "hit@5", "mrr@10", "ndcg@10")
HEADERS = ("none", "title")


# ─── Metrics (binary relevance, page-level) ───────────────────────


def hit_at(ranked: Sequence[str], relevant: set[str] | frozenset[str], k: int) -> float:
    return 1.0 if any(s in relevant for s in ranked[:k]) else 0.0


def reciprocal_rank(
    ranked: Sequence[str], relevant: set[str] | frozenset[str], k: int = 10
) -> float:
    for i, slug in enumerate(ranked[:k]):
        if slug in relevant:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at(
    ranked: Sequence[str], relevant: set[str] | frozenset[str], k: int
) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, s in enumerate(ranked[:k]) if s in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def score_ranking(
    ranked: Sequence[str], relevant: set[str] | frozenset[str]
) -> dict[str, float]:
    return {
        "hit@1": hit_at(ranked, relevant, 1),
        "hit@3": hit_at(ranked, relevant, 3),
        "hit@5": hit_at(ranked, relevant, 5),
        "mrr@10": reciprocal_rank(ranked, relevant, 10),
        "ndcg@10": ndcg_at(ranked, relevant, 10),
    }


@dataclass(frozen=True)
class Delta:
    """Paired difference (candidate - baseline) with a bootstrap 95% CI."""

    mean: float
    lo: float
    hi: float

    @property
    def significant(self) -> bool:
        return self.lo > 0 or self.hi < 0


def paired_bootstrap(
    baseline: Sequence[float],
    candidate: Sequence[float],
    n: int = 2000,
    seed: int = 0,
) -> Delta:
    """Resample queries (not configs) so both runs see the same draw.

    With a few dozen queries the bootstrap is conservative but still flags
    deltas that are noise; only a CI that excludes 0 counts as a change.
    """
    if len(baseline) != len(candidate):
        raise ValueError("runs must score the same queries")
    diffs = [c - b for b, c in zip(baseline, candidate, strict=True)]
    if not diffs:
        return Delta(0.0, 0.0, 0.0)
    rng = random.Random(seed)
    m = len(diffs)
    means = sorted(sum(diffs[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return Delta(
        mean=sum(diffs) / m,
        lo=means[int(0.025 * n)],
        hi=means[min(n - 1, int(0.975 * n))],
    )


# ─── Golden set ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Case:
    query: str
    relevant: frozenset[str]
    tags: tuple[str, ...] = ()


def load_cases(path: str) -> list[Case]:
    with open(path) as f:
        raw = json.load(f)["cases"]
    # Harvested candidates have no labels yet; they are kept in the file
    # for the operator to fill in, but not scored.
    return [
        Case(c["query"], frozenset(c["relevant"]), tuple(c.get("tags", ())))
        for c in raw
        if c.get("relevant")
    ]


def load_pages(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        return json.load(f)["pages"]


def load_brain_pages(path: str) -> list[dict[str, Any]]:
    """Pages from a real brain DB, opened read-only."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT slug, title, page_type, compiled FROM pages ORDER BY slug"
        ).fetchall()
    finally:
        con.close()
    return [
        {"slug": s, "title": t, "page_type": pt, "compiled": c or ""}
        for s, t, pt, c in rows
    ]


_SEARCH_Q = re.compile(r"brain/search\?q=([^\"&\s\\]+)")


def harvest_queries(root: str) -> list[str]:
    """Real /brain/search queries in OpenClaw transcripts, in order.

    Reads ``transcript_events`` from each per-agent ``openclaw-agent.sqlite``
    under ``root``. zstd-compressed events are skipped: the stdlib cannot
    decode them before Python 3.14.
    """
    found: dict[str, None] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".sqlite"):
                continue
            for raw in _SEARCH_Q.findall(_transcript_text(os.path.join(dirpath, name))):
                q = " ".join(urllib.parse.unquote_plus(raw).split())
                if q and q != "...":
                    found.setdefault(q, None)
    return list(found)


def _transcript_text(path: str) -> str:
    """Uncompressed transcript events of an OpenClaw agent DB, or ''."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        rows = db.execute(
            "SELECT event_json FROM transcript_events "
            "WHERE event_json IS NOT NULL ORDER BY session_id, seq"
        ).fetchall()
    except sqlite3.Error:  # not an agent DB (locks, quarantine, ...)
        return ""
    finally:
        db.close()
    return "\n".join(r[0] for r in rows)


def merge_candidates(path: str, queries: Iterable[str]) -> int:
    """Append unseen queries as unlabelled cases; returns how many were added."""
    try:
        with open(path) as f:
            body = json.load(f)
    except FileNotFoundError:
        body = {
            "description": "Local golden set for the real brain. Fill in "
            "'relevant' with page slugs; unlabelled cases are not scored.",
            "cases": [],
        }
    known = {c["query"] for c in body["cases"]}
    added = 0
    for q in queries:
        if q not in known:
            body["cases"].append({"query": q, "relevant": [], "tags": ["harvested"]})
            known.add(q)
            added += 1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(body, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return added


# ─── Vector cache ─────────────────────────────────────────────────


class CacheMiss(KeyError):
    """A text the run needs has no cached vector (run with --refresh)."""


class VectorCache:
    """Vectors keyed by sha256(model, dim, text), stored as float16.

    float16 halves the fixture size; the rounding error (~1e-3) is far
    below the gaps between ranked cosine scores in practice. Values are
    lowercase hex, not base64: base64 text can spell an AWS-style key id
    ("AKIA" + uppercase) and trip CI's secret scan.
    """

    def __init__(self, path: str, model: str, dim: int):
        self.path, self.model, self.dim = path, model, dim
        self._vectors: dict[str, str] = {}

    def key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\n{self.dim}\n{text}".encode()).hexdigest()

    def put(self, text: str, vector: Sequence[float]) -> None:
        if len(vector) != self.dim:
            raise ValueError(f"expected {self.dim} dims, got {len(vector)}")
        packed = struct.pack(f"<{self.dim}e", *vector)
        self._vectors[self.key(text)] = packed.hex()

    def get(self, text: str) -> list[float]:
        try:
            packed = bytes.fromhex(self._vectors[self.key(text)])
        except KeyError:
            raise CacheMiss(text[:80]) from None
        return list(struct.unpack(f"<{self.dim}e", packed))

    def missing(self, texts: Iterable[str]) -> list[str]:
        out: list[str] = []
        for t in dict.fromkeys(texts):
            if self.key(t) not in self._vectors:
                out.append(t)
        return out

    def save(self) -> None:
        body = {"model": self.model, "dim": self.dim, "vectors": self._vectors}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(body, f, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path: str) -> VectorCache:
        with open(path) as f:
            body = json.load(f)
        cache = cls(path, body["model"], body["dim"])
        cache._vectors = body["vectors"]
        return cache


def truncate(vector: Sequence[float], dim: int) -> list[float]:
    """Matryoshka-style truncation: keep the first ``dim`` values, renormalize."""
    head = list(vector[:dim])
    norm = math.sqrt(sum(x * x for x in head)) or 1.0
    return [x / norm for x in head]


def embed_text(chunk: str, title: str, header: str) -> str:
    """The text a chunk is embedded as under a header recipe.

    ``title`` prepends the page title to chunks that lack it. The first
    chunk already starts with it, because pages chunk ``title + body``.
    """
    if header == "none":
        return chunk
    if header == "title":
        return chunk if chunk.startswith(title) else f"{title}\n\n{chunk}"
    raise ValueError(f"unknown header recipe {header!r}")


# ─── Configurations ───────────────────────────────────────────────


@dataclass(frozen=True)
class SearchConfig:
    name: str
    mode: str = "hybrid"  # keyword | vector | hybrid
    rrf_k: int = 60
    kw_weight: float = 1.0
    vec_weight: float = 1.0
    depth: int | None = None  # pages per retriever before fusion
    kw_match: str | None = None  # all = FTS5 AND, any = OR, None = hybrid default
    dim: int | None = None  # None = the cache's full dimension
    header: str = "none"
    chunk_words: int = 300
    chunk_overlap: int = 50
    limit: int = 10


def production_config() -> SearchConfig:
    """What /brain/search?mode=hybrid does today, using BrainDB's defaults."""
    return SearchConfig("production")


DEFAULT_MATRIX: list[SearchConfig] = [
    production_config(),
    SearchConfig("keyword-only", mode="keyword"),
    SearchConfig("vector-only", mode="vector"),
    # What hybrid returns with no query vector (embedding outage, no key).
    SearchConfig("no-vectors", mode="hybrid-novec"),
    SearchConfig("rrf-k10", rrf_k=10),
    SearchConfig("rrf-k30", rrf_k=30),
    SearchConfig("rrf-k100", rrf_k=100),
    SearchConfig("kw-weight-0.5", kw_weight=0.5),
    SearchConfig("kw-weight-2", kw_weight=2.0),
    SearchConfig("depth-40", depth=40),
    SearchConfig("dim-512", dim=512),
    SearchConfig("dim-1024", dim=1024),
    SearchConfig("title-header", header="title"),
    SearchConfig("chunk-150", chunk_words=150, chunk_overlap=30),
    SearchConfig("chunk-150-title", chunk_words=150, chunk_overlap=30, header="title"),
    SearchConfig("no-overlap", chunk_overlap=0),
    # Any-term keyword matching, alone and fused; the fusion knobs only
    # matter once the keyword side returns pages.
    SearchConfig("kw-any-only", mode="keyword", kw_match="any"),
    SearchConfig("kw-any", kw_match="any"),
    SearchConfig("kw-any-k10", kw_match="any", rrf_k=10),
    SearchConfig("kw-any-k100", kw_match="any", rrf_k=100),
    SearchConfig("kw-any-w0.5", kw_match="any", kw_weight=0.5),
    SearchConfig("kw-any-w2", kw_match="any", kw_weight=2.0),
    SearchConfig("kw-any-chunk150", kw_match="any", chunk_words=150, chunk_overlap=30),
]


# ─── Running ──────────────────────────────────────────────────────


@dataclass
class Result:
    config: SearchConfig
    per_query: dict[str, dict[str, float]]
    mean: dict[str, float] = field(default_factory=dict)
    by_tag: dict[str, dict[str, float]] = field(default_factory=dict)


def _mean(rows: Iterable[dict[str, float]]) -> dict[str, float]:
    rows = list(rows)
    return {m: sum(r[m] for r in rows) / len(rows) for m in METRICS} if rows else {}


class _CorpusDB:
    """A throwaway BrainDB loaded with the corpus under one chunking recipe."""

    def __init__(self, pages: list[dict[str, Any]], words: int, overlap: int):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = BrainDB(os.path.join(self._tmp.name, "eval.db"))
        self.db.chunk_max_words, self.db.chunk_overlap_words = words, overlap
        self.db.init_db()
        self.titles = {p["slug"]: p["title"] for p in pages}
        for p in pages:
            self.db.upsert_page(p["slug"], p["title"], p["page_type"], p["compiled"])

    def chunks(self) -> list[dict[str, Any]]:
        return [c for slug in self.titles for c in self.db.get_chunks(slug)]

    def close(self) -> None:
        self.db._conn.close()
        self._tmp.cleanup()


def texts_needed(
    pages: list[dict[str, Any]], cases: list[Case], configs: Iterable[SearchConfig]
) -> list[str]:
    """Every text the configs embed: chunks per (chunking, header) + queries."""
    texts: dict[str, None] = dict.fromkeys(c.query for c in cases)
    recipes = {(c.chunk_words, c.chunk_overlap, c.header) for c in configs}
    for words, overlap, header in sorted(recipes):
        corpus = _CorpusDB(pages, words, overlap)
        try:
            for ch in corpus.chunks():
                title = corpus.titles[ch["page_slug"]]
                texts[embed_text(ch["content"], title, header)] = None
        finally:
            corpus.close()
    return list(texts)


def _rank(corpus: _CorpusDB, cfg: SearchConfig, case: Case, qvec) -> list[str]:
    db = corpus.db
    if cfg.mode == "keyword":
        hits = db.search_keyword(
            case.query, limit=cfg.limit, match=cfg.kw_match or "all"
        )
    elif cfg.mode == "vector":
        hits = db.search_vector(qvec, limit=cfg.limit)
    elif cfg.mode == "hybrid-novec":
        hits = db.search_hybrid(case.query, None, limit=cfg.limit, k=cfg.rrf_k)
    elif cfg.mode == "hybrid":
        hits = db.search_hybrid(
            case.query,
            qvec,
            limit=cfg.limit,
            k=cfg.rrf_k,
            kw_weight=cfg.kw_weight,
            vec_weight=cfg.vec_weight,
            depth=cfg.depth,
            kw_match=cfg.kw_match,
        )
    else:
        raise ValueError(f"unknown mode {cfg.mode!r}")
    return [h["slug"] for h in hits]


def run_config(
    pages: list[dict[str, Any]],
    cases: list[Case],
    cfg: SearchConfig,
    cache: VectorCache,
) -> Result:
    dim = cfg.dim or cache.dim
    corpus = _CorpusDB(pages, cfg.chunk_words, cfg.chunk_overlap)
    try:
        if cfg.mode != "keyword":
            corpus.db.use_vector_table(f"vec_eval_{dim}", dim)
            rows = []
            for ch in corpus.chunks():
                title = corpus.titles[ch["page_slug"]]
                vec = cache.get(embed_text(ch["content"], title, cfg.header))
                rows.append((ch["id"], truncate(vec, dim)))
            corpus.db.store_embeddings(rows)
        per_query: dict[str, dict[str, float]] = {}
        for case in cases:
            qvec = (
                truncate(cache.get(case.query), dim) if cfg.mode != "keyword" else None
            )
            per_query[case.query] = score_ranking(
                _rank(corpus, cfg, case, qvec), case.relevant
            )
    finally:
        corpus.close()
    result = Result(cfg, per_query, mean=_mean(per_query.values()))
    tags = sorted({t for c in cases for t in c.tags})
    result.by_tag = {
        t: _mean(per_query[c.query] for c in cases if t in c.tags) for t in tags
    }
    return result


def evaluate(
    pages: list[dict[str, Any]],
    cases: list[Case],
    configs: Iterable[SearchConfig],
    cache: VectorCache,
) -> dict[str, Result]:
    return {cfg.name: run_config(pages, cases, cfg, cache) for cfg in configs}


# ─── Regression gate ──────────────────────────────────────────────


GATED_CONFIGS = ("production", "no-vectors")
GATED_METRICS = ("hit@1", "hit@3", "mrr@10")
# Fixture runs are deterministic (cached vectors, fixed corpus), so any drop
# is a code change. One query is 1/72 of a metric; the epsilon only absorbs
# float summation order.
GATE_EPSILON = 1e-9


def gate(
    baseline: dict[str, float],
    current: dict[str, float],
    baseline_tags: dict[str, dict[str, float]] | None = None,
    current_tags: dict[str, dict[str, float]] | None = None,
) -> list[str]:
    """Return one message per gated metric that fell below the baseline.

    Overall metrics are checked first, then hit@1 per query tag, so a gain
    on semantic questions cannot hide a loss on lexical ones. A deliberate
    trade is made by rewriting baseline.json with --write-baseline in the
    same change.
    """
    failures = []
    for m in GATED_METRICS:
        if current[m] < baseline[m] - GATE_EPSILON:
            failures.append(f"{m} fell from {baseline[m]:.3f} to {current[m]:.3f}")
    for tag, base in sorted((baseline_tags or {}).items()):
        now = (current_tags or {}).get(tag, {}).get("hit@1", 0.0)
        if now < base["hit@1"] - GATE_EPSILON:
            failures.append(f"{tag} hit@1 fell from {base['hit@1']:.3f} to {now:.3f}")
    return failures


# ─── CLI ──────────────────────────────────────────────────────────


def _refresh(cache: VectorCache, texts: list[str]) -> int:
    import asyncio

    import embeddings

    cfg = dataclasses.replace(embeddings.config(), model=cache.model, dim=cache.dim)
    if not cfg.api_key:
        sys.exit("OPENROUTER_API_KEY is not set; cannot refresh the cache")
    missing = cache.missing(texts)
    if not missing:
        return 0
    vectors = asyncio.run(embeddings.embed_texts(missing, cfg))
    if vectors is None:
        sys.exit("embedding request failed; cache unchanged")
    for text, vec in zip(missing, vectors, strict=True):
        cache.put(text, vec)
    cache.save()
    return len(missing)


def check_truncation(cache: VectorCache, text: str, dim: int = 512) -> float:
    """Cosine between a truncated cached vector and a native ``dim`` vector.

    Confirms that offline truncation stands in for asking the API for
    fewer dimensions (expected ~1.0 for text-embedding-3).
    """
    import asyncio

    import embeddings

    cfg = dataclasses.replace(embeddings.config(), model=cache.model, dim=dim)
    native = asyncio.run(embeddings.embed_texts([text], cfg))
    if not native:
        sys.exit("embedding request failed")
    a, b = truncate(cache.get(text), dim), truncate(native[0], dim)
    return sum(x * y for x, y in zip(a, b, strict=True))


def format_report(report: dict[str, Result], baseline: str = "production") -> str:
    base = report[baseline]
    tags = sorted(base.by_tag)
    head = f"{'config':<18}" + "".join(f"{m:>9}" for m in METRICS)
    head += "".join(f"{t + ' h@1':>14}" for t in tags) + "   Δmrr@10 [95% CI]"
    lines = [head, "-" * len(head)]
    queries = list(base.per_query)
    for name, res in report.items():
        row = f"{name:<18}" + "".join(f"{res.mean[m]:>9.3f}" for m in METRICS)
        row += "".join(f"{res.by_tag[t]['hit@1']:>14.3f}" for t in tags)
        if name != baseline:
            d = paired_bootstrap(
                [base.per_query[q]["mrr@10"] for q in queries],
                [res.per_query[q]["mrr@10"] for q in queries],
            )
            mark = " *" if d.significant else ""
            row += f"   {d.mean:+.3f} [{d.lo:+.3f}, {d.hi:+.3f}]{mark}"
        lines.append(row)
    lines.append("* = 95% CI excludes zero (paired bootstrap over queries)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline retrieval evaluation.")
    ap.add_argument("--fixtures", default=FIXTURES)
    ap.add_argument("--brain", help="evaluate a real brain DB (read-only) instead")
    ap.add_argument("--golden", help="golden set path (default per mode)")
    ap.add_argument("--vectors", help="vector cache path (default per mode)")
    ap.add_argument(
        "--harvest",
        nargs="?",
        const=SESSIONS_DIR,
        metavar="SESSIONS_DIR",
        help="add real /brain/search queries from OpenClaw sessions to the "
        "golden set as unlabelled cases, then exit",
    )
    ap.add_argument("--refresh", action="store_true", help="embed cache misses live")
    ap.add_argument("--check-truncation", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    args = ap.parse_args(argv)

    base_dir = LOCAL_DIR if args.brain else args.fixtures
    golden = args.golden or os.path.join(base_dir, "golden.json")
    vec_path = args.vectors or os.path.join(base_dir, "vectors.json")

    if args.harvest:
        n = merge_candidates(golden, harvest_queries(args.harvest))
        print(f"added {n} unlabelled queries to {golden}; label them, then rerun")
        return 0
    if args.brain and args.write_baseline:
        sys.exit("--write-baseline only applies to the committed fixtures")

    if args.brain:
        pages = load_brain_pages(args.brain)
    else:
        pages = load_pages(os.path.join(args.fixtures, "corpus.json"))
    if not os.path.exists(golden):
        sys.exit(f"no golden set at {golden} (try --harvest)")
    cases = load_cases(golden)
    if not cases:
        sys.exit(f"{golden} has no labelled cases yet")
    if os.path.exists(vec_path):
        cache = VectorCache.load(vec_path)
    else:
        import embeddings

        cfg = embeddings.config()
        os.makedirs(os.path.dirname(vec_path) or ".", exist_ok=True)
        cache = VectorCache(vec_path, cfg.model, cfg.dim)

    needed = texts_needed(pages, cases, DEFAULT_MATRIX)
    if args.refresh:
        print(f"embedded {_refresh(cache, needed)} new texts")
    if args.check_truncation:
        for dim in (512, 1024):
            cos = check_truncation(cache, cases[0].query, dim)
            print(f"truncated-vs-native cosine at {dim}: {cos:.4f}")
    missing = cache.missing(needed)
    if missing:
        print(f"{len(missing)} texts have no cached vector; run with --refresh")
        return 1

    report = evaluate(pages, cases, DEFAULT_MATRIX, cache)
    print(f"{len(pages)} pages, {len(cases)} queries, model {cache.model}\n")
    print(format_report(report))
    if args.write_baseline:
        body = {
            "configs": {
                name: {"mean": report[name].mean, "by_tag": report[name].by_tag}
                for name in GATED_CONFIGS
            }
        }
        with open(os.path.join(args.fixtures, "baseline.json"), "w") as f:
            json.dump(body, f, indent=2)
            f.write("\n")
        print("\nwrote baseline.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
