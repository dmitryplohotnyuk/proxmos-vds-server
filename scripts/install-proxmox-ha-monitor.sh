#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 && $# -eq 2 ]] || { echo "Usage (root): $0 PROXMOX_LAN_IP HA_IP" >&2; exit 1; }
ROOT=$(cd "$(dirname "$0")/.." && pwd)
python3 - "$1" "$2" <<'PY'
import ipaddress, json, os, pathlib, secrets, sys
bind, ha_ip = (str(ipaddress.IPv4Address(value)) for value in sys.argv[1:])
path = pathlib.Path('/etc/proxmox-ha-monitor.json')
if path.exists():
    config = json.loads(path.read_text())
    if config['bind'] != bind or config['ha_ip'] != ha_ip:
        raise SystemExit('Existing monitor addresses differ; review config explicitly')
else:
    config = {'bind': bind, 'ha_ip': ha_ip, 'token': secrets.token_urlsafe(48)}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream)
PY
install -d /usr/local/libexec
install -m 0755 "$ROOT/scripts/proxmox-ha-monitor.py" /usr/local/libexec/proxmox-ha-monitor.py
install -m 0644 "$ROOT/config/systemd/proxmox-ha-monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now proxmox-ha-monitor.service
systemctl restart proxmox-ha-monitor.service
echo 'Monitor installed; secret remains in root-only /etc/proxmox-ha-monitor.json'
