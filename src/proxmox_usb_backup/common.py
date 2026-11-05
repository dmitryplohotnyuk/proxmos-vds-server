import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl

CONFIG = Path('/etc/proxmox-usb-backup/config.json')
STATE = Path('/var/lib/proxmox-usb-backup')
RECOVERY_LOCK = Path('/run/lock/proxmox-usb-recovery.lock')
GENERATION = re.compile(r'set-\d{8}T\d{6}Z-[a-f0-9]{8}')
ARCHIVE = re.compile(r'vzdump-(qemu|lxc)-(\d+)-\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2}\.(vma|tar)\.zst')


@contextmanager
def recovery_lock(exclusive=False):
    fd = os.open(RECOVERY_LOCK, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'a') as lock:
        if os.fstat(lock.fileno()).st_uid != os.geteuid():
            raise RuntimeError('Unexpected recovery lock owner')
        mode = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock, mode)
        except BlockingIOError as error:
            raise RuntimeError('Recovery reader active; old recovery sets retained') from error
        yield


def now():
    return datetime.now(timezone.utc).isoformat()


def command(*args, timeout=60):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[0]}: {result.stderr.strip()[-1200:]}')
    return result.stdout.strip()


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def disk_status(config):
    path = Path(config['mount'])
    try:
        uuid = command('findmnt', '--mountpoint', str(path), '--noheadings', '--output', 'UUID')
        if uuid != config['uuid'] or not path.is_mount():
            raise RuntimeError('Wrong or missing backup filesystem')
        # ProtectSystem=strict makes the health service's VFS read-only, not the host filesystem.
        options = command('findmnt', '--mountpoint', str(path), '--noheadings', '--output', 'FS-OPTIONS')
        if 'ro' in options.split(','):
            raise RuntimeError('Backup filesystem is read-only; inspect USB and filesystem health')
        stat = os.statvfs(path)
        return dict(online=True, uuid=uuid, path=str(path), checked_at=now(),
                    total_bytes=stat.f_blocks * stat.f_frsize,
                    free_bytes=stat.f_bavail * stat.f_frsize,
                    used_bytes=(stat.f_blocks - stat.f_bfree) * stat.f_frsize)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return dict(online=False, path=str(path), checked_at=now(), error=str(error)[:300])


def require_disk(config):
    status = disk_status(config)
    if not status['online']:
        raise RuntimeError('Backup USB filesystem is absent or has the wrong UUID')
    return status


def safe_path(root, relative):
    root = Path(root).resolve()
    relative = PurePosixPath(relative)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('Unsafe relative backup path')
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Backup path escapes its set')
    return path


def digest(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as source:
        if hasattr(os, 'posix_fadvise'):
            os.posix_fadvise(source.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_set(path, verify_files=False):
    path = Path(path)
    if path.is_symlink() or not GENERATION.fullmatch(path.name):
        raise ValueError('Invalid recovery set directory')
    marker = read_json(path / 'COMPLETE.json')
    manifest_path = path / 'manifest.json'
    if not marker or marker.get('manifest_sha256') != digest(manifest_path):
        raise ValueError('Recovery set has no valid completion marker')
    manifest = read_json(manifest_path)
    if manifest.get('version') != 1 or manifest.get('id') != path.name:
        raise ValueError('Unsupported recovery manifest')
    for item in manifest['files']:
        file = safe_path(path, item['path'])
        if not file.is_file() or file.stat().st_size != item['bytes']:
            raise ValueError(f'Missing or truncated archive: {item["path"]}')
        if verify_files:
            actual = digest(file)
            if actual != item['sha256']:
                raise ValueError(f'Checksum mismatch: {item["path"]}; expected {item["sha256"]}, got {actual}')
    return manifest


def guest_inventory(base=Path('/etc/pve')):
    result = []
    for kind, directory in [('lxc', 'lxc'), ('qemu', 'qemu-server')]:
        for file in sorted((base / directory).glob('*.conf')):
            if not file.stem.isdigit():
                continue
            # Snapshot sections are not current guest configuration.
            text = file.read_text().split('\n[', 1)[0]
            fields = {}
            for line in text.splitlines():
                if ': ' in line and not line.startswith('#'):
                    key, value = line.split(': ', 1)
                    fields[key] = value
            stable = {key: value for key, value in fields.items() if key != 'lock'}
            result.append(dict(id=int(file.stem), kind=kind,
                               name=fields.get('hostname', fields.get('name', file.stem)),
                               config=fields, config_sha256=hashlib.sha256(
                                   json.dumps(stable, sort_keys=True).encode()).hexdigest()))
    return sorted(result, key=lambda g: (g['id'], g['kind']))


def exclusions(guests, allowed):
    warnings = []
    for guest in guests:
        for key, value in guest['config'].items():
            if guest['kind'] == 'lxc' and re.fullmatch(r'mp\d+', key):
                source = value.split(',', 1)[0]
                if source.startswith('/') or 'backup=1' not in value.split(','):
                    if source not in allowed:
                        raise ValueError(f'Guest {guest["id"]}: unapproved excluded mount {key}: {source}')
                    warnings.append(dict(guest=guest['id'], source=source, reason=allowed[source]))
            if guest['kind'] == 'qemu' and re.fullmatch(r'(scsi|virtio|sata|ide)\d+', key):
                if 'backup=0' in value.split(',') and 'media=cdrom' not in value:
                    raise ValueError(f'Guest {guest["id"]}: data disk {key} has backup=0')
    return warnings
