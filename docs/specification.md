# ClawRange Marketing Orchestrator — Specification

## Overview

Add scheduled task generation, project-aware research, and Telegram command extensions to the workflows service. All persistence lives in the existing `brain.db`. All external API calls go through dedicated adapters with graceful degradation when credentials are missing.

## Data Model

### Projects

Tracked GitHub repos with associated marketing context.

```
projects
├── slug         TEXT PRIMARY KEY      -- short ID: "claude-night-market"
├── owner        TEXT NOT NULL          -- GitHub owner: "athola"
├── repo         TEXT NOT NULL          -- GitHub repo: "claude-night-market"
├── topics       TEXT NOT NULL DEFAULT '[]'  -- JSON array of topics
├── subreddits   TEXT NOT NULL DEFAULT '[]'  -- JSON array of subreddit names (no r/ prefix)
├── search_terms TEXT NOT NULL DEFAULT '[]'  -- JSON array of search query strings
├── posture      TEXT NOT NULL DEFAULT '' -- short marketing-angle hint for prompts
├── created_at   TEXT NOT NULL
└── updated_at   TEXT NOT NULL
```

Constraint: `UNIQUE(owner, repo)`.

### Schedules

User-managed cron-style job definitions. APScheduler 4 manages its own runtime state in separate `apscheduler_*` tables.

```
schedules
├── id           TEXT PRIMARY KEY      -- short hash, e.g. "morning_scan"
├── name         TEXT NOT NULL UNIQUE  -- human label
├── kind         TEXT NOT NULL         -- generator name: morning_scan|weekly_traffic|awesome_lists_watch|custom_scan
├── cron         TEXT NOT NULL         -- crontab string OR "every 6h" duration form
├── kwargs       TEXT NOT NULL DEFAULT '{}'  -- JSON kwargs passed to generator
├── paused       INTEGER NOT NULL DEFAULT 0  -- 0|1
├── last_run     TEXT                  -- ISO 8601 of last fire
├── last_status  TEXT                  -- "ok" | "error: ..."
├── created_at   TEXT NOT NULL
└── updated_at   TEXT NOT NULL
```

The `kind` is the dispatch key into the generator registry. `kwargs` are passed as `**kwargs` to that generator. Both `paused` and `last_*` are mirrored from APScheduler state for `/sched list` queries (read-only display).

### Scan Cache

Optional dedup cache so repeated runs don't re-surface the same Reddit/GitHub items.

```
scan_cache
├── kind        TEXT NOT NULL         -- "reddit_post" | "github_repo" | "github_issue"
├── external_id TEXT NOT NULL         -- e.g., reddit post ID, github node_id
├── project_slug TEXT                 -- nullable when not project-scoped
├── seen_at     TEXT NOT NULL
└── PRIMARY KEY(kind, external_id, project_slug)
```

Used by generators to filter results to "new since last seen." Configurable lookback (default 7 days for Reddit, 14 days for GitHub).

## API Endpoints

All new endpoints live under the existing FastAPI app. Existing `/task`, `/brain`, `/v1`, `/healthz`, `/tier` endpoints unchanged.

### Project Registry

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/projects` | List tracked projects |
| `GET` | `/projects/{slug}` | Get a project |
| `POST` | `/projects` | Create or update a project (upsert by slug) |
| `DELETE` | `/projects/{slug}` | Remove a project |

```json
POST /projects
{
  "slug": "claude-night-market",
  "owner": "athola",
  "repo": "claude-night-market",
  "topics": ["claude-code", "plugins", "marketplace"],
  "subreddits": ["ClaudeAI", "LocalLLaMA", "SideProject"],
  "search_terms": ["claude code plugin", "claude marketplace"],
  "posture": "Lead with: browse and install plugins. Bury AI angle."
}
```

### Schedule Management

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/sched` | List schedules (mirrors APScheduler state into rows) |
| `GET` | `/sched/{id}` | Get a single schedule with runtime status |
| `POST` | `/sched` | Create a schedule (registers with APScheduler) |
| `PATCH` | `/sched/{id}` | Update cron/kwargs/paused state |
| `DELETE` | `/sched/{id}` | Remove from APScheduler and schedules table |
| `POST` | `/sched/{id}/run` | Force-run now (does not affect normal cadence) |

