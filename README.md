# ClawRange

Personal AI ops stack for [@athola](https://github.com/athola). Wraps an
OpenClaw assistant ("John-117") around a FastAPI workflows service that
holds the persistent brain, task queue, scheduler, and marketing scanners.
Designed to run on a single machine (laptop, ThinkCentre, or droplet) and
optionally exposed to the public internet through a Tailscale + Caddy
gateway.

## Stack

| Service | Purpose | Port |
|---------|---------|------|
| [OpenClaw](https://github.com/openclaw/openclaw) | AI assistant gateway — routes Telegram + chat messages through the LLM proxy | 3000 |
| Workflows (FastAPI) | Brain DB, task queue, LLM proxy, scheduler, Reddit/GitHub scanners — replaces n8n | 5678 |
| [DeerFlow](https://github.com/bytedance/deer-flow) | Deep research agent (optional, for heavy market analysis) | 2026 |
| [Ollama](https://ollama.com/) | Local LLM inference (optional, for air-gapped tests) | 11434 |

## Requirements

- Docker 24+ and Docker Compose v2
- 8 GB free RAM (workflows container is capped at 128 MB; OpenClaw at 2 GB)
- [OpenRouter API key](https://openrouter.ai/settings/keys) with $10+ balance
- Optional: Z.AI key (GLM Tier 2 fallback), Telegram bot token, Reddit
  script-app credentials, GitHub PAT — see `.env.example`

## Quick Start

```bash
make setup          # generates .env from template + a random gateway token
# edit .env -- add OPENROUTER_API_KEY at minimum
make start          # bring up OpenClaw + workflows
make health         # confirm both services answer /healthz
make test           # run the validation suite
```

For Tailscale-secured deployment (cloud gateway + onsite node), see
[docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md). For specs covering
the marketing orchestrator and brain database, see
[docs/specification.md](docs/specification.md) and
[docs/project-brief.md](docs/project-brief.md).

## Testing

**Live tests** (require running services):

```bash
make test            # full validation suite via scripts/test_all.sh
make test-openclaw   # OpenClaw layer only
make test-workflows  # Workflows endpoints (health + canary webhook)
make test-deerflow   # DeerFlow research layer
make test-ollama     # local Ollama inference
```

**Offline validation** (no services required):

```bash
make validate        # tests/validate_stack.py + pytest unit tests
make test-unit       # workflows/tests/ — app, brain, llm_proxy, marketing
```

The Python unit tests under `workflows/tests/` cover the FastAPI app, brain
database, LLM proxy routing, marketing scanners, and Telegram formatting.
`tests/test_validate_stack.py` validates project structure and configs.

## Make Targets

**Lifecycle:**
`setup` | `start` | `start-full` | `stop` | `stop-clean` | `restart` |
`reset` | `start-prod` | `stop-prod`

**Testing:**
`test` | `test-openclaw` | `test-workflows` | `test-deerflow` |
`test-ollama` | `test-unit` | `validate`

**Inspection:**
`health` | `ps` | `logs` | `logs-openclaw` | `logs-workflows`

**Quality:**
`lint` | `format` | `env-check`

Run `make help` for descriptions of each target.

## Persona

The default persona is **John-117** — Alex's executive assistant. He owns
the task queue, watches infrastructure tier health, runs the morning
standup, and drives the marketing scanners for Alex's three tracked
projects (`claude-night-market`, `skrills`, `simple-resume`). The persona
lives in `openclaw/soul.md`. To repurpose this stack for a different
operator, swap that file (and `openclaw/soul-ops.md` for ops mode).

## Architecture

```
Production (Tailscale-secured):
  Internet → DigitalOcean droplet (Caddy + TLS) → Tailscale → Onsite node
                                                              [OpenClaw + Workflows + Ollama]

Local stack (this repo):
  Docker on localhost
  [OpenClaw:3000] ──► [Workflows:5678] ──► brain.db (SQLite)
                              │                │
                              ▼                ▼
                        OpenRouter       APScheduler
                        (LLM tiers)      (cron jobs)
                              │                │
                              ▼                ▼
                          [Z.AI GLM]     Reddit + GitHub scanners
```

The workflows service is a single FastAPI process. It owns:

- `/healthz`, `/tier`, `/tier/notify` — health and tier status
- `/v1/chat/completions` — OpenAI-compatible LLM proxy with tiered
  routing, balance circuit breaker, and anti-hallucination guard
- `/task`, `/task/{id}`, `/task/{id}/claim`, `/task/{id}/result` —
  task queue
- `/brain/*` — persistent knowledge store (pages + embeddings)
- `/projects`, `/sched`, `/scan/{reddit,github,web}` — marketing
  orchestrator (`/scan/web` routes through GLM server-side web search)
- `/research`, `/research/sessions` — multi-source research
  orchestrator with citation flagging and persistent sessions
- `/webhook-test/test` — connectivity canary

### Research Orchestrator

`POST /research` fans out across Reddit, GitHub, and GLM web search
in parallel, then merges and ranks the results with authority
bonuses (stars, scores, citations), recency bonus, and a
cross-channel triangulation bonus capped at +0.15. Each finding is
tagged with a confidence flag (high/medium/low) so John-117 can
mark single-source claims as "needs verification".

Every call persists a session in the brain so earlier research is
recoverable via `GET /research/sessions/{id}` without re-running
the fanout. See [docs/research-and-marketing.md](docs/research-and-marketing.md)
for the full operator guide.

### Marketing Orchestrator

Scheduled scans are driven by APScheduler with six built-in generators
registered in `workflows/generators.py`:

- `morning_scan` — daily Reddit + GitHub scan per tracked project
- `weekly_traffic` — weekly traffic snapshot tasks
- `awesome_lists_watch` — alerts when projects are missing from
  curated awesome-lists
- `custom_scan` — generic topic scan for user-defined schedules
- `content_idea` — turns recent research findings into one content
  idea per project (technical post / Reddit comment / X thread)
- `comment_draft` — drafts a useful, non-promotional reply for a
  given URL and queues it as a `[DRAFT]` task for human approval

The four tracked projects (`claude-night-market`, `skrills`,
`simple-resume`, and `personal-brand` for Alex's AI-systems voice)
are seeded automatically on first boot. Schedules are stored in the
brain DB and managed via `/sched`. Each generator enqueues tasks
into the same queue agents read from `/task/{id}/claim`, so manual
and scheduled work share one pipeline. Drafts are never auto-posted —
the human-in-the-loop pattern is the entire point.
