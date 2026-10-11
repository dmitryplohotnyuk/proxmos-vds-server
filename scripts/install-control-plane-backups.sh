#!/bin/bash
set -euo pipefail
[[ $(id -u) == 0 && $# == 1 && -f $1 ]] || { echo 'Usage (Proxmox root): install-control-plane-backups.sh PUBLIC-RECIPIENT-FILE' >&2; exit 1; }
command -v age >/dev/null || { echo 'Install age from Debian packages first.' >&2; exit 1; }
recipient=$(cat "$1")
[[ $recipient =~ ^age1[a-z0-9]{58}$ ]] || exit 1
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -d -m 0700 /etc/control-plane-backup
install -m 0600 "$1" /etc/control-plane-backup/recipient.txt
install -m 0755 "$script_dir/control-plane-backup.py" /usr/local/sbin/control-plane-backup
install -m 0644 "$script_dir/../config/systemd/control-plane-backup.service" /etc/systemd/system/
install -m 0644 "$script_dir/../config/systemd/control-plane-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl start control-plane-backup.service
systemctl enable --now control-plane-backup.timer
