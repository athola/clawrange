# ClawRange Brain — Implementation Plan

## Architecture

```
workflows/
├── app.py              # modify: import brain router, replace in-memory task queue
├── brain.py            # NEW: brain module (pages, timeline, search, embeddings)
├── brain_db.py         # NEW: SQLite + FTS5 + sqlite-vec database layer
├── llm_proxy.py        # unchanged
├── telegram.py         # unchanged
├── models.json         # unchanged
├── requirements.txt    # modify: add sqlite-vec
├── Dockerfile          # modify: install sqlite-vec system deps
└── tests/
    ├── test_brain_db.py    # NEW: database layer tests
    ├── test_brain.py       # NEW: brain API endpoint tests
    ├── test_app.py         # modify: update task tests for SQLite backend
    ├── test_llm_proxy.py   # unchanged
    └── test_telegram.py    # unchanged

docker-compose.yml          # modify: add brain volume mount
openclaw/soul.md            # modify: add brain commands
openclaw/soul-ops.md        # modify: add brain logging to heartbeat
openclaw/HEARTBEAT.md       # modify: add brain health check
```

## Phases

### Phase A: Database Layer (brain_db.py + tests)

Foundation — SQLite schema, connections, and basic CRUD. No API yet.

**Tasks:**

1. **A1: Create brain_db.py with schema initialization**
   - SQLite connection management (thread-safe)
   - `init_db()` creates pages, timeline, tasks tables
   - FTS5 virtual table for pages
   - Risk: LOW | Est: 30 min

2. **A2: Write test_brain_db.py — schema and page CRUD**
   - Test DB initialization creates all tables
   - Test page create, read, update, delete
   - Test upsert behavior (create if missing, update if exists)
   - Test page listing with type filter
   - Risk: LOW | Est: 20 min

3. **A3: Implement page CRUD in brain_db.py**
   - `create_or_update_page(slug, title, page_type, compiled)`
   - `get_page(slug)` — returns page dict or None
   - `delete_page(slug)` — cascades to timeline and embeddings
   - `list_pages(page_type=None, limit=50)`
   - Risk: LOW | Est: 20 min

4. **A4: Add timeline operations**
   - `append_timeline(page_slug, content, source)`
   - `get_timeline(page_slug, limit=50)`
   - Tests for append, retrieval, ordering
   - Risk: LOW | Est: 15 min

5. **A5: Add FTS5 keyword search**
   - `search_keyword(query, limit=10)` — BM25-ranked results
   - FTS5 triggers to keep index in sync with pages table
   - Tests for keyword matching, ranking, empty results
   - Risk: LOW | Est: 20 min

6. **A6: Migrate task queue to SQLite**
   - `create_task()`, `get_task()`, `list_tasks()`, `claim_task()`, `complete_task()`, `cancel_task()`
   - Same behavior as in-memory queue, persistent backend
   - Tests verify identical API behavior
   - Risk: MEDIUM (behavioral parity) | Est: 30 min

**Phase A exit**: All database tests pass. No API or embeddings yet.

### Phase B: Brain API Endpoints (brain.py + tests)

FastAPI router with brain CRUD and keyword search.

**Tasks:**

7. **B1: Create brain.py FastAPI router**
   - Pydantic models: PageCreate, TimelineCreate, SearchResult
   - `POST /brain/pages` — create/update page
   - `GET /brain/pages/{slug:path}` — get page with timeline
   - `DELETE /brain/pages/{slug:path}` — delete page
   - `GET /brain/pages` — list pages
   - Risk: LOW | Est: 25 min

8. **B2: Add search endpoint**
   - `GET /brain/search?q=...&mode=keyword&limit=10`
   - Keyword mode only at this stage (vector comes in Phase C)
   - Risk: LOW | Est: 15 min

9. **B3: Add timeline endpoint**
   - `POST /brain/pages/{slug:path}/timeline`
   - `GET /brain/pages/{slug:path}/timeline`
   - Risk: LOW | Est: 10 min

10. **B4: Write test_brain.py — API tests**
    - Test all CRUD endpoints with TestClient
    - Test search with various queries
    - Test timeline append and retrieval
    - Test error cases (404, validation)
    - Risk: LOW | Est: 25 min

11. **B5: Integrate brain router into app.py**
    - Import and include brain router
    - Replace in-memory task queue with brain_db calls
    - Update existing task endpoint implementations
    - Risk: MEDIUM (must not break existing tests) | Est: 20 min

