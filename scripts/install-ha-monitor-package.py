#!/usr/bin/env python3
"""Run on Proxmox: add the HA package through QGA without exposing its token."""

import argparse
import base64
import json
from pathlib import Path
import subprocess


GUEST_INSTALL = r'''
import base64,datetime,json,os,pathlib,shutil,yaml
payload=json.loads(base64.b64decode(PAYLOAD))
root=pathlib.Path('/config')
config=root/'configuration.yaml'
text=config.read_text()
document=yaml.compose(text)
def child(mapping,key):
    if not isinstance(mapping,yaml.MappingNode):
        raise ValueError('Expected YAML mapping')
    return next((v for k,v in mapping.value if k.value==key),None)
ha=child(document,'homeassistant')
packages=child(ha,'packages')
if not isinstance(packages,yaml.MappingNode):
    raise ValueError('Expected existing homeassistant.packages mapping; merge manually')
entry=child(packages,'proxmox_monitor')
include='includes/proxmox_monitor/monitor.yaml'
if entry is not None and (entry.tag!='!include' or entry.value!=include):
    raise ValueError('Existing proxmox_monitor package differs')
backup=root/('.proxmox-monitor-backup-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
backup.mkdir(mode=0o700)
shutil.copy2(config,backup/'configuration.yaml')
shutil.copy2(root/'.storage/lovelace.lovelace',backup/'lovelace.lovelace')
resources=root/'.storage/lovelace_resources'
if resources.exists(): shutil.copy2(resources,backup/'lovelace_resources')
www=root/'www'
www.mkdir(exist_ok=True)
card=www/'proxmox-guests-card.js'
if card.exists(): shutil.copy2(card,backup/card.name)
card.write_text(payload['card'])
folder=root/'includes/proxmox_monitor'
folder.mkdir(mode=0o700,exist_ok=True)
for name in ['monitor.yaml','secrets.yaml']:
    if (folder/name).exists(): shutil.copy2(folder/name,backup/name)
(folder/'monitor.yaml').write_text(payload['package'])
secret={'proxmox_monitor_url':'http://'+payload['bind']+':9120/metrics',
        'proxmox_monitor_authorization':'Bearer '+payload['token']}
(folder/'secrets.yaml').write_text(yaml.safe_dump(secret))
os.chmod(folder/'secrets.yaml',0o600)
if entry is None:
    index=packages.start_mark.index
    indent=' '*packages.start_mark.column
    text=text[:index]+'proxmox_monitor: !include '+include+'\n'+indent+text[index:]
    config.write_text(text)
print('HA package installed; backup:',backup)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vmid', type=int, required=True)
    args = parser.parse_args()
    config = json.loads(Path('/etc/proxmox-ha-monitor.json').read_text())
    package = Path(__file__).resolve().parents[1] / 'config/home-assistant/proxmox_monitor.yaml'
    config['package'] = package.read_text()
    config['card'] = (package.parent / 'proxmox-guests-card.js').read_text()
    encoded = base64.b64encode(json.dumps(config).encode()).decode()
    script = GUEST_INSTALL.replace('PAYLOAD', repr(encoded))
    result = subprocess.run(
        ['qm', 'guest', 'exec', str(args.vmid), '--', '/usr/bin/docker', 'exec',
         'homeassistant', 'python3', '-c', script], capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    if data.get('exitcode') != 0:
        # Do not print QGA tracebacks: they can include the embedded secret payload.
        raise SystemExit('HA package install failed; inspect configuration/backup on guest')
    print(data.get('out-data', '').strip())


if __name__ == '__main__':
    main()
