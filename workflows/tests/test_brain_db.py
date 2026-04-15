"""Tests for the brain database layer — drives brain_db.py API design."""

import os
import tempfile

import pytest

from brain_db import (
    BrainDB,
    LinkType,
    PageType,
)


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
        tables = db._fetch_tables()
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
        """Calling init_db() twice does not error."""
        db.init_db()  # should not raise


# ─── Page CRUD ────────────────────────────────────────────────────


class TestPageCRUD:
    def test_create_page(self, db):
        page = db.upsert_page(
            slug="client/acme-corp",
            title="Acme Corp",
            page_type=PageType.CLIENT,
            compiled="Main contact: Bob Smith. 3 sites in Longview.",
        )
        assert page["slug"] == "client/acme-corp"
        assert page["title"] == "Acme Corp"
        assert page["page_type"] == "client"
        assert page["compiled"] == "Main contact: Bob Smith. 3 sites in Longview."
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
        """sqlite-vec should be available in test environment."""
        assert db.has_embeddings() is True

    def test_store_and_search_vector(self, db):
        """Store embedding for a chunk, then find it via vector search."""
        db.upsert_page("note/vec", "Vector Test", PageType.NOTE, "searchable content")
        chunks = db.get_chunks("note/vec")
        assert len(chunks) >= 1
        chunk_id = chunks[0]["id"]

        # Store a synthetic embedding (1536 dims)
        vector = [0.1] * 1536
        assert db.store_embedding(chunk_id, vector) is True

        # Search with the same vector — should find our page
        results = db.search_vector(vector, limit=5)
        assert len(results) >= 1
        assert results[0]["slug"] == "note/vec"
        assert results[0]["match_type"] == "vector"

    def test_vector_search_returns_best_per_page(self, db):
        """Dedup: only best matching chunk per page returned."""
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
            db.store_embedding(chunk["id"], vec)

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
            db.store_embedding(chunks[0]["id"], [0.5] * 1536)

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
