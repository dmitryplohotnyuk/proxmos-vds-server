#!/usr/bin/env python3
"""Trust the private server subnet without enabling public remote access."""
import datetime
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

if os.geteuid() != 0:
    raise SystemExit("Run as root inside the Jellyfin container")

path = Path("/etc/jellyfin/network.xml")
backup = path.with_name("network.xml.before-tailscale-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
subprocess.run(["systemctl", "stop", "jellyfin"], check=True)
try:
    shutil.copy2(path, backup)
    tree = ET.parse(path)
    root = tree.getroot()
    subnets = root.find("LocalNetworkSubnets")
    if subnets is None:
        subnets = ET.SubElement(root, "LocalNetworkSubnets")
    if "10.77.0.0/24" not in [node.text for node in subnets]:
        ET.SubElement(subnets, "string").text = "10.77.0.0/24"
    tree.write(path, encoding="utf-8", xml_declaration=True)
finally:
    subprocess.run(["systemctl", "start", "jellyfin"], check=True)
print("Private subnet configured; backup:", backup)
