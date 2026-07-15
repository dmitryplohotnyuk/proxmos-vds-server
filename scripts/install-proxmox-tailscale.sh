#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
require_root

TAILSCALE_HOSTNAME=${PROXMOX_TAILSCALE_HOSTNAME:-proxmox}
PROXMOX_UI_ORIGIN=${PROXMOX_UI_ORIGIN:-https+insecure://127.0.0.1:8006}

if ! command -v tailscale >/dev/null 2>&1; then
    log "Installing Tailscale"
    curl -fsSL https://tailscale.com/install.sh -o /tmp/install-tailscale.sh
    sh /tmp/install-tailscale.sh
    rm -f /tmp/install-tailscale.sh
fi

if ! command -v jq >/dev/null 2>&1; then
    apt-get update
    apt-get install -y jq
fi

systemctl enable --now tailscaled

log "Connecting Proxmox to Tailscale"
if tailscale ip -4 >/dev/null 2>&1; then
    tailscale set \
        --hostname="$TAILSCALE_HOSTNAME" \
        --accept-routes=false \
        --accept-dns=true
else
    tailscale up \
        --hostname="$TAILSCALE_HOSTNAME" \
        --accept-routes=false \
        --accept-dns=true
fi

log "Publishing the Proxmox UI privately"
tailscale funnel reset >/dev/null 2>&1 || true
tailscale serve reset >/dev/null 2>&1 || true
tailscale serve --bg --yes "$PROXMOX_UI_ORIGIN"

log "Verification"
systemctl is-enabled tailscaled
systemctl is-active tailscaled
tailscale ip -4
tailscale serve status

if tailscale serve status --json | jq -e '([.AllowFunnel[]?] | any) | not' >/dev/null; then
    printf 'Tailscale Funnel: disabled\n'
else
    die "Tailscale Funnel is enabled"
fi
