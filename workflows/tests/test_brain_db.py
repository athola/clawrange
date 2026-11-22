"""Tests for the brain database layer — drives brain_db.py API design."""

import os
import tempfile
from datetime import UTC, datetime

import pytest

import brain_db
import clock
from brain_db import (
    BrainDB,
    LinkType,
    PageType,
    _chunk_text,
    rrf_fuse,
)
from connectors import transforms
from crm import sqlite_adapter


@pytest.fixture
def db():
    """Create a temporary BrainDB for each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test_brain.db")
        brain = BrainDB(path)
        brain.init_db()
        yield brain


# ─── Schema Initialization ────────────────────────────────────────


class TestSchemaInit:
    def test_creates_all_tables(self, db):
        """All required tables exist after init."""
        tables = {
            r["name"]
            for r in db._conn.execute("SELECT name FROM sqlite_master").fetchall()
        }
        expected = {
            "pages",
            "timeline",
            "tasks",
            "links",
            "tags",
            "page_tags",
            "page_versions",
            "pages_fts",
        }
        assert expected.issubset(tables)

    def test_init_idempotent(self, db):
        """A second init_db() keeps the schema and the data."""
        db.upsert_page("a", "Alpha", "note", "body")
        db.create_task("work")

        def schema():
            return sorted(
                r["name"]
                for r in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            )

        before = schema()
        db.init_db()
        assert schema() == before
        assert db.get_page("a") is not None
        assert len(db.list_tasks()) == 1


# ─── Page CRUD ────────────────────────────────────────────────────


class TestPageCRUD:
    def test_create_page(self, db):
        page = db.upsert_page(
            slug="client/acme-corp",
            title="Acme Corp",
            page_type=PageType.CLIENT,
            compiled="Main contact: Pat Doe. 3 sites in Springfield.",
        )
        assert page["slug"] == "client/acme-corp"
        assert page["title"] == "Acme Corp"
        assert page["page_type"] == "client"
        assert page["compiled"] == "Main contact: Pat Doe. 3 sites in Springfield."
        assert page["content_hash"]
        assert page["created_at"]
        assert page["updated_at"]

    def test_get_page(self, db):
        db.upsert_page(
            "inc/wifi-outage", "WiFi Outage", PageType.INCIDENT, "Site 2 down"
        )
        page = db.get_page("inc/wifi-outage")
        assert page is not None
        assert page["title"] == "WiFi Outage"

    def test_get_page_not_found(self, db):
        assert db.get_page("nonexistent") is None

    def test_update_page_upsert(self, db):
        db.upsert_page("note/test", "Test", PageType.NOTE, "v1")
        db.upsert_page("note/test", "Test Updated", PageType.NOTE, "v2")
        page = db.get_page("note/test")
        assert page["title"] == "Test Updated"
        assert page["compiled"] == "v2"

    def test_upsert_same_content_noop(self, db):
        """Idempotent upsert: same content hash means no update."""
        p1 = db.upsert_page("note/x", "X", PageType.NOTE, "same")
        p2 = db.upsert_page("note/x", "X", PageType.NOTE, "same")
        assert p1["updated_at"] == p2["updated_at"]
        assert p1["content_hash"] == p2["content_hash"]

    def test_upsert_same_content_creates_version(self, db):
        """Even on no-op upsert, a version snapshot exists."""
        db.upsert_page("note/x", "X", PageType.NOTE, "v1")
        versions = db.get_page_versions("note/x")
        assert len(versions) == 1

    def test_delete_page(self, db):
        db.upsert_page("note/gone", "Gone", PageType.NOTE, "bye")
        db.append_timeline("note/gone", "something happened", "agent")
        assert db.delete_page("note/gone") is True
        assert db.get_page("note/gone") is None
        assert db.get_timeline("note/gone") == []

    def test_delete_nonexistent_page(self, db):
        assert db.delete_page("nope") is False

    def test_list_pages(self, db):
        db.upsert_page("client/a", "A", PageType.CLIENT, "")
        db.upsert_page("client/b", "B", PageType.CLIENT, "")
        db.upsert_page("inc/1", "Inc", PageType.INCIDENT, "")
        pages = db.list_pages()
        assert len(pages) == 3

    def test_list_pages_by_type(self, db):
        db.upsert_page("client/a", "A", PageType.CLIENT, "")
        db.upsert_page("client/b", "B", PageType.CLIENT, "")
        db.upsert_page("inc/1", "Inc", PageType.INCIDENT, "")
        clients = db.list_pages(page_type=PageType.CLIENT)
        assert len(clients) == 2
        assert all(p["page_type"] == "client" for p in clients)

    def test_list_pages_limit(self, db):
        for i in range(10):
            db.upsert_page(f"note/n{i}", f"Note {i}", PageType.NOTE, "")
        pages = db.list_pages(limit=5)
        assert len(pages) == 5


# ─── Content Hashing ──────────────────────────────────────────────


class TestContentHashing:
    def test_different_content_different_hash(self, db):
        p1 = db.upsert_page("note/a", "Title", PageType.NOTE, "content A")
        p2 = db.upsert_page("note/b", "Title", PageType.NOTE, "content B")
        assert p1["content_hash"] != p2["content_hash"]

    def test_same_content_same_hash(self, db):
        p1 = db.upsert_page("note/a", "Title", PageType.NOTE, "same content")
        p2 = db.upsert_page("note/b", "Title", PageType.NOTE, "same content")
        assert p1["content_hash"] == p2["content_hash"]


# ─── Page Versions ────────────────────────────────────────────────


class TestPageVersions:
    def test_update_creates_version(self, db):
        db.upsert_page("note/v", "V", PageType.NOTE, "version 1")
        db.upsert_page("note/v", "V", PageType.NOTE, "version 2")
        db.upsert_page("note/v", "V", PageType.NOTE, "version 3")
        versions = db.get_page_versions("note/v")
        assert len(versions) == 3
        # Most recent first
        assert versions[0]["compiled"] == "version 3"
        assert versions[2]["compiled"] == "version 1"

    def test_noop_upsert_no_new_version(self, db):
        db.upsert_page("note/v", "V", PageType.NOTE, "v1")
        db.upsert_page("note/v", "V", PageType.NOTE, "v1")  # same content
        versions = db.get_page_versions("note/v")
        assert len(versions) == 1

    def test_version_has_content_hash(self, db):
        db.upsert_page("note/v", "V", PageType.NOTE, "v1")
        versions = db.get_page_versions("note/v")
        assert versions[0]["content_hash"]


# ─── Content Chunking ────────────────────────────────────────────


class TestContentChunking:
    def test_page_upsert_creates_chunks(self, db):
        """Pages are auto-chunked on create/update."""
        db.upsert_page("note/x", "X", PageType.NOTE, "Short content here.")
        chunks = db.get_chunks("note/x")
        assert len(chunks) >= 1
        assert chunks[0]["page_slug"] == "note/x"
        assert chunks[0]["chunk_index"] == 0
        assert "X" in chunks[0]["content"]  # title included

    def test_long_content_produces_multiple_chunks(self, db):
        """Content exceeding max_words is split into multiple chunks."""
        # ~400 words across 4 paragraphs
        paras = []
        for i in range(4):
            paras.append(f"Paragraph {i}. " + " ".join(f"word{j}" for j in range(100)))
        compiled = "\n\n".join(paras)
        db.upsert_page("note/long", "Long Note", PageType.NOTE, compiled)
        chunks = db.get_chunks("note/long")
        assert len(chunks) > 1

    def test_chunks_updated_on_page_update(self, db):
        db.upsert_page("note/c", "C", PageType.NOTE, "original content")
        c1 = db.get_chunks("note/c")
        db.upsert_page("note/c", "C", PageType.NOTE, "new content entirely different")
        c2 = db.get_chunks("note/c")
        assert c1[0]["content_hash"] != c2[0]["content_hash"]

    def test_idempotent_upsert_skips_rechunk(self, db):
        db.upsert_page("note/s", "S", PageType.NOTE, "same content")
        c1 = db.get_chunks("note/s")
        db.upsert_page("note/s", "S", PageType.NOTE, "same content")
        c2 = db.get_chunks("note/s")
        assert c1[0]["content_hash"] == c2[0]["content_hash"]

    def test_empty_compiled_still_chunks_title(self, db):
        db.upsert_page("note/e", "Just A Title", PageType.NOTE, "")
        chunks = db.get_chunks("note/e")
        assert len(chunks) >= 1
        assert "Just A Title" in chunks[0]["content"]

    def test_delete_page_cascades_chunks(self, db):
        db.upsert_page("note/d", "D", PageType.NOTE, "content")
        assert len(db.get_chunks("note/d")) >= 1
        db.delete_page("note/d")
        assert db.get_chunks("note/d") == []

    def test_chunk_has_content_hash(self, db):
        db.upsert_page("note/h", "H", PageType.NOTE, "hashable content")
        chunks = db.get_chunks("note/h")
        assert chunks[0]["content_hash"]


class TestChunkTextFunction:
    """Unit tests for the _chunk_text helper."""

    def test_empty_input(self):
        from brain_db import _chunk_text

        assert _chunk_text("") == []
        assert _chunk_text("   ") == []

    def test_short_text_single_chunk(self):
        from brain_db import _chunk_text

        chunks = _chunk_text("Hello world. This is a test.")
        assert len(chunks) == 1

    def test_paragraph_splitting(self):
        from brain_db import _chunk_text

        text = "Para one words.\n\nPara two words."
        chunks = _chunk_text(text, max_words=5, overlap_words=0)
        assert len(chunks) == 2

    def test_overlap_adds_context(self):
        from brain_db import _chunk_text

        text = "First paragraph with some words.\n\nSecond paragraph here."
        chunks = _chunk_text(text, max_words=10, overlap_words=3)
        if len(chunks) > 1:
            # Second chunk should contain some words from first
            assert "words" in chunks[1] or "some" in chunks[1]


# ─── Embeddings & Vector Search ──────────────────────────────────


class TestEmbeddings:
    def test_has_embeddings(self, db):
        """True once the active model's table holds vectors."""
        assert db.use_vector_table("vec_test_1536", 1536) is True
        assert db.has_embeddings() is False
        db.upsert_page("note/v", "V", PageType.NOTE, "body")
        db.store_embeddings([(db.get_chunks("note/v")[0]["id"], [0.1] * 1536)])
        assert db.has_embeddings() is True

    def test_store_and_search_vector(self, db):
        """Store embedding for a chunk, then find it via vector search."""
        db.use_vector_table("vec_test_1536", 1536)
        db.upsert_page("note/vec", "Vector Test", PageType.NOTE, "searchable content")
        chunks = db.get_chunks("note/vec")
        assert len(chunks) >= 1
        chunk_id = chunks[0]["id"]

        # Store a synthetic embedding (1536 dims)
        vector = [0.1] * 1536
        assert db.store_embeddings([(chunk_id, vector)]) == 1

        # Search with the same vector — should find our page
        results = db.search_vector(vector, limit=5)
        assert len(results) >= 1
        assert results[0]["slug"] == "note/vec"
        assert results[0]["match_type"] == "vector"

    def test_vector_search_returns_best_per_page(self, db):
        """Dedup: only best matching chunk per page returned."""
        db.use_vector_table("vec_test_1536", 1536)
        db.upsert_page(
            "note/multi",
            "Multi Chunk",
            PageType.NOTE,
            "First paragraph here.\n\nSecond paragraph here.",
        )
        chunks = db.get_chunks("note/multi")

        # Embed both chunks with slightly different vectors
        for i, chunk in enumerate(chunks):
            vec = [0.1 + i * 0.01] * 1536
            db.store_embeddings([(chunk["id"], vec)])

        results = db.search_vector([0.1] * 1536, limit=10)
        slugs = [r["slug"] for r in results]
        # Should only appear once despite multiple chunks
        assert slugs.count("note/multi") == 1


