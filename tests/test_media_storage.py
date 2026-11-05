import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('monitor', Path(__file__).resolve().parents[1] / 'scripts/proxmox-ha-monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

class MediaStorageTest(unittest.TestCase):
    def test_unmounted_disk_never_reports_root_capacity(self):
        with patch.object(Path, 'is_mount', return_value=False), patch.object(monitor.os, 'statvfs') as stat:
            self.assertIsNone(monitor.read_media_storage())
            stat.assert_not_called()

    def test_reserved_space_and_available_capacity(self):
        stat = SimpleNamespace(f_blocks=1000, f_bfree=600, f_bavail=550, f_frsize=4096)
        with patch.object(Path, 'is_mount', return_value=True), patch.object(monitor.os, 'statvfs', return_value=stat):
            disk = monitor.read_media_storage()
        self.assertEqual(disk['used_bytes'], 400 * 4096)
        self.assertEqual(disk['free_bytes'], 550 * 4096)
        self.assertEqual(disk['reserved_bytes'], 50 * 4096)
        self.assertEqual(disk['used_percent'], 42.1)
        self.assertEqual(disk['total_bytes'], sum(disk[k] for k in ['used_bytes', 'free_bytes', 'reserved_bytes']))

    def test_full_disk(self):
        stat = SimpleNamespace(f_blocks=1000, f_bfree=50, f_bavail=0, f_frsize=4096)
        with patch.object(Path, 'is_mount', return_value=True), patch.object(monitor.os, 'statvfs', return_value=stat):
            self.assertEqual(monitor.read_media_storage()['used_percent'], 100)

if __name__ == '__main__':
    unittest.main()
