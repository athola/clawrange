# ClawRange Marketing Orchestrator — Project Brief

## Problem Statement

The current ClawRange task system is purely *reactive*. The 5-minute heartbeat processes pending tasks from the queue, but **nothing generates marketing-research tasks on a schedule**. The OpenClaw `cron` tool is allow-listed for max-ops but never wired up. As a result:

- No automated daily/weekly Reddit, HN, or GitHub scans for Alex's three target projects
- No way to say "every Monday 9am, look for new awesome-list opportunities"
- `!task` (via Telegram) works for ad-hoc work, but Alex has to remember to ask
- Web search uses GLM's generic `web_search` tool — no targeted Reddit JSON, no GitHub Search API, no project context
- John-117 has no concept of *which* repos he's helping market

The cost of this gap: marketing momentum stalls. Awesome-list PRs go unmade. Reddit threads where the projects could be relevant are missed. Stargazer changes go unnoticed.

## Goals

1. Add a real cron-style scheduler that survives container restarts
2. Generate scheduled marketing-research tasks for three named projects (`claude-night-market`, `skrills`, `simple-resume`)
3. Use targeted Reddit + GitHub Search APIs (not just generic web search)
4. Let Alex add/pause/remove schedules from Telegram on the fly
5. Format multi-result outputs cleanly for mobile Telegram reading
6. Surface the *anti-pattern* findings so Alex doesn't accidentally hurt engagement (don't lead with "built with Claude")

## Success Criteria

- [ ] Schedules persist across `docker compose restart` with no data loss
- [ ] At least one scheduled job (daily Reddit + GitHub scan per project) generates tasks automatically without manual trigger
- [ ] Telegram `/sched add ...` and `/scan reddit <topic>` commands work end-to-end
- [ ] Reddit scans return real post URLs, scores, subreddits — no hallucinated results
- [ ] GitHub scans cover both adjacent repos (marketing leads) and self-repo traffic (analytics)
- [ ] Backwards compatible: `!task` and existing endpoints still work
- [ ] Test coverage on new code ≥85%
- [ ] Docker container memory stays under 128M ceiling

## Constraints

- **Single workflows container** — no Redis, RabbitMQ, or extra services
- **SQLite-only persistence** — schedules live in the existing brain.db
- **Single uvicorn worker** — APScheduler 4 single-process mode (no event broker)
- **OpenRouter-first**, but Reddit/GitHub APIs called directly with their own credentials
- **One user (Alex)** — no multi-tenant, no permissions model
- **128MB RAM ceiling** — pick lightweight async libraries
- **Branch budget waived** — user has explicitly opted out of scope-guard for this work

## Selected Approach

**APScheduler 4 AsyncScheduler with SQLAlchemyDataStore + Reddit (asyncpraw) + GitHub (githubkit) + project registry**

Embed APScheduler 4's `AsyncScheduler` in the FastAPI lifespan, persisting jobs to the existing SQLite database via `SQLAlchemyDataStore`. Add two thin client wrappers (`reddit_search.py`, `github_search.py`) that call the official APIs with credentials from environment variables. A new `projects` table holds tracked GitHub repos with associated topics, subreddits, and search queries. Schedule generators read from `projects` and emit tasks into the existing task queue. Telegram command parser is extended with `/sched` and `/scan` subcommands using `shlex` parsing and HTML-formatted multi-result output.

### Why This Approach

- Adds zero new containers (everything embeds in workflows service)
- Reuses existing task queue (heartbeat already drains it; no new worker process)
- Schedules are SQLite rows — same `make backup` flow protects them
- Reddit/GitHub APIs return *real* data with citations, not LLM hallucinations
- Telegram grammar mirrors industry norm (`/cmd subcmd args`), keeps `!` as 30-day alias
- Project registry decouples "what to scan for" from "when to scan" — Alex can add a 4th project later by inserting a row, no code change

## Approaches Considered

### Approach A: External cron container — Rejected
Extra container, eats the 128MB ceiling, hard to add jobs at runtime via Telegram. Adds operational complexity for no testability gain.

### Approach B: arq or Celery beat — Rejected
Both require Redis. Violates "no extra services" constraint.

### Approach C: FastAPI-Utils `@repeat_every` — Rejected
Decorator-only, no cron expressions, no persistence, can't add jobs at runtime. Won't scale past two jobs.

### Approach D: Generic LLM web search only — Rejected (current state)
Already in production via GLM's `web_search` tool, but returns hallucinated subreddit names and stale URLs. Not actionable for marketing decisions where wrong data costs reputation.

### Approach E: Build a separate Node.js scheduler microservice — Rejected
Polyglot operational burden, more containers, no testability win.

## Trade-offs Accepted

- **APScheduler 4 alpha status**: Latest is `4.0.0a6`. Stable line is 3.11.2. We accept alpha because the async API has been stable across alphas and the maintainer (`agronholm`, also of `anyio`) is reliable. Fallback path: APScheduler 3.11 with `AsyncIOScheduler`.
- **Reddit OAuth requires application approval**: Self-service was removed in 2024. Alex must create a script app via Reddit's developer portal one time. Documented in setup guide.
- **GitHub PAT scope creep**: Self-repo traffic API requires `repo` scope (push access). We document the scope explicitly and isolate the PAT from other scopes via dedicated env var.
- **Single-process mode**: With multiple uvicorn workers, scheduler would fire jobs N times. We document `--workers 1` as a hard requirement.
- **No HN/Lobsters scraping**: Both lack official search APIs. Continue using GLM `web_search` for those, with citations from tool calls.