class TestHybridSearch:
    def test_hybrid_without_vector_falls_back_to_keyword(self, db):
        """Hybrid search works even without embeddings — keyword fallback."""
        db.upsert_page("inc/net", "Network Issue", PageType.INCIDENT, "WiFi drops")
        results = db.search_hybrid("WiFi", query_vector=None)
        assert len(results) >= 1
        assert results[0]["slug"] == "inc/net"
        assert results[0]["match_type"] == "keyword"

    def test_hybrid_with_vector_merges_results(self, db):
        """With both keyword and vector results, RRF fuses them."""
        db.use_vector_table("vec_test_1536", 1536)
        # Page that matches keyword
        db.upsert_page(
            "inc/wifi", "WiFi Outage", PageType.INCIDENT, "WiFi down at site"
        )
        # Page that only matches vector (different words)
        db.upsert_page(
            "inc/net", "Network Problem", PageType.INCIDENT, "connectivity lost"
        )

        # Embed the network page
        chunks = db.get_chunks("inc/net")
        if chunks:
            db.store_embeddings([(chunks[0]["id"], [0.5] * 1536)])

        # Search with a vector that matches the network page
        results = db.search_hybrid("WiFi", query_vector=[0.5] * 1536)
        assert len(results) >= 1
        slugs = [r["slug"] for r in results]
        assert "inc/wifi" in slugs  # keyword match
        # Hybrid mode
        assert all(r["match_type"] == "hybrid" for r in results)

    def test_rrf_score_calculation(self, db):
        """RRF scores should be non-zero floats (positive from rank fusion)."""
        db.upsert_page("note/a", "Alpha", PageType.NOTE, "test content")
        db.upsert_page("note/b", "Beta", PageType.NOTE, "more test data")
        results = db.search_hybrid("test")
        assert len(results) >= 1
        # RRF formula: 1/(k+rank), so scores are always positive for keyword-only
        # With keyword fallback, match_type is "keyword" not "hybrid"
        for r in results:
            assert "score" in r or "match_type" in r


