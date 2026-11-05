#!/usr/bin/env python3
"""Read-only RAPL and guest-state endpoint; standard library only."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time


def read_energy(root=Path('/sys/class/powercap')):
    result = {}
    for path in sorted(root.glob('intel-rapl:*')):
        name = (path / 'name').read_text().strip()
        # Core/uncore are nested in package energy and must not be added again.
        if name.startswith('package-') or name == 'dram':
            result[path.name] = (name, int((path / 'energy_uj').read_text()),
                                 int((path / 'max_energy_range_uj').read_text()))
    return result


def power_watts(previous, current, elapsed):
    if not 0 < elapsed <= 30 or previous.keys() != current.keys():
        return None
    if not any(v[0].startswith('package-') for v in current.values()):
        return None
    if not any(v[0] == 'dram' for v in current.values()):
        return None
    energy = 0
    for key, (name, value, maximum) in current.items():
        old_name, old_value, old_maximum = previous[key]
        if (name != old_name or maximum != old_maximum or maximum <= 0
                or not 0 <= value < maximum or not 0 <= old_value < maximum):
            return None
        energy += (value - old_value) % maximum
    watts = energy / 1_000_000 / elapsed
    return round(watts, 2) if 0 <= watts <= 500 else None


def normalize_guests(resources):
    result = []
    for item in resources:
        if item.get('type') not in ('lxc', 'qemu') or item.get('template'):
            continue
        status = item.get('status', 'unknown')
        result.append({
            'id': int(item['vmid']),
            'type': 'lxc' if item['type'] == 'lxc' else 'vm',
            'name': re.sub(r'[^a-zA-Z0-9_.-]', '_', str(item.get('name', '')))[:128],
            'status': status if status in ('running', 'stopped', 'paused') else 'unknown',
        })
    return sorted(result, key=lambda item: item['id'])


def read_guests():
    process = subprocess.run(
        ['/usr/bin/pvesh', 'get', '/cluster/resources', '--type', 'vm',
         '--output-format', 'json'], check=True, capture_output=True,
        text=True, timeout=8,
    )
    resources = json.loads(process.stdout)
    if not isinstance(resources, list):
        raise ValueError('Invalid resource inventory')
    return normalize_guests(resources)


def read_media_storage(path=Path('/mnt/media-ssd')):
    # Never report the host root filesystem if the external SSD is unmounted.
    if not path.is_mount():
        return None
    stat = os.statvfs(path)
    total = stat.f_blocks * stat.f_frsize
    used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    reserved = (stat.f_bfree - stat.f_bavail) * stat.f_frsize
    if total <= 0 or min(used, free, reserved) < 0 or used + free <= 0:
        return None
    return {'guest_id': 104, 'total_bytes': total, 'used_bytes': used,
            'free_bytes': free, 'reserved_bytes': reserved,
            'used_percent': round(100 * used / (used + free), 1)}


class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.updated = 0

    def run(self):
        previous, previous_time = {}, 0
        guests, inventory_time = None, -100
        while True:
            started = time.monotonic()
            try:
                energy = read_energy()
                watts = power_watts(previous, energy, started - previous_time)
                previous, previous_time = energy, started
            except (OSError, ValueError):
                previous, previous_time, watts = {}, 0, None
            if started - inventory_time >= 30:
                try:
                    guests = read_guests()
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                    guests = None
                    print('Guest inventory unavailable', flush=True)
                inventory_time = started
            try:
                media_storage = read_media_storage()
            except (OSError, ValueError):
                media_storage = None
            data = {
                'sampled_at': datetime.now(timezone.utc).isoformat(),
                'total_rapl_w': watts,
                'power_scope': 'cpu_package_and_dram_only',
                'guests': guests,
                'media_storage': media_storage,
                'running_count': sum(g['status'] == 'running' for g in guests)
                if guests is not None else None,
                'guest_count': len(guests) if guests is not None else None,
            }
            with self.lock:
                self.data, self.updated = data, time.monotonic()
            time.sleep(max(0.1, 10 - (time.monotonic() - started)))

    def snapshot(self):
        with self.lock:
            if self.data is None or time.monotonic() - self.updated > 35:
                return None
            return dict(self.data)


def handler_for(collector, token, allowed_ip):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def do_GET(self):
            if self.client_address[0] not in (allowed_ip, '127.0.0.1'):
                self.send_error(403)
                return
            if not secrets.compare_digest(self.headers.get('Authorization', ''),
                                          'Bearer ' + token):
                self.send_error(401)
                return
            if self.path != '/metrics':
                self.send_error(404)
                return
            data = collector.snapshot()
            if data is None:
                self.send_error(503)
                return
            body = json.dumps(data, allow_nan=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    bind = str(ipaddress.IPv4Address(config['bind']))
    allowed = str(ipaddress.IPv4Address(config['ha_ip']))
    if len(config['token']) < 32:
        raise ValueError('Use a random token of at least 32 characters')
    collector = Collector()
    threading.Thread(target=collector.run, daemon=True).start()
    server = ThreadingHTTPServer((bind, 9120), handler_for(collector, config['token'], allowed))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == '__main__':
    main()
