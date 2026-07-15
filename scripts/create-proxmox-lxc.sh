#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
load_env "$PROJECT_DIR/.env"
require_root
require_command pct
require_command pveam

CT_ID=${CT_ID:-100}
CT_HOSTNAME=${CT_HOSTNAME:-domain-router}
CT_STORAGE=${CT_STORAGE:-local-lvm}
CT_TEMPLATE_STORAGE=${CT_TEMPLATE_STORAGE:-local}
CT_CORES=${CT_CORES:-1}
CT_MEMORY=${CT_MEMORY:-1024}
CT_SWAP=${CT_SWAP:-512}
CT_DISK_GB=${CT_DISK_GB:-8}
WAN_BRIDGE=${WAN_BRIDGE:-vmbr0}
PRIVATE_BRIDGE=${PRIVATE_BRIDGE:-vmbr1}
PROXMOX_PRIVATE_IP=${PROXMOX_PRIVATE_IP:-10.77.0.1/24}
ROUTER_PRIVATE_IP=${ROUTER_PRIVATE_IP:-10.77.0.2/24}

if pct status "$CT_ID" >/dev/null 2>&1; then
    die "CT $CT_ID already exists; refusing to modify it"
fi

if ! grep -Eq "^[[:space:]]*iface[[:space:]]+$PRIVATE_BRIDGE[[:space:]]+inet" /etc/network/interfaces; then
    log "Adding private bridge $PRIVATE_BRIDGE"
    cat >>/etc/network/interfaces <<EOF

# Domain Router private application network
auto $PRIVATE_BRIDGE
iface $PRIVATE_BRIDGE inet static
        address $PROXMOX_PRIVATE_IP
        bridge-ports none
        bridge-stp off
        bridge-fd 0
EOF
    if command -v ifreload >/dev/null 2>&1; then
        ifreload -a
    else
        ifup "$PRIVATE_BRIDGE"
    fi
else
    log "Bridge $PRIVATE_BRIDGE already exists; leaving it unchanged"
fi

template=$(pveam list "$CT_TEMPLATE_STORAGE" 2>/dev/null | awk '/debian-12-standard/ {print $1; exit}')
if [ -z "$template" ]; then
    template_name=$(pveam available --section system | awk '/debian-12-standard/ {print $2; exit}')
    [ -n "$template_name" ] || die "Debian 12 LXC template not found"
    log "Downloading $template_name"
    pveam download "$CT_TEMPLATE_STORAGE" "$template_name"
    template="$CT_TEMPLATE_STORAGE:vztmpl/$template_name"
fi

log "Creating LXC $CT_ID ($CT_HOSTNAME)"
pct create "$CT_ID" "$template" \
    --hostname "$CT_HOSTNAME" \
    --ostype debian \
    --unprivileged 1 \
    --cores "$CT_CORES" \
    --memory "$CT_MEMORY" \
    --swap "$CT_SWAP" \
    --rootfs "$CT_STORAGE:$CT_DISK_GB" \
    --features nesting=1 \
    --net0 "name=eth0,bridge=$WAN_BRIDGE,ip=dhcp,type=veth" \
    --net1 "name=lan0,bridge=$PRIVATE_BRIDGE,ip=$ROUTER_PRIVATE_IP,type=veth" \
    --onboot 1 \
    --startup order=1

config_file="/etc/pve/lxc/$CT_ID.conf"
cat >>"$config_file" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF

pct start "$CT_ID"
log "Waiting for container networking"
for _ in $(seq 1 30); do
    if pct exec "$CT_ID" -- test -e /dev/net/tun && pct exec "$CT_ID" -- ping -c 1 -W 1 1.1.1.1 >/dev/null 2>&1; then
        log "LXC $CT_ID is ready"
        exit 0
    fi
    sleep 2
done
die "LXC started but network or /dev/net/tun is not ready"