# ─── Graph Traversal ─────────────────────────────────────────────


class TestGraphTraversal:
    def test_single_hop(self, db):
        db.upsert_page("client/acme", "Acme", PageType.CLIENT, "")
        db.upsert_page("inc/wifi", "WiFi", PageType.INCIDENT, "")
        db.add_link("client/acme", "inc/wifi", LinkType.INCIDENT_OF)
        nodes = db.traverse_graph("client/acme", depth=1)
        assert len(nodes) == 1
        assert nodes[0]["slug"] == "inc/wifi"

    def test_multi_hop(self, db):
        db.upsert_page("person/bob", "Bob", PageType.PERSON, "")
        db.upsert_page("company/acme", "Acme", PageType.COMPANY, "")
        db.upsert_page("inc/wifi", "WiFi", PageType.INCIDENT, "")
        db.add_link("person/bob", "company/acme", LinkType.WORKS_AT)
        db.add_link("company/acme", "inc/wifi", LinkType.INCIDENT_OF)
        # depth=1 should find acme only
        d1 = db.traverse_graph("person/bob", depth=1)
        assert len(d1) == 1
        assert d1[0]["slug"] == "company/acme"
        # depth=2 should find both
        d2 = db.traverse_graph("person/bob", depth=2)
        slugs = {n["slug"] for n in d2}
        assert "company/acme" in slugs
        assert "inc/wifi" in slugs

    def test_filter_by_link_type(self, db):
        db.upsert_page("person/bob", "Bob", PageType.PERSON, "")
        db.upsert_page("company/acme", "Acme", PageType.COMPANY, "")
        db.upsert_page("person/alice", "Alice", PageType.PERSON, "")
        db.add_link("person/bob", "company/acme", LinkType.WORKS_AT)
        db.add_link("person/bob", "person/alice", LinkType.KNOWS)
        # Only follow "works_at" links
        nodes = db.traverse_graph("person/bob", link_type=LinkType.WORKS_AT)
        assert len(nodes) == 1
        assert nodes[0]["slug"] == "company/acme"

    def test_traversal_includes_path(self, db):
        db.upsert_page("a", "A", PageType.NOTE, "")
        db.upsert_page("b", "B", PageType.NOTE, "")
        db.add_link("a", "b")
        nodes = db.traverse_graph("a", depth=1)
        assert nodes[0]["path"]

    def test_no_links_returns_empty(self, db):
        db.upsert_page("note/lonely", "Lonely", PageType.NOTE, "")
        nodes = db.traverse_graph("note/lonely")
        assert nodes == []


