import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / 'scripts/configure-usb-backup-compat.py'
spec = importlib.util.spec_from_file_location('usb_backup_compat', path)
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def test_quirks_preserve_unrelated_devices_and_existing_flags():
    assert compat.merge_quirks('') == '0bda:9210:u'
    assert compat.merge_quirks('1234:5678:r,0bda:9210:m') == '1234:5678:r,0bda:9210:mu'
    assert compat.merge_quirks('0BDA:9210:u,0bda:9210:r') == '0bda:9210:ru'
    assert compat.merge_quirks('0bda:9210:u') == '0bda:9210:u'


def test_quirks_reject_unknown_parameter_format():
    with pytest.raises(ValueError):
        compat.merge_quirks('unrecognized value')
