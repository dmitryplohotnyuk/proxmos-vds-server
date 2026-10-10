#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
require_root

if ! command -v ethtool >/dev/null; then
    apt-get update
    apt-get install -y ethtool
fi

install -m 0755 "$SCRIPT_DIR/proxmox-nic-watchdog.sh" \
    /usr/local/sbin/proxmox-nic-watchdog
install -m 0644 "$PROJECT_DIR/config/systemd/proxmox-nic-watchdog.service" \
    /etc/systemd/system/proxmox-nic-watchdog.service
install -m 0644 "$PROJECT_DIR/config/systemd/proxmox-nic-watchdog.timer" \
    /etc/systemd/system/proxmox-nic-watchdog.timer

if [ ! -e /etc/default/proxmox-nic-watchdog ]; then
    install -m 0644 "$PROJECT_DIR/config/proxmox/proxmox-nic-watchdog.default.example" \
        /etc/default/proxmox-nic-watchdog
fi

systemctl daemon-reload
systemctl enable --now proxmox-nic-watchdog.timer

WATCHDOG_FORCE=1 WATCHDOG_DRY_RUN=1 /usr/local/sbin/proxmox-nic-watchdog
systemctl is-enabled proxmox-nic-watchdog.timer
systemctl is-active proxmox-nic-watchdog.timer
