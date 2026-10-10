#!/usr/bin/env python3
"""Allow trusted subnet gateways without opening TorrServer to the internet."""
import datetime
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

if os.geteuid() != 0:
    raise SystemExit("Run as root inside the TorrServer container")
path = Path("/etc/nftables.conf")
original = path.read_text()
old = "ip saddr 192.168.0.0/24 tcp dport { 22, 8090, 9080 } accept"
new = "ip saddr { 192.168.0.0/24, 10.77.0.2, 10.77.0.3 } tcp dport { 22, 8090, 9080 } accept"
if new in original:
    raise SystemExit("Private gateway rule already configured")
if original.count(old) != 1:
    raise SystemExit("Unexpected firewall configuration; inspect before modifying")
with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as candidate:
    candidate.write(original.replace(old, new))
    temporary = Path(candidate.name)
try:
    subprocess.run(["nft", "--check", "-f", str(temporary)], check=True)
    backup = path.with_name("nftables.conf.before-tailscale-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    shutil.copystat(path, temporary)
    os.chown(temporary, path.stat().st_uid, path.stat().st_gid)
    os.replace(temporary, path)
    subprocess.run(["systemctl", "reload", "nftables"], check=True)
    print("Gateway rule applied; backup:", backup)
finally:
    temporary.unlink(missing_ok=True)
