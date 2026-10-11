import importlib.util
from pathlib import Path
import tarfile

import pytest

spec = importlib.util.spec_from_file_location('vault_verify', Path(__file__).resolve().parents[1] / 'scripts/verify-vaultwarden-backup.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('name', ['../secret', '/etc/shadow', 'data/../../secret', 'unrelated.txt'])
def test_archive_path_validation(name):
    assert not module.valid_member(tarfile.TarInfo(name))


def test_symlink_and_regular_files():
    for name in ['data/db.sqlite3', 'data/attachments/test/file', 'vaultwarden.env', 'image.ref']:
        assert module.valid_member(tarfile.TarInfo(name))
    member = tarfile.TarInfo('data/link')
    member.type = tarfile.SYMTYPE
    member.linkname = '/etc/shadow'
    assert not module.valid_member(member)
