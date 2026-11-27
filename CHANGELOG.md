# Changelog

Notable changes to ClawRange. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/). While the version is
below 1.0, a minor bump may carry breaking changes; they are listed under
**Upgrading** first.

## [Unreleased]

### Upgrading

- **The Z.AI GLM tier is gone.** `zai-direct` is replaced by
  `claude-sub`, served by the host bridge `scripts/claude_bridge.py`
  (`make claude-bridge`) through your logged-in `claude` CLI in tmux.
  Set `CLAUDE_BRIDGE_TOKEN` in `.env` (and drop `ZAI_API_KEY`), run the
  bridge, then restart the stack. The `!zai`/`!glm` hints no longer
  route; use `!sub`.

## [0.2.0] - 2026-11-21

The first entry. It summarises the work since `v0.1.0` by theme rather
than commit by commit; `git log v0.1.0..v0.2.0` has the detail.

### Upgrading

- **n8n is gone.** The FastAPI workflows service on port 5678 replaces
  it, along with `scripts/test_n8n.sh`.
- **Every workflows route needs a bearer token** except the health
  checks, `GET /tier` and `GET /sched[/{id}]`. Set `PROXY_AUTH_TOKEN` in
  `.env` (`openssl rand -hex 32`); `make start` refuses to start with it
  empty or left at the template placeholder. Anything that runs
  `scripts/tome_bridge.py` directly, such as a systemd unit, needs the
  token too: give it a token-only `EnvironmentFile` (see "Heavy research
  via tome" in `docs/research-and-marketing.md`).
- **OpenClaw 2026.11.6 retires heartbeat config.** With a state dir from
  an earlier image, stop OpenClaw, back it up and run
  `openclaw doctor --fix --yes` before the first start (see "Upgrading
  OpenClaw" in `docs/DEPLOYMENT_GUIDE.md`); otherwise `render-config.js`
  exits non-zero and the container stops at start. The heartbeat's cadence
  and active hours are edited with `openclaw automations edit`.
- **The proxy token leaves OpenClaw's state.** On an install set up before
  0.2.0's SecretRef change, the startup secrets audit reports a plaintext
  `profiles.clawrange-proxy:default.key`. Run "Moving the proxy token out
  of OpenClaw's state" in `docs/DEPLOYMENT_GUIDE.md` once. It also
  recreates the heartbeat job, whose history is lost. Then rotate
  `PROXY_AUTH_TOKEN` ("Rotating PROXY_AUTH_TOKEN"), since older backups
  hold the old value.
- **Smoke-test scripts moved.** `scripts/test_*.sh` are now
  `scripts/tests/*.sh` without the `test_` prefix. The make targets are
  unchanged; update anything that calls the scripts by path.
- **`/docs`, `/redoc` and `/openapi.json` are no longer served.**
- **Tenant config moved into profiles.** Timezone, tracked projects,
  outreach policy and deal targets live in
  `config/profiles/<name>/profile.yaml`, selected by `CLAWRANGE_PROFILE`
  (default `starter`). Run `make profile PROFILE=<name>` after pulling.
- **Dev tooling runs through uv** (0.13.9 or newer). Run `make setup` (or
  `make hooks`) to install the pre-commit and pre-push hooks.

### Added

- **Multi-tenant profiles**: a declarative profile loader and validator,
  shipped `starter`, `marketing`, `lead-crm` and `chief-of-staff`
  examples, and a connector registry (source, transform, sink).
- **CRM**: a pluggable `CRMAdapter` (SQLite default, REST seam), read-only
  query templates with a natural-language router, and `/crm/*` routes,
  mounted only when the profile defines `crm`.
- **Persona learning**: profile-rendered `soul.md` and `identity.md`, an
  approval-gated propose/approve/reject loop (`/persona/*`, `!persona`),
  scheduled `persona_reflect`, and a git-tracked `learned.yaml` overlay
  with `make persona-export`.
- **Research orchestrator**: `POST /research` across Reddit, GitHub,
  OpenRouter `:online` web search, arXiv/Semantic Scholar and TRIZ, with
  ranking, triangulation, confidence flags, persisted sessions and
  `GET /healthz/research`. Heavy topics route to a local Claude Code
  session through `scripts/tome_bridge.py`, which gets no credentials:
  the bridge strips `PROXY_AUTH_TOKEN` and the make targets pass it only
  that token from `.env`.
- **Marketing orchestrator**: scheduled scans, the 8am `morning_digest`
  outreach rundown, `hot_pulse`, content ideas and `[DRAFT]` comment
  drafts queued for human approval. 14 generators in all.
- **Homelab deal rundown** (`homelab_deals`) over eBay, Slickdeals,
  ServeTheHome and Reddit, with a per-source status line in every report.
- **Brain**: persistent pages, timeline, links, tags, versions and graph;
  hybrid search with OpenRouter embeddings in sqlite-vec, falling back to
  keyword search; and an offline retrieval eval (`make retrieval-eval`)
  whose baseline gates every push.
- **LLM proxy**: OpenAI-compatible chat and `/v1/embeddings`, tiered
  routing across OpenRouter and Z.AI, an anti-hallucination guard and a
  balance circuit breaker.
- **Ops**: a heartbeat that OpenClaw runs as an automation job
  (`openclaw/sync-heartbeat.sh` ensures it, caps its tools and flags any
  other enabled heartbeat job as unhealthy) and the
  proxy answers in Python: it works one task per cycle, sends an hourly
  digest whose outage lines name each tier's cause (`out of credit`,
  `rate-limited`, `empty reply`), and closes its `Stale queue:` nudge from
  the live queue. Plus a watchdog that alerts Telegram when heartbeats
  stall, and a `/healthz` that names the degraded part (`db`,
  `vector_search`, `scheduler`, `profile`).
- Secure gateway deployment via Tailscale and Caddy (`make start-prod`).
- **Credential hygiene for OpenClaw**: the proxy `apiKey` is an env
  SecretRef that is never written to disk, agents' file tools are
  confined to their workspace (`tools.fs.workspaceOnly`), and a startup
  `openclaw secrets audit` marks the container unhealthy on any plaintext
  credential at rest.

### Changed

- `llm_proxy.py` split into `chat_commands.py`, `heartbeat.py` and
  `digest.py`.
- Docker images are pinned (`ghcr.io/openclaw/openclaw:2026.11.6`,
  `python:3.12.16-slim-trixie`).
- Ports bind `127.0.0.1` and the docker0 address only, never `0.0.0.0`.

### Fixed

- Scanner, LLM and fetch outages surface as errors (`UpstreamError`, HTTP
  502) instead of empty results.
- Stale `active` tasks are reaped after `HEARTBEAT_ACTIVE_REAP_HOURS`, and
  a task that keeps hitting LLM outages fails after `LLM_RETRY_LIMIT`
  cycles instead of retrying forever.
- Persona reloads fail closed when the profile breaks.

## [0.1.0] - 2026-03-27

Initial testbed: OpenClaw with n8n, DeerFlow and Ollama under Docker
Compose, plus CI, Makefile targets and the validation suite.

[Unreleased]: https://github.com/athola/clawrange/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/athola/clawrange/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/athola/clawrange/releases/tag/v0.1.0