12. **B6: Update test_app.py for persistent tasks**
    - Adjust fixtures to use temp SQLite DB
    - Verify all existing task tests still pass
    - Add test for task persistence across "restarts"
    - Risk: MEDIUM | Est: 20 min

**Phase B exit**: All brain and task API tests pass. Keyword search works. No embeddings yet.

### Phase C: Vector Search (embeddings + hybrid)

Add sqlite-vec and embedding pipeline for semantic search.

**Tasks:**

13. **C1: Add sqlite-vec to requirements and Dockerfile**
    - `sqlite-vec` Python package
    - Update Dockerfile if system deps needed
    - Verify import works in container
    - Risk: MEDIUM (dependency compatibility) | Est: 15 min

14. **C2: Add embedding storage to brain_db.py**
    - sqlite-vec virtual table for vector storage
    - `store_embedding(page_slug, vector)`
    - `search_vector(query_vector, limit=10)` — cosine similarity
    - Tests with synthetic vectors
    - Risk: MEDIUM (sqlite-vec API) | Est: 30 min

15. **C3: Add embedding generation via OpenRouter**
    - `generate_embedding(text)` — calls OpenRouter embedding API
    - Async, non-blocking — fire after page create/update
    - Graceful failure (log warning, mark stale)
    - Risk: MEDIUM (external API) | Est: 25 min

16. **C4: Implement hybrid search with RRF**
    - Run keyword + vector search in parallel
    - Merge with RRF formula: `score = Σ 1/(60 + rank)`
    - Fallback: keyword-only if embeddings unavailable
    - Update search endpoint to support `mode=hybrid|keyword|vector`
    - Risk: MEDIUM (algorithm correctness) | Est: 30 min

17. **C5: Test hybrid search**
    - Test with mocked embeddings (deterministic)
    - Test RRF score calculation
    - Test fallback when embedding API fails
    - Test mode parameter filtering
    - Risk: LOW | Est: 20 min

**Phase C exit**: Hybrid search works. Embeddings generated on write. Fallback to keyword-only on failure.

### Phase D: Integration (docker-compose + agent personas)

Wire everything together for end-to-end operation.

**Tasks:**

18. **D1: Update docker-compose.yml**
    - Add brain volume mount: `./data/brain:/data`
    - Add `OPENROUTER_EMBEDDING_MODEL` env var
    - Risk: LOW | Est: 10 min

19. **D2: Update soul.md with brain commands**
    - Add brain awareness to John-117's capabilities
    - Add `!remember`, `!recall`, `!page` commands
    - Add "search brain before answering" instruction
    - Risk: LOW | Est: 15 min

20. **D3: Update soul-ops.md and HEARTBEAT.md**
    - Add brain logging to Max Ops heartbeat
    - Log infrastructure events to brain timeline
    - Add brain DB health to heartbeat checklist
    - Risk: LOW | Est: 10 min

21. **D4: Add brain health to /healthz**
    - Check SQLite DB is readable
    - Report page count and DB size
    - Risk: LOW | Est: 10 min

22. **D5: End-to-end validation**
    - Run full test suite
    - Docker build and startup test
    - Manual smoke test of brain CRUD via curl
    - Risk: LOW | Est: 15 min

**Phase D exit**: Full integration working. All tests pass. Docker builds cleanly.

## Dependency Graph

```
A1 ──▶ A2 ──▶ A3 ──▶ A4 ──▶ A5 ──▶ A6
                                      │
                                      ▼
                              B1 ──▶ B2 ──▶ B3 ──▶ B4
                                                    │
                                                    ▼
                                            B5 ──▶ B6
                                                    │
                                                    ▼
                                    C1 ──▶ C2 ──▶ C3 ──▶ C4 ──▶ C5
                                                                  │
                                                                  ▼
                                                    D1 ──▶ D2 ──▶ D3 ──▶ D4 ──▶ D5
```

## Parallel Opportunities

- A2 (tests) can be written while A1 (schema) is being implemented
- B4 (API tests) can be written while B1-B3 (endpoints) are built
- D2/D3 (persona updates) are independent of D1 (docker-compose)

## Risk Summary

| Risk | Mitigation |
|------|-----------|
| sqlite-vec compatibility | Test import early in Phase C; fallback to keyword-only |
| Embedding API costs | text-embedding-3-small is cheapest; monitor via balance check |
| Memory pressure | sqlite-vec uses ~10-15MB; well within 128M limit |
| Task queue migration | Keep in-memory as fallback; feature flag if needed |
| Breaking existing tests | Run full suite after each phase |