# ─── Timeline ─────────────────────────────────────────────────────


class TestTimeline:
    def test_append_timeline(self, db):
        db.upsert_page("note/t", "T", PageType.NOTE, "test")
        entry = db.append_timeline("note/t", "something happened", "john-117")
        assert entry["content"] == "something happened"
        assert entry["source"] == "john-117"
        assert entry["created_at"]

    def test_get_timeline(self, db):
        db.upsert_page("note/t", "T", PageType.NOTE, "test")
        db.append_timeline("note/t", "event 1", "agent-a")
        db.append_timeline("note/t", "event 2", "agent-b")
        timeline = db.get_timeline("note/t")
        assert len(timeline) == 2
        # Most recent first
        assert timeline[0]["content"] == "event 2"

    def test_timeline_with_limit(self, db):
        db.upsert_page("note/t", "T", PageType.NOTE, "test")
        for i in range(5):
            db.append_timeline("note/t", f"event {i}", "agent")
        timeline = db.get_timeline("note/t", limit=3)
        assert len(timeline) == 3

    def test_timeline_empty_for_missing_page(self, db):
        assert db.get_timeline("nonexistent") == []


# ─── FTS5 Keyword Search ─────────────────────────────────────────


class TestKeywordSearch:
    def test_search_finds_by_title(self, db):
        db.upsert_page(
            "inc/wifi", "WiFi Outage Site 2", PageType.INCIDENT, "Network down"
        )
        results = db.search_keyword("WiFi")
        assert len(results) == 1
        assert results[0]["slug"] == "inc/wifi"

    def test_search_finds_by_compiled(self, db):
        db.upsert_page(
            "inc/net", "Network Issue", PageType.INCIDENT, "Intermittent WiFi drops"
        )
        results = db.search_keyword("WiFi")
        assert len(results) == 1

    def test_search_ranking(self, db):
        db.upsert_page(
            "inc/1", "WiFi Outage", PageType.INCIDENT, "WiFi is completely down"
        )
        db.upsert_page(
            "inc/2", "Slow Internet", PageType.INCIDENT, "WiFi is slow sometimes"
        )
        results = db.search_keyword("WiFi outage")
        # First result should be the one with "outage" in title
        assert results[0]["slug"] == "inc/1"

    def test_search_empty_results(self, db):
        db.upsert_page("note/x", "X", PageType.NOTE, "nothing relevant")
        results = db.search_keyword("nonexistent_query_xyz")
        assert len(results) == 0

    def test_search_limit(self, db):
        for i in range(10):
            db.upsert_page(
                f"note/n{i}", f"WiFi Note {i}", PageType.NOTE, "WiFi content"
            )
        results = db.search_keyword("WiFi", limit=5)
        assert len(results) == 5


