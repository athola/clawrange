# ClawRange: Project Instructions

## What This Is

Personal AI ops stack: an OpenClaw assistant (persona rendered from the
active tenant profile) backed by a FastAPI workflows service that owns the brain database, task queue, LLM
proxy, scheduler, and marketing scanners. Runs in Docker on a single host
and optionally exposes the assistant to the internet through a
Tailscale and Caddy gateway.

## Project Structure

- `docker-compose.yml`: OpenClaw and workflows orchestration
- `docker-compose.prod.yml`: production overrides (Tailscale binding)
- `openclaw/`: assistant config: `soul.md` (persona) and `soul-ops.md`
  (ops agent), both rendered and gitignored (`make persona PROFILE=<name>`;
  ops comes from `soul-ops.template.md` plus the profile's `assistant.ops`
  block, with org and location in `.env`), `HEARTBEAT.md` (the ops
  agent's heartbeat checklist), `sync-heartbeat.sh` (on every container
  start, ensures the heartbeat automation job exists, creating it on a fresh
  state dir with the message the proxy intercepts, caps its tools to the
  `max-ops` allow list and reloads `HEARTBEAT.md` into its scratch, then
  marks the container unhealthy if any other heartbeat job is enabled or
  `openclaw secrets audit` finds a plaintext credential;
  OpenClaw 2026.11.5+ runs heartbeats as ordinary `agentTurn` automations,
  not config; upgrade an older state dir with `doctor --fix` first, see
  `docs/DEPLOYMENT_GUIDE.md`),
  `config/openclaw.json` (rendered over the live config on start by
  `render-config.js`: agents, Telegram binding, tool policy; no heartbeat
  keys, which 2026.11.5+ rejects; keeps dreaming, self-learning and
  cross-conversation recall off; the proxy `apiKey` is an env SecretRef,
  never the token, and `tools.fs.workspaceOnly` keeps agents' file tools in
  their workspace), `secrets-plan.json` (one-time migration of the
  plaintext auth-profile copy; never run on start, see
  `docs/DEPLOYMENT_GUIDE.md`)
