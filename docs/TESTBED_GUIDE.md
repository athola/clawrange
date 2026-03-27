# AI MSP Testbed Guide

Local validation environment for the AI-powered MSP stack: OpenClaw + n8n + DeerFlow + Ollama.

## Quick Start

```bash
# 1. Clone or navigate to the testbed
cd ~/clawrange

# 2. Create your environment file and fill in your OpenRouter API key
#    Get one at https://openrouter.ai/settings/keys — keep balance above $10
make setup
nano .env

# 3. Start the core stack (OpenClaw + n8n)
make start

# 4. Open n8n and activate the workflows
#    Go to http://localhost:5678
#    Import workflows from n8n/workflows/ if not auto-loaded
#    Toggle each workflow to "Active"

# 5. Run validation tests
make test

# 6. (Optional) Start with DeerFlow research layer
make start-full

# 7. (Optional) Test local Ollama inference
make test-ollama
```

Total setup time: under 10 minutes (excluding Docker image pulls).

## What Each Service Does

**OpenClaw** is the AI gateway. It receives messages from clients (via WhatsApp, Telegram, web chat, etc.), routes them through an LLM (via OpenRouter), and returns responses shaped by the soul.md persona. It's the "brain" of the system — the thing that makes your manufactured home dealer's AI assistant sound like it works at a manufactured home dealer. Accessible at http://localhost:3000.

**n8n** is the workflow automation engine. It handles everything the AI can't do alone: scheduled morning briefings, CRM lead lookups, webhook integrations, appointment scheduling triggers, and any future automation you wire up. Think of it as the "nervous system" connecting the AI brain to business tools. Dashboard at http://localhost:5678.

**DeerFlow** is the deep research layer (optional). When a client question needs real research — market analysis, competitor comparisons, regulatory questions — DeerFlow dispatches sub-agents that search the web, synthesize findings, and produce structured reports. It's overkill for most daily interactions but valuable for pre-sales research and market intelligence. Runs at http://localhost:2026 when started with `--with-deerflow`.

**Ollama** provides local LLM inference for onsite deployment. When a client site needs AI responses without internet dependency (air-gapped environments, unreliable connections), Ollama runs a small model directly on the ThinkCentre hardware. Test it with `./scripts/test_ollama.sh` to validate your hardware can handle it.

## Testing the Stack

Run the full validation suite:

```bash
./scripts/test_all.sh
```

This runs 6 tests in sequence:

| Test | What It Checks | Requirements |
|------|---------------|-------------|
| 1. Stack Health | OpenClaw and n8n respond to health checks | Services running |
| 2. OpenClaw Response | AI returns a relevant answer about financing | OPENROUTER_API_KEY set |
| 3. n8n Roundtrip | Test webhook echoes a payload back | Test Webhook workflow active |
| 4. Lead Lookup | Lead status returns John Smith's data | Lead Status workflow active |
| 5. Morning Briefing | Morning Briefing workflow exists and can trigger | Workflow imported |
| 6. DeerFlow Research | Research agent returns relevant results | DeerFlow running (optional) |

**Target: 5/6 or 6/6 passed.** DeerFlow (test 6) is optional.

You can also run individual test scripts:
```bash
./scripts/test_openclaw.sh    # OpenClaw only
./scripts/test_n8n.sh         # n8n workflows only
./scripts/test_deerflow.sh    # DeerFlow only
./scripts/test_ollama.sh      # Local Ollama inference
```

Or use the Python suite:
```bash
python3 tests/validate_stack.py
```

## Common Issues

| Problem | Symptom | Fix |
|---------|---------|-----|
| Port already in use | `bind: address already in use` | `lsof -ti :3000 \| xargs kill` (replace 3000 with the conflicting port) |
| Docker not running | `Cannot connect to Docker daemon` | Start Docker Desktop or run `sudo systemctl start docker` |
| OpenRouter API key invalid | OpenClaw returns errors, test 2 fails | Check key at https://openrouter.ai/settings/keys. Verify balance > $0. |
| OpenRouter balance zero | LLM calls return 402 errors | Add credits at https://openrouter.ai/credits |
| OpenClaw can't reach n8n | Webhook calls from OpenClaw fail | Both must be on `msp-network`. Check `docker network ls` and `docker network inspect msp-network`. |
| DeerFlow using ByteDance endpoints | Research calls go to Doubao/Volcengine | Check `deer-flow/config.yaml` — all models must have `base_url: https://openrouter.ai/api/v1` |
| Ollama out of memory | `out of memory` or killed process | Use a smaller model: `ollama run llama3.2:1b`. Or close other apps to free RAM. |
| n8n workflow import fails | Workflows don't appear in n8n UI | Manually import: n8n UI > Workflows > Import > select JSON from `n8n/workflows/` |
| Morning briefing not triggering | Test 5 fails, no briefing generated | The cron runs at 8 AM weekdays. For manual testing, trigger via n8n UI "Execute" button. |

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

When deploying to a real client site (DigitalOcean Droplet + ThinkCentre M75q):

| Aspect | Testbed (Local) | Production |
|--------|----------------|------------|
| **Docker images** | `:latest` tags | Pin to specific versions (e.g., `ghcr.io/openclaw/openclaw:v1.2.3`) |
| **Network** | Docker bridge on localhost | Tailscale VPN mesh between Droplet and ThinkCentre |
| **Domain/SSL** | http://localhost | Real domain with Let's Encrypt SSL via Caddy or nginx |
| **activeHours** | Test schedule | Client's actual business hours |
| **soul.md** | "Longview Home Center" test persona | Client-specific persona from intake interview |
| **n8n workflows** | Mock data stubs | Connected to real CRM, email, calendar APIs |
| **Volumes** | Local Docker volumes | Persistent volumes with backup strategy |
| **Secrets** | .env file | Docker secrets or Vault |
| **Monitoring** | Manual test scripts | Uptime Kuma + alerting |
| **DeerFlow** | Optional, local | Runs on Droplet for research-heavy clients |
| **Ollama** | Test on laptop | Runs on ThinkCentre for air-gapped/Tier 3 clients |

**Tailscale VPN setup** (done separately):
1. Install Tailscale on both the Droplet and ThinkCentre
2. Join both to your tailnet
3. Replace `localhost` URLs with Tailscale IPs (e.g., `http://100.x.y.z:5678`)
4. Enable Tailscale MagicDNS for friendly names

## Claude Code Max Tip

For every new client, use this prompt template with Claude Code:

```
Based on these notes from a [business type] in [city, state], write:

1. An OpenClaw soul.md persona for their AI assistant
2. Three n8n workflows for: [specific tasks from intake interview]
3. A DeerFlow research prompt for: [industry-specific research need]

Use these mock API endpoints for testing:
- Lead lookup: http://localhost:5678/webhook-test/lead-status
- Morning briefing: http://localhost:5678/webhook/morning-briefing
- Test webhook: http://localhost:5678/webhook-test/test

Client notes:
[paste your interview notes here]
```

This generates a complete testable configuration for each new client in minutes.