```json
POST /sched
{
  "id": "morning_scan",
  "name": "Morning marketing scan",
  "kind": "morning_scan",
  "cron": "0 9 * * *",
  "kwargs": {}
}
```

`cron` accepts either a 5-field crontab (`m h dom mon dow`) or a duration alias: `every 6h`, `every 30m`, `every 2d`. Validated server-side via `croniter.is_valid` or duration parser before persisting.

### Ad-hoc Scans

One-shot research without scheduling. Results returned in the response AND optionally enqueued as a task for Telegram delivery.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/scan/reddit` | Search Reddit subreddits |
| `POST` | `/scan/github` | Search GitHub repos/issues |
| `POST` | `/scan/web` | Generic LLM web search (existing behavior, exposed) |

```json
POST /scan/reddit
{
  "topic": "claude code plugin",
  "subreddits": ["ClaudeAI", "LocalLLaMA"],   // optional, defaults to project's list when project_slug given
  "project_slug": "claude-night-market",       // optional
  "since": "7d",                                // optional, default 7d
  "limit": 25,                                  // optional, default 25
  "deliver_to_telegram": true                   // optional, default true
}
```

Response:
```json
{
  "results": [
    {
      "url": "https://reddit.com/r/ClaudeAI/comments/abc",
      "title": "Built a plugin that auto-summarizes PRs",
      "subreddit": "ClaudeAI",
      "score": 234,
      "comments": 47,
      "created_utc": "2026-05-01T12:34:00Z",
      "snippet": "first 200 chars of selftext or null"
    }
  ],
  "total": 1,
  "query": "claude code plugin",
  "subreddits": ["ClaudeAI", "LocalLLaMA"],
  "task_id": "abc12345"   // present if deliver_to_telegram=true
}
```

```json
POST /scan/github
{
  "topic": "claude-code plugin",
  "kind": "repos",          // "repos" | "issues" | "discussions"
  "min_stars": 5,
  "language": null,
  "project_slug": null,
  "limit": 25,
  "deliver_to_telegram": true
}
```

```json
POST /scan/github
{
  "kind": "self_traffic",
  "project_slug": "skrills"   // pulls owner/repo from projects table
}
```

Returns 14-day rolling traffic stats (views, uniques, clones, top referrers, top paths).

## Reddit Adapter Contract

Module: `workflows/reddit_search.py`

```python
async def search_subreddits(
    topic: str,
    subreddits: list[str],
    since: str = "7d",
    sort: str = "new",
    limit_per_sub: int = 25,
) -> list[RedditPost]: ...

async def is_configured() -> bool: ...   # checks REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET + REDDIT_USERNAME + REDDIT_PASSWORD
```

`RedditPost` is a Pydantic model with the same shape as the JSON response above plus `id` (post ID for dedup).

Behavior:
- Uses `asyncpraw.Reddit(...)` script-app flow with credentials from env
- Translates `since` strings ("7d", "24h", "30d") to `time_filter` values for asyncpraw search
- Sets explicit User-Agent: `clawrange-marketing-bot/0.1 by u/{REDDIT_USERNAME}`
- Closes the Reddit instance after each call (no long-lived session — keeps memory low)
- On rate limit, sleeps based on `X-Ratelimit-Reset` header up to a 60s ceiling, then returns partial results
- On missing credentials → returns empty list and logs warning (no exception). The caller is expected to fall back to GLM web search.
- All HTTP errors caught and logged; never raises to caller

## GitHub Adapter Contract

Module: `workflows/github_search.py`

```python
async def search_repos(
    query: str,
    min_stars: int = 0,
    language: str | None = None,
    sort: str = "stars",
    limit: int = 25,
) -> list[GitHubRepo]: ...

