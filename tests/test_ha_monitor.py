import importlib.util
import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ha_monitor', ROOT / 'scripts/proxmox-ha-monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def test_power_wrap_and_sum_without_nested_core():
    before = {'p': ('package-0', 90_000_000, 100_000_000), 'd': ('dram', 0, 100_000_000)}
    after = {'p': ('package-0', 10_000_000, 100_000_000), 'd': ('dram', 10_000_000, 100_000_000)}
    assert monitor.power_watts(before, after, 10) == 3
    for elapsed in (0, -1, 31):
        assert monitor.power_watts(before, after, elapsed) is None
    assert monitor.power_watts({}, after, 10) is None
    assert monitor.power_watts({'p': before['p']}, {'p': after['p']}, 10) is None


def test_energy_reader_excludes_nested_domains(tmp_path):
    for key, name in [('0', 'package-0'), ('0:0', 'core'), ('0:1', 'uncore'), ('0:2', 'dram')]:
        p = tmp_path / ('intel-rapl:' + key)
        p.mkdir()
        (p / 'name').write_text(name)
        (p / 'energy_uj').write_text('10')
        (p / 'max_energy_range_uj').write_text('100')
    assert {v[0] for v in monitor.read_energy(tmp_path).values()} == {'package-0', 'dram'}


def test_guests_include_stopped_exclude_templates_and_sanitize():
    result = monitor.normalize_guests([
        {'type': 'lxc', 'vmid': 108, 'name': '<bad>|name', 'status': 'stopped'},
        {'type': 'qemu', 'vmid': 101, 'name': 'content-factory', 'status': 'running'},
        {'type': 'qemu', 'vmid': 999, 'template': 1},
    ])
    assert [x['id'] for x in result] == [101, 108]
    assert result[1]['status'] == 'stopped'
    assert result[1]['name'] == '_bad__name'
    assert 'password' not in json.dumps(result)


def test_stale_snapshot_is_not_served():
    collector = monitor.Collector()
    collector.data = {'guests': []}
    assert collector.snapshot() is None


def test_dashboard_uses_automatic_guest_cards():
    view = yaml.safe_load((ROOT / 'config/home-assistant/server_view.yaml').read_text())

    def guest_cards(cards):
        for card in cards:
            if card['type'] == 'custom:proxmox-guests-card':
                yield card
            yield from guest_cards(card.get('cards', []))

    found = list(guest_cards(view['cards']))
    assert [card['guest_type'] for card in found] == ['lxc', 'vm']
    for card in found:
        assert card['entity'] == 'sensor.proxmox_inventory'


def test_http_auth_path_and_stale_data():
    collector = monitor.Collector()
    server = ThreadingHTTPServer(('127.0.0.1', 0), monitor.handler_for(collector, 'x' * 40, '192.0.2.2'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/metrics'
    try:
        for path, auth, code in [(url, '', 401), (url + '?cmd=stop', 'Bearer ' + 'x' * 40, 404),
                                  (url, 'Bearer ' + 'x' * 40, 503)]:
            with pytest.raises(HTTPError) as error:
                urlopen(Request(path, headers={'Authorization': auth}), timeout=2)
            assert error.value.code == code
        collector.data = {'total_rapl_w': 21.5, 'guests': []}
        collector.updated = monitor.time.monotonic()
        with urlopen(Request(url, headers={'Authorization': 'Bearer ' + 'x' * 40}), timeout=2) as response:
            assert json.load(response)['total_rapl_w'] == 21.5
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
