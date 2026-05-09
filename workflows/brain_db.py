"""Brain database layer — SQLite + FTS5 + content hashing.

Contract-first: all brain operations defined here.
The FastAPI router (brain.py) is a thin wrapper over this module.
"""

import hashlib
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class PageType(str, Enum):
    CLIENT = "client"
    SYSTEM = "system"
    INCIDENT = "incident"
    DECISION = "decision"
    NOTE = "note"
    PERSON = "person"
    COMPANY = "company"
    PROJECT = "project"


class LinkType(str, Enum):
    REFERENCES = "references"
    PARENT_OF = "parent_of"
    INCIDENT_OF = "incident_of"
    RELATED_TO = "related_to"
    WORKS_AT = "works_at"
    KNOWS = "knows"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(title: str, compiled: str) -> str:
    return hashlib.sha256(f"{title}\x00{compiled}".encode()).hexdigest()


def _chunk_text(text: str, max_words: int = 300, overlap_words: int = 50) -> list[str]:
    """Split text into chunks using paragraph boundaries with word-count limits.

    Strategy (gbrain-inspired, simplified for SQLite testbed):
    1. Split on paragraph breaks (double newline)
    2. Merge small paragraphs into chunks up to max_words
    3. Split oversized paragraphs at sentence boundaries
    4. Add overlap_words from previous chunk for context continuity
    """
    if not text or not text.strip():
        return []

    # Split into paragraphs
    paragraphs = re.split(r"\n\s*\n", text.strip())
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    if not paragraphs:
        return []

    # Merge small paragraphs, split large ones
    raw_chunks: list[str] = []
    current: list[str] = []
    current_words = 0

    for para in paragraphs:
        words = para.split()
        if current_words + len(words) <= max_words:
            current.append(para)
            current_words += len(words)
        else:
            if current:
                raw_chunks.append("\n\n".join(current))
            # If single paragraph exceeds max_words, split at sentence boundaries
            if len(words) > max_words:
                sentences = re.split(r"(?<=[.!?])\s+", para)
                sent_buf: list[str] = []
                sent_words = 0
                for sent in sentences:
                    sw = len(sent.split())
                    if sent_words + sw <= max_words:
                        sent_buf.append(sent)
                        sent_words += sw
                    else:
                        if sent_buf:
                            raw_chunks.append(" ".join(sent_buf))
                        sent_buf = [sent]
                        sent_words = sw
                if sent_buf:
                    current = [" ".join(sent_buf)]
                    current_words = sent_words
                else:
                    current = []
                    current_words = 0
            else:
                current = [para]
                current_words = len(words)

    if current:
        raw_chunks.append("\n\n".join(current))

    if not raw_chunks:
        return []

    # Add overlap from previous chunk
    if overlap_words <= 0 or len(raw_chunks) <= 1:
        return raw_chunks

    result = [raw_chunks[0]]
    for i in range(1, len(raw_chunks)):
        prev_words = raw_chunks[i - 1].split()
        overlap = (
            " ".join(prev_words[-overlap_words:])
            if len(prev_words) > overlap_words
            else raw_chunks[i - 1]
        )
        result.append(f"{overlap}\n\n{raw_chunks[i]}")

    return result


EMBEDDING_DIM = 1536  # text-embedding-3-small


