#!/bin/bash
set -euo pipefail
[[ $(id -u) == 0 ]] || exit 1
umask 077
exec 9>/run/vaultwarden-host-backup.lock
flock -n 9 || { echo 'Host backup already running' >&2; exit 1; }
CT_ID=${VAULT_CT_ID:-108}
[[ $CT_ID =~ ^[0-9]+$ ]] || exit 1
destination=/var/lib/vz/dump/vaultwarden
install -d -m 0700 "$destination"
name=$(pct exec "$CT_ID" -- /usr/local/sbin/vaultwarden-backup)
[[ $name =~ ^vaultwarden-[0-9]{8}T[0-9]{6}Z.tar.age$ ]] || exit 1
trap 'rm -f "$destination/.$name.tmp"' EXIT
pct pull "$CT_ID" "/var/backups/vaultwarden/$name" "$destination/.$name.tmp"
[[ -s "$destination/.$name.tmp" ]] || { echo 'Empty encrypted backup' >&2; exit 1; }
chmod 0600 "$destination/.$name.tmp"
mv "$destination/.$name.tmp" "$destination/$name"
trap - EXIT
find "$destination" -maxdepth 1 -type f -name 'vaultwarden-*.tar.age' -mtime +30 -delete
echo "$destination/$name"