async def search_issues(
    query: str,
    limit: int = 25,
) -> list[GitHubIssue]: ...

async def get_self_traffic(owner: str, repo: str) -> GitHubTrafficSnapshot: ...

async def is_configured() -> bool: ...
```

Behavior:
- Uses `githubkit.GitHub(token=GITHUB_PAT)` (async context)
- Search functions reach `/search/repositories` and `/search/issues`
- Traffic functions need PAT with `repo` push scope; raises `GitHubAuthError` if 403 (handled by caller)
- Rate-limit headers respected; logs remaining quota when below 20%
- Missing `GITHUB_PAT` → unauthenticated 60/hr ceiling for search; traffic endpoints return None with a warning

## Scheduler Contract

Module: `workflows/scheduler.py`

```python
async def init_scheduler(app: FastAPI) -> AsyncScheduler: ...

async def add_schedule(spec: ScheduleSpec) -> dict: ...
async def remove_schedule(schedule_id: str) -> None: ...
async def pause_schedule(schedule_id: str) -> None: ...
async def resume_schedule(schedule_id: str) -> None: ...
async def run_schedule_now(schedule_id: str) -> dict: ...
async def list_schedules() -> list[dict]: ...
```

`init_scheduler` is called from FastAPI lifespan. It:
1. Creates `AsyncScheduler` with `SQLAlchemyDataStore` over `sqlite+aiosqlite:///{BRAIN_DB_PATH}`
2. Loads all rows from the `schedules` table
3. For each non-paused row, registers the corresponding generator with APScheduler
4. Calls `start_in_background()`
5. Returns the scheduler so the app can store it on `app.state.scheduler`

## Generator Registry

Module: `workflows/generators.py`

```python
GENERATORS: dict[str, Callable[..., Awaitable[None]]] = {
    "morning_scan": morning_scan_generator,
    "weekly_traffic": weekly_traffic_generator,
    "awesome_lists_watch": awesome_lists_watch_generator,
    "custom_scan": custom_scan_generator,
}
```

Each generator function:
- Takes `**kwargs` from the schedule's `kwargs` JSON
- Reads from `projects` table as needed
- Calls `brain_db.create_task(description, priority, source="schedule")` for each task
- Updates the schedule's `last_run` and `last_status` on success/failure
- Never raises to APScheduler (catches and logs)

### morning_scan_generator(project_slugs: list[str] | None = None)

For each tracked project (or filtered subset):
1. Build a Reddit task description: `"Scan reddit for {project.slug}: search '{topic}' across {subs}. Surface posts where commenting would be relevant."`
2. Build a GitHub task description: `"Scan github for {project.slug}: find {N} adjacent repos and recent issues asking about {topics}."`
3. Both tasks emit one combined summary task that the heartbeat will pick up

### weekly_traffic_generator(project_slugs: list[str] | None = None)

For each tracked project, enqueue: `"Weekly traffic snapshot for {owner}/{repo}: pull views/clones/uniques and stargazer delta. Compare to previous week."`

### awesome_lists_watch_generator(lists: list[str] | None = None)

Default lists: `["ComposioHQ/awesome-claude-plugins", "hesreallyhim/awesome-claude-code"]`. For each list, fetch the README, check whether each tracked project's repo URL appears, and enqueue: `"Awesome-list watch: {project.slug} not yet listed in {list}. Draft a PR description and submission plan."` only when missing.

### custom_scan_generator(topic: str, kind: str, project_slug: str | None = None)

Generic — used when Alex creates a custom schedule via `/sched add`.

## Telegram Command Grammar

### Prefix Policy

