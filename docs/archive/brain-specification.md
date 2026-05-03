# ClawRange Brain — Specification

## Overview

Add persistent, searchable knowledge management to the ClawRange workflows service. Agents read context before responding and write updates after acting, building institutional memory over time.

## Data Model

### Pages

The core knowledge unit. Each page represents an entity with compiled truth and a timeline of events.

```
pages
├── slug          TEXT PRIMARY KEY   -- hierarchical ID: "client/acme-corp"
├── title         TEXT NOT NULL      -- display name
├── page_type     TEXT NOT NULL      -- client | system | incident | decision | note | person | company | project
├── compiled      TEXT DEFAULT ''    -- distilled current truth (rewritable)
├── content_hash  TEXT NOT NULL      -- SHA-256 of title+compiled for idempotent upserts
├── created_at    TEXT NOT NULL      -- ISO 8601
└── updated_at    TEXT NOT NULL      -- ISO 8601
```

**FTS5 virtual table** indexes `title`, `compiled` for keyword search.

### Timeline

Append-only event log per page. Never edited, never deleted.

```
timeline
├── id          INTEGER PRIMARY KEY AUTOINCREMENT
├── page_slug   TEXT NOT NULL REFERENCES pages(slug)
├── content     TEXT NOT NULL      -- what happened
├── source      TEXT DEFAULT ''    -- agent ID: "john-117", "max-ops", "alex"
├── created_at  TEXT NOT NULL      -- ISO 8601
```

### Content Chunks

Chunked segments of page content for fine-grained embedding and search (GBrain-inspired).

```
content_chunks
├── id            INTEGER PRIMARY KEY AUTOINCREMENT
├── page_slug     TEXT NOT NULL REFERENCES pages(slug) ON DELETE CASCADE
├── chunk_index   INTEGER NOT NULL  -- 0-based ordering within page
├── content       TEXT NOT NULL      -- chunk text (≤300 words)
├── content_hash  TEXT NOT NULL      -- SHA-256 for idempotent upserts
├── created_at    TEXT NOT NULL
├── UNIQUE(page_slug, chunk_index)
```

Pages are automatically chunked on create/update using recursive chunking with 50-word overlap.

### Embeddings

Vector representations per content chunk for semantic search.

```
embeddings
├── chunk_id    INTEGER PRIMARY KEY REFERENCES content_chunks(id) ON DELETE CASCADE
├── vector      FLOAT[1536]       -- text-embedding-3-small output
└── updated_at  TEXT NOT NULL
```

Uses sqlite-vec for approximate nearest neighbor search.

### Links (Knowledge Graph)

Typed cross-references between pages — inspired by gbrain's link graph.

```
links
├── id          INTEGER PRIMARY KEY AUTOINCREMENT
├── from_slug   TEXT NOT NULL REFERENCES pages(slug)
├── to_slug     TEXT NOT NULL REFERENCES pages(slug)
├── link_type   TEXT NOT NULL DEFAULT 'related_to'  -- references|parent_of|incident_of|related_to|works_at|knows
├── created_at  TEXT NOT NULL
├── UNIQUE(from_slug, to_slug, link_type)
```

### Tags

Flexible categorization across pages.

```
tags
├── id          INTEGER PRIMARY KEY AUTOINCREMENT
├── name        TEXT NOT NULL UNIQUE

page_tags
├── page_slug   TEXT NOT NULL REFERENCES pages(slug)
├── tag_id      INTEGER NOT NULL REFERENCES tags(id)
├── UNIQUE(page_slug, tag_id)
```

### Page Versions

Snapshot history when compiled truth changes — inspired by gbrain's version tracking.

```
page_versions
├── id          INTEGER PRIMARY KEY AUTOINCREMENT
├── page_slug   TEXT NOT NULL REFERENCES pages(slug)
├── compiled    TEXT NOT NULL      -- snapshot of compiled at this version
├── content_hash TEXT NOT NULL     -- SHA-256 hash for idempotent detection
├── created_at  TEXT NOT NULL
```

### Content Hashing

SHA-256 hash of `title + compiled` stored on pages for idempotent upserts — inspired by gbrain's content hashing pattern.

```
pages (additional column)
├── content_hash TEXT NOT NULL     -- SHA-256 of title+compiled
```

Upsert behavior: if content hash matches existing page, skip update (no-op). This avoids unnecessary embedding regeneration.

### Tasks (Persistent Queue)

Replaces the in-memory `_task_queue` list.

```
tasks
├── id          TEXT PRIMARY KEY   -- 8-char UUID prefix
├── description TEXT NOT NULL
├── priority    INTEGER DEFAULT 3  -- 1=urgent, 5=low
├── status      TEXT DEFAULT 'pending'  -- pending|active|completed|failed|cancelled
├── result      TEXT               -- completion notes
├── created_at  TEXT NOT NULL
└── completed_at TEXT
```

