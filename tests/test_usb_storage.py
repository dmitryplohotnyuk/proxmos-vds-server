import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('usb_storage', ROOT / 'scripts/prepare-usb-photo-storage.py')
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


def disk(**overrides):
    return {'type': 'disk', 'tran': 'usb', 'size': 2000398934016,
            'ro': False, 'mountpoints': [None], **overrides}


def test_decimal_gb_layout_is_aligned_and_leaves_room():
    plan = storage.partition_plan(2000398934016, 400)
    assert 400 * 10**9 - 1024**2 < plan['size'] * 512 <= 400 * 10**9
    assert plan['start'] == 2048
    assert plan['size'] % 2048 == 0
    assert plan['photos_start'] == plan['start'] + plan['size']
    for value in (0, 2000, -1):
        with pytest.raises(ValueError):
            storage.partition_plan(2000398934016, value)


def test_disk_identity_and_idle_state_are_required():
    storage.validate_device(disk(), 'SERIAL', 'SERIAL', 2000398934016, 'SERIAL')
    for target, serial, capacity, confirmation in [
        (disk(tran='nvme'), 'SERIAL', 2000398934016, 'SERIAL'),
        (disk(ro=True), 'SERIAL', 2000398934016, 'SERIAL'),
        (disk(type='part'), 'SERIAL', 2000398934016, 'SERIAL'),
        (disk(), 'WRONG', 2000398934016, 'SERIAL'),
        (disk(), 'SERIAL', 1, 'SERIAL'),
        (disk(), 'SERIAL', 2000398934016, 'WRONG'),
        (disk(mountpoints=['/']), 'SERIAL', 2000398934016, 'SERIAL'),
        (disk(children=[{'mountpoints': [None]}]), 'SERIAL', 2000398934016, 'SERIAL'),
        (disk(children=[{'mountpoints': ['/data']}]), 'SERIAL', 2000398934016, None),
    ]:
        with pytest.raises(ValueError):
            storage.validate_device(target, serial, 'SERIAL', capacity, confirmation)


def test_lsblk_explicitly_requests_partition_tree(monkeypatch):
    device = Path('/dev/sdz')
    info = disk(path=str(device), children=[{'mountpoints': ['/photos']}])
    calls = []

    def lsblk(*args):
        calls.append(args)
        return json.dumps({'blockdevices': [info]})

    monkeypatch.setattr(storage, 'run', lsblk)
    assert storage.read_device(device) == info
    assert '--tree' in calls[0]
    with pytest.raises(ValueError):
        storage.validate_device(storage.read_device(device), 'S', 'S', info['size'], 'S')


def test_lsblk_flat_or_wrong_device_output_is_rejected(monkeypatch):
    for entries in ([disk(path='/dev/wrong')], [disk(path='/dev/sdz'), disk(path='/dev/sdz1')]):
        monkeypatch.setattr(storage, 'run', lambda *args: json.dumps({'blockdevices': entries}))
        with pytest.raises(ValueError):
            storage.read_device(Path('/dev/sdz'))


def test_fstab_preserves_existing_configuration_and_is_idempotent():
    a = 'a38c8b9e-8486-4e9d-bd11-c53088a1fb97'
    b = '5820beaf-b8ec-46e2-a87d-aec99b1ee986'
    original = '/dev/pve/root / ext4 errors=remount-ro 0 1\n# existing USB disk\nUUID=old /mnt/media-ssd ext4 defaults,nofail 0 2\n'
    rendered = storage.render_fstab(original, a, b)
    assert rendered.startswith(original)
    assert storage.render_fstab(rendered, a, b) == rendered
    for path in (storage.BACKUPS, storage.PHOTOS):
        line = next(line for line in rendered.splitlines() if str(path) in line)
        assert 'nofail' in line
        assert 'x-systemd.device-timeout=5s' in line
        assert 'x-systemd.mount-timeout=15s' in line
        assert 'x-systemd.device-bound=yes' in line
        assert 'automount' not in line
    replaced = storage.render_fstab(rendered, b, a)
    assert replaced.count(storage.BEGIN) == 1
    assert replaced.startswith(original)


def test_fstab_rejects_invalid_uuid_and_partial_managed_block():
    valid = 'a38c8b9e-8486-4e9d-bd11-c53088a1fb97'
    for original, value in [('', 'x\n/dev/sda'), (storage.BEGIN, valid)]:
        with pytest.raises(ValueError):
            storage.render_fstab(original, value, valid)
    with pytest.raises(ValueError):
        storage.render_fstab('', valid, valid)


def test_unmounted_underlying_directory_is_guarded(tmp_path, monkeypatch):
    path = tmp_path / 'mount'
    calls = []
    monkeypatch.setattr(storage, 'run', lambda *args: calls.append(args))
    try:
        storage.guard_directory(path)
        assert path.stat().st_mode & 0o777 == 0
        assert calls == [('chattr', '+i', str(path))]
    finally:
        if path.exists():
            path.chmod(0o700)


def test_guard_refuses_existing_data_or_symlink(tmp_path, monkeypatch):
    path = tmp_path / 'mount'
    path.mkdir()
    (path / 'existing-data').write_text('keep')
    link = tmp_path / 'link'
    link.symlink_to(path)
    calls = []
    monkeypatch.setattr(storage, 'run', lambda *args: calls.append(args))
    for target in (path, link):
        with pytest.raises(ValueError):
            storage.guard_directory(target)
    assert (path / 'existing-data').read_text() == 'keep'
    assert calls == []


def test_storage_add_and_update_keep_mount_guard_without_updating_fixed_path(monkeypatch):
    calls = []
    monkeypatch.setattr(storage, 'run', lambda *args: calls.append(args))
    storage.register_backup_storage(False)
    storage.register_backup_storage(True)
    assert calls[0][:4] == ('pvesm', 'add', 'dir', 'usb-backups')
    assert calls[1][:3] == ('pvesm', 'set', 'usb-backups')
    assert calls[0][calls[0].index('--path') + 1] == str(storage.BACKUPS)
    assert '--path' not in calls[1]
    for args in calls:
        assert args[args.index('--is_mountpoint') + 1] == '1'
        assert args[args.index('--content') + 1] == 'backup'
        assert args[args.index('--create-base-path') + 1] == '0'
        assert args[args.index('--create-subdirs') + 1] == '0'
