"""Tests for brain API endpoints — drives brain.py router design."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from brain_db import BrainDB


@pytest.fixture
def brain_db():
    """Create a temporary BrainDB for each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test_brain.db")
        db = BrainDB(path)
        db.init_db()
        yield db


@pytest.fixture
def client(brain_db):
    """Create a TestClient with the brain router and test DB."""
    from brain import create_brain_router
    from fastapi import FastAPI

    app = FastAPI()
    router = create_brain_router(brain_db)
    app.include_router(router, prefix="/brain")
    return TestClient(app)


# ─── Page CRUD ────────────────────────────────────────────────────


class TestPageEndpoints:
    def test_create_page(self, client):
        r = client.post(
            "/brain/pages",
            json={
                "slug": "client/acme",
                "title": "Acme Corp",
                "page_type": "client",
                "compiled": "Main contact: Bob.",
            },
        )
        assert r.status_code == 200
        page = r.json()
        assert page["slug"] == "client/acme"
        assert page["title"] == "Acme Corp"
        assert page["content_hash"]

    def test_get_page_with_timeline(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "client/acme",
                "title": "Acme",
                "page_type": "client",
            },
        )
        client.post(
            "/brain/pages/client/acme/timeline",
            json={
                "content": "Onboarded client.",
                "source": "john-117",
            },
        )
        r = client.get("/brain/pages/client/acme")
        assert r.status_code == 200
        data = r.json()
        assert data["slug"] == "client/acme"
        assert len(data["timeline"]) == 1
        assert data["timeline"][0]["content"] == "Onboarded client."

    def test_get_page_not_found(self, client):
        r = client.get("/brain/pages/nonexistent")
        assert r.status_code == 404

    def test_update_page(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "note/x",
                "title": "X",
                "page_type": "note",
                "compiled": "v1",
            },
        )
        r = client.post(
            "/brain/pages",
            json={
                "slug": "note/x",
                "title": "X Updated",
                "page_type": "note",
                "compiled": "v2",
            },
        )
        assert r.status_code == 200
        assert r.json()["title"] == "X Updated"
        assert r.json()["compiled"] == "v2"

    def test_delete_page(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "note/gone",
                "title": "Gone",
                "page_type": "note",
            },
        )
        r = client.delete("/brain/pages/note/gone")
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        # Verify it's gone
        assert client.get("/brain/pages/note/gone").status_code == 404

    def test_delete_nonexistent(self, client):
        r = client.delete("/brain/pages/nope")
        assert r.status_code == 404

    def test_list_pages(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "client/a",
                "title": "A",
                "page_type": "client",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/1",
                "title": "Inc",
                "page_type": "incident",
            },
        )
        r = client.get("/brain/pages")
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_list_pages_by_type(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "client/a",
                "title": "A",
                "page_type": "client",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/1",
                "title": "Inc",
                "page_type": "incident",
            },
        )
        r = client.get("/brain/pages?page_type=client")
        assert r.json()["total"] == 1


# ─── Search ───────────────────────────────────────────────────────


class TestSearchEndpoint:
    def test_keyword_search(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/wifi",
                "title": "WiFi Outage",
                "page_type": "incident",
                "compiled": "Network down at site 2",
            },
        )
        r = client.get("/brain/search?q=WiFi&mode=keyword")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["results"][0]["slug"] == "inc/wifi"
        assert data["mode"] == "keyword"

    def test_hybrid_search_defaults(self, client):
        """Default search mode is hybrid, falls back to keyword without embeddings."""
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/wifi",
                "title": "WiFi Outage",
                "page_type": "incident",
                "compiled": "Network down at site 2",
            },
        )
        r = client.get("/brain/search?q=WiFi")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert data["mode"] == "hybrid"

    def test_search_empty_query(self, client):
        r = client.get("/brain/search?q=")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_search_no_results(self, client):
        r = client.get("/brain/search?q=nonexistent_xyz")
        assert r.json()["total"] == 0

    def test_invalid_search_mode(self, client):
        r = client.get("/brain/search?q=test&mode=invalid")
        assert r.status_code == 400


# ─── Timeline ─────────────────────────────────────────────────────


class TestTimelineEndpoints:
    def test_append_timeline(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "note/t",
                "title": "T",
                "page_type": "note",
            },
        )
        r = client.post(
            "/brain/pages/note/t/timeline",
            json={
                "content": "Something happened",
                "source": "agent-a",
            },
        )
        assert r.status_code == 200
        assert r.json()["content"] == "Something happened"

    def test_get_timeline(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "note/t",
                "title": "T",
                "page_type": "note",
            },
        )
        client.post(
            "/brain/pages/note/t/timeline",
            json={
                "content": "Event 1",
                "source": "a",
            },
        )
        client.post(
            "/brain/pages/note/t/timeline",
            json={
                "content": "Event 2",
                "source": "b",
            },
        )
        r = client.get("/brain/pages/note/t/timeline")
        assert r.status_code == 200
        assert len(r.json()["entries"]) == 2

    def test_timeline_for_missing_page(self, client):
        r = client.get("/brain/pages/nonexistent/timeline")
        assert r.status_code == 404


# ─── Links ────────────────────────────────────────────────────────


