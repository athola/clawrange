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
| `academic` | arXiv + Semantic Scholar | Public, no API key. Fans out both in parallel; per-source failures are logged and swallowed. |
| `triz` | GLM web search with TRIZ prompt | Cross-domain analogical reasoning — finds solutions in adjacent fields with bridge mappings. |

### Channel readiness check

```bash
curl -s http://localhost:5678/healthz/research | jq .
```

Returns `{configured_count, total_channels, status, channels: {...}}`
where each channel reports `configured`, `source`, and (when not
configured) a `reason` like `GITHUB_PAT not set`. Use this to
diagnose empty `/research` responses before chasing logic bugs.

The shell wrapper `scripts/test_research.sh` (also exposed as
`make test-research`) runs this check, posts a sample `/research`
query, and dumps the top 3 findings with confidence flags.

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

For research that needs the full tome plugin (multi-hop digging,
heavy academic synthesis, or TRIZ depth=heavy mode), John-117
enqueues a task whose description starts with
`research:tome: <topic>` (or the bracket form
`[research:tome] <topic>`).

The bridge script in `scripts/tome_bridge.py` runs from Alex's
local machine where the tome Claude Code plugin lives:

```bash
make tome-bridge          # one polling pass
make tome-bridge-watch    # poll forever
```

The bridge:

1. Polls `/task?status=pending` every 60s (configurable).
2. Matches descriptions against the three accepted formats
   (`research:tome:`, `[research:tome]`, `Research via tome:`).
3. Claims each match, runs `claude --print '/tome:research <topic>'`
   as a subprocess (timeout default 1800s), and posts the output
   back via `/task/{id}/result`.
4. Marks tasks `failed` with diagnostic on timeout (rc=124),
   missing-binary (rc=127), or non-zero exit.

Setup options:
- **One-off**: `make tome-bridge` after enqueuing a `research:tome`
  task from John-117.
- **Always-on**: run `make tome-bridge-watch` in a tmux/screen pane,
  or wire it into a systemd user unit / launchd job.
- **Per-task timeout**: `TOME_BRIDGE_TIMEOUT=600 make tome-bridge`.

The bridge has zero dependencies beyond Python stdlib + the
`claude` CLI on `$PATH`, so it ships and runs anywhere.

## Marketing Orchestrator (extended)

The four canonical projects are seeded on first boot of the
workflows service:

| Slug | Purpose | Key subreddits |
|------|---------|----------------|
| `claude-night-market` | Plugin marketplace for Claude Code | ClaudeAI, LocalLLaMA, SideProject |
| `skrills` | Trade-skill capture chrome extension | Construction, ITCareerQuestions, SideProject |
| `simple-resume` | YAML → PDF/HTML resume generator | resumes, cscareerquestions, SideProject |
| `clawrange` | This stack — personal AI ops gateway | ClaudeAI, claudecode, vibecoding, opensourceai, codex, sideprojects, LocalLLaMA, selfhosted |
| `personal-brand` | Alex's AI-systems engineer voice | ClaudeAI, LocalLLaMA, MachineLearning, ExperiencedDevs |

### Generators

| Name | Cron suggestion | What it does |
|------|-----------------|--------------|
| `morning_scan` | `0 8 * * *` | Reddit + GitHub scan tasks per project (queue-only) |
| `morning_digest` | `0 8 * * *` (auto-seeded) | Live 24h Reddit scan, Telegram digest of comment-worthy posts grouped by project, plus `[DRAFT]` comment tasks |
| `weekly_traffic` | `0 8 * * 1` | Stargazer / clone deltas per repo |
| `awesome_lists_watch` | `0 10 * * 3` | PR-target reminders for awesome-lists |
| `custom_scan` | (ad-hoc) | Generic single-topic task emitter |
| `content_idea` | `0 9 * * *` | Turns recent research into 1 idea/project |
| `comment_draft` | (ad-hoc) | Drafts a reply for a specific URL |

#### `morning_digest` — the 8am rundown

Delivered by `morning_digest_generator` (in `workflows/generators.py`).
On a fresh boot, `seed_default_projects` registers a `0 8 * * *` schedule
in the `SCHEDULER_TZ` (default `America/Chicago`), so the morning rundown
fires without manual `/sched add`.

