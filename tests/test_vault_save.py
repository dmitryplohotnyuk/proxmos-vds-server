import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import base64

import pytest

spec = importlib.util.spec_from_file_location('vault_save', Path(__file__).resolve().parents[1] / 'scripts/vault-save.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ITEM = 'a4d32612-262b-49c7-9701-a85fd25be230'


def test_save_password_only_in_stdin(monkeypatch):
    monkeypatch.setenv('BW_SESSION', 'test-session-not-a-real-key')
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        value = dict(status='unlocked', serverUrl='https://vault.test') if argv[-1] == 'status' else dict(id=ITEM, login={'password': 'SECRET'})
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    receipt = module.save('test', 'root', 'SECRET', 'ssh://10.77.0.10', 'https://vault.test')
    assert 'SECRET' not in json.dumps(receipt)
    assert all('SECRET' not in ' '.join(argv) for argv, _ in calls)
    saved = json.loads(base64.b64decode(calls[1][1]['input']))
    assert saved['login']['password'] == 'SECRET'
    assert receipt['vault_item_id'] == ITEM


def test_locked_or_wrong_server_never_creates(monkeypatch):
    monkeypatch.setenv('BW_SESSION', 'test-session-not-a-real-key')
    calls = []
    def call(*args, **kwargs):
        calls.append(args)
        return dict(status='unlocked', serverUrl='https://wrong.test')
    monkeypatch.setattr(module, 'bw', call)
    with pytest.raises(module.VaultError):
        module.save('test', 'root', 'SECRET', '', 'https://vault.test')
    assert calls == [('status',)]


def test_no_session_and_no_secret_error_output(monkeypatch):
    monkeypatch.delenv('BW_SESSION', raising=False)
    with pytest.raises(module.VaultError):
        module.save('test', 'root', 'SECRET', '', 'https://vault.test')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout='SECRET', stderr='SECRET'))
    with pytest.raises(module.VaultError) as error:
        module.bw('create', 'item', data='SECRET')
    assert 'SECRET' not in str(error.value)