Both `/cmd` and `!cmd` accepted. The router strips the leading `!` or `/` before dispatching. New commands documented with `/`. Deprecation timeline for `!`: indefinite (no breakage planned).

### Subcommand Style

```
/sched add <name> cron "<expr>" -- <generator-or-prompt>
/sched add <name> every <duration> -- <generator-or-prompt>
/sched list [--paused]
/sched show <id|name>
/sched pause <id|name>
/sched resume <id|name>
/sched rm <id|name>
/sched run <id|name>

/scan reddit <topic> [--subs <comma-sep>] [--project <slug>] [--since 7d] [--limit 25]
/scan github <topic> [--kind repos|issues] [--stars 5] [--lang python] [--limit 25]
/scan github traffic <project-slug>
/scan web <prompt>            # forwards to GLM web_search

/projects                                   # list tracked projects
/projects show <slug>
/projects add <slug> <owner>/<repo> [--topics ...] [--subs ...] [--posture "..."]
/projects rm <slug>

/marketing                                  # alias: /sched run morning_scan
/help [verb]                                # /help sched, /help scan
```

### Parsing Rules

- Use `shlex.split(rest, posix=True)` for everything before `--`. Raw string after `--`.
- For `/sched add`, the freeform after `--` is treated as either a known generator name (matches `GENERATORS` registry) or a free-form prompt string (becomes a `custom_scan` with `topic` set).
- All flag values use `--key value` (not `--key=value`).
- Unknown subcommand returns help excerpt for the verb.

### Output Formatting

- `parse_mode="HTML"`, `disable_web_page_preview=True`
- Helper `tg.escape_html(s)` wraps `html.escape(s, quote=False)`
- Multi-result responses: 5-10 items per message, paginate with `--page N`
- For result lists with >10 items, attach a Markdown file via `send_document`
- All inline links use `<a href="...">title</a>`
- Code blocks use `<pre>` for monospace
- Truncate snippets to 200 chars with `…`

### Example: Reddit Scan Reply

```html
<b>Reddit scan: claude code plugin</b> <i>(5 of 12)</i>

<b>1.</b> <a href="https://reddit.com/r/ClaudeAI/comments/abc">Built a plugin that auto-summarizes PRs</a>
   r/ClaudeAI · 234↑ · 47 comments · 3h ago
   <i>Angle: mention claude-night-market as a discovery surface</i>

<b>2.</b> <a href="https://reddit.com/r/LocalLLaMA/comments/def">Anyone using Claude Skills in production?</a>
   r/LocalLLaMA · 89↑ · 12 comments · 6h ago
   <i>Angle: link to skrills with concrete skill examples</i>

<i>Reply</i> <code>/scan reddit claude code plugin --page 2</code> <i>for more</i>
```

### Existing Aliases (Preserved)

- `!task`, `!tasks`, `!task list`, `!task tail`, `!task cancel`, `!task priority` — unchanged
- `!remember`, `!recall`, `!page` — unchanged
- `!tier`, `!status`, `!help` — unchanged

New `/help` (no arg) appends a "MARKETING" section listing the `/sched`, `/scan`, `/projects`, `/marketing` verbs.

## Heartbeat Integration

Existing `_handle_heartbeat()` continues to drain ONE task per cycle. Behavior unchanged for non-marketing tasks. New behavior:

When a pending task description starts with `Scan reddit ` or `Scan github `, the heartbeat:
1. Parses the task to extract `(kind, project_slug, topic, params)` (regex extractor)
2. Calls the appropriate adapter directly (Reddit or GitHub) — bypassing LLM call
3. Formats results via the same HTML helper
4. Stores result in task and sends to Telegram
5. Returns adapter-sourced data with citations, NOT LLM hallucinations

For tasks that aren't structured marketing scans (e.g., "Draft a HN launch post for skrills"), the heartbeat falls back to the existing `_llm_work_task()` flow but with an enhanced prompt that includes:
- Project context (loaded from `projects` table when `project_slug` parseable from description)
- Anti-pattern rules (do not lead with "built with Claude", lead with problem solved)
- Citation requirements