For each tracked project, the generator searches the union of that
project's subreddits and the AI-coding extras Alex curated
(`vibecoding`, `opensourceai`, `claudecode`, `ClaudeAI`, `codex`,
`sideprojects`) for posts created in the last 24h. Each post is scored
against the project's topics + search_terms, routed to its best-fit
project, deduplicated against `scan_cache` (so tomorrow's run won't
re-surface today's posts), and rendered as a Markdown digest grouped
by project. Telegram delivery via `telegram.notify`. Top picks become
`[DRAFT]` comment-draft tasks for human review — never auto-posted.

Override at runtime with `kwargs` on the schedule:
- `project_slugs`: limit to specific projects
- `extra_subreddits`: replace the default extras list
- `top_per_project`: cap the digest size (default 4)
- `queue_drafts`: set false to skip task creation

#### Reddit API access — script-app setup

The digest works on a fresh deploy without credentials by falling
back to Reddit's unauthenticated public JSON endpoint. That fallback
is rate-limited (~30 req/min anonymous) and omits some fields, so
wire a script-app for production-quality lookups.

**Step 1 — Create the script app**

1. Sign in to Reddit as the account whose voice the bot will speak
   in (typically `u/athola`).
2. Visit https://www.reddit.com/prefs/apps and click
   **"are you a developer? create an app..."** at the bottom.
3. Fill in the form:
   - **name**: `clawrange-marketing-bot` (or any identifier you like)
   - **type**: select **`script`** — this is the only OAuth flow
     that supports username + password and works for read-only
     personal use. Do **not** pick `web app` or `installed app`.
   - **description**: optional. e.g. *"Personal marketing-research
     bot. Read-only search across AI-coding subreddits."*
   - **about url**: leave blank or point at your repo.
   - **redirect uri**: required even for script apps — set it to
     `http://localhost:8080` (it isn't used by the script flow but
     Reddit rejects the form if it's empty).
4. Click **"create app"**. You should land on the app's detail card.

**Step 2 — Pull the credentials**

On the app card you just created:
- **client_id**: the short string directly under the app name —
  it's labelled `personal use script` (~14 characters, base62).
- **client_secret**: the longer `secret` field on the same card.
  Click **"edit"** if it's hidden behind a placeholder.

You also need the Reddit account's own credentials:
- **username**: the account you logged in as (e.g. `athola`).
- **password**: that account's password. If 2FA is enabled, append
  the current TOTP code with a colon, e.g. `mypassword:123456`,
  per asyncpraw's documented script-app flow. Long-running daemons
  generally disable 2FA on the bot account or use an app-password.

**Step 3 — Wire the credentials**

Add to `.env` at the repo root (the file is gitignored):

```dotenv
REDDIT_CLIENT_ID=abc123XYZ
REDDIT_CLIENT_SECRET=longersecretvalue
REDDIT_USERNAME=athola
REDDIT_PASSWORD=yourpassword
REDDIT_USER_AGENT=clawrange-marketing-bot/0.1 (by u/athola)
```

The `REDDIT_USER_AGENT` is required by Reddit's API rules — include
your username so they can contact you about misbehaving bots. The
default value is acceptable but the personalised form is preferred.

**Step 4 — Reload the workflows container**

```bash
docker compose restart workflows
```

The container reads `.env` at start; a restart picks up the new
values. No image rebuild needed.

**Step 5 — Verify the OAuth path is live**

```bash
curl -X POST http://localhost:5678/sched/morning_digest/run
docker logs msp-workflows --since 30s | grep -i reddit
```

Look for the absence of `Reddit OAuth not configured — using public
JSON fallback` in the logs. Confirm `[DRAFT]` tasks appear in the
queue with `curl http://localhost:5678/task?status=pending` and
that the Markdown digest landed in Telegram.

**Troubleshooting**

| Symptom | Cause |
|---------|-------|
| `401 Unauthorized` from asyncpraw | Wrong client_id/secret, or app type is not `script` |
| `invalid_grant` | Wrong username or password |
| Empty results despite valid creds | Account has no Reddit history; new accounts hit shadow filters. Use an account with at least one comment / 1+ karma. |
| Rate limit warnings (`429`) on public fallback | Expected when creds are missing under heavy fan-out — set creds to upgrade to authenticated rate limits (~60 req/min). |

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
