# ClawRange Secure Deployment Guide

Node (this machine) and Gateway (DigitalOcean droplet) via Tailscale VPN.

## Architecture

```
Internet → Droplet (Caddy + TLS) → Tailscale tunnel → Node (Docker services)
```

| Component | Role | Location |
|-----------|------|----------|
| Node machine | Runs OpenClaw, Workflows (FastAPI), and optional DeerFlow | Behind NAT, no public IP needed |
| DigitalOcean droplet | Gateway: reverse proxy and TLS termination | Public IP, domain pointed here |
| Tailscale | Encrypted WireGuard tunnel between them | Mesh VPN, NAT-traversal built in |
| Caddy | HTTPS reverse proxy on gateway | Auto Let's Encrypt certificates |

## Security Model

1. **No public ports on the node**: Docker binds to `127.0.0.1`,
   `DOCKER_HOST_IP` (the docker0 gateway, for sibling compose projects;
   defaults to the `127.0.0.2` loopback alias when empty) and the
   Tailscale IP only
2. **Tailscale for transport**: WireGuard encryption, identity-based access, no port forwarding
3. **TLS termination at gateway**: Caddy handles Let's Encrypt automatically
4. **UFW on gateway**: only SSH (22), HTTP (80), HTTPS (443), Tailscale (41641/udp)
5. **Gateway token authentication**: OpenClaw requires `OPENCLAW_GATEWAY_TOKEN` header
6. **Workflows bearer token**: every workflows route needs
   `Authorization: Bearer $PROXY_AUTH_TOKEN` except the health checks,
   `GET /tier` and `GET /sched[/{id}]`. An empty token fails closed (401)
7. **WSL2 NAT isolation**: Windows host NAT provides additional layer

## Prerequisites

- [ ] Node machine: Docker, Docker Compose, Tailscale installed and running
- [ ] DigitalOcean account
- [ ] Domain name (for TLS, can use a subdomain like `ai.yourdomain.com`)
- [ ] Tailscale account
- [ ] OpenRouter API key with balance

## Part 1: Node Setup (This Machine)

### 1.1 Verify Tailscale is running

```bash
tailscale status
tailscale ip -4
# Note your Tailscale IP (you'll need it for TAILSCALE_IP in .env)
```

### 1.2 Configure environment

```bash
cp .env.example .env
# Edit .env and set:
#   OPENROUTER_API_KEY=<your key>
#   TAILSCALE_IP=<your tailscale ip from step 1.1>
#   OPENCLAW_GATEWAY_TOKEN=<generate with: openssl rand -base64 32 | tr -d '/+=' | head -c 40>
#   PROXY_AUTH_TOKEN=<generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
#   CLAUDE_BRIDGE_TOKEN=<openssl rand -hex 32; optional claude-sub tier,
#     served by `make claude-bridge` on the host>
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID=<for tier alerts and task delivery>
```

`PROXY_AUTH_TOKEN` ships empty in `.env.example`. `make start-prod`
(through `scripts/start.sh --ensure-soul`) exits with an error until it
is set, and renders `openclaw/soul.md` from the active profile if it is
missing.

### 1.3 Start services in production mode

```bash
# Production mode binds to 127.0.0.1 and Tailscale IP only
make start-prod

# Verify ports are bound correctly
ss -tlnp | grep -E '3000|5678'
# Should show 127.0.0.1:3000, <DOCKER_HOST_IP or 127.0.0.2>:3000 and
# <your-ts-ip>:3000 (NOT 0.0.0.0:3000)
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

The workflows `/healthz` always answers 200 while the process serves.
Read its `status`: `degraded` means something is broken, and `problems`
names it (`db`, `vector_search`, `scheduler`, `profile`). Without the
bearer token you get the names only; add
`-H "Authorization: Bearer $PROXY_AUTH_TOKEN"` for the error detail.

## Part 2: Gateway Setup (DigitalOcean Droplet)

### 2.1 Create the droplet

1. Go to [DigitalOcean](https://cloud.digitalocean.com/)
2. Create Droplet:
   - **Image**: Ubuntu 24.04 LTS
   - **Plan**: Basic $6/mo (1 vCPU, 1GB RAM). Gateway is just proxying
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
4. Configure UFW (SSH, HTTP, HTTPS, and Tailscale only)
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
# From the droplet: test Tailscale tunnel to node
curl -s http://<node-tailscale-ip>:3000/healthz

# From anywhere: test public HTTPS
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
https://login.tailscale.com/admin/acls and add the policy below (also
available paste-ready, with tagging instructions, as
[tailscale-acls.json](tailscale-acls.json)):

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

- [ ] Docker ports bound to 127.0.0.1, `DOCKER_HOST_IP` and the Tailscale
  IP only (`make start-prod`)
- [ ] `OPENCLAW_GATEWAY_TOKEN` set to random value
- [ ] `PROXY_AUTH_TOKEN` set to random value (gates every workflows route
  except the health checks, `GET /tier` and `GET /sched[/{id}]`, not just
  the LLM proxy)
- [ ] `OPENROUTER_API_KEY` set in `.env`; balance is read live from the
  OpenRouter credits API (`/api/v1/credits`)
- [ ] Docker images stay pinned (`docker-compose.yml` ships
  `ghcr.io/openclaw/openclaw:2026.11.6`)
- [ ] Workflows runs single uvicorn worker (default: do not raise)
- [ ] Brain DB at `data/brain/brain.db` is included in your backup plan

### Gateway (droplet)

- [ ] UFW enabled (SSH, HTTP/S, and Tailscale only)
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

## Upgrading OpenClaw

The image is pinned in `docker-compose.yml`. To move an existing install
past a release that changes stored state, migrate the state before the new
image first starts. 2026.11.5 is one: it turns the old `heartbeat` config
into an automation job. Until `openclaw doctor --fix` has done that,
`render-config.js` refuses to render and the container will not start.

```bash
git pull                                   # new pin, config and scripts
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop openclaw
mkdir -p data/backups
tar -czf "data/backups/openclaw-state-$(date +%Y%m%d-%H%M%S).tar.gz" \
  -C data openclaw-state                   # or your OPENCLAW_STATE_DIR
