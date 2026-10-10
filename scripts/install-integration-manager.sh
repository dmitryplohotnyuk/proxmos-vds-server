#!/bin/bash
set -euo pipefail
source_dir=$(cd "${1:-$(dirname "$0")/..}" && pwd)
[[ $(id -u) == 0 ]] || { echo 'Run on Proxmox as root' >&2; exit 1; }
for guest in 100 107; do
    [[ $(pct status "$guest") == *running* ]] || { echo "CT $guest must be running" >&2; exit 1; }
    pct exec "$guest" -- install -d -m 0755 /opt/domain-router-integrations
    pct exec "$guest" -- install -d -m 0700 /etc/domain-router-integrations /var/lib/domain-router-integrations
    pct push "$guest" "$source_dir/src/domain_router_services/integrations.py" /opt/domain-router-integrations/agent.py --perms 0755
done
role_file=$(mktemp)
trap 'rm -f "$role_file"' EXIT
printf 'panel\n' > "$role_file"
pct push 100 "$role_file" /etc/domain-router-integrations/role --perms 0600
printf 'gateway\n' > "$role_file"
pct push 107 "$role_file" /etc/domain-router-integrations/role --perms 0600
install -m 0755 "$source_dir/src/domain_router_services/agent.py" /opt/service-inventory/agent.py
for unit in domain-router-cloudflare-rollback.service domain-router-cloudflare-rollback.timer; do
    pct push 100 "$source_dir/config/systemd/$unit" "/etc/systemd/system/$unit" --perms 0644
done
pct exec 100 -- systemctl daemon-reload
echo 'Integration agents installed. Existing Tunnel, Tailscale and firewall are unchanged.'
