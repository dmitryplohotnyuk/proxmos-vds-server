#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
load_env "$PROJECT_DIR/.env"
require_root

CT_ID=${CT_ID:-100}

if ! pct status "$CT_ID" >/dev/null 2>&1; then
    "$SCRIPT_DIR/create-proxmox-lxc.sh"
else
    log "Using existing CT $CT_ID; its network and resources will not be changed"
    pct status "$CT_ID" | grep -q 'status: running' || pct start "$CT_ID"
fi

archive=$(mktemp /tmp/domain-router-source.XXXXXX.tar.gz)
trap 'rm -f "$archive"' EXIT
log "Copying repository into CT $CT_ID"
tar --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=.env \
    -czf "$archive" -C "$PROJECT_DIR" .
pct exec "$CT_ID" -- mkdir -p /opt/domain-router-ui/source
pct push "$CT_ID" "$archive" /tmp/domain-router-source.tar.gz
pct exec "$CT_ID" -- tar -xzf /tmp/domain-router-source.tar.gz -C /opt/domain-router-ui/source
pct exec "$CT_ID" -- rm -f /tmp/domain-router-source.tar.gz

domain_zone=${DOMAIN_ZONE:-content-factory-vps.win}
private_cidr=${PRIVATE_CIDR:-10.77.0.0/24}
cloudflare_tunnel_name=${CLOUDFLARE_TUNNEL_NAME:-proxmox-domain-router}
tailscale_hostname=${TAILSCALE_HOSTNAME:-domain-router}
log "Installing local services in CT $CT_ID"
pct exec "$CT_ID" -- env \
    DOMAIN_ZONE="$domain_zone" \
    PRIVATE_CIDR="$private_cidr" \
    CLOUDFLARE_TUNNEL_NAME="$cloudflare_tunnel_name" \
    TAILSCALE_HOSTNAME="$tailscale_hostname" \
    /opt/domain-router-ui/source/scripts/install-lxc.sh /opt/domain-router-ui/source

cat <<EOF

Local deployment completed.

Next commands:
  pct enter $CT_ID
  /opt/domain-router-ui/source/scripts/configure-integrations.sh
  /opt/domain-router-ui/source/scripts/verify-deployment.sh
EOF
