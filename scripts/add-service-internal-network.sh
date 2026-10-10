#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/lib.sh"
require_root
require_command pct
require_command python3

[ "$#" -eq 2 ] || die "Usage: $0 CT_ID 10.77.0.X/24"
CT_ID=$1
ADDRESS=$2
[[ "$CT_ID" =~ ^[0-9]+$ ]] || die "Invalid CT ID"
python3 - "$ADDRESS" <<'VALIDATE'
import ipaddress
import sys
address = ipaddress.ip_interface(sys.argv[1])
if address.network != ipaddress.ip_network('10.77.0.0/24') or address.ip in [address.network.network_address, address.network.broadcast_address]:
    raise SystemExit('Expected a host address in 10.77.0.0/24')
if int(address.ip) - int(address.network.network_address) in (1, 2, 3):
    raise SystemExit('Addresses .1, .2 and .3 are reserved for infrastructure')
if 128 <= int(address.ip) - int(address.network.network_address) <= 239:
    raise SystemExit('Addresses .128-.239 are reserved for inventory virtual IPs')
VALIDATE

if grep -RFq "ip=${ADDRESS%/*}/" /etc/pve/lxc /etc/pve/qemu-server; then
    die "Address is already present in a Proxmox guest configuration"
fi
if pct config "$CT_ID" | grep -q '^net1:'; then
    die "CT $CT_ID already has net1; inspect it before making changes"
fi
BACKUP_DIR="/root/tailscale-gateway-backups/$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$BACKUP_DIR"
cp "/etc/pve/lxc/$CT_ID.conf" "$BACKUP_DIR/$CT_ID.conf"
pct exec "$CT_ID" -- ip -4 route > "$BACKUP_DIR/$CT_ID.routes.before"
default_before=$(pct exec "$CT_ID" -- ip -4 route show default)

# Hot-add a connected route only. The LAN interface and default route stay intact.
pct set "$CT_ID" --net1 "name=lan0,bridge=vmbr1,ip=$ADDRESS,ip6=manual,type=veth"
pct exec "$CT_ID" -- ip link set lan0 up
if ! pct exec "$CT_ID" -- ip -4 addr show dev lan0 | grep -Fq "inet $ADDRESS "; then
    pct exec "$CT_ID" -- ip address add "$ADDRESS" dev lan0
fi
default_after=$(pct exec "$CT_ID" -- ip -4 route show default)
[ "$default_before" = "$default_after" ] || die "Default route changed; inspect $BACKUP_DIR before continuing"
pct exec "$CT_ID" -- ip -4 route
printf 'Backup: %s\n' "$BACKUP_DIR"
printf 'Next: allow the required service ports in config/tailscale-gateway.nft.\n'
