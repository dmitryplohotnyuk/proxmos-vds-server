#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
require_root

failed=0
check() {
    description=$1
    shift
    if "$@" >/dev/null 2>&1; then
        printf 'PASS  %s\n' "$description"
    else
        printf 'FAIL  %s\n' "$description"
        failed=1
    fi
}

check "WAN interface has IPv4" sh -c "ip -4 addr show dev eth0 | grep -q 'inet '"
check "private interface is 10.77.0.2/24" sh -c "ip -4 addr show dev lan0 | grep -q '10.77.0.2/24'"
check "IPv4 forwarding enabled" sh -c "[ \"$(sysctl -n net.ipv4.ip_forward)\" = 1 ]"
check "nftables active" systemctl is-active --quiet nftables
check "Caddy active" systemctl is-active --quiet caddy
check "Caddy configuration valid" caddy validate --config /etc/caddy/Caddyfile
check "network watchdog timer active" systemctl is-active --quiet domain-router-network-watchdog.timer
check "admin panel active" systemctl is-active --quiet domain-router-ui
check "admin panel health endpoint" curl -fsS http://127.0.0.1:8090/healthz
check "CLI status" domain-router status --json
check "Tailscale active" systemctl is-active --quiet tailscaled
check "Tailscale has IPv4" sh -c "[ -n \"$(tailscale ip -4 2>/dev/null)\" ]"
check "Tailscale Serve configured" sh -c "tailscale serve status 2>/dev/null | grep -q '127.0.0.1:8090'"
check "Tailscale Funnel disabled" sh -c \
    "tailscale serve status --json 2>/dev/null | jq -e '([.AllowFunnel[]?] | any) | not'"

if systemctl is-enabled --quiet cloudflared.service 2>/dev/null; then
    check "Cloudflare Tunnel active" systemctl is-active --quiet cloudflared
else
    printf 'SKIP  Cloudflare Tunnel is not configured yet\n'
fi

if [ "$failed" -ne 0 ]; then
    printf '\nDeployment verification failed.\n' >&2
    exit 1
fi
printf '\nDeployment verification passed.\n'
