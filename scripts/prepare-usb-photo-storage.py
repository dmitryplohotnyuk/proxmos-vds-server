#!/usr/bin/env python3
"""Initialize an explicitly confirmed empty USB disk, then configure optional mounts."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

BACKUPS = Path('/mnt/pve/usb-backups')
PHOTOS = Path('/mnt/immich-photos')
LABELS = ('pve_usb_backup', 'immich_photos')
STORAGE = 'usb-backups'
BEGIN = '# BEGIN domain-router optional USB storage'
END = '# END domain-router optional USB storage'
OPTIONS = ('defaults,nofail,noatime,nodev,nosuid,errors=remount-ro,'
           'x-systemd.device-timeout=5s,x-systemd.mount-timeout=15s,'
           'x-systemd.device-bound=yes')


def run(*args, input=None):
    try:
        result = subprocess.run(args, input=input, text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f'{args[0]} failed: {error.stderr.strip()}') from error
    return result.stdout.strip()


def read_device(device):
    # PATH without --tree otherwise yields a flat list and hides mounted children.
    entries = json.loads(run('lsblk', '--json', '--tree', '--bytes', '--output',
                             'PATH,TYPE,SIZE,TRAN,MOUNTPOINTS,RO', str(device)))['blockdevices']
    if len(entries) != 1 or entries[0].get('path') != str(device):
        raise ValueError('Expected one complete block-device tree')
    return entries[0]


def partition_plan(total_bytes, backup_gb):
    mib = 1024 ** 2
    backup_bytes = backup_gb * 10 ** 9 // mib * mib
    if backup_gb < 10 or total_bytes - backup_bytes < 10 * 10 ** 9:
        raise ValueError('Both partitions must have at least 10 GB')
    return {'start': mib // 512, 'size': backup_bytes // 512,
            'photos_start': (mib + backup_bytes) // 512}


def validate_device(info, serial, expected_serial, expected_size, erase_serial=None):
    if info['type'] != 'disk' or info.get('tran') != 'usb' or info.get('ro'):
        raise ValueError('Target must be a writable whole USB disk')
    if serial != expected_serial or int(info['size']) != expected_size:
        raise ValueError('Disk serial or exact byte capacity does not match')
    nodes = [info, *info.get('children', [])]
    if any(any(n.get('mountpoints') or []) for n in nodes):
        raise ValueError('Target or a partition is mounted; unmount explicitly first')
    if erase_serial is not None:
        if erase_serial != expected_serial:
            raise ValueError('Erase confirmation must match the disk serial')
        if info.get('children'):
            raise ValueError('Initialization refuses existing partitions; inspect manually')


def render_fstab(original, backup_uuid, photo_uuid):
    for value in (backup_uuid, photo_uuid):
        uuid.UUID(value)
    if backup_uuid == photo_uuid:
        raise ValueError('Backup and photo filesystems must have different UUIDs')
    block = (f'{BEGIN}\n'
             f'UUID={backup_uuid} {BACKUPS} ext4 {OPTIONS} 0 2\n'
             f'UUID={photo_uuid} {PHOTOS} ext4 {OPTIONS} 0 2\n{END}\n')
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise ValueError('Malformed managed fstab block')
    if BEGIN in original:
        start = original.index(BEGIN)
        end = original.index(END, start) + len(END)
        if original[end:end + 1] == '\n':
            end += 1
        return original[:start] + block + original[end:]
    return original.rstrip('\n') + '\n\n' + block


def atomic_write(path, text):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def guard_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.is_mount() or any(path.iterdir()):
        raise ValueError(f'Mount directory is not an empty unmounted directory: {path}')
    if path.stat().st_mode & 0o777:
        path.chmod(0o000)
    # Immutable underlying directories reject writes even from root when USB is absent.
    run('chattr', '+i', str(path))


def register_backup_storage(existing):
    command = ['pvesm', 'set', STORAGE] if existing else [
        'pvesm', 'add', 'dir', STORAGE, '--path', str(BACKUPS)]
    # Existing directory paths are immutable in the API and checked in preflight.
    run(*command, '--content', 'backup', '--is_mountpoint', '1',
        '--create-base-path', '0', '--create-subdirs', '0')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', required=True, type=Path)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--expected-size-bytes', required=True, type=int)
    parser.add_argument('--backup-gb', type=int, default=400, help='Decimal GB, not GiB')
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--confirm-erase-serial')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run on Proxmox as root')
    if args.device.parent != Path('/dev/disk/by-id') or not args.device.exists():
        parser.error('Use an existing whole-disk /dev/disk/by-id path')
    if args.initialize != bool(args.confirm_erase_serial):
        parser.error('--initialize requires --confirm-erase-serial; omit both to configure only')
    for command in ('lsblk', 'udevadm', 'sfdisk', 'mkfs.ext4', 'blkid', 'chattr', 'wipefs',
                    'findmnt', 'systemctl', 'systemd-escape', 'pvesh', 'pvesm'):
        if shutil.which(command) is None:
            raise RuntimeError(f'Missing command: {command}')

    device = args.device.resolve()
    info = read_device(device)
    properties = dict(line.split('=', 1) for line in run(
        'udevadm', 'info', '--query=property', f'--name={device}').splitlines() if '=' in line)
    validate_device(info, properties.get('ID_SERIAL_SHORT'), args.serial,
                    args.expected_size_bytes, args.confirm_erase_serial)
    if any((Path('/sys/class/block') / device.name / 'holders').iterdir()):
        raise ValueError('Target disk has active holders')
    plan = partition_plan(int(info['size']), args.backup_gb)

    fstab = Path('/etc/fstab')
    original = fstab.read_text()
    managed = BEGIN in original
    entries = json.loads(run('findmnt', '--fstab', '--json', '--output', 'TARGET'))
    for path in (BACKUPS, PHOTOS):
        count = sum(e['target'] == str(path) for e in entries['filesystems'])
        if count > 1 or (count and not managed):
            raise ValueError('An unmanaged fstab entry already uses a target path')
    storages = json.loads(run('pvesh', 'get', '/storage', '--output-format', 'json'))
    for storage in storages:
        if storage['storage'] == STORAGE or storage.get('path') == str(BACKUPS):
            if (storage['storage'] != STORAGE or storage.get('type') != 'dir'
                    or storage.get('path') != str(BACKUPS)):
                raise ValueError('Storage ID or path conflicts with existing storage')
    for path in (BACKUPS, PHOTOS):
        if path.is_symlink() or path.is_mount() or (path.exists() and any(path.iterdir())):
            raise ValueError(f'Target directory is already in use: {path}')

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path('/root') / f'usb-storage-before-{stamp}'
    backup.mkdir(mode=0o700)
    shutil.copy2(fstab, backup / 'fstab')
    shutil.copy2('/etc/pve/storage.cfg', backup / 'storage.cfg')
    print(f'Configuration backup: {backup}', flush=True)

    if args.initialize:
        signatures = json.loads(run('wipefs', '--no-act', '--json', str(device)))
        if any(s['type'] not in ('gpt', 'PMBR') for s in signatures.get('signatures', [])):
            raise ValueError('Unexpected existing data signature; inspect before erasing')
        layout = (f'label: gpt\nunit: sectors\n\n'
                  f'start={plan["start"]}, size={plan["size"]}, type=linux, name="PVE Backups"\n'
                  f'start={plan["photos_start"]}, type=linux, name="Immich Photos"\n')
        print(f'Initializing confirmed serial {args.serial}: {args.backup_gb} decimal GB + remainder', flush=True)
        run('sfdisk', '--lock', '--wipe', 'always', '--wipe-partitions', 'always',
            str(args.device), input=layout)
        run('udevadm', 'settle', '--timeout=20')

    partitions = [Path(str(args.device) + f'-part{i}') for i in (1, 2)]
    for part in partitions:
        for _ in range(20):
            if part.exists():
                break
            time.sleep(0.5)
        if not part.exists():
            raise ValueError(f'Missing partition: {part}')
    if args.initialize:
        for part, label in zip(partitions, LABELS):
            print(f'Creating ext4: {label}', flush=True)
            run('mkfs.ext4', '-q', '-m', '0', '-L', label, str(part))
    else:
        children = info.get('children', [])
        if len(children) != 2 or int(children[0]['size']) != plan['size'] * 512:
            raise ValueError('Existing layout does not match requested two partitions')
    uuids = []
    for part, label in zip(partitions, LABELS):
        if run('blkid', '-s', 'TYPE', '-o', 'value', str(part)) != 'ext4':
            raise ValueError('Expected ext4 filesystem')
        if run('blkid', '-s', 'LABEL', '-o', 'value', str(part)) != label:
            raise ValueError('Unexpected filesystem label')
        uuids.append(run('blkid', '-s', 'UUID', '-o', 'value', str(part)))
    for path in (BACKUPS, PHOTOS):
        guard_directory(path)
    atomic_write(fstab, render_fstab(original, *uuids))
    run('findmnt', '--verify')
    run('systemctl', 'daemon-reload')
    for path, expected_uuid in zip((BACKUPS, PHOTOS), uuids):
        unit = run('systemd-escape', '--path', '--suffix=mount', str(path))
        run('systemctl', 'start', unit)
        if run('findmnt', '--mountpoint', str(path), '--noheadings', '--output', 'UUID') != expected_uuid:
            raise ValueError(f'Wrong filesystem mounted at {path}')
        path.chmod(0o750)
    for child in ('dump', 'host-configs'):
        (BACKUPS / child).mkdir(mode=0o700, exist_ok=True)
    existing = any(s['storage'] == STORAGE for s in storages)
    register_backup_storage(existing)
    print(json.dumps({'backup_path': str(BACKUPS), 'photo_path': str(PHOTOS),
                      'backup_uuid': uuids[0], 'photo_uuid': uuids[1],
                      'backup_partition_bytes': plan['size'] * 512,
                      'scheduled_backups': False}, indent=2), flush=True)


if __name__ == '__main__':
    main()
