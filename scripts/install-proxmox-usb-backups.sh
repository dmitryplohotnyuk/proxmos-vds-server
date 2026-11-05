#!/bin/bash
set -euo pipefail
[[ $(id -u) == 0 && $# == 1 ]] || { echo 'Usage: install-proxmox-usb-backups.sh SOURCE_DIRECTORY' >&2; exit 1; }
source=$(realpath "$1")
[[ -f "$source/src/proxmox_usb_backup/engine.py" && -f "$source/docs/21-usb-disaster-recovery.md" ]] || exit 1
mount=/mnt/pve/usb-backups
uuid=$(findmnt --mountpoint "$mount" --noheadings --output UUID)
[[ $uuid == ca744010-53c5-4cde-846a-d66c60f4cf02 ]] || { echo 'Expected backup filesystem is not mounted' >&2; exit 1; }
for tool in python3 vzdump qmrestore pct vma zstd pvesh tailscale; do command -v "$tool" >/dev/null; done
state=$(systemctl show proxmox-usb-backup.service -p ActiveState --value 2>/dev/null || true)
if [[ $state == active || $state == activating || $state == deactivating ]]; then
 echo 'A backup is running; wait before updating its code' >&2
 exit 1
fi
getent passwd pve-backup-ui >/dev/null || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pve-backup-ui
install -d -m 0755 /opt/proxmox-usb-backup /etc/proxmox-usb-backup /var/lib/proxmox-usb-backup
tar --exclude=__pycache__ --exclude='*.pyc' -C "$source/src" -cf - proxmox_usb_backup | tar -C /opt/proxmox-usb-backup -xf -
install -m 0644 "$source/docs/21-usb-disaster-recovery.md" /opt/proxmox-usb-backup/proxmox_usb_backup/static/recovery-guide.md
for file in "$source"/config/systemd/proxmox-usb-backup*.service "$source"/config/systemd/proxmox-usb-backup*.timer; do
 install -m 0644 "$file" /etc/systemd/system/
done
export BACKUP_UUID="$uuid"
python3 - <<'PY'
import json,os,subprocess
from pathlib import Path
config=Path('/etc/proxmox-usb-backup/config.json')
if config.exists():
 data=json.loads(config.read_text())
 if data['uuid']!=os.environ['BACKUP_UUID']: raise SystemExit('Existing backup UUID differs')
else:
 data=dict(version=1,mount='/mnt/pve/usb-backups',uuid=os.environ['BACKUP_UUID'],reserve_bytes=8*1024**3,
           bwlimit_kib=131072,auto_retry=True,allowed_exclusions={
           '/mnt/media-ssd':'Медиафайлы исключены владельцем; настройки и база Jellyfin включены',
           '/mnt/immich-photos':'Фототека на этом USB требует отдельной внешней резервной копии'})
 config.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
 config.chmod(0o600)
ui=Path('/etc/proxmox-usb-backup/ui.json')
if not ui.exists():
 status=json.loads(subprocess.check_output(['tailscale','status','--json']))
 owner=status['User'][str(status['Self']['UserID'])]['LoginName']
 ui.write_text(json.dumps(dict(allowed_logins=[owner],origin='https://proxmox.tail6ace7f.ts.net:8443'),indent=2)+'\n')
 ui.chmod(0o644)
PY
kit=/opt/proxmox-usb-backup/recovery
install -d -m 0700 "$kit" "$kit/proxmox_usb_backup"
for file in __init__.py common.py recovery.py; do
 install -m 0600 "$source/src/proxmox_usb_backup/$file" "$kit/proxmox_usb_backup/$file"
done
install -m 0600 "$source/docs/21-usb-disaster-recovery.md" "$kit/README.ru.md"
if [[ -d "$source/.git" || -f "$source/.git" ]]; then
 bash "$source/scripts/package-source.sh" "$kit/project-source.tar.gz"
else
 tar --exclude=.git --exclude-vcs-ignores --exclude=.venv --exclude=__pycache__ \
     --exclude='*.pyc' --exclude=.env --exclude=secrets --exclude=integration-backups \
     --exclude=service-inventory-backups -czf "$kit/project-source.tar.gz" -C "$source" .
fi
# This public-to-root recovery bootstrap contains code/docs only, no runtime secrets.
install -d -m 0700 "$mount/recovery"
cp -a "$kit/." "$mount/recovery/"
systemctl daemon-reload
systemd-analyze --generators=yes verify /etc/systemd/system/proxmox-usb-backup.service /etc/systemd/system/proxmox-usb-backup-ui.service
systemctl enable --now proxmox-usb-backup-control.service proxmox-usb-backup-ui.service
systemctl enable --now proxmox-usb-backup.timer proxmox-usb-backup-health.timer
python3 - <<'PY'
import json,subprocess
from pathlib import Path
before=json.loads(subprocess.check_output(['tailscale','serve','status','--json']))
Path('/root/tailscale-serve-before-backup-ui.json').write_text(json.dumps(before,indent=2))
subprocess.run(['tailscale','serve','--bg','--https=8443','http://127.0.0.1:9130'],check=True)
after=json.loads(subprocess.check_output(['tailscale','serve','status','--json']))
for key,value in before.get('Web',{}).items():
 if key.endswith(':443') and after.get('Web',{}).get(key)!=value: raise SystemExit('Existing Proxmox HTTPS route changed unexpectedly')
PY
systemctl start proxmox-usb-backup-health.service
echo 'Dashboard: https://proxmox.tail6ace7f.ts.net:8443'
echo 'First backup starts automatically when a valid USB disk is present.'
