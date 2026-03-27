# ClawRange

Local validation testbed for the AI-powered MSP (Managed Service Provider)
stack. Simulates the production architecture (DigitalOcean cloud gateway +
Lenovo ThinkCentre onsite node) entirely on a single machine using Docker.

## Stack

| Service | Purpose | Port |
|---------|---------|------|
| [OpenClaw](https://github.com/openclaw/openclaw) | AI assistant gateway -- routes client messages through LLMs via OpenRouter | 3000 |
| [n8n](https://n8n.io/) | Workflow automation -- webhooks, CRM lookups, scheduled briefings | 5678 |
| [DeerFlow](https://github.com/bytedance/deer-flow) | Deep research agent -- market analysis, competitor research (optional) | 2026 |
| [Ollama](https://ollama.com/) | Local LLM inference -- for air-gapped/onsite deployment testing | 11434 |

## Requirements

- Docker 24+ and Docker Compose v2
- 8 GB free RAM
- [OpenRouter API key](https://openrouter.ai/settings/keys) ($10+ balance
  recommended)
- Ollama (optional, for local inference testing)

## Quick Start

```bash
make setup          # create .env from template, generate encryption key
# edit .env -- add your OPENROUTER_API_KEY
make start          # bring up OpenClaw + n8n
make test           # run 6-test validation suite
```

See [docs/TESTBED_GUIDE.md](docs/TESTBED_GUIDE.md) for DeerFlow setup,
Ollama testing, cost estimation, and production deployment notes.

## Testing

**Live tests** (requires running services):

```bash
make test                # full 6-test suite against running stack
make test-openclaw       # OpenClaw only
make test-n8n            # n8n workflows only
make test-deerflow       # DeerFlow research layer
make test-ollama         # local Ollama inference
```

**Offline validation** (no services required):

```bash
make validate            # config checks + unit tests
```

This runs `tests/validate_stack.py` (project structure and config validation)
and `tests/test_validate_stack.py` (34 pytest unit tests covering all 8
validation checks).

## Make Targets

**Lifecycle:**
`setup` | `start` | `start-full` | `stop` | `stop-clean` | `restart` | `reset`

**Testing:**
`test` | `test-openclaw` | `test-n8n` | `test-deerflow` | `test-ollama` |
`validate`

**Inspection:**
`health` | `ps` | `logs` | `logs-openclaw` | `logs-n8n`

**Quality:**
`lint` | `format` | `env-check`

Run `make help` for descriptions of each target.

## Test Persona

The testbed ships with "Max", an AI assistant for **Longview Home Center**
(manufactured home dealer in Longview, TX). Max handles questions about
Jessup and Titanium brand homes, FHA/VA/conventional/in-house financing,
and appointment scheduling.

## Architecture

```
Production:
  Cloud (DigitalOcean) --Tailscale VPN--> Onsite (ThinkCentre M75q)
  [OpenClaw + n8n + DeerFlow]              [Ollama]

Testbed (this repo):
  Docker on localhost
  [OpenClaw:3000] <-> [n8n:5678] <-> [DeerFlow:2026]
                                      [Ollama:11434]
```
