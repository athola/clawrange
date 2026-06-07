# ClawRange Secure Deployment Guide

Node (this machine) + Gateway (DigitalOcean droplet) via Tailscale VPN.

## Architecture

```
Internet → Droplet (Caddy + TLS) → Tailscale tunnel → Node (Docker services)
```

| Component | Role | Location |
|-----------|------|----------|
| Node machine | Runs OpenClaw + Workflows (FastAPI) + optional DeerFlow | Behind NAT, no public IP needed |
| DigitalOcean droplet | Gateway — reverse proxy + TLS termination | Public IP, domain pointed here |
| Tailscale | Encrypted WireGuard tunnel between them | Mesh VPN, NAT-traversal built in |
| Caddy | HTTPS reverse proxy on gateway | Auto Let's Encrypt certificates |

## Security Model

1. **No public ports on the node** — Docker binds to `127.0.0.1` + Tailscale IP only
2. **Tailscale for transport** — WireGuard encryption, identity-based access, no port forwarding
3. **TLS termination at gateway** — Caddy handles Let's Encrypt automatically
4. **UFW on gateway** — only SSH (22), HTTP (80), HTTPS (443), Tailscale (41641/udp)
5. **Gateway token authentication** — OpenClaw requires `OPENCLAW_GATEWAY_TOKEN` header
6. **WSL2 NAT isolation** — Windows host NAT provides additional layer

## Prerequisites

- [ ] Node machine: Docker, Docker Compose, Tailscale installed and running
- [ ] DigitalOcean account
- [ ] Domain name (for TLS — can use a subdomain like `ai.yourdomain.com`)
- [ ] Tailscale account
- [ ] OpenRouter API key with balance

## Part 1: Node Setup (This Machine)

### 1.1 Verify Tailscale is running

```bash
tailscale status
tailscale ip -4
# Note your Tailscale IP — you'll need it for TAILSCALE_IP in .env
```

### 1.2 Configure environment

```bash
cp .env.example .env
# Edit .env and set:
#   OPENROUTER_API_KEY=<your key>
#   TAILSCALE_IP=<your tailscale ip from step 1.1>
#   OPENCLAW_GATEWAY_TOKEN=<generate with: openssl rand -base64 32 | tr -d '/+=' | head -c 40>
#   PROXY_AUTH_TOKEN=<generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
#   ZAI_API_KEY=<your Z.AI key, optional Tier 2 fallback>
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID=<for tier alerts and task delivery>
```

### 1.3 Start services in production mode

```bash
# Production mode binds to 127.0.0.1 + Tailscale IP only
make start-prod

# Verify ports are bound correctly
ss -tlnp | grep -E '3000|5678'
# Should show 127.0.0.1:3000 and <your-ts-ip>:3000 — NOT 0.0.0.0:3000
```

### 1.4 Verify services are accessible via Tailscale

```bash
# From this machine (via localhost)
curl -s http://localhost:3000/healthz
curl -s http://localhost:5678/healthz

# Via Tailscale IP (simulates what the gateway will use)
curl -s http://<your-tailscale-ip>:3000/healthz
curl -s http://<your-tailscale-ip>:5678/healthz
```

## Part 2: Gateway Setup (DigitalOcean Droplet)

### 2.1 Create the droplet

1. Go to [DigitalOcean](https://cloud.digitalocean.com/)
2. Create Droplet:
   - **Image**: Ubuntu 24.04 LTS
   - **Plan**: Basic $6/mo (1 vCPU, 1GB RAM) — gateway is just proxying
   - **Region**: Choose closest to your node's location
   - **Auth**: SSH key (strongly recommended over password)
   - **Hostname**: `clawrange-gateway`
3. Note the droplet's public IP

### 2.2 Point your domain

Create a DNS A record:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A | `ai` (or `@`) | `<droplet-public-ip>` | 300 |

If you need to expose workflow webhooks (e.g. Telegram callbacks) on a separate hostname:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A | `webhooks` | `<droplet-public-ip>` | 300 |

### 2.3 Run the gateway setup script

```bash
# From this machine, copy the script to the droplet
scp scripts/setup_gateway.sh root@<droplet-ip>:/root/

# SSH into the droplet and run it, passing your node's Tailscale IP
ssh root@<droplet-ip>
NODE_TAILSCALE_IP=<your-node-tailscale-ip> bash /root/setup_gateway.sh
```

The script will:
1. Update system packages
2. Install Tailscale (prompts for auth key)
3. Install Caddy
4. Configure UFW (SSH + HTTP + HTTPS + Tailscale only)
5. Create a Caddyfile with your node IP baked in

### 2.4 Get a Tailscale auth key (for headless droplet)

1. Go to https://login.tailscale.com/admin/settings/keys
2. Click **Generate auth key**
3. Settings:
   - **Reusable**: No (one-time use)
   - **Ephemeral**: No (persistent node)
   - **Pre-approved**: Yes (skip admin approval)
   - **Tags**: optional
   - **Expiry**: 1 hour (you only need it for setup)
4. Copy the key (starts with `tskey-auth-`)

### 2.5 Configure the Caddyfile

On the droplet, edit `/etc/caddy/Caddyfile`:

```bash
nano /etc/caddy/Caddyfile
```

Replace `YOUR_DOMAIN` with your actual domain (e.g., `ai.yourdomain.com`).

Then validate and reload:

```bash
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

### 2.6 Verify end-to-end

```bash
# From the droplet — test Tailscale tunnel to node
curl -s http://<node-tailscale-ip>:3000/healthz

# From anywhere — test public HTTPS
curl -s https://ai.yourdomain.com/healthz

# Test a chat completion
curl -s https://ai.yourdomain.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-gateway-token>" \
  -d '{
    "messages": [{"role": "user", "content": "What homes do you have?"}]
  }'
