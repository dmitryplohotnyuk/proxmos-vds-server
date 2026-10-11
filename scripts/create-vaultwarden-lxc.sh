#!/bin/bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/lib.sh"
require_root
require_command pct
require_command pveam
CT_ID=${VAULT_CT_ID:-108}
CT_STORAGE=${VAULT_CT_STORAGE:-local-lvm}
CT_PRIVATE_IP=${VAULT_PRIVATE_IP:-10.77.0.4/24}
[[ $CT_ID =~ ^[0-9]+$ ]] || die "Invalid CT ID"
if pct status "$CT_ID" >/dev/null 2>&1 || qm status "$CT_ID" >/dev/null 2>&1; then
    die "Guest $CT_ID already exists; refusing to modify it"
fi
ip link show "${PRIVATE_BRIDGE:-vmbr1}" >/dev/null
template=$(pveam list local | awk '/debian-12-standard/ {print $1; exit}')
[ -n "$template" ] || die "Download a Debian 12 template to local first"
pct create "$CT_ID" "$template" --hostname vaultwarden --ostype debian \
    --unprivileged 1 --cores 1 --memory 1024 --swap 256 \
    --rootfs "$CT_STORAGE:8" --features nesting=1,keyctl=1 \
    --net0 "name=eth0,bridge=${WAN_BRIDGE:-vmbr0},ip=dhcp,ip6=manual,type=veth" \
    --net1 "name=lan0,bridge=${PRIVATE_BRIDGE:-vmbr1},ip=$CT_PRIVATE_IP,ip6=manual,type=veth" \
    --nameserver 1.1.1.1 --onboot 1 --startup order=2,up=10,down=30 --tags vaultwarden
pct start "$CT_ID"
archive=$(mktemp /tmp/vaultwarden-source.XXXXXX.tar.gz)
trap 'rm -f "$archive"' EXIT
if [[ -n ${VAULT_SOURCE_ARCHIVE:-} ]]; then
    [[ -f $VAULT_SOURCE_ARCHIVE ]] || die "Source archive not found"
    cp -- "$VAULT_SOURCE_ARCHIVE" "$archive"
else
    bash "$SCRIPT_DIR/package-source.sh" "$archive"
fi
pct exec "$CT_ID" -- mkdir -p /opt/vaultwarden/source
pct push "$CT_ID" "$archive" /tmp/vaultwarden-source.tar.gz
pct exec "$CT_ID" -- tar -xzf /tmp/vaultwarden-source.tar.gz -C /opt/vaultwarden/source
pct exec "$CT_ID" -- rm -f /tmp/vaultwarden-source.tar.gz
pct exec "$CT_ID" -- bash /opt/vaultwarden/source/scripts/install-vaultwarden.sh
printf '\nCT %s created. Connect Tailscale interactively, then run configure-vaultwarden-https.sh.\n' "$CT_ID"
