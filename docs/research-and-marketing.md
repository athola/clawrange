# Research and Marketing Operator Guide

This guide covers John-117's multi-source research orchestrator
and the top-of-funnel marketing tooling that surrounds it. It is
the operator-side companion to the planning artifact in
`research-and-marketing-plan.md`.

## TL;DR

```bash
# Run a research session and persist it
curl -s -X POST http://localhost:5678/research \
  -H 'content-type: application/json' \
  -d '{"topic":"agent platforms shipping in 2026"}' | jq .

# Recall recent sessions
curl -s http://localhost:5678/research/sessions | jq .

# Schedule daily content idea passes (uses last 5 sessions)
curl -s -X POST http://localhost:5678/sched \
  -H 'content-type: application/json' \
  -d '{"id":"daily-content","name":"Daily content ideas",
       "kind":"content_idea","cron":"0 8 * * *"}'

# Queue a comment draft for a specific Reddit post
curl -s -X POST http://localhost:5678/sched/draft-once/run -d '{}'
```

## Research Orchestrator

`POST /research` runs a multi-source research session and returns
ranked, deduplicated, citation-bearing findings.

### Channels

| Channel | Source | Notes |
|---------|--------|-------|
| `discourse` | Reddit subreddits | Default subs: ClaudeAI, LocalLLaMA, SideProject. Override with `subreddits`. |
| `code` | GitHub repos | Default `min_stars=50`. |
| `discourse_web` (or `web`) | GLM server-side web search | Returns one synthesized summary finding per call. |

### Request body

```json
{
  "topic": "agent platforms shipping in 2026",
  "channels": ["discourse", "code", "web"],
  "subreddits": ["ClaudeAI", "LocalLLaMA"],
  "limit": 10,
  "since": "30d",
  "min_stars": 50
}
```

### Response shape

```json
{
  "topic": "...",
  "channels": ["discourse", "code", "discourse_web"],
  "findings": [
    {
      "source": "github",
      "channel": "code",
      "title": "owner/repo",
      "url": "https://github.com/...",
      "relevance": 0.85,
      "summary": "...",
      "metadata": {"stars": 4200},
      "confidence": "medium"
    }
  ],
  "errors": {"discourse": "reddit creds not configured"},
  "total": 12,
  "session_id": "a1b2c3d4e5f6"
}
```

### Synthesis details

Findings are processed in this order:

1. **URL deduplication**: same URL collapses to the highest-
   relevance entry.
2. **Authority bonus**: GitHub stars (>1000 → +0.1, >5000 → +0.2),
   HN score (>100 → +0.1, >500 → +0.2), arXiv citations (>50 → +0.1,
   >200 → +0.2), Reddit upvotes (>50 → +0.05, >200 → +0.1).
3. **Recency bonus**: metadata.year within 2 calendar years → +0.05.
4. **Triangulation bonus**: each additional channel that contains
   a finding with Jaccard ≥ 0.6 to this title adds +0.05, capped at
   +0.15.
5. **Confidence flag**: triangulation bonus ≥ 0.10 → "high",
   ≥ 0.05 → "medium", else "low" (single-source / needs verification).
6. **Score cap**: composite relevance is capped at 1.0.

### Persisting and recalling sessions

Every `/research` call persists the session in the `research_sessions`
brain table. Each finding lives in `research_findings`. List recent
sessions:

```bash
curl -s http://localhost:5678/research/sessions?limit=10 | jq .
```

Get a single session with all findings:

```bash
curl -s http://localhost:5678/research/sessions/<id> | jq .
```

The `session_id` is short (12 chars) and is included in every
`/research` response. John-117 uses this to recall earlier work
without re-running the fanout.

### Heavy research via tome

For academic literature reviews, TRIZ analogical reasoning, or
deep multi-hop digs, John-117 enqueues a task tagged
`research:tome` with the topic. Alex's Claude Code session picks
these up and runs `/tome:research`, posting the synthesized output
back via `/task/{id}/result`.

## Marketing Orchestrator (extended)

The four canonical projects are seeded on first boot of the
workflows service:

| Slug | Purpose | Key subreddits |
|------|---------|----------------|
| `claude-night-market` | Plugin marketplace for Claude Code | ClaudeAI, LocalLLaMA, SideProject |
| `skrills` | Trade-skill capture chrome extension | Construction, ITCareerQuestions, SideProject |
| `simple-resume` | YAML → PDF/HTML resume generator | resumes, cscareerquestions, SideProject |
| `personal-brand` | Alex's AI-systems engineer voice | ClaudeAI, LocalLLaMA, MachineLearning, ExperiencedDevs |

### Generators

| Name | Cron suggestion | What it does |
|------|-----------------|--------------|
| `morning_scan` | `0 8 * * *` | Reddit + GitHub scan tasks per project |
| `weekly_traffic` | `0 8 * * 1` | Stargazer / clone deltas per repo |
| `awesome_lists_watch` | `0 10 * * 3` | PR-target reminders for awesome-lists |
| `custom_scan` | (ad-hoc) | Generic single-topic task emitter |
| `content_idea` | `0 9 * * *` | Turns recent research into 1 idea/project |
| `comment_draft` | (ad-hoc) | Drafts a reply for a specific URL |

### Posture

The marketing posture is encoded in each project's `posture` field
and reinforced in `openclaw/soul.md`. Core rules:

1. **Lurk first, comment later** — be a community member before a
   marketer.
2. **Useful comments first** — lead with specifics, code, numbers.
3. **Honest disclaimers beat polish** on r/SideProject and
   r/programming.
4. **Never auto-post** — every draft lands as a `[DRAFT]` task and
   Alex sends manually after review.
5. **HN for depth, Reddit for breadth** — converts ~3x better,
   reaches ~1/3 the audience.
6. **r/SideProject format**: `[Launch] Name — one-liner ≤100ch`
   + opening + journey + 3-5 features + ending question.
7. **External article links boost X reach in 2026** —
   Medium/dev.to/Substack/personal blog.

### Operator workflow

```text
morning_scan ──► task queue ──► John-117 reviews → enqueues
                                                   comment_draft
                                                          │
                                                          ▼
                                                  [DRAFT] task
                                                          │
                                                  Alex reviews
                                                          │
                                                          ▼
                                                  Manual post
```

## Acceptance evidence

The implementation is covered by 425 unit tests under
`workflows/tests/` (see `test_research.py`,
`test_brain_db_research.py`, `test_marketing.py`,
`test_app.py::TestResearchEndpoint`). Run them with:

```bash
make test-unit
```

For service-level smoke tests, hit `/healthz` and `/research`
directly:

```bash
make health
curl -s -X POST http://localhost:5678/research \
  -H 'content-type: application/json' \
  -d '{"topic":"hello world"}' | jq .
```
