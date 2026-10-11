import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/configure-root-ssh.py'))


@pytest.mark.parametrize('kind', ['lxc', 'qemu'])
def test_guest_password_only_stdin(monkeypatch, kind):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(exited=True, exitcode=0)).encode())
    monkeypatch.setattr(MODULE['subprocess'], 'run', run)
    MODULE['guest'](kind, 104, ['/usr/sbin/chpasswd'], b'root:unique-secret\n')
    args, kwargs = calls[0]
    assert 'unique-secret' not in ' '.join(args)
    assert kwargs['input'] == b'root:unique-secret\n'
    if kind == 'qemu':
        assert '--pass-stdin' in args


def test_root_ssh_is_scoped_and_preserves_key_settings():
    setup = MODULE['SETUP']
    assert 'Match User root' in setup
    assert 'PermitRootLogin yes' in setup and 'PasswordAuthentication yes' in setup
    assert 'PubkeyAuthentication no' not in setup
    assert 'sshd_config' in setup and '/usr/sbin/sshd -t' in setup
    assert 'systemctl reload' in MODULE['RELOAD']
