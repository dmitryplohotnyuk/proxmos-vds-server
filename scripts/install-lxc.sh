#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
require_root

SOURCE_DIR=${1:-/opt/domain-router-ui/source}
DOMAIN_ZONE=${DOMAIN_ZONE:-content-factory-vps.win}
PRIVATE_CIDR=${PRIVATE_CIDR:-10.77.0.0/24}
CLOUDFLARE_TUNNEL_NAME=${CLOUDFLARE_TUNNEL_NAME:-proxmox-domain-router}
TAILSCALE_HOSTNAME=${TAILSCALE_HOSTNAME:-domain-router}

[ -f "$SOURCE_DIR/pyproject.toml" ] || die "project not found at $SOURCE_DIR"

log "Installing Debian packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates caddy curl jq nftables python3 python3-pip \
    python3-venv sudo

if ! command -v cloudflared >/dev/null 2>&1; then
    arch=$(dpkg --print-architecture)
    [ "$arch" = amd64 ] || die "automatic cloudflared install currently supports amd64 only"
    package=/tmp/cloudflared.deb
    curl -fsSL -o "$package" \
        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    apt-get install -y "$package"
    rm -f "$package"
fi
cloudflared_path=$(command -v cloudflared)
if [ "$cloudflared_path" != /usr/local/bin/cloudflared ]; then
    ln -sfn "$cloudflared_path" /usr/local/bin/cloudflared
fi

if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh -o /tmp/install-tailscale.sh
    sh /tmp/install-tailscale.sh
    rm -f /tmp/install-tailscale.sh
fi

log "Installing network and reverse proxy configuration"
install -d -m 0755 /etc/domain-router /etc/systemd/system/nftables.service.d
if [ ! -f /etc/domain-router/routes.yml ]; then
    install -m 0644 "$SOURCE_DIR/config/routes.yml" /etc/domain-router/routes.yml
fi
printf 'DOMAIN_ROUTER_ALLOWED_DOMAIN=%s\nDOMAIN_ROUTER_UPSTREAM_NETWORKS=%s\n' \
    "$DOMAIN_ZONE" "$PRIVATE_CIDR" >/etc/domain-router/environment
chmod 0644 /etc/domain-router/environment
printf 'DOMAIN_ZONE=%s\nPRIVATE_CIDR=%s\nCLOUDFLARE_TUNNEL_NAME=%s\nTAILSCALE_HOSTNAME=%s\n' \
    "$DOMAIN_ZONE" "$PRIVATE_CIDR" "$CLOUDFLARE_TUNNEL_NAME" "$TAILSCALE_HOSTNAME" \
    >/etc/domain-router/deployment.env
chmod 0600 /etc/domain-router/deployment.env
install -m 0644 "$SOURCE_DIR/config/sysctl/99-domain-router.conf" \
    /etc/sysctl.d/99-domain-router.conf
install -m 0755 "$SOURCE_DIR/config/nftables/domain-router.nft" /etc/nftables.conf
install -m 0644 "$SOURCE_DIR/config/systemd/nftables-tailscale-order.conf" \
    /etc/systemd/system/nftables.service.d/order.conf
install -m 0755 "$SOURCE_DIR/scripts/network-watchdog.sh" \
    /usr/local/sbin/domain-router-network-watchdog
install -m 0644 "$SOURCE_DIR/config/systemd/domain-router-network-watchdog.service" \
    /etc/systemd/system/domain-router-network-watchdog.service
install -m 0644 "$SOURCE_DIR/config/systemd/domain-router-network-watchdog.timer" \
    /etc/systemd/system/domain-router-network-watchdog.timer

sysctl --system >/dev/null
systemctl daemon-reload
systemctl enable --now nftables caddy domain-router-network-watchdog.timer tailscaled

log "Installing CLI and administration panel"
DOMAIN_ZONE="$DOMAIN_ZONE" PRIVATE_CIDR="$PRIVATE_CIDR" \
    "$SOURCE_DIR/scripts/install-admin-panel.sh" "$SOURCE_DIR"

domain-router render
systemctl reload caddy
log "Local LXC installation completed"
