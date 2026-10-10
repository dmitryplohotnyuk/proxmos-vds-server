#!/bin/bash
set -euo pipefail

# Run on a Tailscale client, not inside Proxmox or the gateway.
command -v tailscale >/dev/null
command -v curl >/dev/null
command -v dig >/dev/null
command -v python3 >/dev/null
tailscale status >/dev/null

check_http() {
    local name=$1 url=$2 expected=$3 actual
    actual=$(curl --noproxy '*' --connect-timeout 5 --max-time 15 -sS -o /dev/null -w '%{http_code}' "$url")
    [[ ",$expected," == *",$actual,"* ]] || { printf '%s: unexpected HTTP %s\n' "$name" "$actual"; return 1; }
    printf '%s: HTTP %s\n' "$name" "$actual"
}

check_http 'Jellyfin private API' http://10.77.0.14:8096/System/Info/Public 200
check_http 'Jellyfin private web' http://10.77.0.14:8096/web/index.html 200
if [ "${CHECK_LAN:-0}" = 1 ]; then
    check_http 'Jellyfin original LAN web' http://192.168.0.156:8096/web/index.html 200
fi
check_http 'TorrServer private' http://10.77.0.15:8090/ 200,401
check_http 'AdGuard private' http://10.77.0.53:3000/ 200,302
check_http 'Home Assistant' http://192.168.0.222:8123/ 200,302
check_http 'Proxmox private UI' https://proxmox.tail6ace7f.ts.net/ 200
check_http 'Domain router private UI' https://domain-router.tail6ace7f.ts.net/ 200,302,303

answer=$(dig @10.77.0.53 example.com A +short +time=3 +tries=1)
[ -n "$answer" ] || { printf 'AdGuard private DNS failed\n'; exit 1; }
[ "$(dig @10.77.0.53 doubleclick.net A +short +time=3 +tries=1 | head -n 1)" = 0.0.0.0 ]
printf 'AdGuard private DNS and blocking: OK\n'

# Jellyfin's SMB port must NOT be exposed through the subnet gateway.
python3 - <<'PORT_CHECK'
import socket
try:
    connection = socket.create_connection(('10.77.0.14', 445), timeout=3)
except OSError:
    pass
else:
    connection.close()
    raise SystemExit('ERROR: unexpected SMB access through gateway')
PORT_CHECK
printf 'Restricted port check: OK\n'