- `workflows/`: FastAPI service:
  - `app.py`: FastAPI entrypoint. `create_app(brain_db=None)` builds an
    app around its own brain (`app.state.brain_db`, routes read it via
    `Depends`) and mounts the routers; uvicorn serves it as a factory
    (`uvicorn --factory app:create_app` in `entrypoint.sh`), so importing
    `app` builds nothing (no `/docs`, `/redoc` or `/openapi.json`). Only
    tests import `app`: chat commands and the heartbeat get the brain,
    profile and scheduler from `llm_proxy` off `request.app.state`
  - `tasks_api.py`: `TaskCreate`/`TaskResult`, shared by `/task` and `!task`
  - `auth.py`: app-wide bearer auth (`PROXY_AUTH_TOKEN`), fails closed on
    an empty token or the old `generate-a-token-here` placeholder; only
    `GET /healthz`, `GET /healthz/{research,crm,persona}`, `GET /tier` and
    `GET /sched[/{id}]` are public
  - `http_auth.py`: `auth_headers`, the one builder that turns a
    profile's `auth` block into outbound request headers (connector
    sources and the REST CRM adapter)
  - `clock.py`: `utcnow_iso`, the timestamp format every store writes
  - `errors.py`: `UpstreamError`, raised when a scanner, the LLM tiers or
    a fetch failed, so callers never read an outage as an empty result
  - `rundown.py`, `deals.py`, `deal_sources.py`: daily outreach and
    homelab deal rundowns
  - `brain.py`, `brain_db.py`: persistent knowledge store and task queue
  - `embeddings.py`: OpenRouter embeddings (`EMBED_MODEL`/`EMBED_DIM`), one
    sqlite-vec table per model, 10-minute backfill job; hybrid search embeds
    the query and falls back to keyword-only
  - `retrieval_eval.py`: offline search eval (hit@k, MRR, nDCG, paired
    bootstrap) over a fixture corpus and golden set with cached vectors in
    `tests/fixtures/retrieval/`; the pre-push pytest run gates production
    and the no-vector fallback (overall and per query tag) against
    `baseline.json`. Real-brain mode keeps its golden set under
    `data/retrieval/` (gitignored)
  - `llm_proxy.py`: OpenAI-compatible chat and embeddings routes, tiered
    routing (chat races the free tiers, background `_llm_call` tries them
    in order), web-search routing to the OpenRouter `:online` tier,
    anti-hallucination guard, balance circuit breaker; dispatches chat
    commands and heartbeats to the modules below
  - `chat_commands.py`: `/tier`, `!help`, `!task`, `!persona` (alias
    `!learn`), `/sched`, `/scan` and `/projects` chat commands handled
    inside the proxy
  - `heartbeat.py`: heartbeat interceptor (`_handle_heartbeat`, task
    selection and settling, stale-active reaping, proactive scan, thinking
    prompts, soul.md loading) plus the heartbeat-only helpers and state
    moved out of `llm_proxy`; reads the LLM, balance and breaker helpers
    from `llm_proxy` at call time. A task `active` longer than
    `HEARTBEAT_ACTIVE_REAP_HOURS` (default 6) is failed; a task that hits
    an LLM outage goes back to pending and is failed on its 6th outage
    cycle (`LLM_RETRY_LIMIT`), its digest line naming each tier's cause
    from `llm_proxy.describe_last_failure()`. The `Stale queue:` nudge
    closes from the live queue, never the LLM
  - `digest.py`: hourly heartbeat digest buffer, message planner and the
    heartbeat stamps the watchdog reads. Heartbeat and digest log through
    the `clawrange.*` loggers (`clawrange.heartbeat`, `clawrange.digest`)
  - `research_router.py`: routes heartbeat tasks to the marketing scanners
    and the research orchestrator
  - `scheduler.py`, `generators.py`: APScheduler jobs and generators
  - `watchdog.py`: heartbeat watchdog — alerts Telegram directly when
    OpenClaw heartbeats stall during active hours
  - `reddit_search.py`, `github_search.py`: marketing scanners
  - `telegram.py`: Telegram delivery
  - `tenant_profile.py`: declarative profile loader/validator/env-resolver
  - `persona.py`: render `soul.md` and `identity.md` from a profile's
    persona/identity blocks. Compose the non-destructive `## Learned`
    overlay and atomically `render_all` to the configured targets
  - `persona_learning.py`: meta-learning pipeline: `propose`/`approve`/
    `reject`, brain↔`learned.yaml` overlay (`seed_overlay`/`export_overlay`/
    `load_overlay`), flagged outcome-signal scan
  - `persona_api.py`: `/persona/*` router (propose/approve/reflect/render/
    health), mounted in `app.py`
  - `connectors/`: source→transform→sink registry (`http_csv`,
    `login_scrape`, `leads_clean`, `crm` sink, `run_connector`)
  - `crm/`: pluggable `CRMAdapter` (SQLite default and REST seam) and
    read-only query templates / NL router (`crm/query.py`)
  - `crm_api.py`: `/crm/*` router (mounted only when profile defines `crm`)
  - `tests/`: pytest suite (app, brain, llm_proxy, marketing, telegram,
    profile, persona, persona_api, persona_learning, connectors, crm,
    crm_api, lifespan)
- `deerflow/`: DeerFlow research agent config (optional, OpenRouter)
- `scripts/`: POSIX shell scripts for lifecycle and testing
- `tests/`: Python validation suite (`validate_stack.py`)
- `docs/`: testbed and deployment guides, the multi-tenant and
  research/marketing operator guides, `tradeoffs.md` (decision journal).
  Plans and specs are working files: record the lasting decisions in
  `tradeoffs.md` when the work ships, then delete them. `docs/archive/`
  is gitignored for local scratch

## Key Conventions

- All LLM calls route through the workflows `llm_proxy` (which fans out
  to OpenRouter and the `claude-sub` bridge). Never call
  Anthropic/OpenAI/ByteDance APIs directly: Claude is reached only
  through `scripts/claude_bridge.py`, which drives the operator's
  logged-in `claude` CLI in tmux (subscription, not API credit).
- Shell scripts use **Bash** (`#!/usr/bin/env bash`) and must work on
  macOS and Ubuntu.
