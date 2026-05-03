# ClawRange Brain — Project Brief

## Problem Statement

ClawRange's AI agents (John-117, Max Ops) have no persistent memory. Every session starts from zero — tasks completed, infrastructure events observed, client interactions, and decisions made are all lost on container restart. For an MSP business, this prevents institutional knowledge from compounding over time.

Current state: in-memory task queue (`list[dict]`) in the workflows service, lost on restart. No searchable history, no entity tracking, no decision log.

## Goals

1. Give agents persistent, searchable memory that survives container restarts
2. Enable semantic search (find by meaning, not just keywords)
3. Track entities (clients, systems, incidents) with timeline history
4. Implement the agent-brain loop: read context before responding, write updates after acting
5. Persist the task queue across restarts

## Success Criteria

- [ ] Knowledge pages persist across container restarts
- [ ] Agents can search by keyword AND by semantic meaning
- [ ] Entity pages accumulate timeline entries over time
- [ ] Task queue survives restarts without data loss
- [ ] All brain operations are covered by tests
- [ ] No new Docker services required (fits in existing workflows container)
- [ ] Memory usage stays under 128M container limit

## Constraints

- **Technical**: SQLite + sqlite-vec within existing 128M workflows container
- **Filesystem**: Read-only container; SQLite database on bind-mounted volume
- **LLM**: Embeddings via OpenRouter (text-embedding-3-small or equivalent)
- **Integration**: New `/brain/*` endpoints on existing FastAPI service
- **Cost**: Embedding costs negligible (~$1-2/month at testbed scale)

## Selected Approach

**SQLite + Embeddings Brain (Approach 2)**

Hybrid search combining SQLite FTS5 (keyword) and sqlite-vec (vector similarity). Knowledge stored as pages with compiled truth + append-only timeline, inspired by gbrain's data model. Embeddings generated via OpenRouter on write.

### Why This Approach

- Fits within existing container constraints (no new services)
- Hybrid search gives semantic discovery without PostgreSQL overhead
- Clean upgrade path to PostgreSQL + pgvector if needed at scale
- Minimal new dependencies (sqlite-vec, no Bun/Node required)

## Approaches Considered

### Approach 1: SQLite FTS5 Only — Rejected
Keyword search only. No semantic discovery. Too limited for knowledge management.

### Approach 3: Separate PostgreSQL + pgvector — Deferred
Production-grade but adds operational complexity. Good upgrade target once brain proves value.

### Approach 4: gbrain MCP Server — Deferred
Full gbrain feature set but requires Supabase ($25/month) and Bun runtime. Consider when scaling to multiple client sites.

## Trade-offs Accepted

- **sqlite-vec maturity**: Less battle-tested than pgvector, acceptable for testbed
- **Embedding costs**: Small but non-zero; OpenRouter pass-through pricing
- **Single-node**: No replication; acceptable for testbed, not production

## Out of Scope

- Multi-user/multi-tenant knowledge isolation
- Graph traversal (gbrain's traverse-graph)
- File/binary storage (gbrain's S3 mirror)
- Dream cycle / autonomous consolidation
- MCP server protocol (future upgrade path)

## Architecture Sketch

```
OpenClaw Agents ──web_fetch──▶ FastAPI Workflows Service
                                  │
                                  ├── /task/*     (task queue — now persistent)
                                  ├── /brain/*    (knowledge CRUD + search)
                                  ├── /v1/*       (LLM proxy, unchanged)
                                  └── /healthz
                                  │
                              SQLite DB (bind-mounted volume)
                                  ├── pages table (FTS5 index)
                                  ├── timeline table
                                  ├── embeddings table (sqlite-vec)
                                  └── tasks table (persistent queue)
```

## Next Steps

1. Specify — define API contracts, data model, and acceptance criteria
2. Plan — dependency-ordered implementation tasks
3. Execute — implement with TDD methodology