## Prompt Templates

Module additions in `llm_proxy.py`:

```python
ANTI_PATTERN_RULES = """
ANTI-PATTERNS (do not produce text that violates these):
- Do NOT lead with "I built this with Claude" or similar AI-builder framing
- Do NOT use superlatives like "fastest", "best", "revolutionary"
- Do NOT fabricate post titles, subreddit names, usernames, or URLs
- Do NOT recommend posting in subreddits where self-promotion is banned
  (r/programming requires answering-a-question framing only)

POSITIVE RULES:
- Lead with the user problem solved
- Include real upvote counts, comment counts, and timestamps when available
- Cite source URLs for every claim
- For comment suggestions, anchor in the user's question first; mention the project as a relevant tool second
"""

def build_marketing_prompt(task: str, project: dict | None, evidence: list[dict]) -> str:
    """Compose marketing-task prompt with project posture + anti-patterns + evidence list."""
```

Evidence is passed as a structured list (Reddit posts, GitHub repos) so the LLM only summarizes/synthesizes, never invents.

## Non-Functional Requirements

- **Latency**: `/scan reddit` < 5s (Reddit API), `/scan github` < 3s, `/scan github traffic` < 2s. Schedule registration < 100ms.
- **Memory**: Total new dependencies (apscheduler, sqlalchemy[asyncio], aiosqlite, asyncpraw, githubkit, croniter, pytimeparse, shlex stdlib) add ~25MB. Container ceiling 128MB still holds.
- **Reliability**: Adapter failures must not crash heartbeat. Scheduler failures must not crash FastAPI startup (degrade to "scheduling unavailable" mode).
- **Persistence**: Schedules survive `docker compose restart` with no data loss. Verified by integration test that boots, registers, restarts, and confirms next-fire time matches.
- **Idempotency**: Adapter calls dedup against `scan_cache` so re-running a scheduled job doesn't surface the same Reddit post twice within the lookback window.
- **Single-process invariant**: `docker-compose.yml` must hard-pin uvicorn `--workers 1`. Documented in CLAUDE.md.
- **Test coverage**: New modules ≥85% line coverage. Integration test for scheduler restart-resilience.

## Configuration

### New Environment Variables

```
# Reddit (script app via developer.reddit.com — Alex creates one-time)
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USERNAME=
REDDIT_PASSWORD=
REDDIT_USER_AGENT=clawrange-marketing-bot/0.1   # default if unset

# GitHub (PAT with repo + read:org scope)
GITHUB_PAT=

# Scheduler timezone (default: America/Chicago for Alex's Texas location)
SCHEDULER_TZ=America/Chicago
```

All four Reddit vars optional — adapter gracefully degrades. `GITHUB_PAT` optional — search degrades to unauthenticated quotas, traffic endpoints disabled.

### docker-compose.yml Changes

```yaml
workflows:
  command: ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5678", "--workers", "1"]
  environment:
    - REDDIT_CLIENT_ID
    - REDDIT_CLIENT_SECRET
    - REDDIT_USERNAME
    - REDDIT_PASSWORD
    - REDDIT_USER_AGENT
    - GITHUB_PAT
    - SCHEDULER_TZ
  # existing env vars preserved
```

### requirements.txt Additions

```
apscheduler>=4.0.0a6
sqlalchemy[asyncio]>=2.0
aiosqlite>=0.19
asyncpraw>=7.8
githubkit>=0.13
croniter>=2.0
pytimeparse>=1.1
```

## User Stories

### US-1: Schedules Survive Restart
**As** Alex, **when** I `docker compose restart workflows`, **I** see all my scheduled jobs intact and the next-fire time advances correctly.

**Acceptance**:
- Integration test boots scheduler, adds 3 schedules, restarts, asserts all 3 reload
- `/sched list` shows correct next-fire timestamps after restart

