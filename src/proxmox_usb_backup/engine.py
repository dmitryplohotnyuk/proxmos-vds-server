"""Weekly transactional recovery sets. No previous set is pruned before verification."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import posixpath
import pwd
import re
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import uuid

from .common import (ARCHIVE, CONFIG, GENERATION, STATE, atomic_json, command, digest,
                     disk_status, exclusions, guest_inventory, load_set, now, read_json,
                     require_disk, safe_path, sync_directory)

HOST_PATHS = ('etc', 'usr/local', 'opt/service-inventory', 'opt/proxmox-usb-backup',
              'root', 'var/lib/service-inventory', 'var/lib/service-inventory-ssh',
              'var/lib/tailscale', 'var/lib/vz/snippets')


@contextmanager
def job_lock(path=Path('/run/lock/proxmox-usb-backup.lock')):
    with open(path, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def identity(guests):
    return [(g['id'], g['kind'], g['config_sha256']) for g in guests]


def estimate_bytes(guests):
    data = json.loads(command('lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix',
                              '-o', 'lv_name,lv_size,data_percent'))
    sizes = {}
    for row in data['report'][0]['lv']:
        match = re.fullmatch(r'vm-(\d+)-(?:disk-\d+|cloudinit)', row['lv_name'])
        if match and row['data_percent']:
            identifier = int(match[1])
            sizes[identifier] = sizes.get(identifier, 0) + int(
                float(row['lv_size']) * float(row['data_percent']) / 100)
    total = 512 * 1024**2
    for guest in guests:
        if guest['id'] in sizes:
            total += sizes[guest['id']]
        else:
            matches = re.findall(r'\bsize=([\d.]+)([KMGT])', ' '.join(guest['config'].values()))
            if not matches:
                raise RuntimeError(f'Cannot estimate disks of guest {guest["id"]}')
            total += sum(float(n) * 1024**('KMGT'.index(unit) + 1) for n, unit in matches)
    return int(total * 1.15)


def check_space(config, needed):
    disk = require_disk(config)
    if disk['free_bytes'] < needed + config['reserve_bytes']:
        raise RuntimeError('Not enough USB space for a new set plus reserve; old backups retained')
    return disk


class Runner:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['mount'])
        self.status = read_json(STATE / 'status.json', {})
        self.process = None
        self.log = None
        self.last_update = 0

    def update(self, **values):
        self.status.update(values, updated_at=now())
        atomic_json(STATE / 'status.json', self.status, 0o644)

    def long(self, args, *, progress=False):
        last_error = ''
        self.process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True,
                                        umask=0o022)
        try:
            for line in self.process.stdout:
                self.log.write(line)
                self.log.flush()
                if 'ERROR:' in line or 'Permission denied' in line:
                    last_error = line.strip()[-600:]
                if time.monotonic() - self.last_update >= 3:
                    check_space(self.config, 0)
                    values = {'message': line.strip()[-400:]}
                    match = re.search(r'\b(\d{1,3})%\b|\b(\d{1,3})% ', line)
                    if progress and match:
                        values['guest_percent'] = min(100, int(match[1] or match[2]))
                    self.update(**values)
                    self.last_update = time.monotonic()
            if self.process.wait():
                raise RuntimeError(last_error or f'{args[0]} failed; see the root-only job log')
        finally:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
            self.process.stdout.close()
            self.process = None

    def verify_archive(self, path, kind):
        check_space(self.config, 0)
        self.long(['zstd', '--test', '--quiet', str(path)])
        decoder = subprocess.Popen(['zstd', '-dc', str(path)], stdout=subprocess.PIPE,
                                    stderr=self.log, start_new_session=True)
        try:
            if kind == 'qemu':
                verifier = subprocess.run(['vma', 'verify', '-'], stdin=decoder.stdout,
                                          stdout=self.log, stderr=self.log)
                if verifier.returncode:
                    raise RuntimeError('VMA structural/data verification failed')
            else:
                with tarfile.open(fileobj=decoder.stdout, mode='r|') as archive:
                    for member in archive:
                        name = member.name.lstrip('./')
                        if member.name.startswith('/') or '..' in Path(member.name).parts:
                            raise ValueError('Unsafe tar member path')
                        if member.isfile():
                            source = archive.extractfile(member)
                            for _ in iter(lambda: source.read(4 * 1024**2), b''):
                                pass
            decoder.stdout.close()
            if decoder.wait(timeout=60):
                raise RuntimeError('Archive decompression failed')
        finally:
            if decoder.poll() is None:
                decoder.kill()
                decoder.wait()

    def host_snapshot(self, destination, metadata):
        skipped = []

        def safe_member(member):
            if (member.isdev() or member.isfifo() or member.name.endswith('/.bash_history')
                    or '/.cache/' in member.name):
                return None
            if member.issym():
                target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
                if member.linkname.startswith('/') or target.startswith('../'):
                    skipped.append(member.name)
                    return None
            return member

        with tempfile.TemporaryDirectory(prefix='pve-host-snapshot-', dir='/run') as temporary:
            database = Path(temporary) / 'pmxcfs-config.db'
            source = sqlite3.connect('file:/var/lib/pve-cluster/config.db?mode=ro', uri=True)
            target = sqlite3.connect(database)
            try:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('pmxcfs database integrity failure')
            finally:
                source.close()
                target.close()
            metadata['packages'] = command('dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n').splitlines()
            metadata['links'] = json.loads(command('ip', '-json', 'link'))
            metadata['addresses'] = json.loads(command('ip', '-json', 'address'))
            metadata['storage_config'] = json.loads(command('pvesh', 'get', '/storage', '--output-format', 'json'))
            metadata['enabled_units'] = command('systemctl', 'list-unit-files', '--state=enabled',
                                                 '--no-legend', '--no-pager').splitlines()
            metadata['local_users'] = []
            for name in ('service-inventory', 'pve-backup-ui'):
                try:
                    user = pwd.getpwnam(name)
                    metadata['local_users'].append(dict(name=name, uid=user.pw_uid,
                                                       gid=user.pw_gid, home=user.pw_dir, shell=user.pw_shell))
                except KeyError:
                    pass
            with tarfile.open(destination, 'w:gz', compresslevel=3) as archive:
                for relative in HOST_PATHS:
                    path = Path('/') / relative
                    if path.exists():
                        archive.add(path, arcname='host/' + relative, filter=safe_member)
                archive.add(database, arcname='reference/pmxcfs-config.db')
            metadata['skipped_external_symlinks'] = skipped
            with tarfile.open(destination, 'r:gz') as archive:
                for member in archive:
                    if member.isfile():
                        source = archive.extractfile(member)
                        for _ in iter(lambda: source.read(1024**2), b''):
                            pass

    def perform(self):
        guests = guest_inventory()
        if not guests:
            raise RuntimeError('Empty inventory is not a valid full backup')
        warnings = exclusions(guests, self.config['allowed_exclusions'])
        resources = json.loads(command('pvesh', 'get', '/cluster/resources', '--type', 'vm',
                                       '--output-format', 'json'))
        running = {int(g['vmid']): g.get('status') == 'running' for g in resources}
        if any(g['config'].get('lock') for g in guests):
            raise RuntimeError('A guest is locked; retry when other maintenance is complete')
        needed = estimate_bytes(guests)
        check_space(self.config, needed)
        identifier = datetime.now(timezone.utc).strftime('set-%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
        stage = self.root / '.staging' / identifier
        stage.mkdir(parents=True, mode=0o700)
        archive_dir = stage / 'guests'
        archive_dir.mkdir(mode=0o700)
        self.update(running=True, phase='guests', set_id=identifier, started_at=now(),
                    total_guests=len(guests), completed_guests=0, guest_percent=0,
                    warnings=warnings, error=None, guests=[], estimated_bytes=needed,
                    finished_at=None, cleanup_warning=None)
        manifest = dict(version=1, id=identifier, created_at=now(),
                        source_node=os.uname().nodename, pve_version=command('pveversion'),
                        backup_uuid=self.config['uuid'], warnings=warnings, guests=[], files=[])
        try:
            for index, guest in enumerate(guests):
                check_space(self.config, 0)
                self.update(current_guest=dict(id=guest['id'], kind=guest['kind'], name=guest['name']),
                            guest_percent=0, message='Creating snapshot archive')
                self.long(['vzdump', str(guest['id']), '--dumpdir', str(archive_dir), '--mode', 'snapshot',
                           '--tmpdir', '/var/tmp',
                           '--compress', 'zstd', '--zstd', '1', '--remove', '0',
                           '--bwlimit', str(self.config['bwlimit_kib']), '--lockwait', '1',
                           '--notification-mode', 'notification-system'], progress=True)
                matches = [p for p in archive_dir.iterdir() if ARCHIVE.fullmatch(p.name)
                           and ARCHIVE.fullmatch(p.name)[2] == str(guest['id'])
                           and ARCHIVE.fullmatch(p.name)[1] == guest['kind']]
                if len(matches) != 1:
                    raise RuntimeError(f'Expected one complete archive for guest {guest["id"]}')
                path = matches[0]
                self.update(phase='verify', message='Verifying archive', guest_percent=100)
                self.verify_archive(path, guest['kind'])
                item = dict(id=guest['id'], kind=guest['kind'], name=guest['name'],
                            archive='guests/' + path.name, bytes=path.stat().st_size,
                            verified_at=now(), config=guest['config'], was_running=running.get(guest['id'], False))
                manifest['guests'].append(item)
                self.update(phase='guests', completed_guests=index + 1,
                            current_guest=None,
                            guests=[{k: v for k, v in g.items() if k != 'config'} for g in manifest['guests']])
            if identity(guest_inventory()) != identity(guests):
                raise RuntimeError('Guest inventory/configuration changed during backup; retry without pruning')
            self.update(phase='host', current_guest=None, message='Collecting Proxmox configuration')
            self.host_snapshot(stage / 'host-config.tar.gz', manifest)
            kit = Path('/opt/proxmox-usb-backup/recovery')
            if not (kit / 'proxmox_usb_backup/recovery.py').is_file():
                raise RuntimeError('Portable recovery kit is missing')
            shutil.copytree(kit, stage / 'recovery', symlinks=False)
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    path.chmod(0o600)
                    with path.open('rb') as source:
                        os.fsync(source.fileno())
                    manifest['files'].append(dict(path=str(path.relative_to(stage)),
                                                  bytes=path.stat().st_size, sha256=digest(path)))
            manifest['completed_at'] = now()
            manifest['total_bytes'] = sum(f['bytes'] for f in manifest['files'])
            for directory in sorted((p for p in stage.rglob('*') if p.is_dir()),
                                    key=lambda p: len(p.parts), reverse=True):
                sync_directory(directory)
            atomic_json(stage / 'manifest.json', manifest)
            atomic_json(stage / 'COMPLETE.json', dict(manifest_sha256=digest(stage / 'manifest.json')))
            load_set(stage, verify_files=True)
            check_space(self.config, 0)
            sets = self.root / 'sets'
            sets.mkdir(mode=0o700, exist_ok=True)
            final = sets / identifier
            stage.rename(final)
            sync_directory(sets)
            publish_warning = None
            try:
                publish_archives(final, self.root / 'dump', manifest)
                prune_sets(self.root, identifier)
            except (OSError, ValueError, RuntimeError) as error:
                publish_warning = 'Verified set retained; old copies were not fully pruned: ' + str(error)[:300]
            summary = {key: manifest[key] for key in ('id', 'created_at', 'completed_at', 'total_bytes', 'warnings')}
            summary['guests'] = [{k: v for k, v in g.items() if k != 'config'} for g in manifest['guests']]
            self.update(running=False, phase='success', message='Verified recovery set ready',
                        last_success=summary, error=None, cleanup_warning=publish_warning,
                        finished_at=now(), current_guest=None)
            return summary
        finally:
            # Never clean another run or a mounted replacement device after USB loss.
            if disk_status(self.config)['online'] and stage.is_dir() and not stage.is_symlink():
                shutil.rmtree(stage)


def publish_archives(directory, dump, manifest):
    dump.mkdir(mode=0o700, exist_ok=True)
    for guest in manifest['guests']:
        archive = safe_path(directory, guest['archive'])
        target = dump / archive.name
        if target.exists():
            if not os.path.samefile(target, archive):
                raise RuntimeError('Backup name collision; refusing to overwrite')
        else:
            os.link(archive, target)
    sync_directory(dump)


def prune_sets(root, keep):
    if not GENERATION.fullmatch(keep):
        raise ValueError('Invalid retained set')
    load_set(root / 'sets' / keep, verify_files=True)
    # Do not remove archives while a restore task may still be reading an old copy.
    tasks = json.loads(command('pvesh', 'get', f'/nodes/{os.uname().nodename}/tasks',
                               '--source', 'active', '--output-format', 'json'))
    if any('restore' in task.get('type', '') for task in tasks):
        raise RuntimeError('Restore in progress; pruning deferred')
    for path in sorted((root / 'sets').iterdir()):
        if path.name == keep or not path.is_dir() or path.is_symlink() or not GENERATION.fullmatch(path.name):
            continue
        manifest = load_set(path)
        for guest in manifest['guests']:
            archive = safe_path(path, guest['archive'])
            link = root / 'dump' / archive.name
            if link.exists():
                if not os.path.samefile(link, archive):
                    raise ValueError('Archive index collision; old set retained')
                link.unlink()
        shutil.rmtree(path)
    sync_directory(root / 'sets')


def health(config):
    disk = disk_status(config)
    active = command('systemctl', 'show', 'proxmox-usb-backup.service', '-p', 'ActiveState', '--value')
    disk['worker_active'] = active in ('active', 'activating', 'deactivating')
    disk['inventory'] = [{k: g[k] for k in ('id', 'kind', 'name')} for g in guest_inventory()]
    disk['next_run'] = command('systemctl', 'show', 'proxmox-usb-backup.timer',
                                '-p', 'NextElapseUSecRealtime', '--value')
    atomic_json(STATE / 'disk.json', disk, 0o644)
    status = read_json(STATE / 'status.json', {})
    if status.get('running') and not disk['worker_active']:
        status.update(running=False, phase='failed', error='Previous job was interrupted', finished_at=now())
        atomic_json(STATE / 'status.json', status, 0o644)
    latest = status.get('last_success', {}).get('completed_at')
    due = not latest or time.time() - datetime.fromisoformat(latest).timestamp() > 7 * 86400
    last_attempt = status.get('started_at')
    retry = not last_attempt or time.time() - datetime.fromisoformat(last_attempt).timestamp() > 3600
    if disk['online'] and due and retry and not disk['worker_active'] and config.get('auto_retry', True):
        command('systemctl', 'start', '--no-block', 'proxmox-usb-backup.service')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'health'])
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run on Proxmox as root')
    os.umask(0o077)
    STATE.mkdir(mode=0o755, parents=True, exist_ok=True)
    config = read_json(CONFIG)
    if args.action == 'health':
        health(config)
        return
    runner = Runner(config)

    def interrupted(*_):
        raise InterruptedError('Backup was stopped or USB became unavailable')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with job_lock(), open(STATE / 'current.log', 'w') as log:
        runner.log = log
        runner.update(running=True, phase='preflight', started_at=now(), error=None,
                      message='Checking backup disk and guest inventory')
        try:
            runner.perform()
        except Exception as error:
            runner.update(running=False, phase='failed', error=str(error)[:600], finished_at=now())
            print(str(error), flush=True)
            raise


if __name__ == '__main__':
    main()