## API Endpoints

### Brain CRUD

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/brain/pages` | Create or update a page |
| `GET` | `/brain/pages/{slug}` | Get page with timeline |
| `DELETE` | `/brain/pages/{slug}` | Delete page and its timeline |
| `GET` | `/brain/pages` | List pages (filterable by type) |

### Brain Search

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/brain/search?q=...` | Hybrid search (keyword + vector) |
| `GET` | `/brain/search?q=...&mode=keyword` | Keyword-only search |
| `GET` | `/brain/search?q=...&mode=vector` | Vector-only search |

### Timeline

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/brain/pages/{slug}/timeline` | Append timeline entry |
| `GET` | `/brain/pages/{slug}/timeline` | List timeline entries |

### Links

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/brain/pages/{slug}/links` | Add a link to another page |
| `GET` | `/brain/pages/{slug}/links` | List links from/to a page |
| `DELETE` | `/brain/pages/{slug}/links/{link_id}` | Remove a link |
| `GET` | `/brain/pages/{slug}/graph?depth=3&link_type=knows` | Traverse knowledge graph from page |

### Tags

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/brain/pages/{slug}/tags` | Add tags to a page |
| `GET` | `/brain/pages/{slug}/tags` | Get tags for a page |
| `GET` | `/brain/tags` | List all tags with counts |
| `DELETE` | `/brain/pages/{slug}/tags/{tag}` | Remove a tag from a page |

### Page Versions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/brain/pages/{slug}/versions` | List version history |

### Task Queue (Unchanged API, Persistent Backend)

All existing `/task/*` endpoints remain identical. Backend changes from in-memory list to SQLite.

## Request/Response Schemas

### Create/Update Page

```json
POST /brain/pages
{
  "slug": "client/longview-home-center",
  "title": "Longview Home Center",
  "page_type": "client",
  "compiled": "Main contact: Bob Smith. 3 retail sites in Longview, TX."
}
```

Response: the created/updated page object with timestamps.

Upsert behavior: if slug exists, update title/compiled/updated_at. If not, create.

### Get Page

```json
GET /brain/pages/client/longview-home-center

{
  "slug": "client/longview-home-center",
  "title": "Longview Home Center",
  "page_type": "client",
  "compiled": "Main contact: Bob Smith. 3 retail sites in Longview, TX.",
  "created_at": "2026-04-10T12:00:00Z",
  "updated_at": "2026-04-10T14:30:00Z",
  "timeline": [
    {
      "id": 1,
      "content": "Initial client onboarding. Bob Smith is primary contact.",
      "source": "john-117",
      "created_at": "2026-04-10T12:00:00Z"
    }
  ]
}
```

### Search

```json
GET /brain/search?q=wifi+issues&limit=5

{
  "results": [
    {
      "slug": "incident/lhc-wifi-site2",
      "title": "LHC WiFi Outage - Site 2",
      "page_type": "incident",
      "snippet": "...intermittent WiFi drops on site 2...",
      "score": 0.87,
      "match_type": "hybrid"
    }
  ],
  "query": "wifi issues",
  "mode": "hybrid",
  "total": 1
}
```

### Append Timeline

```json
POST /brain/pages/client/longview-home-center/timeline
{
  "content": "Reported intermittent WiFi drops on site 2. Created incident page.",
  "source": "john-117"
}
```

## Search Algorithm

### Multi-Query Expansion (GBrain-inspired)

Before searching, the system rephrases the user's query into 2-3 alternative phrasings using the LLM proxy. This improves recall by catching synonyms and different phrasings of the same concept.

```
User Query: "network problems at the store"
  → Expansion: ["network issues retail location", "connectivity outage shop", "WiFi problems storefront"]
    → Search each expansion → merge results → dedup → rank
```

- Uses the LLM proxy (cheapest available tier) for expansion
- Falls back to single-query if LLM unavailable
- Expansion prompt: "Rephrase this query 2-3 different ways for search: {query}"

### Content Chunking (GBrain-inspired)

Pages are chunked before embedding using recursive chunking with a 5-level delimiter hierarchy:

1. Split on `##` headers
2. Split on `###` headers
3. Split on paragraph breaks (double newline)
4. Split on sentence boundaries
5. Split on character boundaries

Each chunk is ≤300 words with 50-word overlap for context preservation. Chunks reference their parent page slug for result attribution.

```
content_chunks
├── id          INTEGER PRIMARY KEY AUTOINCREMENT
├── page_slug   TEXT NOT NULL REFERENCES pages(slug) ON DELETE CASCADE
├── chunk_index INTEGER NOT NULL  -- 0-based ordering
├── content     TEXT NOT NULL      -- chunk text
├── content_hash TEXT NOT NULL     -- SHA-256 for idempotent upserts
├── UNIQUE(page_slug, chunk_index)
```