class TestLinkEndpoints:
    def test_add_link(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "client/acme",
                "title": "Acme",
                "page_type": "client",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/wifi",
                "title": "WiFi",
                "page_type": "incident",
            },
        )
        r = client.post(
            "/brain/pages/client/acme/links",
            json={
                "to_slug": "inc/wifi",
                "link_type": "incident_of",
            },
        )
        assert r.status_code == 200
        assert r.json()["link_type"] == "incident_of"

    def test_get_links(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "client/acme",
                "title": "Acme",
                "page_type": "client",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/wifi",
                "title": "WiFi",
                "page_type": "incident",
            },
        )
        client.post(
            "/brain/pages/client/acme/links",
            json={
                "to_slug": "inc/wifi",
                "link_type": "incident_of",
            },
        )
        r = client.get("/brain/pages/client/acme/links")
        assert r.status_code == 200
        assert len(r.json()["links"]) == 1

    def test_delete_link(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "client/a",
                "title": "A",
                "page_type": "client",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/b",
                "title": "B",
                "page_type": "incident",
            },
        )
        link = client.post(
            "/brain/pages/client/a/links",
            json={
                "to_slug": "inc/b",
            },
        ).json()
        r = client.delete(f"/brain/pages/client/a/links/{link['id']}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True


# ─── Tags ─────────────────────────────────────────────────────────


class TestTagEndpoints:
    def test_set_tags(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/wifi",
                "title": "WiFi",
                "page_type": "incident",
            },
        )
        r = client.post(
            "/brain/pages/inc/wifi/tags",
            json={
                "tags": ["network", "wifi"],
            },
        )
        assert r.status_code == 200
        assert set(r.json()["tags"]) == {"network", "wifi"}

    def test_get_tags(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/wifi",
                "title": "WiFi",
                "page_type": "incident",
            },
        )
        client.post(
            "/brain/pages/inc/wifi/tags",
            json={
                "tags": ["network"],
            },
        )
        r = client.get("/brain/pages/inc/wifi/tags")
        assert r.status_code == 200
        assert r.json()["tags"] == ["network"]

    def test_list_all_tags(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/a",
                "title": "A",
                "page_type": "incident",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/b",
                "title": "B",
                "page_type": "incident",
            },
        )
        client.post("/brain/pages/inc/a/tags", json={"tags": ["network", "wifi"]})
        client.post("/brain/pages/inc/b/tags", json={"tags": ["network", "hardware"]})
        r = client.get("/brain/tags")
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["tags"]}
        assert names == {"network", "wifi", "hardware"}

    def test_remove_tag(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/wifi",
                "title": "WiFi",
                "page_type": "incident",
            },
        )
        client.post("/brain/pages/inc/wifi/tags", json={"tags": ["network", "wifi"]})
        r = client.delete("/brain/pages/inc/wifi/tags/wifi")
        assert r.status_code == 200
        assert "wifi" not in r.json()["tags"]


# ─── Page Versions ────────────────────────────────────────────────


class TestVersionEndpoints:
    def test_get_versions(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "note/v",
                "title": "V",
                "page_type": "note",
                "compiled": "v1",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "note/v",
                "title": "V",
                "page_type": "note",
                "compiled": "v2",
            },
        )
        r = client.get("/brain/pages/note/v/versions")
        assert r.status_code == 200
        assert len(r.json()["versions"]) == 2
        assert r.json()["versions"][0]["compiled"] == "v2"


# ─── Chunks ──────────────────────────────────────────────────────


class TestChunkEndpoints:
    def test_get_chunks(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "note/c",
                "title": "Chunked Note",
                "page_type": "note",
                "compiled": "Some content that gets chunked automatically.",
            },
        )
        r = client.get("/brain/pages/note/c/chunks")
        assert r.status_code == 200
        assert r.json()["total"] >= 1
        assert r.json()["chunks"][0]["page_slug"] == "note/c"

    def test_chunks_missing_page(self, client):
        r = client.get("/brain/pages/nonexistent/chunks")
        assert r.status_code == 404


# ─── Graph Traversal ─────────────────────────────────────────────


class TestGraphEndpoint:
    def test_traverse_graph(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "client/acme",
                "title": "Acme",
                "page_type": "client",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "inc/wifi",
                "title": "WiFi",
                "page_type": "incident",
            },
        )
        client.post(
            "/brain/pages/client/acme/links",
            json={
                "to_slug": "inc/wifi",
                "link_type": "incident_of",
            },
        )
        r = client.get("/brain/pages/client/acme/graph?depth=1")
        assert r.status_code == 200
        data = r.json()
        assert data["root"] == "client/acme"
        assert data["total"] == 1
        assert data["nodes"][0]["slug"] == "inc/wifi"

    def test_graph_missing_page(self, client):
        r = client.get("/brain/pages/nonexistent/graph")
        assert r.status_code == 404

    def test_graph_filter_by_link_type(self, client):
        client.post(
            "/brain/pages",
            json={
                "slug": "person/bob",
                "title": "Bob",
                "page_type": "person",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "company/acme",
                "title": "Acme",
                "page_type": "company",
            },
        )
        client.post(
            "/brain/pages",
            json={
                "slug": "person/alice",
                "title": "Alice",
                "page_type": "person",
            },
        )
        client.post(
            "/brain/pages/person/bob/links",
            json={
                "to_slug": "company/acme",
                "link_type": "works_at",
            },
        )
        client.post(
            "/brain/pages/person/bob/links",
            json={
                "to_slug": "person/alice",
                "link_type": "knows",
            },
        )
        r = client.get("/brain/pages/person/bob/graph?link_type=works_at")
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["nodes"][0]["slug"] == "company/acme"


# ─── Embedding Status ────────────────────────────────────────────


class TestEmbeddingStatus:
    def test_page_includes_tags(self, client):
        """Verify get_page returns tags field."""
        client.post(
            "/brain/pages",
            json={
                "slug": "note/t",
                "title": "Tagged",
                "page_type": "note",
            },
        )
        client.post("/brain/pages/note/t/tags", json={"tags": ["test"]})
        r = client.get("/brain/pages/note/t")
        assert r.status_code == 200
        assert "tags" in r.json()
        assert "test" in r.json()["tags"]
