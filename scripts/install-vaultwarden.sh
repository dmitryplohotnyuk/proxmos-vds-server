#!/bin/bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib.sh"
require_root
[[ ! -e /opt/vaultwarden/vaultwarden.env ]] || die "Already installed; use the documented update procedure"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y
apt-get install -y --no-install-recommends docker.io ca-certificates curl python3 sqlite3 age
curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list
apt-get update
apt-get install -y tailscale
install -d -m 0700 /opt/vaultwarden/data /var/backups/vaultwarden /etc/vaultwarden
install -d -m 0755 /etc/systemd/system/tailscaled.service.d
install -m 0644 "$PROJECT_DIR/config/systemd/vaultwarden-tailscale.conf" /etc/systemd/system/tailscaled.service.d/userspace.conf
install -m 0600 "$PROJECT_DIR/config/vaultwarden/vaultwarden.env" /opt/vaultwarden/vaultwarden.env
image=${VAULTWARDEN_IMAGE:-ghcr.io/dani-garcia/vaultwarden:1.37.4}
docker pull "$image"
docker image inspect --format '{{index .RepoDigests 0}}' "$image" >/opt/vaultwarden/image.ref
chmod 0600 /opt/vaultwarden/image.ref
install -m 0755 "$SCRIPT_DIR/vaultwarden-run.sh" /usr/local/sbin/vaultwarden-run
install -m 0755 "$SCRIPT_DIR/vaultwarden-registration.py" /usr/local/sbin/vaultwarden-registration
install -m 0755 "$SCRIPT_DIR/vaultwarden-backup.sh" /usr/local/sbin/vaultwarden-backup
install -m 0644 "$PROJECT_DIR/config/systemd/vaultwarden.service" /etc/systemd/system/
install -m 0644 "$PROJECT_DIR/config/systemd/vaultwarden-registration.service" /etc/systemd/system/
install -m 0644 "$PROJECT_DIR/config/systemd/vaultwarden-registration.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now docker tailscaled vaultwarden
systemctl restart tailscaled
systemctl disable --now ssh.service
systemctl enable --now vaultwarden-registration.timer
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8080/alive >/dev/null; then
        echo "Vaultwarden ready on loopback; registration closed, HTTPS not configured yet."
        exit 0
    fi
    sleep 2
done
die "Vaultwarden did not become healthy; inspect its journal"