### US-2: Telegram Schedule Management
**As** Alex, **when** I send `/sched add weekly-hn cron "0 8 * * 1" -- custom_scan` from Telegram, **I** get a confirmation back with the schedule ID and next-fire time.

**Acceptance**:
- Command parser handles quoted cron expressions correctly
- Schedule appears in `/sched list`
- Generator fires at the next matching minute

### US-3: Reddit Scan Returns Real Data
**As** Alex, **when** the morning_scan generator fires, **I** receive Telegram messages with real Reddit post URLs that I can click to verify.

**Acceptance**:
- All URLs in scan results return 200 when fetched
- Subreddit names match real subreddits
- Upvote counts and comment counts non-negative integers

### US-4: Anti-Pattern Suppression in Suggestions
**As** Alex, **when** the LLM drafts a comment suggestion for a Reddit post, **I** never see "I built this with Claude" framing.

**Acceptance**:
- Test injects mock evidence and asserts output doesn't contain banned phrases
- Output leads with the user's question/problem
- Project mention is anchored as "a relevant tool", not "the AI-built solution"

### US-5: Project Registry Drives Scans
**As** Alex, **when** I add a 4th project via `/projects add <slug> <owner>/<repo> --subs ... --posture ...`, **I** can immediately schedule a morning_scan that includes it without code changes.

**Acceptance**:
- `/projects add` upserts to `projects` table
- Next `morning_scan` fires include the new project
- Removing a project removes it from subsequent scans

### US-6: Self-Repo Traffic Snapshot
**As** Alex, **when** the `weekly_traffic` job fires Monday morning, **I** receive a Telegram message with views/uniques/clones for each tracked project plus week-over-week deltas.

**Acceptance**:
- Calls `/repos/{owner}/{repo}/traffic/views` and `/clones`
- Stores snapshot in `scan_cache` with `kind="github_traffic"`
- Computes delta vs the most recent prior snapshot
- Gracefully degrades when GITHUB_PAT lacks push scope (logs warning, skips traffic)

### US-7: Awesome-List PR Reminder
**As** Alex, **when** Wednesday morning's `awesome_lists_watch` runs, **I** get a Telegram nudge listing exactly which awesome-* lists my projects aren't yet on, with a draft PR description.

**Acceptance**:
- Reads each list's README markdown
- Substring-matches `github.com/athola/{repo}` to detect existing entries
- Only enqueues a task when at least one list is missing the project
- Draft PR description includes project posture from the projects table

### US-8: Anti-Spam Cache
**As** Alex, **when** morning_scan runs Monday and Tuesday, **I** don't see the same Reddit post on Tuesday that I already saw Monday.

**Acceptance**:
- `scan_cache` table populated on first scan
- Second scan filters out items with `(kind, external_id, project_slug)` already in cache within 7-day window
- Manual `/scan reddit ... --no-cache` flag bypasses dedup for ad-hoc queries

## Test Strategy

- **Unit tests** for each adapter with mocked HTTP (asyncpraw and githubkit support test-mode injection)
- **Unit tests** for parser (`parse_telegram_command`) with table-driven cases
- **Integration tests** that spin up an in-memory SQLite database and verify scheduler persistence
- **Smoke test** in `scripts/test_workflows.sh` that hits new endpoints with curl
- **Anti-pattern test** that runs LLM with stub provider returning canned bad output, asserts post-processing strips/flags it (or accepts bad output reports it; this is a soft guardrail)

Coverage gate: `pytest --cov=workflows --cov-fail-under=85` for new modules.

## Migration Notes

- Existing `tasks` table unchanged (already has `source` column from prior brain mission)
- New tables created on `BrainDB.init_db()` — additive, no destructive migration
- Existing schedule-less behavior is the fallback if scheduler init fails
- Roll-forward only — no rollback path needed for testbed