docker compose run --rm --no-deps -T --entrypoint sh openclaw -c \
  'chown 1000:1000 /home/node/.openclaw /tmp &&
   runuser -u node -- env npm_config_cache=/tmp/.npm \
     node dist/index.js doctor --fix --yes' 2>&1 | tee doctor.log
make start-prod
```

Read `doctor.log` even when doctor exits 0. A run can exit 0 and still
report a failed plugin install or an unfinished state migration. If it
does, run doctor again: the second run completes them. Two warnings are
expected on this stack and need no action:

- `Gateway bound to "lan" (0.0.0.0)`: that is inside the container.
  Docker publishes it only on `127.0.0.1`, docker0 and, in prod,
  `TAILSCALE_IP` (`docker port msp-openclaw`).
- `Failed to generate completion cache`: the container rootfs is read-only.

A `plaintext secret-bearing config` warning means the install still holds
the proxy token in plaintext: run the one-time migration below.

On the first start,
`sync-heartbeat.sh` caps the migrated job's tools to the `max-ops` allow
list and loads `HEARTBEAT.md` into its scratch. Then check:

```bash
docker exec msp-openclaw runuser -u node -- openclaw automations list --all
docker compose logs workflows | grep "heartbeat intercepted"
```

To roll back, stop the container, restore the tarball over the state
dir, and check out the previous commit. The old image also needs the old
`openclaw.json` and `sync-heartbeat.sh`, so reverting only the pin is not
enough.

### Moving the proxy token out of OpenClaw's state (one time)

The template's proxy `apiKey` is an env SecretRef, so `openclaw.json`
holds a reference and OpenClaw resolves `PROXY_AUTH_TOKEN` from its own
environment. `tools.fs.workspaceOnly` keeps every agent's file tools in
its workspace, away from that file and from `/proc/*/environ`. An install
set up before this change still has a plaintext copy in the gateway's
auth-profile store, which the startup secrets audit reports as unhealthy.

Removing that copy also strands the heartbeat job: 2026.11.6 pins an
automation job to the account it was created with and stops it ("was
removed or signed out") rather than fall back. So the job is recreated in
the same step. The plan needs the gateway stopped, because the running
gateway holds a write lock on its state DB:

```bash
docker compose stop openclaw
tar -czf "data/backups/openclaw-state-$(date +%Y%m%d-%H%M%S).tar.gz" \
  -C data openclaw-state
docker compose run --rm --no-deps -T --entrypoint sh openclaw -c \
  'chown 1000:1000 /home/node/.openclaw /tmp &&
   node /opt/render-config.js /opt/openclaw.json.template /home/node/.openclaw/openclaw.json &&
   chown 1000:1000 /home/node/.openclaw/openclaw.json &&
   runuser -u node -- node dist/index.js secrets apply --from /opt/secrets-plan.json'
docker compose up -d openclaw
OC="docker exec msp-openclaw runuser -u node -- openclaw"
$OC automations list --all            # note the old heartbeat job's id
$OC automations rm <old-heartbeat-id>
docker compose restart openclaw       # sync-heartbeat.sh creates a new job
$OC secrets audit                     # expect: clean, plaintext=0, shadowed=0
```

Never add this plan to the container start: on an existing install, every
start would delete the profile the heartbeat job depends on.

### Rotating PROXY_AUTH_TOKEN

Nothing inside OpenClaw stores the value, so a rotation is env only:

1. Put the new value (`openssl rand -hex 32`) in `.env` and in the tome
   bridge's token file (`~/.config/clawrange/tome-bridge.env`).
2. `docker compose up -d --force-recreate workflows openclaw`.
3. `systemctl --user restart clawrange-tome-bridge`, once it has no
   `claude` child running; otherwise that task stays `active` until the
   6-hour reaper fails it.
4. Check that the old token gets 401 on `GET /task`, and watch the
   workflows log for 401s from anything still holding it.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Gateway can't reach node | `tailscale ping <node-ts-ip>` from droplet |
| TLS cert not issued | DNS A record points to droplet? `dig ai.yourdomain.com` |
| 502 Bad Gateway | Node services running? `make health` on node |
| Slow responses | Tailscale using relay? `tailscale status`: look for "relay" vs "direct" |
| Workflows endpoints failing | Tool URL in `openclaw/config/openclaw.json` should use Docker service name (`workflows`), not Tailscale IP |
| DeerFlow not reachable | Started with `--with-deerflow`? Connected to `msp-network`? |
| Workflows routes return 401 | `PROXY_AUTH_TOKEN` empty or not sent. Every route except the health checks, `GET /tier` and `GET /sched[/{id}]` needs `Authorization: Bearer $PROXY_AUTH_TOKEN` |
| `/docs` or `/openapi.json` returns 404 | Expected: the interactive docs are turned off. Routes are listed in `CLAUDE.md` and `workflows/app.py` |
| `/scan/*` or `/research` returns 502 | The upstream (Reddit, GitHub, the search tier) failed; `detail` carries the reason. Not an empty result |
| OpenClaw container unhealthy, heartbeats never reach the proxy | `docker inspect --format '{{json .State.Health}}' msp-openclaw`: a `heartbeat sync failed: ...` line means `sync-heartbeat.sh` could not find or create the heartbeat automation job, or load `HEARTBEAT.md` into its scratch, and wrote `/run/sync-heartbeat.failed`. The script's output is in `docker logs msp-openclaw`; restart the container to retry |
| OpenClaw unhealthy with `heartbeat sync failed: foreign heartbeat job(s) enabled` | An enabled heartbeat that is not ClawRange's exists (named `heartbeat-*` or "Heartbeat (...)"). It would DM you on its own schedule and bypass the proxy. Ours still runs. Turn the other off with `docker exec msp-openclaw runuser -u node -- openclaw automations disable <id>` and restart the container |
| OpenClaw unhealthy with `heartbeat sync failed: secrets audit: [PLAINTEXT_FOUND] ...` | A credential is stored in plaintext in OpenClaw's state. For `profiles.clawrange-proxy:default.key`, run [the one-time token migration](#moving-the-proxy-token-out-of-openclaws-state-one-time); for anything else, `openclaw secrets configure` |
| Heartbeat runs fail with `Automation inference account ... was removed or signed out` (or `was replaced`) | The auth profile the job was pinned to is gone. Remove the job (`openclaw automations rm <id>`) and restart the container; `sync-heartbeat.sh` creates a new one |
| OpenClaw exits at start with `render-config: ... still has retired keys` | The state dir predates 2026.11.5. Run the doctor step in [Upgrading OpenClaw](#upgrading-openclaw) |
| A task stays `active` | A worker claimed it and died. The heartbeat fails it after `HEARTBEAT_ACTIVE_REAP_HOURS` (default 6) with a digest line; re-create it to retry |
| Heartbeat tasks fail with the LLM down | During an outage a task goes back to pending each heartbeat and is failed on its 6th outage cycle (about an hour), so it cannot block the queue. The digest line says why per tier, e.g. `No LLM answered (openrouter-free: empty reply; claude-sub: rate-limited)`. `out of credit` will not clear by waiting: top up that provider. A `claude-sub` usage limit reads `rate-limited` and clears when the subscription window resets |
| `research:tome:` tasks stay pending, a `Stale queue:` nudge lists them under the tome bridge | `journalctl --user -u clawrange-tome-bridge`. A `rejected the request` line means the bridge's `PROXY_AUTH_TOKEN` is missing or stale; see "Heavy research via tome" in `docs/research-and-marketing.md` |

## Cost Estimate

| Item | Monthly Cost |
|------|-------------|
| DigitalOcean droplet (1GB) | $6 |
| Domain (annual / 12) | ~$1 |
| OpenRouter API (20 msg/day) | $3-8 |
| Tailscale (free tier) | $0 |
| **Total** | **$10-15/mo** |
