# ClawRange: Project Instructions

## What This Is

Personal AI ops stack: an OpenClaw assistant ("John-117") backed by a
FastAPI workflows service that owns the brain database, task queue, LLM
proxy, scheduler, and marketing scanners. Runs in Docker on a single host
and optionally exposes the assistant to the internet through a
Tailscale and Caddy gateway.

## Project Structure

- `docker-compose.yml`: OpenClaw and workflows orchestration
- `docker-compose.prod.yml`: production overrides (Tailscale binding)
- `openclaw/`: assistant config: `soul.md` (persona), `soul-ops.md`
  (ops mode), `HEARTBEAT.md` (5-minute heartbeat instructions),
  `config/openclaw.json` (tool routing)
- `workflows/`: FastAPI service replacing n8n:
  - `app.py`: FastAPI entrypoint and route registration
  - `brain.py`, `brain_db.py`: persistent knowledge store and task queue
  - `llm_proxy.py`: OpenAI-compatible proxy with tiered routing,
    web-search routing, anti-hallucination guard, balance circuit breaker
  - `scheduler.py`, `generators.py`: APScheduler jobs and generators
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
- `docs/`: project brief, specification, testbed and deployment guides

## Key Conventions

- All LLM calls route through the workflows `llm_proxy` (which fans out
  to OpenRouter and Z.AI). Never call Anthropic/OpenAI/ByteDance directly.
- Shell scripts use **Bash** (`#!/usr/bin/env bash`) and must work on
  macOS and Ubuntu.
- Python tooling is uv-managed: ruff, mypy, and pytest run via `uv run`
  against the dev group pinned in `uv.lock` (see `pyproject.toml`).
  Pre-commit enforces ruff, ruff-format, and mypy on every commit and the
  full pytest suite at pre-push; `make lint` mirrors the commit gate.
  `make setup` (or `make hooks`) installs both git hooks.
- Docker images use `:latest` for testing: pin versions before production.
- `.env` is gitignored. `.env.example` is the template.
- OpenClaw runs internally on port 18789, mapped to host port 3000.
- Workflows runs on port 5678 (replaces the n8n port).
- DeerFlow runs via its own docker-compose (multi-service), connected to
  `msp-network`.
- Single uvicorn worker is a hard requirement: APScheduler 4 runs in
  single-process mode.
- Brain database lives at `data/brain/brain.db` (bind-mounted into the
  workflows container at `/data/brain.db`).

## Common Commands

```bash
make start            # bring up OpenClaw and workflows
make start-full       # include DeerFlow
make start-prod       # bind to 127.0.0.1 and Tailscale IP only
make test             # run validation suite
make test-unit        # uv run pytest workflows/tests/
make health           # quick curl health checks
make logs             # tail docker logs

# Python quality — uv-managed (ruff + mypy + pytest via pyproject.toml/uv.lock)
make lint             # ShellCheck + ruff + mypy (mirrors the pre-commit gate)
make format           # check YAML/JSON + ruff format (no writes)
make format-fix       # apply ruff formatting + safe auto-fixes
make typecheck        # uv run mypy workflows scripts tests
make py-lint          # uv run ruff check .
```

## API Endpoints

**Health and tiers**
- `GET  /healthz` (port 3000): OpenClaw health
- `GET  /healthz` (port 5678): Workflows health (includes brain page count)
- `GET  /tier`               : current tier status and balance
- `POST /tier/notify`        : push tier status to Telegram

**LLM proxy**
- `POST /v1/chat/completions`: OpenAI-compatible chat (port 3000 via
  OpenClaw, or port 5678 directly on workflows)

**Task queue**
- `POST /task`               : create task
- `GET  /task`               : list tasks (optional `?status=`)
- `GET  /task/{id}`, `POST /task/{id}/claim`, `POST /task/{id}/result`,
  `DELETE /task/{id}`

**Brain (persistent knowledge)**
- `GET/POST /brain/*`: see `workflows/brain.py` for routes (pages,
  embeddings, search)

**Marketing orchestrator**
- `GET/POST/DELETE /projects` and `/projects/{slug}`
- `GET/POST/PATCH/DELETE /sched` and `/sched/{id}`, `POST /sched/{id}/run`
- `POST /scan/reddit`, `POST /scan/github`, `POST /scan/web`

**Research orchestrator**
- `POST /research`: multi-source research across 5 channels
  (`discourse` Reddit, `code` GitHub, `discourse_web` GLM,
  `academic` arXiv and Semantic Scholar, `triz` cross-domain) with
  dedup, ranking, triangulation, and confidence flags. Persists a
  session and returns `session_id`.
- `GET /research/sessions`: list recent sessions, newest first
- `GET /research/sessions/{id}`: full session with all findings
- `GET /healthz/research`: per-channel configured/source/reason
  report so operators can diagnose empty results.
- See `workflows/research.py` for synthesis logic and
  `docs/research-and-marketing.md` for the operator guide.

**Tome bridge (local-only)**
- `scripts/tome_bridge.py` polls the workflows task queue for
  tasks tagged `research:tome:` and runs them through Alex's
  local `claude /tome:research` session. Stdlib only, no deps.
- Make targets: `make tome-bridge` (one pass) and
  `make tome-bridge-watch` (poll forever).

**CRM (lead-crm profile, mounted only when the profile defines `crm`)**
- `POST /crm/query` `{prompt}`: NL question → `{answer, template, params, rows}`
- `POST /crm/query/run` `{template, params}`: run a query template directly
- `GET  /crm/templates`: list available query templates
- `GET  /crm/leads?status=&limit=`: list leads
- `POST /crm/sync/{connector_id}`: run a connector now → counts
- `GET  /healthz/crm`: CRM adapter health and configured connectors
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
- `GET  /healthz/persona`: profile, render-target writability, pending count
- Render via `make persona PROFILE=<name>`. Export approved learnings with
  `make persona-export`. Offline loop demo via `make persona-demo`.
  `PERSONA_SIGNAL_LEARNING=1` enables the flagged outcome-signal source.
  See `docs/multi-tenant-guide.md` for profile authoring.

**Canary**
- `POST /webhook-test/test`: echo payload back

## Research and Marketing Conventions

- **Citation discipline**: every factual claim John-117 makes must
  cite a URL from a recent `/research` finding. Single-source
  claims are flagged "needs verification".
- **Marketing posture**: useful comments first. Product mentions
  only when directly relevant. Drafts queue as `[DRAFT]` tasks for
  human approval: never auto-post.
- **Tracked projects**: `claude-night-market`, `skrills`,
  `simple-resume`, `personal-brand` (Alex's AI-systems voice).
  Seeded by `seed_default_projects` on workflows startup.
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
- New test scripts go in `scripts/test_*.sh` and get a Makefile target.
- Pytest unit tests go in `workflows/tests/test_*.py`.
- Persona edits to `openclaw/soul.md` should keep responses tight
  (Telegram is the main channel: short, actionable messages).
- DeerFlow config must use `base_url: https://openrouter.ai/api/v1`
  for all models.
- Pre-commit hooks run on commit. Do not bypass with `--no-verify`.
