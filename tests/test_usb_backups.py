import json
import io
import os
from pathlib import Path
import threading
import time
import sys
from types import SimpleNamespace
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from proxmox_usb_backup import common, engine, recovery, web


@pytest.mark.parametrize('utc_hour,expected', [(7, False), (8, True), (15, True), (16, False), (23, False)])
def test_automatic_retry_only_starts_in_kyiv_daytime(utc_hour, expected):
    moment = datetime(2026, 11, 5, utc_hour, tzinfo=timezone.utc)
    assert engine.daytime_retry_allowed({}, moment) is expected


def test_automatic_retry_window_follows_summer_timezone_and_rejects_invalid_bounds():
    assert engine.daytime_retry_allowed({}, datetime(2026, 7, 5, 7, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        engine.daytime_retry_allowed({'auto_retry_window':{'start_hour':18,'end_hour':10}})


@pytest.mark.parametrize('daytime,blocked', [(False, False), (True, True)])
def test_health_does_not_retry_at_night_or_after_integrity_failure(tmp_path, monkeypatch, daytime, blocked):
    monkeypatch.setattr(engine, 'STATE', tmp_path)
    common.atomic_json(tmp_path/'status.json', {'auto_retry_blocked':blocked})
    monkeypatch.setattr(engine, 'disk_status', lambda *args: {'online':True})
    monkeypatch.setattr(engine, 'guest_inventory', lambda: [])
    monkeypatch.setattr(engine, 'daytime_retry_allowed', lambda *args: daytime)

    def command(*args):
        assert args[:2] != ('systemctl', 'start')
        return 'inactive' if args[-2] == 'ActiveState' else 'Sunday afternoon'

    monkeypatch.setattr(engine, 'command', command)
    engine.health({'auto_retry':True})


def test_archive_is_flushed_before_page_cache_is_dropped(tmp_path, monkeypatch):
    file = tmp_path/'archive'
    file.write_bytes(b'data')
    calls = []
    monkeypatch.setattr(os, 'fsync', lambda *args: calls.append('fsync'))
    monkeypatch.setattr(os, 'posix_fadvise', lambda *args: calls.append('fadvise'), raising=False)
    monkeypatch.setattr(os, 'POSIX_FADV_DONTNEED', 4, raising=False)
    engine.flush_file(file)
    assert calls == ['fsync', 'fadvise']


def test_vzdump_subprocess_can_read_unprivileged_lxc_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'STATE', tmp_path)
    monkeypatch.setattr(engine, 'check_space', lambda *args: None)
    runner = engine.Runner({'mount': str(tmp_path)})
    runner.log = io.StringIO()
    runner.long([sys.executable, '-c', 'import os; print(os.umask(0))'])
    assert runner.log.getvalue().strip() == str(0o022)


def completed(root, stamp='20261105T120000Z', payload=b'backup'):
    directory = root / 'sets' / f'set-{stamp}-aabbccdd'
    directory.mkdir(parents=True)
    name = 'vzdump-lxc-108-2026_11_05-12_00_00.tar.zst'
    archive = directory / 'guests' / name
    archive.parent.mkdir()
    archive.write_bytes(payload)
    manifest = dict(version=1, id=directory.name, completed_at='2026-11-05T12:00:00+00:00',
                    files=[dict(path='guests/' + name, bytes=len(payload), sha256=common.digest(archive))],
                    guests=[dict(id=108, name='vaultwarden', kind='lxc', archive='guests/' + name)])
    common.atomic_json(directory / 'manifest.json', manifest)
    common.atomic_json(directory / 'COMPLETE.json', {'manifest_sha256': common.digest(directory / 'manifest.json')})
    return directory, manifest


def test_manifest_rejects_corruption_and_no_marker(tmp_path):
    directory, manifest = completed(tmp_path)
    assert common.load_set(directory, True)['id'] == directory.name
    archive = directory / manifest['files'][0]['path']
    archive.write_bytes(b'wrong!')
    with pytest.raises(ValueError):
        common.load_set(directory, True)
    (directory / 'COMPLETE.json').unlink()
    with pytest.raises(ValueError):
        common.load_set(directory)


def test_safe_paths_block_escape_and_symlink(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'set'
    root.mkdir()
    (root / 'link').symlink_to(outside)
    for value in ('../outside', '/etc/shadow', 'link/data'):
        with pytest.raises(ValueError):
            common.safe_path(root, value)


def test_inventory_automatically_follows_guest_files_and_current_config(tmp_path):
    (tmp_path / 'lxc').mkdir()
    (tmp_path / 'qemu-server').mkdir()
    a = tmp_path / 'lxc/100.conf'
    a.write_text('hostname: router\nrootfs: local-lvm:disk,size=8G\n\n[old-snapshot]\nhostname: old\n')
    result = common.guest_inventory(tmp_path)
    assert [(g['id'], g['name']) for g in result] == [(100, 'router')]
    (tmp_path / 'qemu-server/110.conf').write_text('name: new-vm\ntemplate: 1\n')
    assert [g['id'] for g in common.guest_inventory(tmp_path)] == [100, 110]
    a.unlink()
    assert [g['id'] for g in common.guest_inventory(tmp_path)] == [110]


def test_exclusions_are_explicit_and_unknown_data_disks_block_success():
    guest = dict(id=104, kind='lxc', config={'mp0':'/mnt/media-ssd,mp=/media,backup=0'})
    assert common.exclusions([guest], {'/mnt/media-ssd':'Excluded by owner'})[0]['source'] == '/mnt/media-ssd'
    with pytest.raises(ValueError):
        common.exclusions([guest], {})
    with pytest.raises(ValueError):
        common.exclusions([dict(id=101,kind='qemu',config={'scsi0':'local-lvm:disk,backup=0'})], {})


def test_offline_or_wrong_uuid_never_uses_root_capacity(monkeypatch):
    monkeypatch.setattr(common, 'command', lambda *args: 'wrong-uuid')
    monkeypatch.setattr(os, 'statvfs', lambda *args: pytest.fail('must not inspect root filesystem capacity'))
    assert not common.disk_status({'mount':'/not-usb','uuid':'expected'})['online']


def test_readonly_backup_filesystem_is_not_reported_healthy(monkeypatch):
    monkeypatch.setattr(common, 'command', lambda *args: 'expected' if args[-1] == 'UUID' else 'ro,errors=remount-ro')
    monkeypatch.setattr(Path, 'is_mount', lambda *args: True)
    monkeypatch.setattr(os, 'statvfs', lambda *args: pytest.fail('read-only filesystem is not ready'))
    assert not common.disk_status({'mount':'/usb','uuid':'expected'})['online']


def test_readonly_health_sandbox_does_not_mask_writable_host_filesystem(monkeypatch):
    monkeypatch.setattr(common, 'command', lambda *args: 'expected' if args[-1] == 'UUID' else 'rw,errors=remount-ro')
    monkeypatch.setattr(Path, 'is_mount', lambda *args: True)
    monkeypatch.setattr(os, 'statvfs', lambda *args: SimpleNamespace(
        f_flag=os.ST_RDONLY, f_blocks=100, f_frsize=4096, f_bavail=90, f_bfree=90))
    assert common.disk_status({'mount':'/usb','uuid':'expected'})['online']


def test_hardlink_publication_and_pruning_preserve_unrelated_data(tmp_path, monkeypatch):
    old, old_manifest = completed(tmp_path, '20261101T120000Z')
    new, manifest = completed(tmp_path, '20261105T120000Z')
    dump = tmp_path / 'dump'
    engine.publish_archives(old, dump, old_manifest)
    # Different creation timestamp normally gives a different vzdump filename.
    path = new / manifest['guests'][0]['archive']
    renamed = path.with_name(path.name.replace('12_00_00','12_00_01'))
    path.rename(renamed)
    manifest['guests'][0]['archive'] = 'guests/' + renamed.name
    manifest['files'][0]['path'] = 'guests/' + renamed.name
    common.atomic_json(new/'manifest.json', manifest)
    common.atomic_json(new/'COMPLETE.json', {'manifest_sha256':common.digest(new/'manifest.json')})
    engine.publish_archives(new, dump, manifest)
    assert os.path.samefile(renamed, dump / renamed.name)
    unrelated = tmp_path / 'sets/manual-copy'
    unrelated.mkdir()
    (unrelated/'keep').write_text('safe')
    monkeypatch.setattr(engine, 'command', lambda *args: '[]')
    engine.prune_sets(tmp_path, new.name)
    assert new.exists() and not old.exists()
    assert (unrelated/'keep').read_text() == 'safe'
    assert (dump/renamed.name).exists()


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_failed_archive_creation_never_prunes_previous_set(tmp_path, monkeypatch, cleanup_fails):
    old, _ = completed(tmp_path)
    state = tmp_path / 'state'
    common.atomic_json(state/'status.json', {'last_success':{'id':old.name}})
    monkeypatch.setattr(engine, 'STATE', state)
    guests = [dict(id=100,kind='lxc',name='router',config={},config_sha256='a')]
    monkeypatch.setattr(engine, 'guest_inventory', lambda:guests)
    monkeypatch.setattr(engine, 'exclusions', lambda *args:[])
    monkeypatch.setattr(engine, 'estimate_bytes', lambda *args:10)
    monkeypatch.setattr(engine, 'check_space', lambda *args:None)
    monkeypatch.setattr(engine, 'disk_status', lambda *args:{'online':True})
    monkeypatch.setattr(engine, 'prune_sets', lambda *args:pytest.fail('must not prune'))
    monkeypatch.setattr(engine, 'command', lambda *args:'[]' if args[0]=='pvesh' else 'pve-manager/9.2.4')
    runner = engine.Runner({'mount':str(tmp_path),'uuid':'test','allowed_exclusions':{},'bwlimit_kib':100})

    def fake_backup(args, **kwargs):
        folder = Path(args[args.index('--dumpdir')+1])
        (folder/'vzdump-lxc-100-2026_11_05-12_00_00.tar.zst').write_bytes(b'broken')
        raise ValueError('archive failure')

    monkeypatch.setattr(runner, 'long', fake_backup)
    if cleanup_fails:
        monkeypatch.setattr(engine.shutil, 'rmtree', lambda *args: (_ for _ in ()).throw(OSError('USB I/O failure')))
    with pytest.raises(ValueError, match='archive failure'):
        runner.perform()
    assert old.exists()
    assert common.read_json(state/'status.json')['last_success']['id'] == old.name
    assert bool(list((tmp_path/'.staging').iterdir())) == cleanup_fails
    assert bool(common.read_json(state/'status.json').get('cleanup_warning')) == cleanup_fails


def test_corrupt_new_set_blocks_pruning_before_any_delete(tmp_path, monkeypatch):
    old, _ = completed(tmp_path,'20261101T120000Z')
    new, _ = completed(tmp_path)
    (new/'COMPLETE.json').write_text('{}')
    with pytest.raises(ValueError):
        engine.prune_sets(tmp_path,new.name)
    assert old.exists()


@pytest.mark.parametrize('native_failure', [False, True])
def test_integrity_failure_preserves_evidence_and_previous_success(tmp_path, monkeypatch, native_failure):
    old, _ = completed(tmp_path, '20261101T120000Z')
    state = tmp_path/'state'
    common.atomic_json(state/'status.json', {'last_success':{'id':old.name}})
    monkeypatch.setattr(engine, 'STATE', state)
    guest = dict(id=100,kind='lxc',name='router',config={},config_sha256='a')
    monkeypatch.setattr(engine, 'guest_inventory', lambda:[guest])
    monkeypatch.setattr(engine, 'exclusions', lambda *args:[])
    monkeypatch.setattr(engine, 'estimate_bytes', lambda *args:10)
    monkeypatch.setattr(engine, 'check_space', lambda *args:None)
    monkeypatch.setattr(engine, 'disk_status', lambda *args:{'online':True})
    monkeypatch.setattr(engine, 'command', lambda *args:'[]' if args[0]=='pvesh' else 'pve-manager/9.2.4')
    kit = tmp_path/'kit'
    (kit/'proxmox_usb_backup').mkdir(parents=True)
    (kit/'proxmox_usb_backup/recovery.py').write_text('code')
    monkeypatch.setattr(engine, 'RECOVERY_KIT', kit)
    runner = engine.Runner({'mount':str(tmp_path),'uuid':'test','allowed_exclusions':{},'bwlimit_kib':100})

    def backup(args, **kwargs):
        folder = Path(args[args.index('--dumpdir')+1])
        (folder/'vzdump-lxc-100-2026_11_05-12_00_00.tar.zst').write_bytes(b'archive')

    monkeypatch.setattr(runner, 'long', backup)
    def verify(*args):
        if native_failure:
            raise ValueError('Checksum mismatch')
    monkeypatch.setattr(runner, 'verify_archive', verify)
    monkeypatch.setattr(runner, 'host_snapshot', lambda path, manifest:path.write_bytes(b'host'))
    monkeypatch.setattr(engine, 'load_set', lambda *args, **kwargs:(_ for _ in ()).throw(ValueError('Checksum mismatch')))
    with pytest.raises(ValueError, match='Checksum mismatch'):
        runner.perform()
    assert old.exists()
    assert list((tmp_path/'.staging').glob('*/guests/*.zst'))
    assert common.read_json(state/'status.json')['auto_retry_blocked']
    assert not (tmp_path/'dump').exists()


def test_restore_id_mapping_and_no_overwrite_shape():
    guests=[{'id':100},{'id':101}]
    assert recovery.id_mapping(guests,['100=9901','101=9902']) == {100:9901,101:9902}
    for values in (['100=99'],['999=9901'],['100=101']):
        with pytest.raises(ValueError):
            recovery.id_mapping(guests,values)


def test_csrf_is_identity_bound_and_expires(monkeypatch):
    monkeypatch.setattr(web.time,'time',lambda:10000)
    token=web.csrf_token(b'secret','owner',9900)
    assert web.valid_csrf(b'secret','owner',token)
    assert not web.valid_csrf(b'secret','other',token)
    assert not web.valid_csrf(b'secret','owner',web.csrf_token(b'secret','owner',1))
    assert not web.valid_csrf(b'secret','owner',web.csrf_token(b'secret','owner',11000))


def test_http_identity_origin_csrf_and_fixed_action(tmp_path,monkeypatch):
    monkeypatch.setattr(web,'STATE',tmp_path)
    guide = tmp_path / 'guide.md'
    guide.write_text('Recovery instructions')
    monkeypatch.setattr(web,'GUIDE',guide)
    started=[]
    monkeypatch.setattr(web,'start_backup',lambda:started.append(True))
    server=ThreadingHTTPServer(('127.0.0.1',0),web.ui_handler({'allowed_logins':['owner@example.com'],'origin':'https://backup.test'},b'secret'))
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(url+'/api/status')
        assert error.value.code==403
        headers={'Tailscale-User-Login':'owner@example.com'}
        with urlopen(Request(url+'/recovery-guide',headers=headers)) as response:
            assert response.read() == b'Recovery instructions'
        with urlopen(Request(url+'/api/status',headers=headers)) as response:
            status=json.load(response)
            token=status['csrf']
            assert status['proxmox_url']=='https://backup.test/'
        for origin,csrf in [('https://evil.test',token),('https://backup.test','bad')]:
            with pytest.raises(HTTPError) as error:
                urlopen(Request(url+'/api/run',data=b'{}',headers={**headers,'Origin':origin,'X-CSRF-Token':csrf}))
            assert error.value.code==403
        with urlopen(Request(url+'/api/run',data=b'{}',headers={**headers,'Origin':'https://backup.test','X-CSRF-Token':token})) as response:
            assert response.status==202
        assert started==[True]
    finally:
        server.shutdown();server.server_close();thread.join()
