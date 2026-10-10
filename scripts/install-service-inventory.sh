#!/bin/bash
set -euo pipefail
source_dir=$(cd "${1:-$(dirname "$0")/..}" && pwd)
[[ $(id -u) == 0 ]] || { echo 'Run on Proxmox as root' >&2; exit 1; }
[[ -f "$source_dir/src/domain_router_services/agent.py" ]] || exit 1
if ! command -v visudo >/dev/null; then
    apt-get update
    apt-get install -y sudo
fi
for guest in 100 107; do
    [[ $(pct status "$guest") == *running* ]] || { echo "CT $guest must be running" >&2; exit 1; }
done

install -d -m 0700 /var/lib/service-inventory
install -d -m 0755 /opt/service-inventory
install -m 0755 "$source_dir/src/domain_router_services/agent.py" /opt/service-inventory/agent.py
install -m 0755 "$source_dir/scripts/service-inventory-agent" /usr/local/sbin/service-inventory-agent
if ! getent passwd service-inventory >/dev/null; then
    useradd --system --create-home --home-dir /var/lib/service-inventory-ssh --shell /bin/sh service-inventory
fi
install -d -o root -g root -m 0755 /var/lib/service-inventory-ssh
install -d -o root -g root -m 0755 /var/lib/service-inventory-ssh/.ssh
install -m 0440 "$source_dir/config/sudoers/service-inventory" /etc/sudoers.d/service-inventory
visudo -cf /etc/sudoers.d/service-inventory

# The UI gets a forced-command identity, never a Proxmox root key.
pct exec 100 -- /bin/sh -c '
    command -v ssh >/dev/null || { apt-get update && apt-get install -y openssh-client; }
    if [ ! -f /var/lib/domain-router-ui/service-inventory_ed25519 ]; then
        runuser -u domain-router-ui -- ssh-keygen -q -t ed25519 -N "" -f /var/lib/domain-router-ui/service-inventory_ed25519
    fi
'
public_key=$(pct exec 100 -- cat /var/lib/domain-router-ui/service-inventory_ed25519.pub)
printf 'restrict,from="10.77.0.2",command="sudo -n /usr/local/sbin/service-inventory-agent" %s\n' "$public_key" > /var/lib/service-inventory-ssh/.ssh/authorized_keys
chmod 0644 /var/lib/service-inventory-ssh/.ssh/authorized_keys

temporary=$(mktemp)
trap 'rm -f "$temporary"' EXIT
printf '10.77.0.1 %s\n' "$(cat /etc/ssh/ssh_host_ed25519_key.pub)" > "$temporary"
pct push 100 "$temporary" /var/lib/domain-router-ui/service-inventory_known_hosts --perms 0644
pct exec 100 -- chown domain-router-ui:domain-router-ui /var/lib/domain-router-ui/service-inventory_known_hosts
pct exec 100 -- mkdir -p /etc/systemd/system/domain-router-ui.service.d
pct push 100 "$source_dir/config/systemd/service-inventory-ui.conf" /etc/systemd/system/domain-router-ui.service.d/service-inventory.conf --perms 0644

install -m 0644 "$source_dir/config/systemd/service-inventory-sync.service" /etc/systemd/system/service-inventory-sync.service
install -m 0644 "$source_dir/config/systemd/service-inventory-sync.timer" /etc/systemd/system/service-inventory-sync.timer
systemctl daemon-reload
systemctl start service-inventory-sync.service
systemctl enable --now service-inventory-sync.timer
pct exec 100 -- systemctl daemon-reload
pct exec 100 -- systemctl restart domain-router-ui.service
echo 'Inventory installed; UI source must include the internal-services tab.'