def _connect(db_path: str) -> tuple[sqlite3.Connection, bool]:
    """Connect to SQLite and attempt to load sqlite-vec extension.

    Returns (connection, has_vec) — has_vec is True if vector search is available.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    has_vec = False
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        has_vec = True
    except (ImportError, Exception):
        pass  # Graceful degradation: keyword-only search

    return conn, has_vec


class BrainDB:
    def __init__(self, db_path: str = "/data/brain.db"):
        self._db_path = db_path
        # Ensure parent directory exists
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn, self._has_vec = _connect(db_path)

    def init_db(self) -> None:
        conn = self._conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pages (
                slug          TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                page_type     TEXT NOT NULL,
                compiled      TEXT DEFAULT '',
                content_hash  TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS timeline (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                page_slug   TEXT NOT NULL REFERENCES pages(slug) ON DELETE CASCADE,
                content     TEXT NOT NULL,
                source      TEXT DEFAULT '',
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id           TEXT PRIMARY KEY,
                description  TEXT NOT NULL,
                priority     INTEGER DEFAULT 3,
                status       TEXT DEFAULT 'pending',
                source       TEXT DEFAULT 'system',
                result       TEXT,
                created_at   TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                from_slug   TEXT NOT NULL REFERENCES pages(slug) ON DELETE CASCADE,
                to_slug     TEXT NOT NULL REFERENCES pages(slug) ON DELETE CASCADE,
                link_type   TEXT NOT NULL DEFAULT 'related_to',
                created_at  TEXT NOT NULL,
                UNIQUE(from_slug, to_slug, link_type)
            );

            CREATE TABLE IF NOT EXISTS tags (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS page_tags (
                page_slug TEXT NOT NULL REFERENCES pages(slug) ON DELETE CASCADE,
                tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                UNIQUE(page_slug, tag_id)
            );

            CREATE TABLE IF NOT EXISTS page_versions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                page_slug     TEXT NOT NULL REFERENCES pages(slug) ON DELETE CASCADE,
                compiled      TEXT NOT NULL,
                content_hash  TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS content_chunks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                page_slug     TEXT NOT NULL REFERENCES pages(slug) ON DELETE CASCADE,
                chunk_index   INTEGER NOT NULL,
                content       TEXT NOT NULL,
                content_hash  TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                UNIQUE(page_slug, chunk_index)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts
                USING fts5(slug, title, compiled);
        """)
        # FTS5 sync triggers — standalone FTS, managed via triggers
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
                INSERT INTO pages_fts(slug, title, compiled)
                    VALUES (new.slug, new.title, new.compiled);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
                DELETE FROM pages_fts WHERE slug = old.slug;
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
                DELETE FROM pages_fts WHERE slug = old.slug;
                INSERT INTO pages_fts(slug, title, compiled)
                    VALUES (new.slug, new.title, new.compiled);
            END;
        """)

        # Marketing orchestrator tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                slug         TEXT PRIMARY KEY,
                owner        TEXT NOT NULL,
                repo         TEXT NOT NULL,
                topics       TEXT NOT NULL DEFAULT '[]',
                subreddits   TEXT NOT NULL DEFAULT '[]',
                search_terms TEXT NOT NULL DEFAULT '[]',
                posture      TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                UNIQUE(owner, repo)
            );

            CREATE TABLE IF NOT EXISTS schedules (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL UNIQUE,
                kind         TEXT NOT NULL,
                cron         TEXT NOT NULL,
                kwargs       TEXT NOT NULL DEFAULT '{}',
                paused       INTEGER NOT NULL DEFAULT 0,
                last_run     TEXT,
                last_status  TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_cache (
                kind         TEXT NOT NULL,
                external_id  TEXT NOT NULL,
                project_slug TEXT,
                seen_at      TEXT NOT NULL,
                PRIMARY KEY(kind, external_id, project_slug)
            );

            CREATE TABLE IF NOT EXISTS subreddit_stats (
                subreddit       TEXT NOT NULL,
                project_slug    TEXT NOT NULL,
                hits            INTEGER NOT NULL DEFAULT 0,
                impressions     INTEGER NOT NULL DEFAULT 0,
                first_hit_at    TEXT,
                last_hit_at     TEXT,
                last_searched_at TEXT,
                is_curated      INTEGER NOT NULL DEFAULT 0,
                promoted_at     TEXT,
                PRIMARY KEY (subreddit, project_slug)
            );

            CREATE TABLE IF NOT EXISTS research_sessions (
                id           TEXT PRIMARY KEY,
                topic        TEXT NOT NULL,
                channels     TEXT NOT NULL DEFAULT '[]',
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS research_findings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
                source       TEXT NOT NULL,
                channel      TEXT NOT NULL,
                title        TEXT NOT NULL DEFAULT '',
                url          TEXT NOT NULL DEFAULT '',
                relevance    REAL NOT NULL DEFAULT 0,
                summary      TEXT NOT NULL DEFAULT '',
                metadata     TEXT NOT NULL DEFAULT '{}',
                created_at   TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_research_findings_session
                ON research_findings(session_id);
        """)

        # Migrate: add source column to tasks if missing (existing DBs)
        try:
            conn.execute("SELECT source FROM tasks LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE tasks ADD COLUMN source TEXT DEFAULT 'system'")

        # sqlite-vec virtual table for embeddings (optional)
        if self._has_vec:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings
                    USING vec0(chunk_id INTEGER PRIMARY KEY, vector float[{EMBEDDING_DIM}])
            """)

        conn.commit()

    # ─── Internal helpers ────────────────────────────────────────

    def _fetch_tables(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        fts = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' OR (type='table' AND name LIKE '%fts%')"
        ).fetchall()
        return {r["name"] for r in rows} | {r["name"] for r in fts}

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    # ─── Page CRUD ───────────────────────────────────────────────

    def upsert_page(
        self,
        slug: str,
        title: str,
        page_type: PageType | str,
        compiled: str = "",
    ) -> dict[str, Any]:
        now = _now()
        chash = _content_hash(title, compiled)
        pt = page_type.value if isinstance(page_type, PageType) else page_type

        existing = self._conn.execute(
            "SELECT content_hash FROM pages WHERE slug = ?", (slug,)
        ).fetchone()

        if existing and existing["content_hash"] == chash:
            # Idempotent: same content, no update needed
            page = self._conn.execute(
                "SELECT * FROM pages WHERE slug = ?", (slug,)
            ).fetchone()
            return self._row_to_dict(page)

        if existing:
            self._conn.execute(
                "UPDATE pages SET title=?, page_type=?, compiled=?, content_hash=?, updated_at=? WHERE slug=?",
                (title, pt, compiled, chash, now, slug),
            )
        else:
            self._conn.execute(
                "INSERT INTO pages (slug, title, page_type, compiled, content_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (slug, title, pt, compiled, chash, now, now),
            )

        # Always record a version for the new content
        self._conn.execute(
            "INSERT INTO page_versions (page_slug, compiled, content_hash, created_at) VALUES (?, ?, ?, ?)",
            (slug, compiled, chash, now),
        )

        self._conn.commit()

        # Auto-chunk page content for embedding pipeline
        self._rechunk_page(slug)

        page = self._conn.execute(
            "SELECT * FROM pages WHERE slug = ?", (slug,)
        ).fetchone()
        return self._row_to_dict(page)

    def get_page(self, slug: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM pages WHERE slug = ?", (slug,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def delete_page(self, slug: str) -> bool:
        cursor = self._conn.execute("DELETE FROM pages WHERE slug = ?", (slug,))
        self._conn.commit()
        return cursor.rowcount > 0

    def list_pages(
        self,
        page_type: PageType | str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if page_type:
            pt = page_type.value if isinstance(page_type, PageType) else page_type
            rows = self._conn.execute(
                "SELECT * FROM pages WHERE page_type = ? ORDER BY updated_at DESC LIMIT ?",
                (pt, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM pages ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ─── Page Versions ───────────────────────────────────────────

    def get_page_versions(self, slug: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM page_versions WHERE page_slug = ? ORDER BY created_at DESC",
            (slug,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ─── Content Chunks ─────────────────────────────────────────

    def _rechunk_page(self, slug: str) -> list[dict[str, Any]]:
        """Re-chunk a page's content. Called automatically on upsert.

        Idempotent: uses content_hash to skip unchanged chunks.
        """
        page = self.get_page(slug)
        if not page:
            return []

        text = (
            f"{page['title']}\n\n{page['compiled']}"
            if page["compiled"]
            else page["title"]
        )
        chunks = _chunk_text(text)

        if not chunks:
            self._conn.execute(
                "DELETE FROM content_chunks WHERE page_slug = ?", (slug,)
            )
            self._conn.commit()
            return []

        now = _now()
        result = []
        for idx, content in enumerate(chunks):
            chash = hashlib.sha256(content.encode()).hexdigest()
            existing = self._conn.execute(
                "SELECT content_hash FROM content_chunks WHERE page_slug = ? AND chunk_index = ?",
                (slug, idx),
            ).fetchone()

            if existing and existing["content_hash"] == chash:
                row = self._conn.execute(
                    "SELECT * FROM content_chunks WHERE page_slug = ? AND chunk_index = ?",
                    (slug, idx),
                ).fetchone()
                result.append(self._row_to_dict(row))
                continue

            self._conn.execute(
                """INSERT OR REPLACE INTO content_chunks
                   (page_slug, chunk_index, content, content_hash, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (slug, idx, content, chash, now),
            )
            row = self._conn.execute(
                "SELECT * FROM content_chunks WHERE page_slug = ? AND chunk_index = ?",
                (slug, idx),
            ).fetchone()
            result.append(self._row_to_dict(row))

        # Remove stale chunks beyond the new chunk count
        self._conn.execute(
            "DELETE FROM content_chunks WHERE page_slug = ? AND chunk_index >= ?",
            (slug, len(chunks)),
        )
        self._conn.commit()
        return result

    def get_chunks(self, slug: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM content_chunks WHERE page_slug = ? ORDER BY chunk_index",
            (slug,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ─── Embeddings (sqlite-vec) ─────────────────────────────────

    def store_embedding(self, chunk_id: int, vector: list[float]) -> bool:
        """Store a vector embedding for a content chunk. Returns False if vec unavailable."""
        if not self._has_vec:
            return False
        import struct

        blob = struct.pack(f"{len(vector)}f", *vector)
        self._conn.execute(
            "INSERT OR REPLACE INTO chunk_embeddings (chunk_id, vector) VALUES (?, ?)",
            (chunk_id, blob),
        )
        self._conn.commit()
        return True

    def search_vector(
        self, query_vector: list[float], limit: int = 10
    ) -> list[dict[str, Any]]:
        """Find chunks nearest to query_vector using cosine distance.

        Returns page-level results with the best matching chunk snippet.
        """
        if not self._has_vec:
            return []
        import struct

        blob = struct.pack(f"{len(query_vector)}f", *query_vector)
        rows = self._conn.execute(
            """
            SELECT ce.chunk_id, ce.distance,
                   cc.page_slug, cc.content, cc.chunk_index,
                   p.title, p.page_type
            FROM chunk_embeddings ce
            JOIN content_chunks cc ON cc.id = ce.chunk_id
            JOIN pages p ON p.slug = cc.page_slug
            WHERE ce.vector MATCH ?
            AND k = ?
            ORDER BY ce.distance
            """,
            (blob, limit * 2),  # Fetch more for dedup
        ).fetchall()

        # Deduplicate: best chunk per page
        seen_slugs: set[str] = set()
        results: list[dict[str, Any]] = []
        for row in rows:
            slug = row["page_slug"]
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            results.append(
                {
                    "slug": slug,
                    "title": row["title"],
                    "page_type": row["page_type"],
                    "snippet": row["content"][:200],
                    "score": 1.0 - row["distance"],  # Convert distance to similarity
                    "match_type": "vector",
                }
            )
            if len(results) >= limit:
                break

        return results

    def has_embeddings(self) -> bool:
        """Check if vector search is available."""
        return self._has_vec

    # ─── Timeline ────────────────────────────────────────────────

    def append_timeline(
        self, page_slug: str, content: str, source: str = ""
    ) -> dict[str, Any]:
        now = _now()
        cursor = self._conn.execute(
            "INSERT INTO timeline (page_slug, content, source, created_at) VALUES (?, ?, ?, ?)",
            (page_slug, content, source, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM timeline WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._row_to_dict(row)

    def get_timeline(self, page_slug: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM timeline WHERE page_slug = ? ORDER BY created_at DESC LIMIT ?",
            (page_slug, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ─── FTS5 Keyword Search ─────────────────────────────────────

    def search_keyword(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        rows = self._conn.execute(
            """
            SELECT p.slug, p.title, p.page_type, p.compiled,
                   snippet(pages_fts, 2, '...', '...', '...', 32) as snippet,
                   pages_fts.rank as score
            FROM pages_fts
            JOIN pages p ON pages_fts.slug = p.slug
            WHERE pages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search_hybrid(
        self,
        query: str,
        query_vector: list[float] | None = None,
        limit: int = 10,
        k: int = 60,
    ) -> list[dict[str, Any]]:
        """Hybrid search combining keyword (FTS5) + vector (sqlite-vec) with RRF.

        Reciprocal Rank Fusion: score = Σ 1/(k + rank_i)
        Falls back to keyword-only if vector is unavailable or query_vector is None.
        """
        # Keyword results
        kw_results = self.search_keyword(query, limit=limit * 2)

        # Vector results (if available)
        vec_results = []
        if query_vector and self._has_vec:
            vec_results = self.search_vector(query_vector, limit=limit * 2)

        # If only one source, compute RRF scores from single ranking
        if not vec_results:
            results = []
            for rank, r in enumerate(kw_results[:limit]):
                r["match_type"] = "keyword"
                r["score"] = round(1.0 / (k + rank), 6)
                results.append(r)
            return results
        if not kw_results:
            for rank, r in enumerate(vec_results[:limit]):
                r["score"] = round(1.0 / (k + rank), 6)
            return vec_results[:limit]

        # RRF fusion
        rrf_scores: dict[str, float] = {}
        slug_data: dict[str, dict[str, Any]] = {}

        for rank, r in enumerate(kw_results):
            slug = r["slug"]
            rrf_scores[slug] = rrf_scores.get(slug, 0) + 1.0 / (k + rank)
            if slug not in slug_data:
                slug_data[slug] = {
                    "slug": slug,
                    "title": r["title"],
                    "page_type": r["page_type"],
                    "snippet": r.get("snippet", r.get("compiled", "")[:200]),
                }

        for rank, r in enumerate(vec_results):
            slug = r["slug"]
            rrf_scores[slug] = rrf_scores.get(slug, 0) + 1.0 / (k + rank)
            if slug not in slug_data:
                slug_data[slug] = {
                    "slug": slug,
                    "title": r["title"],
                    "page_type": r["page_type"],
                    "snippet": r.get("snippet", ""),
                }

        # Sort by fused score
        sorted_slugs = sorted(rrf_scores, key=lambda s: rrf_scores[s], reverse=True)
        results = []
        for slug in sorted_slugs[:limit]:
            entry = slug_data[slug]
            entry["score"] = round(rrf_scores[slug], 6)
            entry["match_type"] = "hybrid"
            results.append(entry)

        return results

    # ─── Links (Knowledge Graph) ─────────────────────────────────

    def add_link(
        self,
        from_slug: str,
        to_slug: str,
        link_type: LinkType | str = LinkType.RELATED_TO,
    ) -> dict[str, Any]:
        now = _now()
        lt = link_type.value if isinstance(link_type, LinkType) else link_type
        try:
            cursor = self._conn.execute(
                "INSERT INTO links (from_slug, to_slug, link_type, created_at) VALUES (?, ?, ?, ?)",
                (from_slug, to_slug, lt, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM links WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_dict(row)
        except sqlite3.IntegrityError:
            # Duplicate link — return existing
            row = self._conn.execute(
                "SELECT * FROM links WHERE from_slug=? AND to_slug=? AND link_type=?",
                (from_slug, to_slug, lt),
            ).fetchone()
            return self._row_to_dict(row)

    def get_links(self, slug: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM links WHERE from_slug = ? OR to_slug = ? ORDER BY created_at DESC",
            (slug, slug),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete_link(self, link_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def traverse_graph(
        self,
        start_slug: str,
        depth: int = 2,
        link_type: LinkType | str | None = None,
    ) -> list[dict[str, Any]]:
        """Traverse knowledge graph from a starting page using recursive CTE.

        Returns nodes reachable within `depth` hops, with path information.
        """
        depth = max(1, min(depth, 5))  # Clamp to [1, 5]
        lt = link_type.value if isinstance(link_type, LinkType) else link_type

        if lt:
            rows = self._conn.execute(
                """
                WITH RECURSIVE reachable(slug, depth, path) AS (
                    SELECT to_slug, 1, from_slug || ' -> ' || to_slug
                    FROM links WHERE from_slug = ? AND link_type = ?
                    UNION
                    SELECT from_slug, 1, to_slug || ' -> ' || from_slug
                    FROM links WHERE to_slug = ? AND link_type = ?
                    UNION
                    SELECT l.to_slug, r.depth + 1, r.path || ' -> ' || l.to_slug
                    FROM reachable r
                    JOIN links l ON l.from_slug = r.slug AND l.link_type = ?
                    WHERE r.depth < ?
                    UNION
                    SELECT l.from_slug, r.depth + 1, r.path || ' -> ' || l.from_slug
                    FROM reachable r
                    JOIN links l ON l.to_slug = r.slug AND l.link_type = ?
                    WHERE r.depth < ?
                )
                SELECT DISTINCT p.slug, p.title, p.page_type, r.depth, r.path
                FROM reachable r
                JOIN pages p ON p.slug = r.slug
                WHERE r.slug != ?
                ORDER BY r.depth, p.slug
                """,
                (start_slug, lt, start_slug, lt, lt, depth, lt, depth, start_slug),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                WITH RECURSIVE reachable(slug, depth, path) AS (
                    SELECT to_slug, 1, from_slug || ' -> ' || to_slug
                    FROM links WHERE from_slug = ?
                    UNION
                    SELECT from_slug, 1, to_slug || ' -> ' || from_slug
                    FROM links WHERE to_slug = ?
                    UNION
                    SELECT l.to_slug, r.depth + 1, r.path || ' -> ' || l.to_slug
                    FROM reachable r
                    JOIN links l ON l.from_slug = r.slug
                    WHERE r.depth < ?
                    UNION
                    SELECT l.from_slug, r.depth + 1, r.path || ' -> ' || l.from_slug
                    FROM reachable r
                    JOIN links l ON l.to_slug = r.slug
                    WHERE r.depth < ?
                )
                SELECT DISTINCT p.slug, p.title, p.page_type, r.depth, r.path
                FROM reachable r
                JOIN pages p ON p.slug = r.slug
                WHERE r.slug != ?
                ORDER BY r.depth, p.slug
                """,
                (start_slug, start_slug, depth, depth, start_slug),
            ).fetchall()

        return [self._row_to_dict(r) for r in rows]

    # ─── Tags ────────────────────────────────────────────────────

    def _ensure_tag(self, name: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM tags WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cursor = self._conn.execute("INSERT INTO tags (name) VALUES (?)", (name,))
        self._conn.commit()
        return cursor.lastrowid

    def set_tags(self, page_slug: str, tag_names: list[str]) -> list[str]:
        # Remove existing tags
        self._conn.execute("DELETE FROM page_tags WHERE page_slug = ?", (page_slug,))
        # Add new tags
        for name in tag_names:
            tag_id = self._ensure_tag(name)
            self._conn.execute(
                "INSERT OR IGNORE INTO page_tags (page_slug, tag_id) VALUES (?, ?)",
                (page_slug, tag_id),
            )
        self._conn.commit()
        return self.get_tags(page_slug)

    def get_tags(self, page_slug: str) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT t.name FROM tags t
            JOIN page_tags pt ON t.id = pt.tag_id
            WHERE pt.page_slug = ?
            ORDER BY t.name
            """,
            (page_slug,),
        ).fetchall()
        return [r["name"] for r in rows]

    def remove_tag(self, page_slug: str, tag_name: str) -> bool:
        cursor = self._conn.execute(
            """
            DELETE FROM page_tags
            WHERE page_slug = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)
            """,
            (page_slug, tag_name),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_all_tags(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT t.name, COUNT(pt.page_slug) as count
            FROM tags t
            LEFT JOIN page_tags pt ON t.id = pt.tag_id
            GROUP BY t.id
            ORDER BY t.name
            """
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ─── Task Queue (Persistent) ─────────────────────────────────

    def create_task(
        self, description: str, priority: int = 3, source: str = "system"
    ) -> dict[str, Any]:
        now = _now()
        task_id = str(uuid.uuid4())[:8]
        priority = max(1, min(5, priority))
        self._conn.execute(
            "INSERT INTO tasks (id, description, priority, status, source, created_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (task_id, description, priority, source, now),
        )
        self._conn.commit()
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY priority ASC, created_at ASC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, priority ASC, created_at ASC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def claim_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError("Task not found")
        if task["status"] != "pending":
            raise ValueError(f"Task is {task['status']}, not pending")
        self._conn.execute(
            "UPDATE tasks SET status = 'active' WHERE id = ?", (task_id,)
        )
        self._conn.commit()
        return self.get_task(task_id)

    def complete_task(
        self, task_id: str, result: str, status: str = "completed"
    ) -> dict[str, Any]:
        if status not in ("completed", "failed"):
            raise ValueError("Status must be completed or failed")
        now = _now()
        self._conn.execute(
            "UPDATE tasks SET status=?, result=?, completed_at=? WHERE id=?",
            (status, result, now, task_id),
        )
        self._conn.commit()
        return self.get_task(task_id)

    def update_priority(self, task_id: str, priority: int) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError("Task not found")
        priority = max(1, min(5, priority))
        self._conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id)
        )
        self._conn.commit()
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError("Task not found")
        if task["status"] in ("completed", "failed"):
            raise ValueError(f"Task already {task['status']}")
        self._conn.execute(
            "UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task_id,)
        )
        self._conn.commit()
        return self.get_task(task_id)

    # ─── Projects (marketing orchestrator) ───────────────────────

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY slug").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_project(self, slug: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def upsert_project(
        self,
        slug: str,
        owner: str,
        repo: str,
        topics: list[str] | None = None,
        subreddits: list[str] | None = None,
        search_terms: list[str] | None = None,
        posture: str = "",
    ) -> dict[str, Any]:
        import json

        now = _now()
        self._conn.execute(
            """INSERT INTO projects (slug, owner, repo, topics, subreddits, search_terms, posture, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(slug) DO UPDATE SET
                 owner=excluded.owner, repo=excluded.repo,
                 topics=excluded.topics, subreddits=excluded.subreddits,
                 search_terms=excluded.search_terms, posture=excluded.posture,
                 updated_at=excluded.updated_at""",
            (
                slug,
                owner,
                repo,
                json.dumps(topics or []),
                json.dumps(subreddits or []),
                json.dumps(search_terms or []),
                posture,
                now,
                now,
            ),
        )
        self._conn.commit()
        return self.get_project(slug)

    def delete_project(self, slug: str) -> bool:
        cursor = self._conn.execute("DELETE FROM projects WHERE slug = ?", (slug,))
        self._conn.commit()
        return cursor.rowcount > 0

    def add_project_subreddit(self, slug: str, subreddit: str) -> bool:
        """Append a subreddit to the project's `subreddits` list,
        idempotent (case-insensitive). Returns True if newly added.
        Used by auto-promotion to fold an emerging sub into the
        curated list without losing the rest of the project's
        configuration."""
        import json

        proj = self.get_project(slug)
        if not proj:
            return False
        subs = json.loads(proj.get("subreddits", "[]"))
        existing = {s.lower() for s in subs}
        if subreddit.lower() in existing:
            return False
        subs.append(subreddit)
        self._conn.execute(
            "UPDATE projects SET subreddits=?, updated_at=? WHERE slug=?",
            (json.dumps(subs), _now(), slug),
        )
        self._conn.commit()
        return True

    # ─── Schedules (marketing orchestrator) ──────────────────────

    def list_schedules(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM schedules ORDER BY name").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE id = ? OR name = ?",
            (schedule_id, schedule_id),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def upsert_schedule(
        self,
        schedule_id: str,
        name: str,
        kind: str,
        cron: str,
        kwargs: dict | None = None,
    ) -> dict[str, Any]:
        import json

        now = _now()
        self._conn.execute(
            """INSERT INTO schedules (id, name, kind, cron, kwargs, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, kind=excluded.kind,
                 cron=excluded.cron, kwargs=excluded.kwargs,
                 updated_at=excluded.updated_at""",
            (schedule_id, name, kind, cron, json.dumps(kwargs or {}), now, now),
        )
        self._conn.commit()
        return self.get_schedule(schedule_id)

    def update_schedule_status(
        self, schedule_id: str, last_run: str, last_status: str
    ) -> None:
        self._conn.execute(
            "UPDATE schedules SET last_run=?, last_status=?, updated_at=? WHERE id=?",
            (last_run, last_status, _now(), schedule_id),
        )
        self._conn.commit()

    def set_schedule_paused(
        self, schedule_id: str, paused: bool
    ) -> dict[str, Any] | None:
        self._conn.execute(
            "UPDATE schedules SET paused=?, updated_at=? WHERE id=?",
            (1 if paused else 0, _now(), schedule_id),
        )
        self._conn.commit()
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ─── Scan Cache (dedup) ─────────────────────────────────────

    def mark_seen(
        self, kind: str, external_id: str, project_slug: str | None = None
    ) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO scan_cache (kind, external_id, project_slug, seen_at)
               VALUES (?, ?, ?, ?)""",
            (kind, external_id, project_slug, _now()),
        )
        self._conn.commit()

    def is_seen(
        self, kind: str, external_id: str, project_slug: str | None = None
    ) -> bool:
        if project_slug is None:
            row = self._conn.execute(
                "SELECT 1 FROM scan_cache WHERE kind=? AND external_id=? AND project_slug IS NULL",
                (kind, external_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM scan_cache WHERE kind=? AND external_id=? AND project_slug=?",
                (kind, external_id, project_slug),
            ).fetchone()
        return row is not None

    def get_unseen(
        self, kind: str, external_ids: list[str], project_slug: str | None = None
    ) -> list[str]:
        if not external_ids:
            return []
        placeholders = ",".join("?" * len(external_ids))
        if project_slug is None:
            rows = self._conn.execute(
                f"SELECT external_id FROM scan_cache WHERE kind=? AND project_slug IS NULL AND external_id IN ({placeholders})",
                (kind, *external_ids),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT external_id FROM scan_cache WHERE kind=? AND project_slug=? AND external_id IN ({placeholders})",
                (kind, project_slug, *external_ids),
            ).fetchall()
        seen = {r["external_id"] for r in rows}
        return [eid for eid in external_ids if eid not in seen]

    def prune_scan_cache(self, max_age_days: int = 14) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        cursor = self._conn.execute(
            "DELETE FROM scan_cache WHERE seen_at < ?", (cutoff.isoformat(),)
        )
        self._conn.commit()
        return cursor.rowcount

    # ─── Subreddit Stats ─────────────────────────────────────────

    def record_subreddit_search(
        self, subreddit: str, project_slug: str, is_curated: bool = False
    ) -> None:
        """Record one search impression for a (subreddit, project) pair.

        Each digest/pulse cycle increments impressions for every
        subreddit it searched. Combined with hits, this gives a
        per-(sub, project) yield rate.
        """
        sub = subreddit.lower().strip()
        now = _now()
        self._conn.execute(
            """INSERT INTO subreddit_stats
                 (subreddit, project_slug, hits, impressions,
                  last_searched_at, is_curated)
               VALUES (?, ?, 0, 1, ?, ?)
               ON CONFLICT(subreddit, project_slug) DO UPDATE SET
                 impressions = impressions + 1,
                 last_searched_at = excluded.last_searched_at,
                 is_curated = MAX(is_curated, excluded.is_curated)""",
            (sub, project_slug, now, 1 if is_curated else 0),
        )
        self._conn.commit()

    def record_subreddit_hit(self, subreddit: str, project_slug: str) -> None:
        """Record one relevance hit for a (subreddit, project) pair."""
        sub = subreddit.lower().strip()
        now = _now()
        self._conn.execute(
            """INSERT INTO subreddit_stats
                 (subreddit, project_slug, hits, impressions,
                  first_hit_at, last_hit_at)
               VALUES (?, ?, 1, 0, ?, ?)
               ON CONFLICT(subreddit, project_slug) DO UPDATE SET
                 hits = hits + 1,
                 first_hit_at = COALESCE(first_hit_at, excluded.first_hit_at),
                 last_hit_at = excluded.last_hit_at""",
            (sub, project_slug, now, now),
        )
        self._conn.commit()

    def list_subreddit_stats(
        self,
        project_slug: str | None = None,
        curated_only: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return (sub, project) stat rows ordered by hits desc."""
        clauses = []
        params: list[Any] = []
        if project_slug is not None:
            clauses.append("project_slug = ?")
            params.append(project_slug)
        if curated_only is True:
            clauses.append("is_curated = 1")
        elif curated_only is False:
            clauses.append("is_curated = 0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"""SELECT * FROM subreddit_stats {where}
                ORDER BY hits DESC, last_hit_at DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_promotion_candidates(
        self,
        project_slug: str,
        min_hits: int = 5,
        window_days: int = 14,
    ) -> list[dict[str, Any]]:
        """Non-curated subs with >= min_hits whose first_hit_at falls
        within window_days of last_hit_at — i.e. sustained recent yield
        as opposed to a single ancient hit. Returned rows are eligible
        for auto-promotion into the project's subreddits list."""
        rows = self._conn.execute(
            """SELECT * FROM subreddit_stats
               WHERE project_slug = ?
                 AND is_curated = 0
                 AND promoted_at IS NULL
                 AND hits >= ?
                 AND first_hit_at IS NOT NULL
                 AND last_hit_at IS NOT NULL
                 AND CAST(
                       (julianday(last_hit_at) - julianday(first_hit_at))
                     AS INTEGER) <= ?
               ORDER BY hits DESC""",
            (project_slug, min_hits, window_days),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_subreddit_promoted(self, subreddit: str, project_slug: str) -> None:
        """Stamp the (sub, project) row as promoted. Caller is
        responsible for also adding the sub to the project's
        `subreddits` JSON array via upsert_project."""
        sub = subreddit.lower().strip()
        self._conn.execute(
            """UPDATE subreddit_stats
                 SET is_curated = 1, promoted_at = ?
               WHERE subreddit = ? AND project_slug = ?""",
            (_now(), sub, project_slug),
        )
        self._conn.commit()

    # ─── Research Sessions (multi-source orchestrator audit trail) ─

    def create_research_session(
        self, topic: str, channels: list[str]
    ) -> dict[str, Any]:
        import json

        now = _now()
        session_id = str(uuid.uuid4())[:12]
        self._conn.execute(
            "INSERT INTO research_sessions "
            "(id, topic, channels, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (session_id, topic, json.dumps(channels), now, now),
        )
        self._conn.commit()
        return self.get_research_session(session_id) or {}

    def add_research_finding(
        self,
        session_id: str,
        source: str,
        channel: str,
        title: str,
        url: str,
        relevance: float,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        import json

        now = _now()
        cursor = self._conn.execute(
            "INSERT INTO research_findings "
            "(session_id, source, channel, title, url, relevance, summary, "
            " metadata, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                source,
                channel,
                title,
                url,
                float(relevance),
                summary,
                json.dumps(metadata or {}),
                now,
            ),
        )
        self._conn.execute(
            "UPDATE research_sessions SET updated_at=? WHERE id=?",
            (now, session_id),
        )
        self._conn.commit()
        return cursor.lastrowid or 0

    def complete_research_session(self, session_id: str) -> dict[str, Any]:
        now = _now()
        self._conn.execute(
            "UPDATE research_sessions SET status='complete', updated_at=? WHERE id=?",
            (now, session_id),
        )
        self._conn.commit()
        return self.get_research_session(session_id) or {}

    def get_research_session(self, session_id: str) -> dict[str, Any] | None:
        import json

        row = self._conn.execute(
            "SELECT * FROM research_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            return None
        session = self._row_to_dict(row)
        session["channels"] = json.loads(session.get("channels") or "[]")
        finding_rows = self._conn.execute(
            "SELECT * FROM research_findings WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        findings = []
        for fr in finding_rows:
            f = self._row_to_dict(fr)
            f["metadata"] = json.loads(f.get("metadata") or "{}")
            findings.append(f)
        session["findings"] = findings
        session["finding_count"] = len(findings)
        return session

    def list_research_sessions(self, limit: int = 25) -> list[dict[str, Any]]:
        import json

        rows = self._conn.execute(
            "SELECT s.id, s.topic, s.channels, s.status, s.created_at, "
            "       s.updated_at, "
            "       (SELECT COUNT(*) FROM research_findings rf "
            "          WHERE rf.session_id = s.id) AS finding_count "
            "FROM research_sessions s "
            "ORDER BY s.created_at DESC "
            "LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        out = []
        for r in rows:
            d = self._row_to_dict(r)
            d["channels"] = json.loads(d.get("channels") or "[]")
            out.append(d)
        return out
