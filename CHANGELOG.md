# Changelog

Notable changes to ClawRange. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/). While the version is
below 1.0, a minor bump may carry breaking changes; they are listed under
**Upgrading** first.

## [0.2.0] - 2026-11-16

The first entry. It summarises the work since `v0.1.0` by theme rather
than commit by commit; `git log v0.1.0..v0.2.0` has the detail.

### Upgrading

- **n8n is gone.** The FastAPI workflows service on port 5678 replaces
  it, along with `scripts/test_n8n.sh`.
- **Every workflows route needs a bearer token** except the health
  checks, `GET /tier` and `GET /sched[/{id}]`. Set `PROXY_AUTH_TOKEN` in
  `.env` (`openssl rand -hex 32`); `make start` refuses to start with it
  empty or left at the template placeholder.
- **`/docs`, `/redoc` and `/openapi.json` are no longer served.**
- **Tenant config moved into profiles.** Timezone, tracked projects,
  outreach policy and deal targets live in
  `config/profiles/<name>/profile.yaml`, selected by `CLAWRANGE_PROFILE`
  (default `starter`). Run `make profile PROFILE=<name>` after pulling.
- **Dev tooling runs through uv.** Run `make setup` (or `make hooks`) to
  install the pre-commit and pre-push hooks.

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
  session through `scripts/tome_bridge.py`.
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
- **Ops**: heartbeat task processing with an hourly digest, a watchdog
  that alerts Telegram when heartbeats stall, and a `/healthz` that names
  the degraded part (`db`, `vector_search`, `scheduler`, `profile`).
- Secure gateway deployment via Tailscale and Caddy (`make start-prod`).

### Changed

- `llm_proxy.py` split into `chat_commands.py`, `heartbeat.py` and
  `digest.py`.
- Docker images are pinned (`ghcr.io/openclaw/openclaw:2026.11.3`,
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

[0.2.0]: https://github.com/athola/clawrange/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/athola/clawrange/releases/tag/v0.1.0
