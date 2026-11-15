#!/usr/bin/env bash
# setup_gateway.sh — Configure a DigitalOcean droplet as a secure reverse proxy
# for the ClawRange AI MSP testbed node.
#
# Run this ON THE DROPLET (not on the node machine).
#
# Prerequisites:
#   - Fresh Ubuntu 24.04 droplet
#   - SSH access as root or sudo user
#   - Your Tailscale auth key (from https://login.tailscale.com/admin/settings/keys)
#
# Usage:
#   scp scripts/setup_gateway.sh root@<droplet-ip>:/root/
#   ssh root@<droplet-ip> NODE_TAILSCALE_IP=100.x.x.x bash /root/setup_gateway.sh [--reset-firewall]
#
# Options:
#   --reset-firewall   Run `ufw --force reset` before applying rules. Off by
#                      default so reruns keep any custom rules you added.
#
# Environment:
#   NODE_TAILSCALE_IP  (required) node's Tailscale IP
#   OPENCLAW_PORT      OpenClaw host port on the node (default 3000)
#   WORKFLOWS_PORT     workflows host port on the node (default 5678)
#   TS_AUTHKEY         Tailscale auth key for headless join. Passed to
#                      tailscale via a 0600 temp file (--auth-key=file:...),
#                      never on the command line. If unset and stdin is a
#                      terminal you are prompted; otherwise interactive login.
#
# What this does:
#   1. Updates system packages
#   2. Installs and joins Tailscale
#   3. Installs Caddy (reverse proxy with auto-TLS)
#   4. Configures UFW firewall
#   5. Creates Caddyfile template

set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────
# Set NODE_TAILSCALE_IP to your node's Tailscale IP before running.
# Find it on the node with: tailscale ip -4
NODE_TAILSCALE_IP="${NODE_TAILSCALE_IP:-}"
OPENCLAW_PORT="${OPENCLAW_PORT:-3000}"
WORKFLOWS_PORT="${WORKFLOWS_PORT:-5678}"
TS_AUTHKEY="${TS_AUTHKEY:-}"
RESET_FIREWALL=false

for arg in "$@"; do
    case "$arg" in
        --reset-firewall) RESET_FIREWALL=true ;;
        -h|--help)
            echo "Usage: NODE_TAILSCALE_IP=100.x.x.x bash $0 [--reset-firewall]"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: NODE_TAILSCALE_IP=100.x.x.x bash $0 [--reset-firewall]" >&2
            exit 2
            ;;
    esac
done

# ─── Colors ───────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$1"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$1"; }
error() { printf "${RED}[ERROR]${NC} %s\n" "$1" >&2; }

# ─── Preflight ────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    error "Run as root or with sudo"
    exit 1
fi

if [ -z "$NODE_TAILSCALE_IP" ]; then
    error "NODE_TAILSCALE_IP is required."
    echo "  Find it on the node with: tailscale ip -4"
    echo "  Usage: NODE_TAILSCALE_IP=100.x.x.x bash $0"
    exit 1
fi

info "Starting gateway setup (node IP: ${NODE_TAILSCALE_IP})..."

# ─── 1. System Updates ───────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq curl gnupg debian-keyring debian-archive-keyring apt-transport-https

# ─── 2. Install Tailscale ────────────────────────────────────────
info "Installing Tailscale..."
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
    info "Tailscale installed."
else
    info "Tailscale already installed."
fi

# Check if already connected
if tailscale status >/dev/null 2>&1; then
    CURRENT_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
    info "Tailscale already connected. IP: ${CURRENT_IP}"
else
    warn "Tailscale not connected. Run one of the following:"
    echo ""
    echo "  Option A (interactive login — opens browser URL):"
    echo "    tailscale up"
    echo ""
    echo "  Option B (auth key — headless, recommended for droplets):"
    echo "    TS_AUTHKEY=tskey-auth-XXXXX NODE_TAILSCALE_IP=... bash $0"
    echo ""
    echo "  Get an auth key at: https://login.tailscale.com/admin/settings/keys"
    echo ""
    if [ -z "$TS_AUTHKEY" ] && [ -t 0 ]; then
        read -r -s -p "Enter Tailscale auth key (or press Enter to use interactive login): " TS_AUTHKEY
        echo ""
    fi
    if [ -n "$TS_AUTHKEY" ]; then
        # Hand the key over in a 0600 file so it never shows up in ps.
        TS_KEY_FILE=$(umask 077 && mktemp)
        trap 'rm -f "$TS_KEY_FILE"' EXIT
        printf '%s' "$TS_AUTHKEY" > "$TS_KEY_FILE"
        tailscale up --auth-key="file:${TS_KEY_FILE}"
        rm -f "$TS_KEY_FILE"
    else
        tailscale up
    fi
fi

GATEWAY_TS_IP=$(tailscale ip -4)
info "Gateway Tailscale IP: ${GATEWAY_TS_IP}"

# Verify node is reachable
info "Testing connectivity to node (${NODE_TAILSCALE_IP})..."
if ping -c 1 -W 3 "$NODE_TAILSCALE_IP" >/dev/null 2>&1; then
    info "Node is reachable via Tailscale."