- Python tooling is uv-managed: ruff, mypy, and pytest run via `uv run`
  against the dev group pinned in `uv.lock` (see `pyproject.toml`).
  Pre-commit enforces ruff, ruff-format, and mypy on every commit and the
  full pytest suite at pre-push; `make lint` mirrors the commit gate.
  `make setup` (or `make hooks`) installs both git hooks.
- Docker images are pinned (`ghcr.io/openclaw/openclaw:2026.11.6`, and
  `python:3.12.16-slim-trixie` under `workflows/Dockerfile`). Bump them
  on purpose; never switch back to `:latest`.
- `.env` is gitignored. `.env.example` is the template.
- OpenClaw runs internally on port 18789, mapped to host port 3000.
- Ports bind `127.0.0.1` plus `DOCKER_HOST_IP` (docker0, for sibling
  compose projects); prod adds `TAILSCALE_IP`. Never `0.0.0.0`: under WSL
  mirrored networking that is LAN-reachable.
- Dev compose bind-mounts `./workflows` and runs `uvicorn --reload`; prod
  (`make start-prod`) runs the baked image without reload, so code changes
  need a rebuild.
- Workflows runs on port 5678.
- DeerFlow runs via its own docker-compose (multi-service), connected to
  `msp-network`.
- Single uvicorn worker is a hard requirement: APScheduler 3.x (pinned
  `<4`) runs in single-process mode.
- Brain database lives at `data/brain/brain.db` (bind-mounted into the
  workflows container at `/data/brain.db`).

## Common Commands

```bash
make start            # bring up OpenClaw and workflows
make start-full       # include DeerFlow
make start-prod       # bind to 127.0.0.1 and Tailscale IP only
make test             # run validation suite
make test-unit        # pytest workflows/tests/ in parallel (PYTEST_WORKERS=0: serial)
make retrieval-eval   # compare search configs offline (fixture golden set)
make retrieval-harvest   # real brain searches -> data/retrieval/golden.json
make retrieval-eval-brain # same matrix on the real brain (label queries first)
make health           # quick curl health checks
make claude-bridge    # host bridge for the claude-sub tier (or the systemd unit, docs/DEPLOYMENT_GUIDE.md)
make logs             # tail docker logs

# Python quality — uv-managed (ruff + mypy + pytest via pyproject.toml/uv.lock)
make lint             # ShellCheck + ruff + mypy (mirrors the pre-commit gate)
make format           # check YAML/JSON + ruff format (no writes)
make format-fix       # apply ruff formatting + safe auto-fixes
make typecheck        # uv run mypy workflows scripts tests
make py-lint          # uv run ruff check .
```

## API Endpoints

Every route requires `Authorization: Bearer $PROXY_AUTH_TOKEN` except
`GET /healthz`, `/healthz/{research,crm,persona}`, `GET /tier`, `GET /sched`
and `GET /sched/{id}` (polled by the homepage dashboard). Anonymous `/sched`
reads omit `kwargs` and reduce `last_status` to `ok`/`failed`. An unset
token fails closed (401), and `scripts/start.sh` refuses to start the
stack without one. There is no `/docs`, `/redoc` or `/openapi.json`.

**Health and tiers**
- `GET  /healthz` (port 3000): OpenClaw health
- `GET  /healthz` (port 5678): Workflows health. Always 200 while the
  process serves; `status` is `ok` or `degraded`, `problems` names the
  broken parts (`db`, `vector_search`, `scheduler`, `profile`), plus
  `brain` (page and vector counts, `vector_search`), `scheduler` and
  `profile` blocks. Anonymous callers get the problem names but no error
  text; a bearer-token request gets the detail.
- `GET  /tier`               : current tier status and balance
- `POST /tier/notify`        : push tier status to Telegram

**LLM proxy**
- `POST /v1/chat/completions`: OpenAI-compatible chat (port 3000 via
  OpenClaw, or port 5678 directly on workflows)
- `POST /v1/embeddings`: OpenAI-compatible embeddings through OpenRouter
  with the configured `EMBED_MODEL`, for any bearer-token client

**Task queue**
- `POST /task`               : create task
- `GET  /task`               : list tasks (optional `?status=`)
- `GET  /task/{id}`, `POST /task/{id}/claim`, `POST /task/{id}/result`
  (`status` is `completed`, `failed` or `blocked`), `DELETE /task/{id}`

