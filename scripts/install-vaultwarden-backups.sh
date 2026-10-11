#!/bin/bash
set -euo pipefail
[[ $(id -u) == 0 && $# == 1 && -f $1 ]] || { echo 'Usage (Proxmox root): install-vaultwarden-backups.sh PUBLIC-RECIPIENT-FILE' >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
CT_ID=${VAULT_CT_ID:-108}
[[ $CT_ID =~ ^[0-9]+$ ]] || exit 1
recipient=$(cat "$1")
[[ $recipient =~ ^age1[a-z0-9]{58}$ ]] || { echo 'Only a public native age recipient is accepted.' >&2; exit 1; }
pct exec "$CT_ID" -- install -d -m 0700 /etc/vaultwarden
pct push "$CT_ID" "$1" /etc/vaultwarden/backup-recipient.txt --perms 0600
install -m 0755 "$SCRIPT_DIR/vaultwarden-host-backup.sh" /usr/local/sbin/vaultwarden-host-backup
install -m 0644 "$PROJECT_DIR/config/systemd/vaultwarden-host-backup.service" /etc/systemd/system/
install -m 0644 "$PROJECT_DIR/config/systemd/vaultwarden-host-backup.timer" /etc/systemd/system/
install -d -m 0755 /etc/systemd/system/vaultwarden-host-backup.service.d
printf '[Service]\nEnvironment=VAULT_CT_ID=%s\n' "$CT_ID" >/etc/systemd/system/vaultwarden-host-backup.service.d/container.conf
systemctl daemon-reload
systemctl enable --now vaultwarden-host-backup.timer
systemctl start vaultwarden-host-backup.service
systemctl is-active vaultwarden-host-backup.timer
