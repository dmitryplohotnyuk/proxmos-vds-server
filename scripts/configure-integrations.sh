#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
load_env /etc/domain-router/deployment.env
load_env "$PROJECT_DIR/.env"
require_root

DOMAIN_ZONE=${DOMAIN_ZONE:-content-factory-vps.win}
CLOUDFLARE_TUNNEL_NAME=${CLOUDFLARE_TUNNEL_NAME:-proxmox-domain-router}
TAILSCALE_HOSTNAME=${TAILSCALE_HOSTNAME:-domain-router}
PRIVATE_CIDR=${PRIVATE_CIDR:-10.77.0.0/24}

log "Configuring Cloudflare Tunnel for $DOMAIN_ZONE"
if [ ! -f /root/.cloudflared/cert.pem ]; then
    cloudflared tunnel login
fi

tunnel_id=$(cloudflared tunnel list --output json | jq -r \
    --arg name "$CLOUDFLARE_TUNNEL_NAME" '.[] | select(.name == $name) | .id' | head -n1)
if [ -z "$tunnel_id" ]; then
    cloudflared tunnel create "$CLOUDFLARE_TUNNEL_NAME"
    tunnel_id=$(cloudflared tunnel list --output json | jq -r \
        --arg name "$CLOUDFLARE_TUNNEL_NAME" '.[] | select(.name == $name) | .id' | head -n1)
fi
[ -n "$tunnel_id" ] || die "could not determine Cloudflare tunnel ID"

credentials="/root/.cloudflared/$tunnel_id.json"
[ -f "$credentials" ] || die "tunnel credentials not found: $credentials"
install -d -m 0700 /etc/cloudflared
install -m 0600 "$credentials" "/etc/cloudflared/$tunnel_id.json"
sed -e "s/__TUNNEL_ID__/$tunnel_id/g" \
    "$PROJECT_DIR/config/cloudflared/config.yml.example" >/etc/cloudflared/config.yml
chmod 0600 /etc/cloudflared/config.yml
install -m 0644 "$PROJECT_DIR/config/systemd/cloudflared.service" \
    /etc/systemd/system/cloudflared.service

cloudflared tunnel route dns --overwrite-dns "$CLOUDFLARE_TUNNEL_NAME" "$DOMAIN_ZONE"
cloudflared tunnel route dns --overwrite-dns "$CLOUDFLARE_TUNNEL_NAME" "*.$DOMAIN_ZONE"
systemctl daemon-reload
systemctl enable --now cloudflared

log "Configuring private Tailscale access"
if tailscale ip -4 >/dev/null 2>&1; then
    tailscale set --hostname="$TAILSCALE_HOSTNAME" --ssh --advertise-routes="$PRIVATE_CIDR"
else
    tailscale up --hostname="$TAILSCALE_HOSTNAME" --ssh --advertise-routes="$PRIVATE_CIDR"
fi
tailscale serve reset || true
tailscale funnel reset || true
tailscale serve --bg http://127.0.0.1:8090

log "Creating the first panel administrator"
if domain-router-ui-admin status | grep -qx 'users: 0'; then
    domain-router-ui-admin create admin
else
    printf 'An administrator already exists; creation skipped.\n'
fi

cat <<EOF

Integration setup completed.
Approve subnet route $PRIVATE_CIDR in the Tailscale admin console if required.
Panel URL from Tailscale Serve status:
EOF
tailscale serve status
