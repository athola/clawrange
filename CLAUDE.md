# ClawRange — Project Instructions

## What This Is

Docker-based testbed for an AI-powered MSP business. Validates the OpenClaw + n8n + DeerFlow + Ollama stack before deploying to real client sites.

## Project Structure

- `docker-compose.yml` — OpenClaw + n8n orchestration
- `openclaw/` — AI agent config: soul.md (persona), openclaw.json (routing)
- `n8n/workflows/` — importable n8n workflow JSON files
- `deerflow/` — DeerFlow research agent config (OpenRouter, not ByteDance)
- `scripts/` — POSIX shell scripts for lifecycle and testing
- `tests/` — Python validation suite and test message corpus
- `docs/` — detailed testbed guide

## Key Conventions

- All LLM calls route through **OpenRouter** — never call Anthropic/DeepSeek/ByteDance directly
- Shell scripts must be **POSIX-compatible** (macOS + Ubuntu)
- Docker images use `:latest` for testing — pin versions before production
- `.env` is gitignored; `.env.example` is the template
- OpenClaw runs internally on port 18789, mapped to host port 3000
- DeerFlow runs via its own docker-compose (multi-service), connected to `msp-network`

## Common Commands

```bash
make start          # bring up OpenClaw + n8n
make start-full     # include DeerFlow
make test           # run validation suite
make health         # quick curl health checks
make logs           # tail docker logs
```

## API Endpoints

- OpenClaw health: `GET http://localhost:3000/healthz`
- OpenClaw chat: `POST http://localhost:3000/v1/chat/completions` (OpenAI-compatible)
- n8n health: `GET http://localhost:5678/healthz`
- n8n webhooks: `POST http://localhost:5678/webhook-test/<path>`
- DeerFlow health: `GET http://localhost:2026/api/health`

## When Modifying

- New n8n workflows go in `n8n/workflows/` as importable JSON
- New test scripts go in `scripts/test_*.sh` and get a Makefile target
- Keep soul.md under 3 sentences per response guideline
- DeerFlow config must use `base_url: https://openrouter.ai/api/v1` for all models