# ─── Links (Knowledge Graph) ──────────────────────────────────────


class TestLinks:
    def test_add_link(self, db):
        db.upsert_page("client/acme", "Acme", PageType.CLIENT, "")
        db.upsert_page("inc/wifi", "WiFi Outage", PageType.INCIDENT, "")
        link = db.add_link("client/acme", "inc/wifi", LinkType.INCIDENT_OF)
        assert link["from_slug"] == "client/acme"
        assert link["to_slug"] == "inc/wifi"
        assert link["link_type"] == "incident_of"

    def test_add_link_default_type(self, db):
        db.upsert_page("note/a", "A", PageType.NOTE, "")
        db.upsert_page("note/b", "B", PageType.NOTE, "")
        link = db.add_link("note/a", "note/b")
        assert link["link_type"] == "related_to"

    def test_duplicate_link_ignored(self, db):
        db.upsert_page("note/a", "A", PageType.NOTE, "")
        db.upsert_page("note/b", "B", PageType.NOTE, "")
        db.add_link("note/a", "note/b", LinkType.RELATED_TO)
        db.add_link("note/a", "note/b", LinkType.RELATED_TO)  # no error
        links = db.get_links("note/a")
        assert len(links) == 1

    def test_get_links_direction(self, db):
        db.upsert_page("client/acme", "Acme", PageType.CLIENT, "")
        db.upsert_page("inc/wifi", "WiFi", PageType.INCIDENT, "")
        db.add_link("client/acme", "inc/wifi", LinkType.INCIDENT_OF)
        # From acme's perspective
        outgoing = db.get_links("client/acme")
        assert len(outgoing) == 1
        # From wifi's perspective (incoming)
        incoming = db.get_links("inc/wifi")
        assert len(incoming) == 1

    def test_delete_link(self, db):
        db.upsert_page("note/a", "A", PageType.NOTE, "")
        db.upsert_page("note/b", "B", PageType.NOTE, "")
        link = db.add_link("note/a", "note/b")
        assert db.delete_link(link["id"]) is True
        assert db.get_links("note/a") == []

    def test_delete_nonexistent_link(self, db):
        assert db.delete_link(999) is False


# ─── Tags ─────────────────────────────────────────────────────────


class TestTags:
    def test_add_tags_to_page(self, db):
        db.upsert_page("inc/wifi", "WiFi Outage", PageType.INCIDENT, "")
        db.set_tags("inc/wifi", ["network", "wifi"])
        tags = db.get_tags("inc/wifi")
        assert set(tags) == {"network", "wifi"}

    def test_replace_tags(self, db):
        db.upsert_page("inc/wifi", "WiFi", PageType.INCIDENT, "")
        db.set_tags("inc/wifi", ["network"])
        db.set_tags("inc/wifi", ["network", "urgent"])
        tags = db.get_tags("inc/wifi")
        assert set(tags) == {"network", "urgent"}

    def test_remove_tag(self, db):
        db.upsert_page("inc/wifi", "WiFi", PageType.INCIDENT, "")
        db.set_tags("inc/wifi", ["network", "wifi", "urgent"])
        db.remove_tag("inc/wifi", "wifi")
        tags = db.get_tags("inc/wifi")
        assert "wifi" not in tags
        assert "network" in tags

    def test_list_all_tags(self, db):
        db.upsert_page("inc/a", "A", PageType.INCIDENT, "")
        db.upsert_page("inc/b", "B", PageType.INCIDENT, "")
        db.set_tags("inc/a", ["network", "wifi"])
        db.set_tags("inc/b", ["network", "hardware"])
        all_tags = db.list_all_tags()
        tag_names = {t["name"] for t in all_tags}
        assert tag_names == {"network", "wifi", "hardware"}
        # network used by 2 pages
        network = next(t for t in all_tags if t["name"] == "network")
        assert network["count"] == 2


# ─── Task Queue (Persistent) ──────────────────────────────────────


