#!/bin/bash
set -euo pipefail
[[ $# == 2 ]] || { echo 'Usage: pull-vaultwarden-backups.sh PROXMOX_SSH_TARGET LOCAL_DIRECTORY' >&2; exit 1; }
target=$1
directory=$2
[[ $target != -* && $directory != -* ]] || exit 1
umask 077
mkdir -p "$directory"
chmod 0700 "$directory"
scp "$target:/var/lib/vz/dump/vaultwarden/vaultwarden-*.tar.age" "$directory/"
echo 'Encrypted copies downloaded. Keep the age identity separately; no private key is sent to Proxmox.'
