"""Portable restore CLI; run from the USB kit on a clean Proxmox host."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from .common import command, load_set, safe_path

HOST_RESTORE = ('usr/local', 'opt/service-inventory', 'opt/proxmox-usb-backup',
                'etc/sysctl.d', 'etc/default/proxmox-nic-watchdog', 'etc/systemd/system',
                'etc/sudoers.d/service-inventory', 'etc/proxmox-ha-monitor.json',
                'etc/ha-nightly-updates.json', 'etc/control-plane-backup',
                'etc/vaultwarden-host-backup', 'etc/proxmox-usb-backup',
                'var/lib/service-inventory', 'var/lib/service-inventory-ssh',
                'var/lib/vz/snippets')


def list_sets(root):
    result = []
    for path in (Path(root) / 'sets').glob('set-*'):
        try:
            manifest = load_set(path)
            result.append((manifest['completed_at'], path, manifest))
        except (OSError, ValueError, KeyError):
            pass
    return sorted(result, reverse=True)


def select_set(root, identifier=None):
    sets = list_sets(root)
    if identifier:
        sets = [item for item in sets if item[1].name == identifier]
    if not sets:
        raise ValueError('No complete, intact recovery set found')
    return sets[0][1], sets[0][2]


def id_mapping(guests, mappings):
    result = {g['id']: g['id'] for g in guests}
    for value in mappings:
        old, new = map(int, value.split('=', 1))
        if old not in result or not 100 <= new <= 999999999:
            raise ValueError('Invalid guest ID mapping')
        result[old] = new
    if len(set(result.values())) != len(result):
        raise ValueError('Guest ID mapping has duplicate targets')
    return result


def strip_hardware(kind, identifier, drill=False):
    tool = 'qm' if kind == 'qemu' else 'pct'
    config = command(tool, 'config', str(identifier)).split('\n[', 1)[0]
    removed = []
    for line in config.splitlines():
        key, _, value = line.partition(': ')
        if (re.fullmatch(r'(usb|hostpci)\d+', key) or key in ('hookscript', 'protection')
                or (kind == 'lxc' and re.fullmatch(r'mp\d+', key) and value.startswith('/'))):
            if key == 'protection':
                if drill:
                    command(tool, 'set', str(identifier), '--protection', '0')
                continue
            command(tool, 'set', str(identifier), '--delete', key)
            removed.append(dict(key=key, value=value))
        if drill and re.fullmatch(r'net\d+', key):
            value = re.sub(r',?link_down=[01]', '', value)
            command(tool, 'set', str(identifier), '--' + key, value + ',link_down=1')
    command(tool, 'set', str(identifier), '--onboot', '0')
    return removed


def restore_guests(directory, manifest, storage, selected, mappings, drill=False):
    guests = [g for g in manifest['guests'] if not selected or g['id'] in selected]
    if selected and set(selected) != {g['id'] for g in guests}:
        raise ValueError('Requested guest is not in this recovery set')
    mapping = id_mapping(guests, mappings)
    for target in mapping.values():
        if any((Path('/etc/pve') / folder / f'{target}.conf').exists()
               for folder in ('lxc', 'qemu-server')):
            raise ValueError(f'Guest ID {target} already exists; no overwrite is permitted')
    if drill and any(old == new for old, new in mapping.items()):
        raise ValueError('A restore drill requires different, unused target IDs')
    restored = []
    for guest in guests:
        target = mapping[guest['id']]
        archive = safe_path(directory, guest['archive'])
        if guest['kind'] == 'qemu':
            command('qmrestore', str(archive), str(target), '--storage', storage,
                    '--start', '0', '--unique', '1' if drill else '0', timeout=14400)
        else:
            command('pct', 'restore', str(target), str(archive), '--storage', storage,
                    '--onboot', '0', timeout=14400)
        removed = strip_hardware(guest['kind'], target, drill)
        restored.append(dict(source_id=guest['id'], id=target, kind=guest['kind'],
                             name=guest['name'], removed_bindings=removed))
        print(json.dumps(restored[-1], ensure_ascii=False), flush=True)
    return restored


def stage_host(directory, destination):
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(directory / 'host-config.tar.gz', 'r:gz') as archive:
        archive.extractall(destination, filter='data')
    return destination


def copy_config(source, destination):
    if source.is_symlink():
        return
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_config(child, destination / child.name)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def apply_host(staged, manifest, network_map, restore_identities=False):
    source = Path(staged) / 'host'
    if not (source / 'etc/pve/storage.cfg').is_file():
        raise ValueError('Not a staged host configuration snapshot')
    for account in manifest.get('local_users', []):
        result = subprocess.run(['getent', 'passwd', account['name']], capture_output=True)
        if result.returncode:
            command('useradd', '--system', '--home-dir', account['home'],
                    '--shell', account['shell'], account['name'])
    # Never overwrite cluster identity, management networking, LVM/fstab, or PVE credentials.
    for relative in HOST_RESTORE:
        path = source / relative
        if path.exists():
            copy_config(path, Path('/') / relative)
    if (source / 'root/.ssh').exists():
        target = Path('/root/.ssh')
        target.mkdir(mode=0o700, exist_ok=True)
        for path in (source / 'root/.ssh').iterdir():
            if path.name == 'authorized_keys':
                current = (target / path.name).read_text() if (target / path.name).exists() else ''
                merged = '\n'.join(dict.fromkeys((current + '\n' + path.read_text()).splitlines())) + '\n'
                (target / path.name).write_text(merged)
                (target / path.name).chmod(0o600)
            else:
                copy_config(path, target / path.name)
    command('chown', '-R', 'service-inventory:service-inventory', '/var/lib/service-inventory-ssh')
    candidate = Path('/root/proxmox-network-candidate')
    candidate.mkdir(mode=0o700, exist_ok=True)
    network = source / 'etc/network'
    copy_config(network / 'interfaces', candidate / 'interfaces')
    if (network / 'interfaces.d').is_dir():
        copy_config(network / 'interfaces.d', candidate / 'interfaces.d')
    for value in network_map:
        old, new = value.split('=', 1)
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', old + '') or not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', new):
            raise ValueError('Invalid interface mapping')
        if not (Path('/sys/class/net') / new).exists():
            raise ValueError(f'New interface does not exist: {new}')
        for file in candidate.rglob('*'):
            if file.is_file():
                file.write_text(re.sub(r'(?<![A-Za-z0-9_.-])' + re.escape(old) + r'(?![A-Za-z0-9_.-])', new, file.read_text()))
    if restore_identities:
        command('systemctl', 'stop', 'tailscaled')
        copy_config(source / 'var/lib/tailscale', Path('/var/lib/tailscale'))
        for key in (source / 'etc/ssh').glob('ssh_host_*'):
            copy_config(key, Path('/etc/ssh') / key.name)
    command('systemctl', 'daemon-reload')
    print('Custom host files restored. No network/service activation performed.')
    print(f'Review networking in {candidate}; merge fstab/storage/PVE credentials from {source}/etc.')
    print('Do NOT copy reference/pmxcfs-config.db into a running cluster.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['list', 'verify', 'stage-host', 'apply-host', 'guests'])
    parser.add_argument('--root', type=Path, default=Path('/mnt/recovery'))
    parser.add_argument('--set')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--storage', default='local-lvm')
    parser.add_argument('--ids', type=int, nargs='+')
    parser.add_argument('--map-id', action='append', default=[])
    parser.add_argument('--network-map', action='append', default=[])
    parser.add_argument('--old-server-off', action='store_true')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--drill', action='store_true')
    parser.add_argument('--restore-identities', action='store_true')
    args = parser.parse_args()
    if args.action == 'list':
        for _, path, manifest in list_sets(args.root):
            print(path.name, manifest['completed_at'], len(manifest['guests']), manifest['total_bytes'])
        return
    directory, manifest = select_set(args.root, args.set)
    load_set(directory, verify_files=True)
    if args.action == 'verify':
        print('All files and manifest SHA-256 verified:', directory.name)
        return
    if os.geteuid() != 0:
        parser.error('Run recovery actions as root')
    if args.action == 'stage-host':
        destination = args.destination or Path('/root') / ('pve-recovery-' + directory.name)
        print(stage_host(directory, destination))
        return
    if not args.confirm or (not args.drill and not args.old_server_off):
        parser.error('Destructive restore requires --confirm --old-server-off (or isolated --drill)')
    def release(value):
        found = re.search(r'pve-manager/(\d+)\.(\d+)\.(\d+)', value)
        if not found:
            raise ValueError('Cannot compare Proxmox versions')
        return tuple(map(int, found.groups()))
    if release(command('pveversion')) < release(manifest['pve_version']):
        parser.error('Target Proxmox must not be older than the backup source')
    if args.action == 'apply-host':
        if args.drill or not args.destination:
            parser.error('apply-host requires --destination and is forbidden in drill mode')
        if any(Path('/etc/pve/lxc').glob('*.conf')) or any(Path('/etc/pve/qemu-server').glob('*.conf')):
            parser.error('apply-host is restricted to a clean server without guests')
        apply_host(args.destination, manifest, args.network_map, args.restore_identities)
    elif args.action == 'guests':
        restore_guests(directory, manifest, args.storage, args.ids, args.map_id, args.drill)


if __name__ == '__main__':
    main()