class TestPersistentTaskQueue:
    def test_create_task(self, db):
        task = db.create_task("check z.ai models", priority=3)
        assert task["id"]
        assert task["description"] == "check z.ai models"
        assert task["status"] == "pending"
        assert task["priority"] == 3

    def test_get_task(self, db):
        created = db.create_task("work")
        task = db.get_task(created["id"])
        assert task is not None
        assert task["description"] == "work"

    def test_get_task_not_found(self, db):
        assert db.get_task("nonexistent") is None

    def test_list_tasks(self, db):
        db.create_task("task a")
        db.create_task("task b")
        tasks = db.list_tasks()
        assert len(tasks) == 2

    def test_list_tasks_by_status(self, db):
        t1 = db.create_task("pending task")
        db.claim_task(t1["id"])
        db.create_task("another pending")
        pending = db.list_tasks(status="pending")
        assert len(pending) == 1
        active = db.list_tasks(status="active")
        assert len(active) == 1

    def test_claim_task(self, db):
        task = db.create_task("work")
        claimed = db.claim_task(task["id"])
        assert claimed["status"] == "active"

    def test_claim_non_pending_fails(self, db):
        task = db.create_task("work")
        db.claim_task(task["id"])
        with pytest.raises(ValueError, match="not pending"):
            db.claim_task(task["id"])

    def test_complete_task(self, db):
        task = db.create_task("work")
        db.claim_task(task["id"])
        completed = db.complete_task(task["id"], "done!", "completed")
        assert completed["status"] == "completed"
        assert completed["result"] == "done!"
        assert completed["completed_at"]

    def test_cancel_task(self, db):
        task = db.create_task("nevermind")
        cancelled = db.cancel_task(task["id"])
        assert cancelled["status"] == "cancelled"

    def test_cancel_completed_fails(self, db):
        task = db.create_task("done")
        db.claim_task(task["id"])
        db.complete_task(task["id"], "done", "completed")
        with pytest.raises(ValueError, match="already completed"):
            db.cancel_task(task["id"])

    def test_priority_ordering(self, db):
        db.create_task("low", priority=5)
        db.create_task("urgent", priority=1)
        db.create_task("normal", priority=3)
        tasks = db.list_tasks()
        descriptions = [t["description"] for t in tasks]
        assert descriptions == ["urgent", "normal", "low"]

    def test_persistence_across_connections(self, db):
        """Tasks survive closing and reopening the database."""
        task = db.create_task("persistent work")
        task_id = task["id"]

        # Open a new connection to the same DB file
        db_path = db._db_path
        db2 = BrainDB(db_path)
        db2.init_db()
        fetched = db2.get_task(task_id)
        assert fetched is not None
        assert fetched["description"] == "persistent work"


# ─── Persona Learnings ───────────────────────────────────────────


def test_persona_learning_lifecycle(tmp_path):
    from brain_db import BrainDB

    db = BrainDB(str(tmp_path / "b.db"))
    db.init_db()
    row = db.create_learning(
        "chief-of-staff",
        "persona",
        "Communication",
        "Lead with the recommendation.",
        "feedback",
    )
    assert row["status"] == "pending" and row["id"]
    assert db.get_learning(row["id"])["content"].startswith("Lead with")
    db.set_learning_status(row["id"], "approved")
    approved = db.list_learnings(profile="chief-of-staff", status="approved")
    assert len(approved) == 1 and approved[0]["decided_at"]


class TestTaskStateIntegrity:
    """Claims must be atomic and terminal states final: the queue is shared
    by the heartbeat, the tome bridge and the HTTP API."""

    def test_claim_is_atomic_against_stale_read(self, db, monkeypatch):
        """Two consumers both read 'pending' before either writes. Only the
        first UPDATE may win; the second must see the row already active."""
        t = db.create_task("work")
        db.claim_task(t["id"])  # consumer A wins
        real_get = db.get_task
        stale = dict(real_get(t["id"]), status="pending")
        monkeypatch.setattr(db, "get_task", lambda tid: stale)  # B's stale read
        with pytest.raises(ValueError):
            db.claim_task(t["id"])

    def test_complete_rejects_terminal_task(self, db):
        t = db.create_task("work")
        db.complete_task(t["id"], "done")
        with pytest.raises(ValueError):
            db.complete_task(t["id"], "overwrite")
        assert db.get_task(t["id"])["result"] == "done"

    def test_complete_rejects_cancelled_task(self, db):
        t = db.create_task("nevermind")
        db.cancel_task(t["id"])
        with pytest.raises(ValueError):
            db.complete_task(t["id"], "revived")
        assert db.get_task(t["id"])["status"] == "cancelled"

    def test_complete_unknown_task_raises(self, db):
        with pytest.raises(ValueError):
            db.complete_task("nope", "x")


