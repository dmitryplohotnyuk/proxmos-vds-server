#!/bin/bash
set -euo pipefail
[[ $(id -u) == 0 ]] || exit 1
umask 077
exec 9>/run/vaultwarden-backup.lock
flock -n 9 || { echo 'Backup already running' >&2; exit 1; }
recipient=$(cat /etc/vaultwarden/backup-recipient.txt)
[[ $recipient =~ ^age1[a-z0-9]{58}$ ]] || { echo 'Invalid age recipient' >&2; exit 1; }
out=/var/backups/vaultwarden
name="vaultwarden-$(date -u +%Y%m%dT%H%M%SZ).tar.age"
temporary="$out/.$name.tmp"
systemctl is-active --quiet vaultwarden || { echo 'Vaultwarden not running; refusing ambiguous backup' >&2; exit 1; }
cleanup() { rm -f "$temporary"; systemctl start vaultwarden; }
trap cleanup EXIT
# Quiesce SQLite and attachments together; no unencrypted archive is written to disk.
systemctl stop vaultwarden
tar -C /opt/vaultwarden -cf - data vaultwarden.env image.ref | age -r "$recipient" -o "$temporary"
mv "$temporary" "$out/$name"
systemctl start vaultwarden
trap - EXIT
find "$out" -maxdepth 1 -type f -name 'vaultwarden-*.tar.age' -mtime +14 -delete
printf '%s\n' "$name"
