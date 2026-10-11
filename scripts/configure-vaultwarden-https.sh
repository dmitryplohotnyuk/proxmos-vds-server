#!/bin/bash
set -euo pipefail
[[ $(id -u) == 0 ]] || exit 1
[[ $# == 0 ]] || { echo 'This script derives the URL from this Tailscale node.' >&2; exit 1; }
origin=$(tailscale status --json | python3 -c '
import json,sys
s=json.load(sys.stdin)
assert s.get("BackendState")=="Running", "Tailscale is not connected"
name=s["Self"]["DNSName"].rstrip(".")
assert name.endswith(".ts.net") and "/" not in name
print("https://"+name)
')
python3 - "$origin" <<'PY'
from pathlib import Path
import sys
p=Path('/opt/vaultwarden/vaultwarden.env')
p.write_text('\n'.join('DOMAIN='+sys.argv[1] if x.startswith('DOMAIN=') else x for x in p.read_text().splitlines())+'\n')
p.chmod(0o600)
PY
systemctl restart vaultwarden
tailscale serve --bg --yes http://127.0.0.1:8080
tailscale serve status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); assert not any(s.get("AllowFunnel",{}).values()), "Funnel must be disabled"'
printf 'Private vault URL: %s\n' "$origin"