Embeddings are generated per-chunk, not per-page, enabling fine-grained semantic matching.

### Hybrid Search (Default)

1. **Multi-query expansion** — rephrase query into 2-3 alternatives via LLM proxy
2. Run FTS5 keyword query (all expansions) → ranked results with BM25 scores
3. Generate embedding for each expanded query via OpenRouter → cosine similarity search via sqlite-vec
4. Merge using Reciprocal Rank Fusion (RRF): `score = Σ 1/(k + rank_i)` with k=60
5. Apply 4-layer dedup (GBrain pattern):
   - Best chunk per page (deduplicate across chunks/embeddings)
   - Cosine similarity threshold (>0.85 removes near-duplicates)
   - Type diversity (max 60% of results from any one page_type)
   - Page cap (max 3 results from same page in hybrid mode)
6. Return top N results sorted by fused score

### Knowledge Graph Traversal

The links table supports recursive graph traversal up to depth 5, enabling queries like "find all incidents related to this client" or "who works at companies that this person knows."

- Traversal uses recursive CTE: `WITH RECURSIVE ...`
- Link types constrain traversal (e.g., only follow `works_at` and `knows` edges)
- Results include path information (the chain of links followed)

### Fallback Behavior

- If embedding API is unavailable → fall back to keyword-only search
- If no FTS5 matches → return vector-only results
- If query is empty → return recent pages sorted by updated_at
- If multi-query expansion fails → fall back to single query

## Embedding Pipeline

- **Model**: `text-embedding-3-small` via OpenRouter (1536 dimensions)
- **When**: On page create/update (async, non-blocking)
- **Input**: Concatenation of `title + " " + compiled`
- **Retry**: On failure, mark embedding as stale; retry on next search miss
- **Cost**: ~$0.02 per 1M tokens ($0.00002 per page)

## Database Location

SQLite database file: `/data/brain.db` (bind-mounted from `./data/brain/` on host)

Volume mount in docker-compose.yml:
```yaml
volumes:
  - ./data/brain:/data
```

## Agent Integration

### John-117 (soul.md additions)

Add to "What You Can Do":
- Remember and recall information about clients, systems, and incidents
- Search your brain before answering questions about past events
- Write updates after completing tasks or learning new information

Add brain commands:
- `!remember <slug> <info>` — append to a page's timeline
- `!recall <query>` — search the brain
- `!page <slug>` — show a full page with history

### Max Ops (soul-ops.md additions)

Add to heartbeat checklist:
- After completing a task, write the result to the brain
- Log infrastructure events (tier transitions, balance alerts) to brain timeline

## User Stories

### US-1: Agent Recalls Past Context
**As** John-117, **when** Alex asks about a client, **I** search the brain first and include relevant history in my response.

**Acceptance criteria**:
- Search returns relevant pages within 500ms
- Results include both keyword and semantic matches
- Agent includes brain context in response naturally

### US-2: Agent Records New Information
**As** John-117, **when** I learn something new about a client or complete a task, **I** create or update the relevant brain page and append a timeline entry.

**Acceptance criteria**:
- Page created with correct slug and type
- Timeline entry includes source agent ID
- Embedding generated asynchronously

### US-3: Infrastructure Events Persist
**As** Max Ops, **when** I detect a tier transition or balance alert, **I** log it to the brain so patterns can be identified later.

**Acceptance criteria**:
- Events logged with timestamp and source
- Searchable by incident type
- No duplicate entries for the same event

### US-4: Task Queue Survives Restarts
**As** a user, **when** the workflows container restarts, **I** see all my pending and completed tasks intact.

**Acceptance criteria**:
- All task CRUD operations write to SQLite
- Existing `/task/*` API contract unchanged
- Task ordering preserved after restart

### US-5: Hybrid Search Finds Relevant Results
**As** an agent, **when** I search for "network problems at the store," **I** find the incident page titled "LHC WiFi Outage - Site 2" even though the exact words don't match.

**Acceptance criteria**:
- Semantic search finds conceptually related pages
- Keyword search finds exact term matches
- RRF fusion ranks results meaningfully
- Graceful fallback when embedding API unavailable

## Non-Functional Requirements

- **Latency**: Search < 500ms, page CRUD < 100ms
- **Storage**: SQLite DB expected < 50MB for first year of testbed use
- **Memory**: sqlite-vec adds ~10-15MB to container footprint
- **Availability**: Brain operations should not block LLM proxy or task queue on failure
- **Isolation**: Brain module failures must not crash the workflows service