```

## Part 3: Tailscale ACLs (Optional but Recommended)

Lock down which devices can talk to what. Go to
https://login.tailscale.com/admin/acls and add:

```json
{
  "acls": [
    {
      "action": "accept",
      "src": ["tag:gateway"],
      "dst": ["tag:node:3000", "tag:node:5678", "tag:node:2026"]
    },
    {
      "action": "accept",
      "src": ["autogroup:owner"],
      "dst": ["*:*"]
    }
  ],
  "tagOwners": {
    "tag:gateway": ["autogroup:owner"],
    "tag:node":    ["autogroup:owner"]
  }
}
```

Then tag your devices:

```bash
# On the node
sudo tailscale up --advertise-tags=tag:node

# On the gateway droplet
sudo tailscale up --advertise-tags=tag:gateway
```

This means only the gateway can reach ports 3000/5678/2026 on the node.
Your personal devices can still reach everything as the tailnet owner.

## Part 4: Hardening Checklist

### Node

- [ ] Docker ports bound to 127.0.0.1 + Tailscale IP only (`make start-prod`)
- [ ] `OPENCLAW_GATEWAY_TOKEN` set to random value
- [ ] `PROXY_AUTH_TOKEN` set to random value (gates the workflows LLM proxy)
- [ ] `OPENROUTER_API_KEY` set in `.env`; `OPENROUTER_CREDIT_BALANCE` reflects actual deposit
- [ ] Pin Docker images to specific versions before production
  (e.g. `ghcr.io/openclaw/openclaw:2026.3.24`)
- [ ] Workflows runs single uvicorn worker (default — do not raise)
- [ ] Brain DB at `data/brain/brain.db` is included in your backup plan

### Gateway (droplet)

- [ ] UFW enabled (SSH + HTTP/S + Tailscale only)
- [ ] Caddy configured with real domain
- [ ] TLS certificate obtained (automatic with Caddy)
- [ ] Tailscale connected and node reachable
- [ ] SSH key auth only (disable password auth in /etc/ssh/sshd_config)
- [ ] Unattended upgrades enabled

### Tailscale

- [ ] ACLs configured to restrict gateway → node traffic
- [ ] MagicDNS verified working
- [ ] Key expiry notifications enabled

## Operational Commands

### Node

```bash
make start-prod          # Start in production mode
make stop-prod           # Stop production stack
make health              # Quick health check
make logs                # Tail service logs
make test                # Full validation suite
```

### Gateway

```bash
systemctl status caddy            # Caddy status
journalctl -u caddy -f            # Caddy logs (live)
tail -f /var/log/caddy/openclaw-access.log  # Access logs
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy            # Apply config changes
tailscale status                  # VPN status
ufw status                        # Firewall status
```

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Gateway can't reach node | `tailscale ping <node-ts-ip>` from droplet |
| TLS cert not issued | DNS A record points to droplet? `dig ai.yourdomain.com` |
| 502 Bad Gateway | Node services running? `make health` on node |
| Slow responses | Tailscale using relay? `tailscale status` — look for "relay" vs "direct" |
| Workflows endpoints failing | Tool URL in `openclaw/config/openclaw.json` should use Docker service name (`workflows`), not Tailscale IP |
| DeerFlow not reachable | Started with `--with-deerflow`? Connected to `msp-network`? |

## Cost Estimate

| Item | Monthly Cost |
|------|-------------|
| DigitalOcean droplet (1GB) | $6 |
| Domain (annual / 12) | ~$1 |
| OpenRouter API (20 msg/day) | $3-8 |
| Tailscale (free tier) | $0 |
| **Total** | **$10-15/mo** |