## Out of Scope

- Multi-user/multi-tenant scheduling
- Awesome-list PR automation (just notification of opportunity)
- Stargazer growth visualization (raw counts only, no charts)
- Auto-posting to social media (Alex always reviews before posting)
- HN/Lobsters native API integration (no good APIs exist)
- Migration from `!` to `/` enforcement (both supported indefinitely)
- Schedule conflict detection across overlapping cron expressions

## Architecture Sketch

```
                     ┌─────────────────────────────┐
                     │  FastAPI Workflows Service  │
                     │  (single container)         │
                     │                             │
   Telegram ◀───┐    │  ┌─────────────────────┐    │
   Bot          │    │  │ APScheduler 4       │    │
   (DMs only)   │    │  │ AsyncScheduler      │    │
                │    │  │ SQLAlchemyDataStore │◀───┼── /sched add (runtime)
                │    │  └──────────┬──────────┘    │
                │    │             │ fires         │
                │    │             ▼               │
                │    │  ┌─────────────────────┐    │
                │    │  │ Generators:         │    │
                │    │  │  morning_scan       │    │
                │    │  │  weekly_traffic     │    │
                │    │  │  awesome_list_watch │    │
                │    │  └──────────┬──────────┘    │
                │    │             │ enqueue       │
                │    │             ▼               │
                │    │  ┌─────────────────────┐    │
                ▼    │  │ Task Queue (SQLite) │    │
   /sched, /scan   ─┼──▶│ existing tasks      │    │
   /repos, !task   │    │ table               │    │
                   │    │                     │    │
                   │    └──────────┬──────────┘    │
                   │               │ drain         │
                   │               ▼               │
                   │  ┌─────────────────────┐      │
                   │  │ Heartbeat           │      │
                   │  │ (every 5m, exists)  │      │
                   │  │  ─ runs _llm_work_  │      │
                   │  │    task() with new  │      │
                   │  │    research adapters│      │
                   │  └──────────┬──────────┘      │
                   │             │                 │
                   │             ▼                 │
                   │  ┌─────────────────────┐      │
                   │  │ Research Adapters:  │      │
                   │  │  reddit_search.py   │──────┼──▶ Reddit OAuth (asyncpraw)
                   │  │  github_search.py   │──────┼──▶ GitHub Search (githubkit)
                   │  │  llm_proxy (web)    │──────┼──▶ GLM web_search
                   │  └─────────────────────┘      │
                   │                               │
                   │  ┌─────────────────────┐      │
                   │  │ projects (SQLite)   │      │
                   │  │  - athola/claude-   │      │
                   │  │    night-market     │      │
                   │  │  - athola/skrills   │      │
                   │  │  - athola/simple-   │      │
                   │  │    resume           │      │
                   │  └─────────────────────┘      │
                   └───────────────────────────────┘
```

## Tracked Projects (Initial)

| Slug | Repo | Subreddits | GitHub Topics | Marketing Posture |
|------|------|------------|---------------|-------------------|
| `claude-night-market` | athola/claude-night-market | r/ClaudeAI, r/LocalLLaMA, r/SideProject | claude-code, plugins, marketplace | Lead with: "browse and install Claude Code plugins"; bury AI angle |
| `skrills` | athola/skrills | r/ClaudeAI, r/devtools, r/SideProject | claude-code, skills, agents | Lead with: "ready-to-install skills for X workflows"; show concrete skill output |
| `simple-resume` | athola/simple-resume | r/resumes, r/EngineeringResumes, r/Python, r/coolgithubprojects | resume, yaml, pdf, python | Lead with: "YAML→PDF resume builder"; functional-core/imperative-shell architecture as a writeup angle |

## Initial Scheduled Jobs

| Job ID | Cadence | Purpose |
|--------|---------|---------|
| `morning_scan` | `0 9 * * *` (daily 9am local) | For each tracked project: scan Reddit (last 7d) + GitHub Search adjacent repos. Enqueue task summarizing results. |
| `weekly_traffic` | `0 8 * * 1` (Mon 8am) | Snapshot stargazers + traffic for each tracked project. Enqueue task with weekly delta + recommendations. |
| `awesome_lists_watch` | `0 10 * * 3` (Wed 10am) | Check ComposioHQ/awesome-claude-plugins, hesreallyhim/awesome-claude-code for new entries. Enqueue task if our projects aren't yet listed. |

## Anti-Patterns to Encode in Prompts

The work-prompt for marketing tasks must include:
- "Do NOT lead with 'I built this with Claude' — that framing now hurts engagement"
- "When suggesting comments, anchor in the user's question first; only mention the project as a relevant tool"
- "Required outputs: real post URLs, real subreddit names, real upvote counts. No fabrications."
- "Cite sources. If GLM web_search returned citations, include them."

## Next Steps

1. Specify — define data models, API contracts, command grammar, scheduler semantics, prompt templates
2. Plan — dependency-ordered TDD task list
3. Execute — implement with failing-test-first discipline
