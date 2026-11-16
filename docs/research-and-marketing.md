# Research and Marketing Operator Guide

This guide covers the assistant's multi-source research orchestrator
and the top-of-funnel marketing tooling that surrounds it. It is
the operator-side companion to the planning artifact in
`archive/research-and-marketing-plan.md`.

## TL;DR

Every route below except `GET /healthz/research` needs the bearer
token from `.env`. Load it first with `set -a; . ./.env; set +a`.

```bash
# Run a research session and persist it
curl -s -X POST http://localhost:5678/research \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"topic":"agent platforms shipping in 2026"}' | jq .

# Recall recent sessions
curl -s http://localhost:5678/research/sessions \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN" | jq .

# Schedule daily content idea passes (uses last 5 sessions)
curl -s -X POST http://localhost:5678/sched \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"id":"daily-content","name":"Daily content ideas",
       "kind":"content_idea","cron":"0 8 * * *"}'

# Run an existing schedule now (here one with id draft-once)
curl -s -X POST http://localhost:5678/sched/draft-once/run \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN"
```

## Research Orchestrator

`POST /research` runs a multi-source research session and returns
ranked, deduplicated, citation-bearing findings.

### Channels

| Channel | Source | Notes |
|---------|--------|-------|
| `discourse` | Reddit subreddits | Default subs come from the active profile (`reddit.default_subreddits`, else the tracked projects' subreddits). Override with `subreddits`. |
| `code` | GitHub repos | Default `min_stars=50`. |
| `discourse_web` (or `web`) | OpenRouter `:online` web search through the proxy's search tier | Returns one synthesized summary finding per call. Needs `OPENROUTER_API_KEY` and a balance above the floor. |
| `academic` | arXiv and Semantic Scholar | Public, no API key. Fans out both in parallel. If one source fails the other's findings are returned; the channel fails only when both do. |
| `triz` | OpenRouter `:online` web search with a TRIZ prompt | Cross-domain analogical reasoning. Finds solutions in adjacent fields with bridge mappings. |

A request without `channels` runs `discourse`, `code` and
`discourse_web`; ask for `academic` and `triz` by name. A channel that
fails is reported under `errors` while the others still return. When
every requested channel fails, `/research` returns 502 with the reason.

### Channel readiness check

```bash
curl -s http://localhost:5678/healthz/research | jq .
```

Returns `{configured_count, total_channels, status, channels: {...}}`
where each channel reports `configured`, `source`, and (when not
configured) a `reason` like `GITHUB_PAT not set`. `discourse_web` and
`triz` report source `openrouter-online` and are configured only when
`OPENROUTER_API_KEY` is set. This route is public. Use it to
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
curl -s 'http://localhost:5678/research/sessions?limit=10' \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN" | jq .
```

Get a single session with all findings:

```bash
curl -s http://localhost:5678/research/sessions/<id> \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN" | jq .
```

The `session_id` is short (12 chars) and is included in every
`/research` response. The assistant uses this to recall earlier work
without re-running the fanout.

### Heavy research via tome

For research that needs the full tome plugin (multi-hop digging,
heavy academic synthesis, or TRIZ depth=heavy mode), the assistant
enqueues a task whose description starts with
`research:tome: <topic>` (or the bracket form
`[research:tome] <topic>`).

The bridge script in `scripts/tome_bridge.py` runs on the
operator's local machine where the tome Claude Code plugin lives. It
needs `PROXY_AUTH_TOKEN` in its environment (the make targets load
`.env`):

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
   missing-binary (rc=127), or non-zero exit. A topic over 300
   characters or with control characters is failed without running.

The topic is queue text, so the bridged session is locked down: only
the `WebSearch`, `WebFetch`, `Skill` and `Agent` tools exist and are
pre-approved (`--tools`, `--allowedTools`, `--permission-mode
dontAsk`), no MCP server config is loaded (`--strict-mcp-config`), and
it runs in an empty temporary directory rather than the repo. It cannot
read files or run commands.

A worker that dies after claiming leaves its task `active`. The
heartbeat fails any task still active after `HEARTBEAT_ACTIVE_REAP_HOURS`
(default 6) and reports it in the digest; re-create it to retry.

Setup options:
- **One-off**: `make tome-bridge` after enqueuing a `research:tome`
  task from the assistant.
- **Always-on**: run `make tome-bridge-watch` in a tmux/screen pane,
  or wire it into a systemd user unit / launchd job.
- **Per-task timeout**: `TOME_BRIDGE_TIMEOUT=600 make tome-bridge`.

The bridge has zero dependencies beyond Python stdlib and the
`claude` CLI on `$PATH`, so it ships and runs anywhere.

## Marketing Orchestrator (extended)

The `marketing` profile seeds five projects on first boot of the
workflows service (the default `starter` profile seeds none):

| Slug | Purpose | Key subreddits |
|------|---------|----------------|
| `claude-night-market` | Plugin marketplace for Claude Code | ClaudeAI, LocalLLaMA, SideProject |
| `skrills` | Trade-skill capture chrome extension | Construction, ITCareerQuestions, SideProject |
| `simple-resume` | YAML → PDF/HTML resume generator | resumes, cscareerquestions, SideProject |
| `clawrange` | This stack, personal AI ops gateway | ClaudeAI, claudecode, vibecoding, opensourceai, codex, sideprojects, LocalLLaMA, selfhosted |
| `personal-brand` | The operator's AI-systems engineer voice | ClaudeAI, LocalLLaMA, MachineLearning, ExperiencedDevs |

### Generators

| Name | Cron suggestion | What it does |
|------|-----------------|--------------|
| `morning_scan` | `0 8 * * *` | Reddit and GitHub scan tasks per project (queue-only) |
| `morning_digest` | `0 8 * * *` (auto-seeded) | Daily outreach rundown: Reddit, Hacker News, GitHub issues and web search, with an LLM read and action items, always delivered to Telegram |
| `weekly_traffic` | `0 8 * * 1` | Stargazer / clone deltas per repo |
| `awesome_lists_watch` | `0 10 * * 3` | PR-target reminders for awesome-lists |
| `custom_scan` | (ad-hoc) | Generic single-topic task emitter |
| `content_idea` | `0 9 * * *` | Turns recent research into 1 idea/project |
| `comment_draft` | (ad-hoc) | Drafts a reply for a specific URL |

#### `morning_digest`: the 8am rundown

Delivered by `morning_digest_generator` (in `workflows/generators.py`).
On a fresh boot with the `marketing` profile, `seed_from_profile`
registers a `0 8 * * *` schedule
in the operator timezone (`SCHEDULER_TZ`, else the profile's
`assistant.timezone`, else UTC), so the morning rundown
fires without manual `/sched add`.

The report always sends. A morning with no evidence still delivers a
short report with a source-health line and a **Setup needed** list, so a
missing message means Telegram failed, not that the scan was quiet.

Sources, each isolated so one outage never empties the report:

- **Reddit**: the union of each project's subreddits and the AI-coding
  extras, last 24h, scored against topics and search_terms, deduplicated
  against `scan_cache`, plus the popular-bonus tier (★) and emerging-sub
  discovery. After 3 consecutive 403/429 responses the run stops calling
  Reddit and says so in the report.
- **Hacker News**: keyless Algolia search, last 24h, linked to the HN
  discussion.
- **GitHub issues**: open issues on other repos that mention a project's
  first search term (last 72h). Works keyless; `GITHUB_PAT` lifts the
  10 req/min limit.
- **Web search**: the OpenRouter `:online` tier looks for forums,
  Lobsters, dev.to, newsletters and awesome-lists. Single-source, so
  these items are labelled "verify before acting".

Every item gets an `E#` ref. One LLM call writes **Today's read and
action items** over that numbered list. It may cite only `[E#]`; the
renderer links known refs and strips unknown refs and every raw URL, so
the model cannot invent a source. Action items say what to do and where,
not what to write. If synthesis fails, the report falls back to action
items ranked by engagement. Rendering lives in `workflows/rundown.py`.

Long reports are split on line boundaries into several Telegram messages
(`telegram.notify_long`), and each chunk is retried with backoff. The
schedule row records the outcome: `delivered N/N msgs (...)` or
`FAILED telegram n/N msgs (...)`. Posts are marked seen only after full
delivery.

If workflows is down at 08:00, the scheduler runs the missed digest at
boot, as long as the fire was under 4h ago and no later run recorded a
delivery. A schedule opts in with `catch_up: true` in the profile
(`CATCH_UP_WINDOW` and `catch_up_ids` in `workflows/scheduler.py`).

Override at runtime with `kwargs` on the schedule:
- `project_slugs`: limit to specific projects
- `extra_subreddits`: replace the default extras list
- `top_per_project`: cap Reddit picks per project (default 4)

#### Reddit API access: script-app setup

Without credentials the digest falls back to Reddit's unauthenticated
JSON endpoint, which Reddit now answers with 403/429 for most requests.
In practice Reddit evidence needs a script app. Until one is configured,
the rundown lists this under **Setup needed** every morning.

**Step 1: Create the script app**

1. Sign in to Reddit as the account whose voice the bot will speak
   in (for example `u/your-handle`).
2. Visit https://www.reddit.com/prefs/apps and click
   **"are you a developer? create an app..."** at the bottom.
3. Fill in the form:
   - **name**: `clawrange-marketing-bot` (or any identifier you like)
   - **type**: select **`script`**. This is the only OAuth flow
     that supports username and password and works for read-only
     personal use. Do **not** pick `web app` or `installed app`.
   - **description**: optional. e.g. *"Personal marketing-research
     bot. Read-only search across AI-coding subreddits."*
   - **about url**: leave blank or point at your repo.
   - **redirect uri**: required even for script apps. Set it to
     `http://localhost:8080` (it isn't used by the script flow but
     Reddit rejects the form if it's empty).
4. Click **"create app"**. You should land on the app's detail card.

**Step 2: Pull the credentials**

On the app card you just created:
- **client_id**: the short string directly under the app name. It's
  labelled `personal use script` (~14 characters, base62).
- **client_secret**: the longer `secret` field on the same card.
  Click **"edit"** if it's hidden behind a placeholder.

You also need the Reddit account's own credentials:
- **username**: the account you logged in as (e.g. `your-handle`).
- **password**: that account's password. If 2FA is enabled, append
  the current TOTP code with a colon, e.g. `mypassword:123456`,
  per asyncpraw's documented script-app flow. Long-running daemons
  generally disable 2FA on the bot account or use an app-password.

**Step 3: Wire the credentials**

Add to `.env` at the repo root (the file is gitignored):

```dotenv
REDDIT_CLIENT_ID=abc123XYZ
REDDIT_CLIENT_SECRET=longersecretvalue
REDDIT_USERNAME=your-handle
REDDIT_PASSWORD=yourpassword
REDDIT_USER_AGENT="clawrange-marketing-bot/0.1 (by u/your-handle)"
```

The `REDDIT_USER_AGENT` is required by Reddit's API rules. Include
your username so they can contact you about misbehaving bots. The
default value is acceptable but the personalised form is preferred.

**Step 4: Reload the workflows container**

```bash
docker compose up -d workflows
```

Compose substitutes `.env` values into the container environment when
it creates the container, so recreate it: `docker compose restart`
keeps the old environment. Only variables listed under the service's
`environment:` in `docker-compose.yml` reach the container (the
`REDDIT_*` ones are). No image rebuild needed.

**Step 5: Verify the OAuth path is live**

```bash
curl -s -X POST http://localhost:5678/sched/morning_digest/run \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN"
docker logs msp-workflows --since 30s | grep -i reddit
```

Look for the absence of `Reddit OAuth not configured — using public
JSON fallback` in the logs. Confirm `[DRAFT]` tasks appear in the
queue with
`curl -s 'http://localhost:5678/task?status=pending' -H "Authorization: Bearer $PROXY_AUTH_TOKEN"`
and that the Markdown digest landed in Telegram.

**Troubleshooting**

| Symptom | Cause |
|---------|-------|
| `401 Unauthorized` from asyncpraw | Wrong client_id/secret, or app type is not `script` |
| `invalid_grant` | Wrong username or password |
| Empty results despite valid creds | Account has no Reddit history. New accounts hit shadow filters. Use an account with at least one comment / 1+ karma. |
| Rate limit warnings (`429`) on public fallback | Expected when creds are missing under heavy fan-out. Set creds to upgrade to authenticated rate limits (~60 req/min). |

### Posture

The marketing posture is encoded in each project's `posture` field
and reinforced in `openclaw/soul.md`. Core rules:

1. **Lurk first, comment later**: be a community member before a
   marketer.
2. **Useful comments first**: lead with specifics, code, numbers.
3. **Honest disclaimers beat polish** on r/SideProject and
   r/programming.
4. **Never auto-post**: every draft lands as a `[DRAFT]` task and
   the operator sends it manually after review.
5. **HN for depth, Reddit for breadth**: converts ~3x better,
   reaches ~1/3 the audience.
6. **r/SideProject format**: `[Launch] Name — one-liner ≤100ch`,
   opening, journey, 3-5 features, and ending question.
7. **External article links boost X reach in 2026**:
   Medium/dev.to/Substack/personal blog.

### Operator workflow

```text
morning_scan ──► task queue ──► assistant reviews, enqueues
                                                   comment_draft
                                                          │
                                                          ▼
                                                  [DRAFT] task
                                                          │
                                                  operator reviews
                                                          │
                                                          ▼
                                                  Manual post
```

## Acceptance evidence

The implementation is covered by the unit tests under
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
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"topic":"hello world"}' | jq .
```