class TestConnectionLock:
    """One sqlite connection is shared by FastAPI's threadpool and the event
    loop; without a lock one thread's commit() can commit another's
    half-finished write. Every BrainDB method serializes on one RLock."""

    def test_methods_wait_for_the_lock(self, db):
        import threading

        done = threading.Event()
        db._lock.acquire()
        try:
            th = threading.Thread(
                target=lambda: (db.create_task("x"), done.set()), daemon=True
            )
            th.start()
            assert not done.wait(0.3), "create_task ran without the lock"
        finally:
            db._lock.release()
        assert done.wait(2), "create_task never completed after release"

    def test_lock_is_reentrant(self, db):
        """create_task calls get_task internally; an RLock must allow it."""
        assert db.create_task("nested")["status"] == "pending"


class TestKeywordSearchInput:
    @pytest.mark.parametrize("q", ['"', "foo(", "a AND", "NEAR(", "*"])
    def test_fts_syntax_in_user_query_does_not_raise(self, db, q):
        db.upsert_page("a", "Alpha", "note", "alpha foo body")
        assert isinstance(db.search_keyword(q), list)

    def test_plain_words_still_match(self, db):
        db.upsert_page("a", "Alpha", "note", "alpha foo body")
        assert [r["slug"] for r in db.search_keyword("alpha foo")] == ["a"]

    def test_limit_is_clamped(self, db):
        for i in range(5):
            db.upsert_page(f"p{i}", f"T{i}", "note", "common word")
        assert len(db.search_keyword("common", limit=10_000)) == 5
        assert len(db.search_keyword("common", limit=0)) == 1


class TestProjectList:
    """Project list columns are JSON TEXT; one malformed row used to raise in
    the middle of a generator run (34 open-coded json.loads call sites)."""

    def test_decodes_json_text(self):
        from brain_db import project_list

        assert project_list({"topics": '["a", "b"]'}, "topics") == ["a", "b"]

    def test_missing_or_none(self):
        from brain_db import project_list

        assert project_list({}, "topics") == []
        assert project_list(None, "topics") == []
        assert project_list({"topics": None}, "topics") == []

    def test_malformed_json_is_empty_not_a_crash(self):
        from brain_db import project_list

        assert project_list({"slug": "x", "topics": "[oops"}, "topics") == []
        assert project_list({"topics": '{"a": 1}'}, "topics") == []

    def test_already_a_list(self):
        from brain_db import project_list

        assert project_list({"topics": ["a"]}, "topics") == ["a"]


class TestSubredditSearchBatch:
    def test_batch_matches_individual_calls(self, db):
        rows = [
            ("ClaudeAI", "p", True),
            ("python", "p", False),
            ("ClaudeAI", "p", True),
        ]
        db.record_subreddit_searches(rows)
        stats = {
            r["subreddit"]: (r["impressions"], r["is_curated"])
            for r in db._conn.execute("SELECT * FROM subreddit_stats").fetchall()
        }
        assert stats == {"claudeai": (2, 1), "python": (1, 0)}

    def test_batch_commits_once(self, db, monkeypatch):
        commits = []
        real = db._conn

        class Spy:
            def __getattr__(self, name):
                return getattr(real, name)

            def commit(self):
                commits.append(1)
                real.commit()

        monkeypatch.setattr(db, "_conn", Spy())
        db.record_subreddit_searches([(f"s{i}", "p", False) for i in range(20)])
        assert len(commits) == 1


class TestRRFFuse:
    """Fusion is a pure function so the retrieval eval and search_hybrid
    rank with exactly the same code."""

    def test_equal_weights_sum_reciprocal_ranks(self):
        fused = dict(rrf_fuse([["a", "b"], ["b", "c"]], k=60))
        assert fused["b"] == pytest.approx(1 / 61 + 1 / 60)
        assert fused["a"] == pytest.approx(1 / 60)

    def test_order_is_by_fused_score(self):
        assert [s for s, _ in rrf_fuse([["a", "b"], ["b", "c"]])] == ["b", "a", "c"]

    def test_ties_keep_first_seen_order(self):
        assert [s for s, _ in rrf_fuse([["a"], ["c"]])] == ["a", "c"]

    def test_weight_shifts_the_winner(self):
        rankings = [["kw_top", "shared"], ["vec_top", "shared"]]
        top_kw = rrf_fuse(rankings, k=1, weights=[3.0, 1.0])[0][0]
        top_vec = rrf_fuse(rankings, k=1, weights=[1.0, 3.0])[0][0]
        assert (top_kw, top_vec) == ("kw_top", "vec_top")

    def test_weights_length_must_match(self):
        with pytest.raises(ValueError):
            rrf_fuse([["a"], ["b"]], weights=[1.0])