else
    warn "Node not reachable. Ensure Tailscale is running on the node machine."
fi

# ─── 3. Install Caddy ───────────────────────────────────────────
info "Installing Caddy..."
if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
        gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
        tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
    info "Caddy installed."
else
    info "Caddy already installed."
fi

# ─── 4. Configure UFW Firewall ──────────────────────────────────
info "Configuring UFW firewall..."
if [ "$RESET_FIREWALL" = true ]; then
    warn "Resetting UFW (--reset-firewall): existing custom rules are removed."
    ufw --force reset >/dev/null 2>&1
fi
ufw default deny incoming
ufw default allow outgoing

# SSH — essential, don't lock yourself out
ufw allow 22/tcp comment "SSH"

# HTTP/HTTPS — Caddy needs these for Let's Encrypt + serving
ufw allow 80/tcp comment "HTTP (Caddy/Let's Encrypt)"
ufw allow 443/tcp comment "HTTPS (Caddy)"

# Tailscale — uses UDP 41641 by default
ufw allow 41641/udp comment "Tailscale"

# Enable firewall
ufw --force enable
info "UFW configured and enabled."
ufw status numbered

# ─── 5. Create Caddyfile ────────────────────────────────────────
info "Creating Caddyfile template..."

# Back up existing Caddyfile if present
if [ -f /etc/caddy/Caddyfile ]; then
    cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
    info "Backed up existing Caddyfile to /etc/caddy/Caddyfile.bak"
fi

cat > /etc/caddy/Caddyfile << 'CADDYEOF'
# ClawRange Gateway — Reverse Proxy Configuration
#
# Replace YOUR_DOMAIN with your actual domain (e.g., ai.example.com)
# Caddy automatically obtains and renews Let's Encrypt certificates.
#
# To use IP-only (no domain), replace the domain blocks with :443 and
# add tls internal for self-signed certs.

# ─── OpenClaw API ────────────────────────────────────────────────
# Main AI chat endpoint. This is what your website/app calls.
YOUR_DOMAIN {
    # Rate limiting: 20 requests per second per IP
    # (install caddy-ratelimit module for production)

    # Security headers
    header {
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        X-XSS-Protection "1; mode=block"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }

    # Proxy to OpenClaw on the node via Tailscale tunnel
    reverse_proxy NODE_TAILSCALE_IP_PLACEHOLDER:OPENCLAW_PORT_PLACEHOLDER {
        # Health checking — stop sending traffic if node is down
        health_uri /healthz
        health_interval 30s
        health_timeout 10s

        # Pass real client IP to OpenClaw
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }

    # Request logging
    log {
        output file /var/log/caddy/openclaw-access.log {
            roll_size 10mb
            roll_keep 5
        }
    }
}

# ─── Workflows webhooks (if needed from external services) ──────
# Only uncomment if external services need to POST to workflows webhooks.
#
# webhooks.YOUR_DOMAIN {
#     # Only allow specific webhook paths
#     @allowed-webhooks path /webhook/* /webhook-test/*
#     reverse_proxy @allowed-webhooks NODE_TAILSCALE_IP_PLACEHOLDER:WORKFLOWS_PORT_PLACEHOLDER
#
#     # Block everything else
#     respond 404
# }
CADDYEOF

# Replace placeholder with actual node IP
sed -i -e "s/NODE_TAILSCALE_IP_PLACEHOLDER/${NODE_TAILSCALE_IP}/g" \
    -e "s/OPENCLAW_PORT_PLACEHOLDER/${OPENCLAW_PORT}/g" \
    -e "s/WORKFLOWS_PORT_PLACEHOLDER/${WORKFLOWS_PORT}/g" /etc/caddy/Caddyfile

info "Caddyfile created at /etc/caddy/Caddyfile (node IP: ${NODE_TAILSCALE_IP})"
warn "EDIT /etc/caddy/Caddyfile — replace YOUR_DOMAIN with your actual domain"

# Create log directory
mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy

# ─── 6. Summary ──────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Gateway Setup Complete"
echo "============================================"
echo ""
echo "  Gateway Tailscale IP:  ${GATEWAY_TS_IP}"
echo "  Node Tailscale IP:     ${NODE_TAILSCALE_IP}"
echo ""
echo "  Next steps:"
echo "  1. Point your domain's DNS A record to this droplet's public IP"
echo "  2. Edit /etc/caddy/Caddyfile — replace YOUR_DOMAIN"
echo "  3. Reload Caddy:  systemctl reload caddy"
echo "  4. Test:  curl https://YOUR_DOMAIN/healthz"
echo ""
echo "  Useful commands:"
echo "    systemctl status caddy          # Check Caddy status"
echo "    journalctl -u caddy -f          # Caddy logs"
echo "    caddy validate --config /etc/caddy/Caddyfile  # Validate config"
echo "    tailscale status                # Check VPN status"
echo "    ufw status                      # Check firewall"
echo ""
