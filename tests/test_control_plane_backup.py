import importlib.util
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = load('control_backup', 'control-plane-backup.py')
verifier = load('control_verify', 'verify-control-plane-backup.py')


@pytest.mark.parametrize('name', ['../secret', '/etc/shadow', 'ct100/../secret', 'unrelated/file'])
def test_restore_path_validation(name):
    assert not verifier.valid_member(tarfile.TarInfo(name))


def test_restore_rejects_links_and_accepts_bounded_paths():
    for name in verifier.REQUIRED:
        assert verifier.valid_member(tarfile.TarInfo(name))
    member = tarfile.TarInfo('host/unsafe')
    member.type = tarfile.SYMTYPE
    member.linkname = '/etc/shadow'
    assert not verifier.valid_member(member)


def test_retention_does_not_follow_links_or_delete_other_files(tmp_path):
    now = datetime.now(timezone.utc)
    old = tmp_path / 'control-plane-20260101T000000Z.tar.age'
    old.write_bytes(b'encrypted')
    os.utime(old, ((now - timedelta(days=31)).timestamp(),) * 2)
    unrelated = tmp_path / 'control-plane-important.tar.age'
    unrelated.write_bytes(b'keep')
    link = tmp_path / 'control-plane-20260102T000000Z.tar.age'
    link.symlink_to(unrelated)
    backup.prune(tmp_path, now)
    assert not old.exists() and unrelated.exists() and link.is_symlink()


def test_collector_uses_online_sqlite_backup_and_excludes_node_identity():
    assert 'source.backup(destination)' in backup.GUEST_COLLECTOR
    assert 'PRAGMA integrity_check' in backup.GUEST_COLLECTOR
    assert 'var/lib/tailscale' not in backup.GUEST_COLLECTOR
    assert 'systemctl stop' not in backup.GUEST_COLLECTOR
    assert "destination.execute('DELETE FROM sessions')" in backup.GUEST_COLLECTOR
    assert 'fcntl.LOCK_SH | fcntl.LOCK_NB' in backup.GUEST_COLLECTOR
