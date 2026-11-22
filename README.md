# ClawRange

Personal AI ops stack for [@athola](https://github.com/athola). Wraps an
OpenClaw assistant around a FastAPI workflows service that holds the
persistent brain, task queue, scheduler, and marketing scanners.
Designed to run on a single machine (laptop, ThinkCentre, or droplet) and
optionally exposed to the public internet through a Tailscale and Caddy
gateway.

## Stack

| Service | Purpose | Port |
|---------|---------|------|
| [OpenClaw](https://github.com/openclaw/openclaw) `2026.11.6` | AI assistant gateway: routes Telegram and chat messages through the LLM proxy | 3000 |
| Workflows (FastAPI) | Brain DB, task queue, LLM proxy, scheduler, Reddit/GitHub scanners | 5678 |
| [DeerFlow](https://github.com/bytedance/deer-flow) | Deep research agent (optional, `make start-full`) | 2026 |

`make test-ollama` also probes a local [Ollama](https://ollama.com/) on
port 11434 if you run one; the stack does not start it.

## Requirements

- Docker 24+ and Docker Compose v2
- 8 GB free RAM (the workflows container is capped at 128 MB, OpenClaw at
  2 GB)
- [OpenRouter API key](https://openrouter.ai/settings/keys) with $10+ balance
- Optional: Z.AI key (GLM fallback tier), Telegram bot token, Reddit
  script-app credentials, GitHub PAT: see `.env.example`

## Quick Start

```bash
make setup          # creates .env from the template, a gateway token, git hooks
# edit .env: add OPENROUTER_API_KEY and a PROXY_AUTH_TOKEN, e.g.
#   echo "PROXY_AUTH_TOKEN=$(openssl rand -hex 32)" >> .env
make start          # bring up OpenClaw and workflows
make health         # confirm both services answer /healthz
make test           # run the validation suite
```

`make start` refuses to start with `PROXY_AUTH_TOKEN` empty: the
workflows API fails closed, so every protected route would answer 401.
On a fresh clone it also renders `openclaw/soul.md` from the active
profile before the containers start.

To see one real end-to-end run (scheduler, LLM proxy and Telegram), fire
a seeded job instead of waiting for its cron. The default `starter`
profile seeds no jobs; the `marketing` example seeds `morning_digest`,
`hot_pulse`, `daily-content-idea`, `homelab_deals` and
`daily-research-pulse`:

```bash
set -a; . ./.env; set +a                                  # load PROXY_AUTH_TOKEN
curl -s localhost:5678/sched                              # seeded jobs
curl -s -X POST localhost:5678/sched/<id>/run \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN"            # run one now
curl -s localhost:5678/tier                               # tier and balance
```

Each rundown always sends, so an empty Telegram means delivery failed,
not that nothing matched. The report's `Setup needed` block names any
missing credential.

**Upgrading an existing install** from an older OpenClaw pin: run the
doctor step in [Upgrading OpenClaw](docs/DEPLOYMENT_GUIDE.md#upgrading-openclaw)
before the first start, or the container stops at start.

## What's inside

- **Profiles.** Persona, identity, tracked projects, schedules,
  connectors and CRM are profile YAML under `config/profiles/<name>/`,
  selected by `CLAWRANGE_PROFILE`. Examples: `starter`, `marketing`,
  `lead-crm`, `chief-of-staff`.
- **Approval-gated persona learning.** `!persona <feedback>` queues a
  `[DRAFT]` proposal; approved ones re-render the persona and export to
  git with `make persona-export`.
- **Tiered LLM proxy.** OpenAI-compatible `/v1/chat/completions` and
  `/v1/embeddings`, racing the free tiers, with a balance floor and
  circuit breakers.
- **Research.** `POST /research` fans out across Reddit, GitHub, web,
  academic and TRIZ channels with citation flags; `research:tome:` tasks
  run through your local Claude Code via `scripts/tome_bridge.py`.
- **Marketing scans.** 14 scheduled generators enqueue drafts into the
  task queue. Nothing is auto-posted.
- **Heartbeat.** An OpenClaw automation job every 10 minutes, answered in
  Python: works one task, reaps stuck ones, sends an hourly digest.

```
[OpenClaw:3000] ──► [Workflows:5678] ──► brain.db (SQLite)
                         │                    │
                         ▼                    ▼
                  OpenRouter / Z.AI      APScheduler
                  (LLM tiers)            (generators, scanners)
```

## Documentation

| Page | For |
|------|-----|
| [docs/multi-tenant-guide.md](docs/multi-tenant-guide.md) | Writing a profile: persona, connectors, CRM, schedules, meta-learning |
| [docs/research-and-marketing.md](docs/research-and-marketing.md) | Running research, the tome bridge, and the marketing generators |
| [docs/TESTBED_GUIDE.md](docs/TESTBED_GUIDE.md) | A local testbed |
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Tailscale and Caddy deployment, upgrades, troubleshooting |
| [docs/tradeoffs.md](docs/tradeoffs.md) | Design decisions and what each gave up |
| [CLAUDE.md](CLAUDE.md#api-endpoints) | Every HTTP route, and conventions for changing the code |
| [CHANGELOG.md](CHANGELOG.md) | Changes per release, with upgrade notes first |

## Testing

**Live tests** (require running services):

```bash
make test            # full validation suite via scripts/tests/all.sh
make test-openclaw   # OpenClaw layer only
make test-workflows  # Workflows endpoints (health and canary webhook)
make test-deerflow   # DeerFlow research layer
make test-research TOPIC="..."   # one /research fan-out
```

**Offline** (no services required):

```bash
make validate        # tests/: structure, compose, Makefile and config checks
make test-unit       # workflows/tests/: the service's pytest suite
make coverage        # the same suite, failing under 85% coverage
make retrieval-eval  # offline brain-search quality against a golden set
make persona-demo    # the persona learning loop, no keys or containers
```

`make setup` installs the git hooks: ruff and mypy on every commit, the
full pytest suite (including the retrieval-eval baseline gate) on push.

## Make Targets

**Lifecycle:**
`setup` | `start` | `start-full` | `stop` | `stop-clean` | `restart` |
`reset` | `start-prod` | `start-prod-full` | `stop-prod` | `clean-volumes`

**Profiles and persona:**
`profile` | `persona` | `persona-export` | `persona-demo` | `seed-demo`

**Testing:**
`test` | `test-openclaw` | `test-workflows` | `test-deerflow` |
`test-ollama` | `test-research` | `test-unit` | `coverage` | `validate`

**Retrieval eval:**
`retrieval-eval` | `retrieval-eval-refresh` | `retrieval-harvest` |
`retrieval-eval-brain`

**Research bridge:**
`tome-bridge` | `tome-bridge-watch`

**Inspection:**
`health` | `ps` | `logs` | `logs-openclaw` | `logs-workflows` |
`models-check`

**Quality:**
`lint` | `py-lint` | `typecheck` | `format` | `format-fix` | `hooks` |
`env-check`

Run `make help` for descriptions of each target.
