"""Brain API router — thin wrapper over brain_db operations.

Contract-first: all logic lives in brain_db.py. This module
only handles HTTP serialization, validation, and routing.

IMPORTANT: Routes with sub-paths (timeline, links, tags, versions)
MUST be defined before the catch-all {slug:path} routes, otherwise
Starlette's path converter will greedily consume the sub-path segment.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from brain_db import BrainDB, LinkType, PageType


# ─── Request/Response Models ──────────────────────────────────────


class PageCreate(BaseModel):
    slug: str
    title: str
    page_type: str
    compiled: str = ""


class TimelineCreate(BaseModel):
    content: str
    source: str = ""


class LinkCreate(BaseModel):
    to_slug: str
    link_type: str = "related_to"


class TagsUpdate(BaseModel):
    tags: list[str]


# ─── Router Factory ───────────────────────────────────────────────


def create_brain_router(db: BrainDB) -> APIRouter:
    router = APIRouter(tags=["brain"])

    # ─── Fixed-suffix routes FIRST (before catch-all {slug:path}) ──

    # Search (no slug conflict)
    @router.get("/search")
    def search(q: str = "", mode: str = "hybrid", limit: int = 10):
        if not q.strip():
            return {"results": [], "query": q, "mode": mode, "total": 0}

        if mode == "keyword":
            results = db.search_keyword(q, limit)
            for r in results:
                r["match_type"] = "keyword"
        elif mode == "vector":
            if not db.has_embeddings():
                raise HTTPException(
                    503, "Vector search unavailable — no embedding engine"
                )
            # Vector-only requires embeddings to be pre-generated;
            # callers must supply query_vector via the internal API.
            # For HTTP, fall back to hybrid which handles missing vectors gracefully.
            results = db.search_hybrid(q, query_vector=None, limit=limit)
        elif mode == "hybrid":
            # Hybrid search: keyword + vector via RRF.
            # Without a query_vector, falls back to keyword-only automatically.
            results = db.search_hybrid(q, query_vector=None, limit=limit)
        else:
            raise HTTPException(
                400, f"Invalid search mode: {mode}. Use keyword, vector, or hybrid"
            )

        return {
            "results": results,
            "query": q,
            "mode": mode,
            "total": len(results),
        }

    # Tags listing (no slug conflict)
    @router.get("/tags")
    def list_all_tags():
        tags = db.list_all_tags()
        return {"tags": tags, "total": len(tags)}

    # Page listing (no slug conflict — matches before {slug:path})
    @router.get("/pages")
    def list_pages(page_type: str | None = None, limit: int = 50):
        pt = PageType(page_type) if page_type else None
        pages = db.list_pages(pt, limit)
        return {"pages": pages, "total": len(pages)}

    # Page create (POST, no path param conflict)
    @router.post("/pages")
    def create_or_update_page(body: PageCreate):
        try:
            pt = PageType(body.page_type)
        except ValueError:
            raise HTTPException(400, f"Invalid page_type: {body.page_type}")
        return db.upsert_page(body.slug, body.title, pt, body.compiled)

    # ─── Sub-resource routes (MUST come before catch-all) ─────────

    # Timeline
    @router.post("/pages/{slug:path}/timeline")
    def append_timeline(slug: str, body: TimelineCreate):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        return db.append_timeline(slug, body.content, body.source)

    @router.get("/pages/{slug:path}/timeline")
    def get_timeline(slug: str, limit: int = 50):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        entries = db.get_timeline(slug, limit)
        return {"entries": entries, "total": len(entries)}

    # Links
    @router.post("/pages/{slug:path}/links")
    def add_link(slug: str, body: LinkCreate):
        if not db.get_page(slug):
            raise HTTPException(404, "Source page not found")
        if not db.get_page(body.to_slug):
            raise HTTPException(404, "Target page not found")
        try:
            lt = LinkType(body.link_type)
        except ValueError:
            raise HTTPException(400, f"Invalid link_type: {body.link_type}")
        return db.add_link(slug, body.to_slug, lt)

    @router.get("/pages/{slug:path}/links")
    def get_links(slug: str):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        links = db.get_links(slug)
        return {"links": links, "total": len(links)}

    @router.delete("/pages/{slug:path}/links/{link_id}")
    def delete_link(slug: str, link_id: int):
        if not db.delete_link(link_id):
            raise HTTPException(404, "Link not found")
        return {"deleted": True}

    # Tags
    @router.post("/pages/{slug:path}/tags")
    def set_tags(slug: str, body: TagsUpdate):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        tags = db.set_tags(slug, body.tags)
        return {"tags": tags}

    @router.get("/pages/{slug:path}/tags")
    def get_tags(slug: str):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        return {"tags": db.get_tags(slug)}

    @router.delete("/pages/{slug:path}/tags/{tag_name}")
    def remove_tag(slug: str, tag_name: str):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        db.remove_tag(slug, tag_name)
        return {"tags": db.get_tags(slug)}

    # Page versions
    @router.get("/pages/{slug:path}/versions")
    def get_page_versions(slug: str):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        versions = db.get_page_versions(slug)
        return {"versions": versions, "total": len(versions)}

    # Chunks
    @router.get("/pages/{slug:path}/chunks")
    def get_chunks(slug: str):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        chunks = db.get_chunks(slug)
        return {"chunks": chunks, "total": len(chunks)}

    # Graph traversal
    @router.get("/pages/{slug:path}/graph")
    def traverse_graph(slug: str, depth: int = 2, link_type: str | None = None):
        if not db.get_page(slug):
            raise HTTPException(404, "Page not found")
        lt = None
        if link_type:
            try:
                lt = LinkType(link_type)
            except ValueError:
                raise HTTPException(400, f"Invalid link_type: {link_type}")
        nodes = db.traverse_graph(slug, depth=depth, link_type=lt)
        return {"root": slug, "depth": depth, "nodes": nodes, "total": len(nodes)}

    # ─── Catch-all slug routes LAST ──────────────────────────────

    @router.get("/pages/{slug:path}")
    def get_page(slug: str):
        page = db.get_page(slug)
        if not page:
            raise HTTPException(404, "Page not found")
        page["timeline"] = db.get_timeline(slug)
        page["tags"] = db.get_tags(slug)
        return page

    @router.delete("/pages/{slug:path}")
    def delete_page(slug: str):
        if not db.delete_page(slug):
            raise HTTPException(404, "Page not found")
        return {"deleted": True}

    return router