**Brain (persistent knowledge)**
- `GET /brain/search`, `GET /brain/tags`, `GET/POST /brain/pages`
- `GET/DELETE /brain/pages/{slug}`; per page `GET/POST .../timeline`,
  `GET/POST .../links`, `DELETE .../links/{link_id}`, `GET/POST .../tags`,
  `DELETE .../tags/{tag_name}`, `GET .../versions`, `GET .../chunks`,
  `GET .../graph`. See `workflows/brain.py`. Slugs may contain `/`
  (`client/acme`); search `mode` is `hybrid` (default), `keyword` or
  `vector`; graph `depth` defaults to 2, capped at 5. No embeddings route lives
  here: the backfill job writes page vectors, and `POST /v1/embeddings`
  is the only embeddings endpoint.

**Marketing orchestrator**
- `GET/POST/DELETE /projects` and `/projects/{slug}`
- `GET/POST/PATCH/DELETE /sched` and `/sched/{id}`, `POST /sched/{id}/run`
  (returns `{status: ok|delivery_failed|error, schedule_id, last_status}`)
- `POST /scan/reddit`, `POST /scan/github`, `POST /scan/web`: a scan whose
  upstream failed returns 502 with the reason, never an empty 200

**Research orchestrator**
- `POST /research`: multi-source research across up to 5 channels
  (`discourse` Reddit, `code` GitHub, `discourse_web` OpenRouter
  `:online` web search, `academic` arXiv and Semantic Scholar, `triz`
  cross-domain); a request without `channels` runs `discourse`, `code`
  and `discourse_web`. Dedup, ranking, triangulation, and confidence
  flags. Persists a session and returns `session_id`. Partial channel
  failures land in `errors`; when every channel fails it returns 502.
- `GET /research/sessions`: list recent sessions, newest first
- `GET /research/sessions/{id}`: full session with all findings
- `GET /healthz/research`: per-channel configured/source/reason
  report so operators can diagnose empty results. `discourse_web` and
  `triz` report source `openrouter-online` and count as configured only
  when `OPENROUTER_API_KEY` is set.
- See `workflows/research.py` for synthesis logic and
  `docs/research-and-marketing.md` for the operator guide.

**Claude subscription bridge (local-only, `claude-sub` tier)**
- `scripts/claude_bridge.py` (`make claude-bridge`) answers
  `POST /v1/chat/completions` on 127.0.0.1 and `DOCKER_HOST_IP`, port
  8790, behind `CLAUDE_BRIDGE_TOKEN`; the workflows container reaches it
  as `host.docker.internal`. Stdlib only.
- Each request gets a throwaway tmux `claude` session: the request is
  rendered into `--system-prompt-file` (nothing untrusted is typed),
  `--tools ""`, `--safe-mode`, `--strict-mcp-config`,
  `--permission-mode dontAsk`, the advisor tool off, an empty 0700
  working directory and a minimal env. The reply comes from the session
  transcript; error records map to 429/503/502, never reply text. A
  full queue is 503 (a 429 trips the proxy breaker at once), and a
  caller that hangs up (a lost race) gets its session killed.
- OpenClaw's tools stay OpenClaw's: `tools` become a `<tool_calls>`
  contract and the reply becomes OpenAI `tool_calls`. Night-market's
  injection patterns are redacted from tool results (structural markup
  only from other turns). `!sub` forces the tier; `!claude` is still
  OpenRouter paid.

**Tome bridge (local-only)**
- `scripts/tome_bridge.py` polls the workflows task queue for
  tasks tagged `research:tome:` and runs them through the operator's
  local `claude /tome:research` session. Stdlib only, no deps. The
  topic is untrusted queue text, so the session gets only `WebSearch`,
  `WebFetch`, `Skill` and `Agent` (`--tools`/`--allowedTools`,
  `--permission-mode dontAsk`, `--strict-mcp-config`) and runs in an
  empty temp directory, never the repo. It never sees `PROXY_AUTH_TOKEN`
  (the bridge strips it) or the rest of `.env` (the make targets read
  only the token; a systemd unit uses a token-only `EnvironmentFile`).
