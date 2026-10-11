#!/usr/bin/env python3
"""Encrypt a bounded configuration backup; run on Proxmox as root."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone, timedelta


GUEST_COLLECTOR = r'''
import fcntl, os, sqlite3, sys, tarfile, tempfile
from contextlib import ExitStack
from pathlib import Path
role = sys.argv[1]
paths = (['etc/domain-router', 'etc/cloudflared', 'etc/caddy',
          'etc/domain-router-integrations', 'var/lib/domain-router-integrations',
          'etc/network/interfaces', 'etc/nftables.conf', 'etc/sysctl.d/99-domain-router.conf',
          'etc/systemd/system/domain-router-ui.service',
          'etc/systemd/system/domain-router-ui.service.d',
          'etc/systemd/system/cloudflared.service', 'etc/systemd/system/cloudflared.service.d',
          'var/lib/domain-router-ui/service-inventory_ed25519',
          'var/lib/domain-router-ui/service-inventory_ed25519.pub',
          'var/lib/domain-router-ui/service-inventory_known_hosts'] if role == 'router' else
         ['etc/tailscale-gateway.nft', 'etc/sysctl.d/99-tailscale-gateway.conf',
          'etc/network/interfaces', 'etc/systemd/system/tailscale-gateway-firewall.service',
          'etc/domain-router-integrations'])
required = ['etc/domain-router/routes.yml', 'etc/caddy/Caddyfile'] if role == 'router' else ['etc/tailscale-gateway.nft']
for name in required:
    if not Path('/' + name).is_file():
        raise RuntimeError('Required configuration is missing: ' + name)
os.umask(0o077)
with ExitStack() as locks, tempfile.TemporaryDirectory(prefix='control-plane-', dir='/run') as temporary:
    lock_paths = ['/var/lib/domain-router-integrations/lock']
    if role == 'router':
        lock_paths.insert(0, '/run/lock/domain-router.lock')
    for lock_path in lock_paths:
        lock = locks.enter_context(open(lock_path, 'a'))
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    if Path('/var/lib/domain-router-integrations/pending.json').exists():
        raise RuntimeError('Connection change is pending; retry backup later')
    snapshot = Path(temporary) / 'app.db'
    if role == 'router':
        source = sqlite3.connect('file:/var/lib/domain-router-ui/app.db?mode=ro', uri=True)
        destination = sqlite3.connect(snapshot)
        source.backup(destination)
        # Restoring a backup must not resurrect old browser sessions.
        destination.execute('DELETE FROM sessions')
        destination.commit()
        if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite backup integrity failure')
        source.close()
        destination.close()
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        for name in paths:
            if Path('/' + name).exists():
                archive.add('/' + name, arcname=name)
        if role == 'router':
            archive.add(snapshot, arcname='var/lib/domain-router-ui/app.db')
'''

HOST_PATHS = (
    'etc/network/interfaces', 'etc/network/interfaces.d', 'etc/pve/lxc',
    'etc/pve/qemu-server', 'etc/pve/storage.cfg', 'etc/default/proxmox-nic-watchdog',
    'var/lib/service-inventory/inventory.json',
    'var/lib/service-inventory-ssh/.ssh/authorized_keys',
)


def collect_guest(identifier, role, destination):
    destination.mkdir(parents=True, mode=0o700)
    process = subprocess.Popen(['pct', 'exec', str(identifier), '--', 'python3', '-c',
                                GUEST_COLLECTOR, role], stdout=subprocess.PIPE)
    try:
        # Only a fixed root-owned collector produces this stream; reject unsafe paths/links anyway.
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            archive.extractall(destination, filter='data')
        if process.wait(timeout=30):
            raise RuntimeError('Guest configuration collection failed')
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        process.stdout.close()


def prune(directory, now):
    for path in directory.glob('control-plane-*.tar.age'):
        if (re.fullmatch(r'control-plane-\d{8}T\d{6}Z\.tar\.age', path.name)
                and path.is_file() and not path.is_symlink()
                and path.stat().st_mtime < (now - timedelta(days=30)).timestamp()):
            path.unlink()


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run on Proxmox as root')
    os.umask(0o077)
    recipient = Path('/etc/control-plane-backup/recipient.txt').read_text().strip()
    if not re.fullmatch(r'age1[a-z0-9]{58}', recipient):
        raise SystemExit('A public native age recipient is required')
    with open('/run/control-plane-backup.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = datetime.now(timezone.utc)
        output = Path('/var/lib/vz/dump/control-plane')
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = 'control-plane-' + now.strftime('%Y%m%dT%H%M%SZ') + '.tar.age'
        temporary = output / ('.' + name + '.tmp')
        try:
            # /run is tmpfs: sensitive staging never enters a plain archive on persistent disk.
            with tempfile.TemporaryDirectory(prefix='control-plane-', dir='/run') as staging:
                root = Path(staging)
                collect_guest(100, 'router', root / 'ct100')
                collect_guest(107, 'gateway', root / 'ct107')
                for name_on_host in HOST_PATHS:
                    source = Path('/') / name_on_host
                    if not source.exists():
                        continue
                    destination = root / 'host' / name_on_host
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if source.is_dir():
                        shutil.copytree(source, destination, symlinks=True)
                    else:
                        shutil.copy2(source, destination)
                (root / 'manifest.json').write_text(json.dumps(dict(
                    version=1, created_at=now.isoformat(), guests=[100, 107],
                    excludes=['guest disks', 'application data', 'Tailscale node identities',
                              'host SSH private keys', 'Vaultwarden data']), indent=2))
                encryptor = subprocess.Popen(['age', '-r', recipient, '-o', str(temporary)],
                                              stdin=subprocess.PIPE)
                try:
                    with tarfile.open(fileobj=encryptor.stdin, mode='w|gz') as archive:
                        for entry in sorted(root.iterdir()):
                            archive.add(entry, arcname=entry.name)
                    encryptor.stdin.close()
                    if encryptor.wait(timeout=60):
                        raise RuntimeError('Encryption failed')
                except BaseException:
                    encryptor.kill()
                    encryptor.wait()
                    raise
            if temporary.stat().st_size == 0:
                raise RuntimeError('Empty encrypted archive')
            temporary.chmod(0o600)
            temporary.replace(output / name)
            prune(output, now)
            print(output / name)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
