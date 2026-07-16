# ClawRange

Personal AI ops stack for [@athola](https://github.com/athola). Wraps an
OpenClaw assistant ("John-117") around a FastAPI workflows service that
holds the persistent brain, task queue, scheduler, and marketing scanners.
Designed to run on a single machine (laptop, ThinkCentre, or droplet) and
optionally exposed to the public internet through a Tailscale and Caddy
gateway.

## Stack

| Service | Purpose | Port |
|---------|---------|------|
| [OpenClaw](https://github.com/openclaw/openclaw) | AI assistant gateway: routes Telegram and chat messages through the LLM proxy | 3000 |
| Workflows (FastAPI) | Brain DB, task queue, LLM proxy, scheduler, Reddit/GitHub scanners: replaces n8n | 5678 |
| [DeerFlow](https://github.com/bytedance/deer-flow) | Deep research agent (optional, for heavy market analysis) | 2026 |
| [Ollama](https://ollama.com/) | Local LLM inference (optional, for air-gapped tests) | 11434 |

## Requirements

- Docker 24+ and Docker Compose v2
- 8 GB free RAM (workflows container is capped at 128 MB, OpenClaw at 2 GB)
- [OpenRouter API key](https://openrouter.ai/settings/keys) with $10+ balance
- Optional: Z.AI key (GLM Tier 2 fallback), Telegram bot token, Reddit
  script-app credentials, GitHub PAT: see `.env.example`

## Quick Start

```bash
make setup          # generates .env from template and a random gateway token
# edit .env -- add OPENROUTER_API_KEY at minimum
make start          # bring up OpenClaw and workflows
make health         # confirm both services answer /healthz
make test           # run the validation suite
```

For Tailscale-secured deployment (cloud gateway and onsite node), see
[docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md). For specs covering
the marketing orchestrator and brain database, see
[docs/specification.md](docs/specification.md) and
[docs/project-brief.md](docs/project-brief.md).

## Testing

**Live tests** (require running services):

```bash
make test            # full validation suite via scripts/test_all.sh
make test-openclaw   # OpenClaw layer only
make test-workflows  # Workflows endpoints (health and canary webhook)
make test-deerflow   # DeerFlow research layer
make test-ollama     # local Ollama inference
```

**Offline validation** (no services required):

```bash
make validate        # tests/validate_stack.py and pytest unit tests
make test-unit       # workflows/tests/: app, brain, llm_proxy, marketing
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

## Persona & identity (configurable and self-learning)

The assistant's persona **and** identity are profile-driven, so anyone who
pulls this repo can shape their own assistant declaratively: and the
assistant can meta-learn enhancements to itself over time, gated by your
approval.

**Configure it (per use case).** A profile under
`config/profiles/<name>/profile.yaml` carries an `assistant` block (role,
capabilities, owner context) and an optional `identity` block (name,
creature, vibe, emoji, avatar). Pick a profile with `CLAWRANGE_PROFILE` and
render it:

```bash
cp -r config/profiles/chief-of-staff config/profiles/myassistant   # start from an example
$EDITOR config/profiles/myassistant/profile.yaml                   # set name, role, identity
make persona PROFILE=myassistant                                   # → openclaw/soul.md and identity.md
```

Shipped examples: `starter` (identity-free baseline), `lead-crm`,
`marketing` (a John-117 content-marketing persona), and **`chief-of-staff`
(Max)**: the worked, end-to-end example of the whole system.

**It learns (with your approval).** Three layers keep "others configure"
and "the assistant evolves" reconciled: git-tracked config, a brain-backed
`persona_learnings` store, and a non-destructive `## Learned` render overlay.
The loop:

1. **Propose**: say `!persona <feedback>` (e.g. *"lead with the
   recommendation"*), call `POST /persona/propose`, or let the scheduled
   `persona_reflect` job suggest one. Each proposal queues as a `[DRAFT]`
   task: nothing changes yet.
2. **Approve**: `POST /persona/proposals/{id}/approve` appends it to the
   brain and atomically re-renders the assistant's read surfaces. (Approval
   is API-gated by design: persona changes don't happen from a chat
   command.)
3. **Export**: `make persona-export PROFILE=<name>` writes approved
   learnings to `config/profiles/<name>/learned.yaml`, so the evolved
   persona is reproducible for anyone who pulls the repo.

See the loop run end-to-end offline (no keys, no containers):

```bash
make persona-demo PROFILE=chief-of-staff
```

Full authoring guide: [`docs/multi-tenant-guide.md`](docs/multi-tenant-guide.md).

## Architecture

```
Production (Tailscale-secured):
  Internet → DigitalOcean droplet (Caddy and TLS) → Tailscale → Onsite node
                                                              [OpenClaw, Workflows, and Ollama]

Local stack (this repo):
  Docker on localhost
  [OpenClaw:3000] ──► [Workflows:5678] ──► brain.db (SQLite)
                              │                │
                              ▼                ▼
                        OpenRouter       APScheduler
                        (LLM tiers)      (cron jobs)
                              │                │
                              ▼                ▼
                          [Z.AI GLM]     Reddit and GitHub scanners
```

The workflows service is a single FastAPI process. It owns:

- `/healthz`, `/tier`, `/tier/notify`: health and tier status
- `/v1/chat/completions`: OpenAI-compatible LLM proxy with tiered
  routing, balance circuit breaker, and anti-hallucination guard
- `/task`, `/task/{id}`, `/task/{id}/claim`, `/task/{id}/result`:
  task queue
- `/brain/*`: persistent knowledge store (pages and embeddings)
- `/projects`, `/sched`, `/scan/{reddit,github,web}`: marketing
  orchestrator (`/scan/web` routes through GLM server-side web search)
- `/research`, `/research/sessions`: multi-source research
  orchestrator with citation flagging and persistent sessions
- `/webhook-test/test`: connectivity canary

### Research Orchestrator

`POST /research` fans out across five channels in parallel:
Reddit (`discourse`), GitHub (`code`), GLM web search
(`discourse_web`), arXiv and Semantic Scholar (`academic`), and
TRIZ analogical reasoning (`triz`): then merges and ranks the
results with authority bonuses (stars, scores, citations),
recency bonus, and a cross-channel triangulation bonus capped at
+0.15. Each finding is tagged with a confidence flag (high /
medium / low) so John-117 can mark single-source claims as
"needs verification".

Operators can probe per-channel readiness with `GET /healthz/research`
or run the full smoke test with `make test-research TOPIC="..."`.

Every call persists a session in the brain so earlier research is
recoverable via `GET /research/sessions/{id}` without re-running
the fanout. Heavier work (full tome `/tome:research` synthesis,
academic deep-dives) is handled by `scripts/tome_bridge.py` which
polls for `research:tome:` tasks and runs them through Alex's
local Claude Code session. See
[docs/research-and-marketing.md](docs/research-and-marketing.md)
for the full operator guide.

### Marketing Orchestrator

Scheduled scans are driven by APScheduler with six built-in generators
registered in `workflows/generators.py`:

- `morning_scan`: daily Reddit and GitHub scan per tracked project
- `weekly_traffic`: weekly traffic snapshot tasks
- `awesome_lists_watch`: alerts when projects are missing from
  curated awesome-lists
- `custom_scan`: generic topic scan for user-defined schedules
- `content_idea`: turns recent research findings into one content
  idea per project (technical post / Reddit comment / X thread)
- `comment_draft`: drafts a useful, non-promotional reply for a
  given URL and queues it as a `[DRAFT]` task for human approval

The four tracked projects (`claude-night-market`, `skrills`,
`simple-resume`, and `personal-brand` for Alex's AI-systems voice)
are seeded automatically on first boot. Schedules are stored in the
brain DB and managed via `/sched`. Each generator enqueues tasks
into the same queue agents read from `/task/{id}/claim`, so manual
and scheduled work share one pipeline. Drafts are never auto-posted:
the human-in-the-loop pattern is the entire point.

## Multi-Tenant Template

ClawRange is a pull-down-and-configure template. A declarative **tenant
profile** (`config/profiles/<name>/profile.yaml`) owns everything
tenant-specific: persona, seeded projects/schedules, connector wiring, and
CRM config: while the connector registry and CRM adapters live in code and
are shared. The active profile is chosen by `CLAWRANGE_PROFILE` (default
`marketing`, reproducing the original John-117 setup exactly).

A second profile, `lead-crm`, is a worked business example: an assistant that
hourly syncs leads from a web portal into a pluggable CRM (SQLite by default)
and answers relational and time-series questions about them on a schedule or
via Telegram. Stand up your own by copying the profile and editing YAML:

```bash
cp -r config/profiles/lead-crm config/profiles/acme
$EDITOR config/profiles/acme/profile.yaml   # set profile: acme, fields, connectors
make profile PROFILE=acme                     # render openclaw/soul.md and set .env
make seed-demo                                # optional: load demo leads offline
```

New code modules: `workflows/connectors/` (source/transform/sink registry),
`workflows/crm/` (pluggable `CRMAdapter` and SQLite/REST backends and query
templates), `workflows/tenant_profile.py` (loader/validator),
`workflows/persona.py` (persona renderer), and `workflows/crm_api.py` (the
`/crm/*` router, mounted only when the profile defines a CRM). See
[docs/multi-tenant-guide.md](docs/multi-tenant-guide.md) for the full
operator guide.