- Make targets: `make tome-bridge` (one pass) and
  `make tome-bridge-watch` (poll forever).

**CRM (lead-crm profile, mounted only when the profile defines `crm`)**
- `POST /crm/query` `{prompt}`: NL question → `{answer, template, params, rows}`
- `POST /crm/query/run` `{template, params}`: run a query template directly
- `GET  /crm/templates`: list available query templates
- `GET  /crm/leads?status=&limit=`: list leads
- `POST /crm/sync/{connector_id}`: run a connector now → counts
- `GET  /healthz/crm`: CRM adapter health (`status`, `adapter`, `leads`
  only; never the DB path or REST base URL), connectors and templates;
  503 when the adapter reports an error
- See `docs/multi-tenant-guide.md` for profile authoring.

**Persona & meta-learning (profile-driven, identity block optional)**
- `POST /persona/propose` `{kind, target, content, source}`: queue a
  persona/identity enhancement as a pending learning and `[DRAFT]` task
- `GET  /persona/proposals?status=&profile=`: list proposals
- `POST /persona/proposals/{id}/approve`: approve → append and atomic re-render
- `POST /persona/proposals/{id}/reject`: reject (idempotent)
- `POST /persona/render`: re-render current profile to its targets
- `POST /persona/reflect`: on-demand reflection (source=`reflect`)
- `GET  /persona/identity`, `GET /persona/soul`: current rendered text
- `GET  /healthz/persona`: profile, pending count, target names and
  `writable` (per target group, whether a render could replace every
  file it writes; no paths)
- Render via `make persona PROFILE=<name>` (`make profile` renders the
  same files and also sets `CLAWRANGE_PROFILE` in `.env`). Export approved learnings with
  `make persona-export`. Offline loop demo via `make persona-demo`.
  `PERSONA_SIGNAL_LEARNING=1` enables the flagged outcome-signal source.
  See `docs/multi-tenant-guide.md` for profile authoring.

**Canary**
- `POST /webhook/test`: echo payload back

## Research and Marketing Conventions

- **Citation discipline**: every factual claim the assistant makes must
  cite a URL from a recent `/research` finding. Single-source
  claims are flagged "needs verification".
- **Marketing posture**: useful comments first. Product mentions
  only when directly relevant. Drafts queue as `[DRAFT]` tasks for
  human approval: never auto-post.
- **Tracked projects**: `claude-night-market`, `skrills`,
  `simple-resume`, `clawrange`, `personal-brand` (the operator's
  AI-systems voice).
  Seeded from the active profile by `seed_from_profile` on startup
  (the `marketing` profile defines them; the default `starter` has none).
- **Heavy research**: when a question needs academic literature,
  TRIZ analogies, or multi-hop digs, queue a `research:tome` task
  for the local Claude Code session to handle via `/tome:research`.

## When Modifying

- New workflow endpoints go in `workflows/app.py` (or a new module
  registered with `app.include_router`).
- New scheduled generators go in `workflows/generators.py` and must be
  added to the `GENERATORS` registry.
- New connector kinds go in `workflows/connectors/` and must be registered
  in `connectors/__init__.py` AND mirrored in `tenant_profile.KNOWN_*_KINDS`
  so a profile fails validation at load time, not at first cron fire.
- New CRM backends implement `crm.adapter.CRMAdapter` and register in
  `get_adapter`. Tenant-specific config belongs in a profile YAML, never in
  Python. The generic core/template must stay free of any one operator's
  identity.
- New test scripts go in `scripts/tests/<name>.sh` and get a Makefile target.
- Pytest unit tests go in `workflows/tests/test_*.py`. Ask for the
  `brain_db`, `app` and `client` fixtures (fresh per test, from
  `conftest.py`); never share a module-level app or `TestClient`. The
  suite runs in parallel (pytest-xdist: `make test-unit`, the pre-push
  hook and CI pass `-n auto`), so a test must not write a fixed path
  outside `tmp_path` or depend on another test's state.
- Persona edits to `openclaw/soul.md` should keep responses tight
  (Telegram is the main channel: short, actionable messages).
- DeerFlow config must use `base_url: https://openrouter.ai/api/v1`
  for all models.
- Pre-commit hooks run on commit. Do not bypass with `--no-verify`.
