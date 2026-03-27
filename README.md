# ClawRange

Local validation testbed for the AI-powered MSP (Managed Service Provider) stack.

Simulates the production architecture (DigitalOcean cloud gateway + Lenovo ThinkCentre onsite node) entirely on a single machine using Docker.

## Stack

| Service | Purpose | Port |
|---------|---------|------|
| [OpenClaw](https://github.com/openclaw/openclaw) | AI assistant gateway — routes client messages through LLMs via OpenRouter | 3000 |
| [n8n](https://n8n.io/) | Workflow automation — webhooks, CRM lookups, scheduled briefings | 5678 |
| [DeerFlow](https://github.com/bytedance/deer-flow) | Deep research agent — market analysis, competitor research (optional) | 2026 |
| [Ollama](https://ollama.com/) | Local LLM inference — for air-gapped/onsite deployment testing | 11434 |

## Quick Start

```bash
make setup          # create .env from template, generate encryption key
# edit .env — add your OPENROUTER_API_KEY from https://openrouter.ai/settings/keys
make start          # bring up OpenClaw + n8n
make test           # run 6-test validation suite
```

See [docs/TESTBED_GUIDE.md](docs/TESTBED_GUIDE.md) for the full guide including DeerFlow setup, Ollama testing, cost estimation, and production deployment notes.

## Make Targets

```
make start          Start core stack (OpenClaw + n8n)
make start-full     Start full stack including DeerFlow
make stop           Stop all services
make test           Run full validation suite
make health         Quick health check
make logs           Tail service logs
make help           Show all targets
```

## Test Persona

The testbed ships with "Max", an AI assistant for **Longview Home Center** (manufactured home dealer in Longview, TX). Max handles questions about Jessup and Titanium brand homes, FHA/VA/conventional/in-house financing, and appointment scheduling.

## Architecture

```
Production:
  Cloud (DigitalOcean) ──Tailscale VPN──> Onsite (ThinkCentre M75q)
  [OpenClaw + n8n + DeerFlow]              [Ollama]

Testbed (this repo):
  Docker on localhost
  [OpenClaw:3000] ←→ [n8n:5678] ←→ [DeerFlow:2026]
                                      [Ollama:11434]
```

## Requirements

- Docker 24+
- 8 GB free RAM
- OpenRouter API key ($10+ balance recommended)
- Ollama (optional, for local inference testing)
