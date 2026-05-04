# ClawRange Testbed Guide

Local validation environment for the ClawRange stack:
OpenClaw + Workflows (FastAPI) + optional DeerFlow + optional Ollama.

## Quick Start

```bash
# 1. Clone or navigate to the testbed
cd ~/clawrange

# 2. Create your environment file and fill in your OpenRouter API key
#    Get one at https://openrouter.ai/settings/keys — keep balance above $10
make setup
nano .env

# 3. Start the core stack (OpenClaw + Workflows)
make start

# 4. Confirm services are healthy
make health

# 5. Run validation tests
make test

# 6. (Optional) Start with DeerFlow research layer
make start-full

# 7. (Optional) Test local Ollama inference
make test-ollama
```

Total setup time: under 10 minutes (excluding Docker image pulls).

## What Each Service Does

**OpenClaw** is the AI assistant gateway. It receives messages over
Telegram (and HTTP), routes them through the workflows LLM proxy, and
returns responses shaped by `openclaw/soul.md`. The default persona is
"John-117", the operator's executive assistant. Accessible at
http://localhost:3000.

**Workflows** is the FastAPI service that replaced n8n. It owns the
brain database (SQLite + optional embeddings), the persistent task
queue, the OpenAI-compatible LLM proxy with tiered routing, the
APScheduler-driven cron jobs, and the marketing scanners
(Reddit + GitHub). Endpoints live at http://localhost:5678 — see
[specification.md](specification.md) for the full surface.

**DeerFlow** is the deep research layer (optional). When a question
needs broad research — market analysis, regulatory deep dives — DeerFlow
dispatches sub-agents that search the web, synthesize findings, and
produce structured reports. Overkill for most daily ops. Runs at
http://localhost:2026 when started with `--with-deerflow`.

**Ollama** provides local LLM inference for air-gapped or onsite
deployment. When the host needs AI responses without internet
dependency, Ollama runs a small model on the ThinkCentre hardware.
Test it with `./scripts/test_ollama.sh` to validate the hardware.

## Testing the Stack

Run the full validation suite:

```bash
./scripts/test_all.sh
```

The suite checks core service health, OpenClaw chat completions, the
workflows webhook canary, and (optionally) DeerFlow. Individual scripts
can be run on their own:

```bash
./scripts/test_openclaw.sh    # OpenClaw chat-completion path
./scripts/test_workflows.sh   # Workflows endpoints
./scripts/test_deerflow.sh    # DeerFlow research layer
./scripts/test_ollama.sh      # Local Ollama inference
```

Use the Python suites for offline validation (no services required):

```bash
make validate                                    # config + structure checks
python3 tests/validate_stack.py                  # structure check only
python3 -m pytest tests/test_validate_stack.py   # validate_stack unit tests
make test-unit                                   # workflows/tests/ pytest
```

`workflows/tests/` covers the FastAPI app, brain database, LLM proxy
routing, marketing scanners, and Telegram formatting.

## Common Issues

| Problem | Symptom | Fix |
|---------|---------|-----|
| Port already in use | `bind: address already in use` | `lsof -ti :3000 \| xargs kill` (replace 3000 with the conflicting port) |
| Docker not running | `Cannot connect to Docker daemon` | Start Docker Desktop or run `sudo systemctl start docker` |
| OpenRouter API key invalid | OpenClaw returns errors, completion tests fail | Check key at https://openrouter.ai/settings/keys. Verify balance > $0. |
| OpenRouter balance zero | LLM calls return 402 errors | Add credits at https://openrouter.ai/credits |
| OpenClaw can't reach Workflows | Tool/proxy calls from OpenClaw fail | Both must be on `msp-network`. Check `docker network ls` and `docker network inspect msp-network`. |
| DeerFlow using ByteDance endpoints | Research calls go to Doubao/Volcengine | Check `deer-flow/config.yaml` — all models must have `base_url: https://openrouter.ai/api/v1` |
| Ollama out of memory | `out of memory` or killed process | Use a smaller model: `ollama run llama3.2:1b`. Or close other apps to free RAM. |
| Schedules not firing | `/sched` shows next_fire_time but no tasks created | Workflows must run with a single uvicorn worker; check container memory and `make logs-workflows`. |
| Reddit/GitHub scans return empty | `/scan/reddit` or `/scan/github` empty results | Confirm credentials in `.env` (REDDIT_*, GITHUB_PAT). Missing creds degrade gracefully — no exception, just empty list. |

## Cost Estimation

After running the testbed for a week with realistic test traffic:

1. Go to https://openrouter.ai/activity
2. Filter by date range (your test week)
3. Note the total spend

**Estimating per-client cost:**
- Divide total spend by number of test messages sent
- Multiply by expected daily message volume per client
- Add 20% buffer for retries and longer conversations

**Typical ranges** (Haiku-heavy routing):
- Low-volume client (20 messages/day): ~$3-8/month
- Medium-volume (50 messages/day): ~$8-20/month
- High-volume (100+ messages/day): ~$20-50/month

Monitor in real-time: OpenRouter dashboard shows per-model token usage and cost breakdown.

## Moving to Production

When deploying to the live host (DigitalOcean Droplet + ThinkCentre M75q),
see [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for the full Tailscale +
Caddy walkthrough. Key shifts vs. the local testbed:

| Aspect | Testbed (Local) | Production |
|--------|----------------|------------|
| **Docker images** | `:latest` tags | Pin to specific versions (e.g. `ghcr.io/openclaw/openclaw:2026.3.24`) |
| **Network** | Docker bridge on localhost | Tailscale VPN mesh between Droplet and ThinkCentre |
| **Domain/SSL** | http://localhost | Real domain with Let's Encrypt SSL via Caddy |
| **soul.md** | Default John-117 persona | Same, optionally swap `soul-ops.md` for ops mode |
| **Workflows** | Hot-reload bind mounts | Image-baked code, single uvicorn worker |
| **Brain DB** | `data/brain/brain.db` (bind mount) | Persistent volume with backup strategy |
| **Secrets** | `.env` file | Docker secrets or sealed env |
| **Monitoring** | Manual test scripts | Uptime Kuma + Telegram alerts (`/tier/notify`) |
| **DeerFlow** | Optional, local | Runs on Droplet only when needed |
| **Ollama** | Test on laptop | Runs on ThinkCentre for air-gapped fallback |

**Tailscale VPN setup** (done separately):
1. Install Tailscale on both the Droplet and ThinkCentre
2. Join both to your tailnet
3. Replace `localhost` URLs with Tailscale IPs (e.g., `http://100.x.y.z:5678`)
4. Enable Tailscale MagicDNS for friendly names

## Repurposing for a New Operator

The default persona is wired for the operator. To repoint the stack:

1. Replace `openclaw/soul.md` with the new operator's persona and
   `openclaw/soul-ops.md` with their ops-mode instructions. Keep
   responses tight — Telegram is the main channel.
2. Reseed the marketing orchestrator: `POST /projects` with the new
   slugs/repos, then `POST /sched` for any cron jobs that should run.
3. Update `openclaw/HEARTBEAT.md` if the heartbeat cadence or task
   priorities differ.
4. Re-run `make test` to confirm health, completion, and webhook
   canary still pass.

The brain database is operator-specific — wipe `data/brain/brain.db`
before reseeding if you want a clean slate.