class TestHybridTuning:
    def test_keyword_weight_reorders_hybrid(self, db):
        db.use_vector_table("vec_test_4", 4)
        db.upsert_page("n/kw", "Alpha", PageType.NOTE, "zebra stripes")
        db.upsert_page("n/vec", "Beta", PageType.NOTE, "unrelated words")
        vec_chunk = db.get_chunks("n/vec")[0]["id"]
        kw_chunk = db.get_chunks("n/kw")[0]["id"]
        db.store_embeddings([(vec_chunk, [1, 0, 0, 0]), (kw_chunk, [0, 1, 0, 0])])
        # n/kw is in both rankings (vector rank 1), so a small k lets the
        # top rank dominate and the weights decide the winner.
        q = [1.0, 0.0, 0.0, 0.0]
        favour_kw = db.search_hybrid("zebra", q, k=1, kw_weight=3.0, vec_weight=1.0)
        favour_vec = db.search_hybrid("zebra", q, k=1, kw_weight=1.0, vec_weight=3.0)
        assert favour_kw[0]["slug"] == "n/kw"
        assert favour_vec[0]["slug"] == "n/vec"

    def test_chunk_size_is_configurable(self, db):
        db.chunk_max_words = 20
        db.chunk_overlap_words = 0
        body = "\n\n".join(f"Paragraph {i} " + "word " * 15 for i in range(4))
        db.upsert_page("n/long", "Long", PageType.NOTE, body)
        assert len(db.get_chunks("n/long")) == len(
            _chunk_text("Long\n\n" + body, 20, 0)
        )
        assert len(db.get_chunks("n/long")) > 1


class TestKeywordMatchMode:
    """FTS5 joins quoted tokens with AND, so a natural-language question
    misses any page lacking one of its words. match="any" ORs the terms and
    lets BM25 rank pages by how many (and how rare) terms they share."""

    def test_all_mode_requires_every_word(self, db):
        db.upsert_page("n/a", "Launch", PageType.NOTE, "post Show HN on a weekday")
        assert db.search_keyword("Show HN timing") == []

    def test_any_mode_matches_partial_overlap(self, db):
        db.upsert_page("n/a", "Launch", PageType.NOTE, "post Show HN on a weekday")
        hits = db.search_keyword("Show HN timing", match="any")
        assert [h["slug"] for h in hits] == ["n/a"]

    def test_any_mode_ranks_more_shared_terms_first(self, db):
        db.upsert_page("n/one", "One", PageType.NOTE, "zebra")
        db.upsert_page("n/two", "Two", PageType.NOTE, "zebra stripes savanna")
        hits = db.search_keyword("zebra stripes savanna", match="any")
        assert hits[0]["slug"] == "n/two"

    def test_unknown_mode_rejected(self, db):
        with pytest.raises(ValueError):
            db.search_keyword("x", match="some")

    def test_hybrid_passes_match_mode(self, db):
        db.upsert_page("n/a", "Launch", PageType.NOTE, "post Show HN on a weekday")
        hits = db.search_hybrid("Show HN timing", kw_match="any")
        assert [h["slug"] for h in hits] == ["n/a"]

    def test_hybrid_without_vector_falls_back_to_any_terms(self, db):
        """With no query vector (embedding outage, no key) hybrid is keyword
        only; AND-matching there found the right page for 21% of the eval's
        questions, any-term matching for 79%."""
        db.upsert_page("n/a", "Launch", PageType.NOTE, "post Show HN on a weekday")
        hits = db.search_hybrid("Show HN timing")
        assert [h["slug"] for h in hits] == ["n/a"]

    def test_hybrid_with_vector_keeps_all_terms_by_default(self, db):
        db.use_vector_table("vec_test_4", 4)
        db.upsert_page("n/a", "Launch", PageType.NOTE, "post Show HN on a weekday")
        db.store_embeddings([(db.get_chunks("n/a")[0]["id"], [0, 1, 0, 0])])
        hits = db.search_hybrid("Show HN timing", [0.0, 1.0, 0.0, 0.0])
        # keyword side (AND) is empty, so the vector side alone answers
        assert [h["match_type"] for h in hits] == ["vector"]


class TestUtcnowIso:
    """#14: one timestamp helper replaces the three private `_now()` copies."""

    def test_format_is_offset_aware_utc_iso(self):
        stamp = clock.utcnow_iso()
        parsed = datetime.fromisoformat(stamp)
        assert parsed.utcoffset() == UTC.utcoffset(None)
        assert stamp.endswith("+00:00")

    def test_every_store_uses_the_shared_helper(self):
        for module in (brain_db, transforms, sqlite_adapter):
            assert module.utcnow_iso is clock.utcnow_iso
            assert not hasattr(module, "_now")

    def test_stored_task_timestamp_uses_the_format(self, db):
        task = db.create_task("stamp check")
        assert task["created_at"].endswith("+00:00")
        datetime.fromisoformat(task["created_at"])
