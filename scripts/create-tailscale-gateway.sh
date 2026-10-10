#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/lib.sh"
require_root
require_command pct
require_command pvesm

CT_ID=${TAILSCALE_GATEWAY_CT_ID:-107}
TEMPLATE=${TAILSCALE_GATEWAY_TEMPLATE:-local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst}
STORAGE=${TAILSCALE_GATEWAY_STORAGE:-local-lvm}
LAN_BRIDGE=${TAILSCALE_GATEWAY_LAN_BRIDGE:-vmbr0}
INTERNAL_BRIDGE=${TAILSCALE_GATEWAY_INTERNAL_BRIDGE:-vmbr1}
INTERNAL_IP=${TAILSCALE_GATEWAY_INTERNAL_IP:-10.77.0.3/24}

[ ! -e "/etc/pve/lxc/$CT_ID.conf" ] || die "CT $CT_ID already exists; refusing to overwrite"
pvesm path "$TEMPLATE" >/dev/null
[ -e "/sys/class/net/$INTERNAL_BRIDGE" ] || die "Internal bridge missing"
if grep -RFq "ip=${INTERNAL_IP%/*}/" /etc/pve/lxc /etc/pve/qemu-server; then
    die "Gateway address is already present in a Proxmox guest configuration"
fi

pct create "$CT_ID" "$TEMPLATE" \
    --hostname tailscale-gateway --cores 1 --memory 512 --swap 256 \
    --rootfs "$STORAGE:4" --unprivileged 1 --features nesting=1 --onboot 1 \
    --startup order=2,up=10,down=30 --nameserver 1.1.1.1 \
    --net0 "name=eth0,bridge=$LAN_BRIDGE,ip=dhcp,ip6=manual,type=veth" \
    --net1 "name=lan0,bridge=$INTERNAL_BRIDGE,ip=$INTERNAL_IP,ip6=manual,type=veth" \
    --dev0 path=/dev/net/tun
pct start "$CT_ID"
for _ in $(seq 1 30); do
    if pct exec "$CT_ID" -- true >/dev/null 2>&1; then break; fi
    sleep 1
done

pct exec "$CT_ID" -- bash -s <<'INSTALL'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
passwd -l root
apt-get update
apt-get upgrade -y
apt-get install -y ca-certificates curl nftables dnsutils tcpdump python3 iproute2
curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
    -o /etc/apt/sources.list.d/tailscale.list
apt-get update
apt-get install -y tailscale
systemctl stop tailscaled
systemctl disable --now nftables
install -d /etc/systemd/system/tailscaled.service.d
printf '[Unit]\nRequires=tailscale-gateway-firewall.service\nAfter=tailscale-gateway-firewall.service\n' \
    > /etc/systemd/system/tailscaled.service.d/gateway-firewall.conf
printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' \
    > /etc/sysctl.d/99-tailscale-gateway.conf
sysctl --load /etc/sysctl.d/99-tailscale-gateway.conf >/dev/null
INSTALL

pct push "$CT_ID" "$SCRIPT_DIR/../config/tailscale-gateway.nft" /etc/tailscale-gateway.nft --perms 0644
pct push "$CT_ID" "$SCRIPT_DIR/../config/systemd/tailscale-gateway-firewall.service" \
    /etc/systemd/system/tailscale-gateway-firewall.service --perms 0644
pct exec "$CT_ID" -- nft --check -f /etc/tailscale-gateway.nft
pct exec "$CT_ID" -- systemctl daemon-reload
pct exec "$CT_ID" -- systemctl enable --now tailscale-gateway-firewall tailscaled

printf '\nGateway installed. Authenticate interactively (no keys stored in the repository):\n'
printf 'pct exec %s -- tailscale up --hostname=tailscale-gateway --accept-dns=false --accept-routes=false --advertise-routes=10.77.0.0/24,192.168.0.222/32\n' "$CT_ID"
printf 'Approve these routes in the Tailscale console. Change key expiry only with owner approval.\n'
printf 'Remove the old router advertisement only AFTER end-to-end verification.\n'
