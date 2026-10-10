#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
require_root

CT_ID=${ADGUARD_CT_ID:-106}
CT_HOSTNAME=${ADGUARD_CT_HOSTNAME:-adguard-home}
CT_IP=${ADGUARD_CT_IP:-192.168.0.5/24}
CT_GATEWAY=${ADGUARD_CT_GATEWAY:-192.168.0.3}
CT_BRIDGE=${ADGUARD_CT_BRIDGE:-vmbr0}
CT_STORAGE=${ADGUARD_CT_STORAGE:-local-lvm}
CT_TEMPLATE=${ADGUARD_CT_TEMPLATE:-local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst}
ADGUARD_VERSION=${ADGUARD_VERSION:-v0.107.79}
ADGUARD_SHA256=${ADGUARD_SHA256:-c48f4a43000665484c5ec28177de11a004759b620dae8f77b2aabefc9ef3687f}

require_command pct
require_command openssl
require_command pvesm
require_command ping
require_command dig

[ ! -e "/etc/pve/lxc/$CT_ID.conf" ] || die "CT $CT_ID already exists"
pvesm path "$CT_TEMPLATE" >/dev/null 2>&1 || die "template is unavailable: $CT_TEMPLATE"

ip_address=${CT_IP%/*}
if ping -c 2 -W 1 "$ip_address" >/dev/null 2>&1; then
    die "IP address already responds: $ip_address"
fi

root_password=$(openssl rand -hex 16)
admin_password=$(openssl rand -hex 16)

log "Creating LXC $CT_ID ($CT_HOSTNAME)"
pct create "$CT_ID" "$CT_TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --cores 1 \
    --memory 512 \
    --swap 512 \
    --rootfs "$CT_STORAGE:4" \
    --net0 "name=eth0,bridge=$CT_BRIDGE,ip=$CT_IP,gw=$CT_GATEWAY,ip6=manual,type=veth" \
    --unprivileged 1 \
    --onboot 1 \
    --startup order=1,up=20,down=30 \
    --nameserver 1.1.1.1 \
    --password "$root_password"

pct start "$CT_ID"
for _ in $(seq 1 30); do
    if pct exec "$CT_ID" -- true >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
pct exec "$CT_ID" -- true >/dev/null

log "Installing AdGuard Home $ADGUARD_VERSION"
pct exec "$CT_ID" -- env \
    ADGUARD_VERSION="$ADGUARD_VERSION" \
    ADGUARD_SHA256="$ADGUARD_SHA256" \
    ADGUARD_ADMIN_PASSWORD="$admin_password" \
    bash -s <<'IN_CONTAINER'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl python3

cd /tmp
archive=AdGuardHome_linux_amd64.tar.gz
curl -fL -o "$archive" \
    "https://github.com/AdguardTeam/AdGuardHome/releases/download/$ADGUARD_VERSION/$archive"
printf '%s  %s\n' "$ADGUARD_SHA256" "$archive" | sha256sum -c -

rm -rf /opt/AdGuardHome
tar -xzf "$archive" -C /opt
/opt/AdGuardHome/AdGuardHome -s install
systemctl enable --now AdGuardHome

check_payload='{"web":{"ip":"0.0.0.0","port":3000},"dns":{"ip":"0.0.0.0","port":53},"set_static_ip":false}'
curl -fsS -H 'Content-Type: application/json' --data-binary "$check_payload" \
    http://127.0.0.1:3000/control/install/check_config >/dev/null

configure_payload=$(printf \
    '{"web":{"ip":"0.0.0.0","port":3000},"dns":{"ip":"0.0.0.0","port":53},"username":"admin","password":"%s"}' \
    "$ADGUARD_ADMIN_PASSWORD")
curl -fsS -H 'Content-Type: application/json' --data-binary "$configure_payload" \
    http://127.0.0.1:3000/control/install/configure >/dev/null

control_url=http://127.0.0.1:3000/control
auth="admin:$ADGUARD_ADMIN_PASSWORD"

curl -fsS -u "$auth" "$control_url/dns_info" >/tmp/adguard-dns.json
python3 - <<'PY'
import json

path = "/tmp/adguard-dns.json"
with open(path, encoding="utf-8") as source:
    config = json.load(source)
config.pop("default_local_ptr_upstreams", None)
config["upstream_dns"] = ["https://dns.quad9.net/dns-query"]
config["enable_dnssec"] = True
config["fallback_dns"] = ["https://1.1.1.1/dns-query"]
config["cache_optimistic"] = True
with open(path, "w", encoding="utf-8") as target:
    json.dump(config, target)
PY
curl -fsS -u "$auth" -H 'Content-Type: application/json' \
    --data-binary @/tmp/adguard-dns.json "$control_url/dns_config" >/dev/null

curl -fsS -u "$auth" "$control_url/querylog/config" >/tmp/adguard-querylog.json
python3 - <<'PY'
import json

path = "/tmp/adguard-querylog.json"
with open(path, encoding="utf-8") as source:
    config = json.load(source)
config["interval"] = 604800000
with open(path, "w", encoding="utf-8") as target:
    json.dump(config, target)
PY
curl -fsS -u "$auth" -X PUT -H 'Content-Type: application/json' \
    --data-binary @/tmp/adguard-querylog.json "$control_url/querylog/config/update" >/dev/null

filter_url=https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/multi.txt
filter_payload=$(printf \
    '{"name":"HaGeZi Multi NORMAL","url":"%s","whitelist":false}' \
    "$filter_url")
curl -fsS -u "$auth" -H 'Content-Type: application/json' \
    --data-binary "$filter_payload" "$control_url/filtering/add_url" >/dev/null

for rewrite in \
    'proxmox.home.arpa 192.168.0.176' \
    'home-assistant.home.arpa 192.168.0.222'; do
    domain=${rewrite%% *}
    answer=${rewrite#* }
    rewrite_payload=$(printf \
        '{"domain":"%s","answer":"%s","enabled":true}' \
        "$domain" "$answer")
    curl -fsS -u "$auth" -H 'Content-Type: application/json' \
        --data-binary "$rewrite_payload" "$control_url/rewrite/add" >/dev/null
done

chmod 700 /opt/AdGuardHome
rm -f "/tmp/$archive" /tmp/adguard-dns.json /tmp/adguard-querylog.json
systemctl restart AdGuardHome
IN_CONTAINER

log "Verification"
pct exec "$CT_ID" -- systemctl is-enabled AdGuardHome
pct exec "$CT_ID" -- systemctl is-active AdGuardHome
dig "@$ip_address" example.com +short >/dev/null
blocked=$(dig "@$ip_address" doubleclick.net A +short | head -1)
[ "$blocked" = 0.0.0.0 ] || die "blocking verification failed: $blocked"
[ "$(dig "@$ip_address" proxmox.home.arpa A +short | head -1)" = 192.168.0.176 ] || \
    die "local rewrite verification failed"

printf '\nAdGuard Home is ready. Credentials are printed once.\n'
printf 'Panel:              http://%s:3000\n' "$ip_address"
printf 'Admin username:     admin\n'
printf 'Admin password:     %s\n' "$admin_password"
printf 'LXC root password:  %s\n' "$root_password"
