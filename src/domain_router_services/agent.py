from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

STATE_DIR = Path('/var/lib/service-inventory')
PRIVATE_NET = ipaddress.ip_network('10.77.0.0/24')
LAN_NET = ipaddress.ip_network('192.168.0.0/24')
EXCLUDED_GUESTS = {100, 107}
BOOTSTRAP_PORTS = {101: {22, 80, 443}, 102: {8123}, 103: {22},
                   104: {22, 8096}, 105: {22, 8090, 9080}, 106: {22, 53, 3000}}
WEB_PORTS = {80, 443, 3000, 4357, 5000, 6054, 8000, 8080, 8081,
             8090, 8096, 8123, 8443, 8485, 8884, 9000, 9080}
PORT_NAMES = {22: 'SSH', 53: 'DNS', 80: 'HTTP', 443: 'HTTPS', 139: 'SMB',
              445: 'SMB', 1883: 'MQTT', 8883: 'MQTT TLS', 8096: 'Jellyfin',
              8123: 'Home Assistant', 4357: 'HA Observer', 42069: 'Torrent peers'}


def run(*args: str, timeout: int = 12) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {(result.stderr or result.stdout)[:200]}')
    return result.stdout


def atomic_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked():
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE_DIR / 'lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def read_state() -> dict:
    path = STATE_DIR / 'inventory.json'
    return json.loads(path.read_text()) if path.exists() else {
        'services': [], 'aliases': {}, 'revision': 0, 'updated_at': None, 'errors': []}


def valid_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address in PRIVATE_NET or address in LAN_NET
    except ValueError:
        return False


def parse_listeners(output: str) -> list[tuple[str, int, str]]:
    listeners = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[0] not in {'tcp', 'udp'}:
            continue
        address, _, port_text = fields[4].rpartition(':')
        if not port_text.isdigit():
            continue
        port = int(port_text)
        address = address.strip('[]')
        if not 1 <= port <= 65535 or (fields[0] == 'udp' and port != 53):
            continue
        if address not in {'*', '0.0.0.0', '::'} and not valid_address(address):
            continue
        listeners.add((fields[0], port, address))
    return sorted(listeners)


def guest_snapshot(guest: dict) -> tuple[list[str], list[tuple[str, int, str]]]:
    vmid = str(int(guest['vmid']))
    if guest['kind'] == 'lxc':
        addresses = json.loads(run('pct', 'exec', vmid, '--', 'ip', '-j', '-4', 'addr'))
        ips = [entry['local'] for interface in addresses
               for entry in interface.get('addr_info', []) if entry.get('family') == 'inet']
        output = run('pct', 'exec', vmid, '--', 'ss', '-H', '-lntu')
    else:
        addresses = json.loads(run('qm', 'guest', 'cmd', vmid, 'network-get-interfaces'))
        ips = [entry['ip-address'] for interface in addresses
               for entry in interface.get('ip-addresses', [])
               if entry.get('ip-address-type') == 'ipv4']
        # The command is constant, never derived from a browser request.
        result = json.loads(run('qm', 'guest', 'exec', vmid, '--', '/bin/sh', '-c', 'ss -H -lntu'))
        if not result.get('exited') or result.get('exitcode') or result.get('out-truncated'):
            raise RuntimeError('Guest port inventory unavailable')
        output = result.get('out-data', '')
    return sorted({ip for ip in ips if valid_address(ip)}), parse_listeners(output)


def service_name(guest: dict, port: int) -> str:
    name = guest['name'].lower()
    if port == 3000 and 'adguard' in name:
        return 'AdGuard Home'
    if port == 8090 and 'torrserver' in name:
        return 'TorrServer'
    if port == 9080 and 'torrserver' in name:
        return 'Torrent integration'
    return PORT_NAMES.get(port, f'TCP {port}')


def merge_snapshot(previous: dict, guests: list[dict], snapshots: dict) -> dict:
    old = {item['id']: item for item in previous['services']}
    aliases = dict(previous.get('aliases', {}))
    used = {str(ip) for ips, _ in snapshots.values() for ip in ips}
    used.update(aliases.values())
    used.update(item['address'] for item in old.values())
    used.update(ip for guest in guests for ip in guest.get('reserved_ips', []))
    services, errors = [], []
    for guest in guests:
        key = f"{guest['kind']}:{int(guest['vmid'])}"
        snapshot = snapshots.get(key)
        if snapshot is None:
            if guest['status'] == 'running':
                errors.append(f"{guest['name']}: guest inventory unavailable")
            for item in old.values():
                if item['guest_key'] == key and item['guest_identity'] == guest['identity']:
                    services.append({**item, 'reachable': False, 'stale': True,
                                     'guest_status': guest['status']})
            continue
        ips, listeners = snapshot
        private = next((ip for ip in ips if ipaddress.ip_address(ip) in PRIVATE_NET), None)
        lan = next((ip for ip in ips if ipaddress.ip_address(ip) in LAN_NET), None)
        if not ips:
            continue
        # Existing HA /32 remains compatible. Other LAN-only guests get stable
        # virtual addresses, translated at the gateway without changing guest NICs.
        if private:
            public_address = private
        elif guest['vmid'] == 102 and lan == '192.168.0.222':
            public_address = lan
        else:
            alias_key = f"{key}:{guest['identity']}"
            if alias_key not in aliases:
                aliases[alias_key] = next((f'10.77.0.{number}' for number in range(128, 240)
                                     if f'10.77.0.{number}' not in used), '')
                if not aliases[alias_key]:
                    errors.append(f"{guest['name']}: virtual address pool exhausted")
                    continue
                used.add(aliases[alias_key])
            public_address = aliases[alias_key]
            # Never redirect an address newly assigned to a real guest.
            if any(public_address in candidate[0] for candidate in snapshots.values()):
                errors.append(f"{guest['name']}: virtual address collision")
                continue
        ports = sorted({port for _, port, _ in listeners})
        for port in ports:
            bindings = [item for item in listeners if item[1] == port]
            protocols = sorted({item[0] for item in bindings})
            choices = [private, lan]
            target = next((ip for ip in choices if ip and all(
                any(proto == protocol and bound in {'*', '0.0.0.0', '::', ip}
                    for proto, _, bound in bindings) for protocol in protocols)), None)
            if not target:
                continue
            identifier = f"{key}:{guest['identity']}:{port}"
            enabled = old.get(identifier, {}).get('enabled',
                previous.get('updated_at') is None and port in BOOTSTRAP_PORTS.get(guest['vmid'], set()))
            scheme = 'https' if port in {443, 8443} else 'http'
            services.append({
                'id': identifier, 'guest_key': key, 'guest_identity': guest['identity'], 'vmid': guest['vmid'],
                'kind': guest['kind'], 'guest_name': guest['name'], 'guest_status': guest['status'],
                'name': service_name(guest, port), 'port': port, 'protocols': protocols,
                'lan_address': lan, 'address': public_address, 'target': target,
                'url': f'{scheme}://{public_address}:{port}' if port in WEB_PORTS else None,
                'lan_url': f'{scheme}://{lan}:{port}' if lan and port in WEB_PORTS else None,
                'ssh': f'ssh root@{public_address}' if port == 22 else None,
                'enabled': bool(enabled), 'reachable': None, 'stale': False,
            })
    found = {item['id'] for item in services}
    identities = {f"{guest['kind']}:{guest['vmid']}": guest['identity'] for guest in guests}
    for item in old.values():
        if item['id'] not in found and identities.get(item['guest_key']) == item['guest_identity']:
            services.append({**item, 'reachable': False, 'stale': True})
    return {**previous, 'aliases': aliases, 'services': sorted(services, key=lambda item: (
        not item['enabled'], item['guest_name'].lower(), item['port'])), 'errors': errors,
        'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'revision': previous['revision'] + 1}


def firewall_text(services: list[dict]) -> str:
    lines = ['table inet tailscale_gateway', 'flush table inet tailscale_gateway',
             'table inet tailscale_gateway {',
             ' chain prerouting { type nat hook prerouting priority dstnat; policy accept;']
    for item in services:
        if not valid_address(item['target']) or not valid_address(item['address']):
            raise ValueError('Invalid inventory address')
        if not 1 <= int(item['port']) <= 65535:
            raise ValueError('Invalid inventory port')
        for proto in item['protocols']:
            if proto not in {'tcp', 'udp'}:
                raise ValueError('Invalid protocol')
            if item['address'] != item['target']:
                lines.append(f' iifname "tailscale0" ip daddr {item["address"]} {proto} dport {item["port"]} dnat ip to {item["target"]}')
    lines.extend([' }', ' chain forward { type filter hook forward priority 10; policy drop;',
                  ' ct state invalid drop'])
    # Denies precede established flows: switching OFF also stops existing streams.
    for item in services:
        if not item['enabled'] or item['stale'] or item['guest_status'] != 'running':
            for proto in item['protocols']:
                lines.append(f' iifname "tailscale0" ip daddr {item["target"]} {proto} dport {item["port"]} counter drop')
    lines.append(' ct state established,related counter accept')
    for item in services:
        if item['enabled'] and not item['stale'] and item['guest_status'] == 'running':
            for proto in item['protocols']:
                interface = 'lan0' if ipaddress.ip_address(item['target']) in PRIVATE_NET else 'eth0'
                lines.append(f' iifname "tailscale0" oifname "{interface}" ip daddr {item["target"]} {proto} dport {item["port"]} counter accept')
    for address in sorted({item['target'] for item in services if item['enabled'] and not item['stale']}):
        lines.append(f' iifname "tailscale0" ip daddr {address} icmp type echo-request counter accept')
    lines.extend([' counter drop', ' }', '}'])
    return '\n'.join(lines) + '\n'


def apply_firewall(services: list[dict]) -> None:
    content = firewall_text(services)
    with tempfile.NamedTemporaryFile('w', dir=STATE_DIR, delete=False) as stream:
        stream.write(content)
        candidate = Path(stream.name)
    try:
        run('pct', 'push', '107', str(candidate), '/etc/tailscale-gateway.candidate.nft', '--perms', '0600')
        run('pct', 'exec', '107', '--', 'nft', '--check', '-f', '/etc/tailscale-gateway.candidate.nft')
        # Apply first, commit boot configuration second. On failure keep the old
        # effective rules and never report the new state as successfully enabled.
        run('pct', 'exec', '107', '--', 'nft', '-f', '/etc/tailscale-gateway.candidate.nft')
        try:
            run('pct', 'exec', '107', '--', 'mv', '/etc/tailscale-gateway.candidate.nft', '/etc/tailscale-gateway.nft')
        except Exception:
            run('pct', 'exec', '107', '--', 'nft', '-f', '/etc/tailscale-gateway.nft')
            raise
    finally:
        candidate.unlink(missing_ok=True)


GATEWAY_PROBE = '''import json, socket, sys
from concurrent.futures import ThreadPoolExecutor
def probe(item):
    if item['stale'] or item['guest_status'] != 'running':
        return False
    if 'tcp' not in item['protocols']:
        return None
    try:
        with socket.create_connection((item['target'], item['port']), timeout=0.7):
            return True
    except OSError:
        return False
with ThreadPoolExecutor(max_workers=8) as pool:
    print(json.dumps(list(pool.map(probe, json.loads(sys.argv[1])))))
'''


def probe_services(services: list[dict]) -> list[bool | None]:
    # Probe from the same gateway address guest firewalls already trust.
    results = json.loads(run('pct', 'exec', '107', '--', 'python3', '-c',
                             GATEWAY_PROBE, json.dumps(services), timeout=20))
    if len(results) != len(services) or any(value is not None and type(value) is not bool for value in results):
        raise ValueError('Invalid gateway probe response')
    return results


def synchronize() -> dict:
    node = socket.gethostname().split('.')[0]
    guests = []
    for kind, api in [('lxc', 'lxc'), ('qemu', 'qemu')]:
        for guest in json.loads(run('pvesh', 'get', f'/nodes/{node}/{api}', '--output-format', 'json')):
            if int(guest['vmid']) not in EXCLUDED_GUESTS:
                configuration = json.loads(run('pvesh', 'get', f'/nodes/{node}/{api}/{int(guest["vmid"])}/config', '--output-format', 'json'))
                identity_value = configuration.get('smbios1', '') or configuration.get('net0', '')
                mac = re.search(r'(?:hwaddr|virtio|e1000|rtl8139)=([0-9A-Fa-f:]{17})', identity_value)
                identity = hashlib.sha256((mac.group(1).lower() if mac else identity_value).encode()).hexdigest()[:12]
                reserved = re.findall(r'\bip=(\d+\.\d+\.\d+\.\d+)/', ' '.join(str(value) for key, value in configuration.items() if key.startswith(('net', 'ipconfig'))))
                guests.append({'kind': kind, 'vmid': int(guest['vmid']),
                               'name': guest.get('name', str(guest['vmid'])), 'status': guest['status'],
                               'identity': identity, 'reserved_ips': reserved})
    snapshots = {}
    for guest in guests:
        if guest['status'] == 'running':
            try:
                snapshots[f"{guest['kind']}:{guest['vmid']}"] = guest_snapshot(guest)
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
                pass
    # Snapshot collection is outside the lock. Merge with current toggle state,
    # not an earlier copy, so timer refresh cannot undo an operator's decision.
    with locked():
        previous = read_state()
        state = merge_snapshot(previous, guests, snapshots)
        if previous['updated_at'] is None and state['errors']:
            raise RuntimeError('Initial inventory incomplete: ' + '; '.join(state['errors']))
        results = probe_services(state['services'])
        for item, reachable in zip(state['services'], results):
            item['reachable'] = reachable
        apply_firewall(state['services'])
        try:
            atomic_json(STATE_DIR / 'inventory.json', state)
        except Exception:
            if previous['updated_at'] is not None:
                apply_firewall(previous['services'])
            raise
        return state


def handle_request(request: dict) -> dict:
    if isinstance(request, dict) and request.get('action') in {
        'integrations-status', 'integration-action'}:
        return integration_request(request)
    if not isinstance(request, dict) or request.get('action') not in {'list', 'toggle'}:
        raise ValueError('Invalid request')
    with locked():
        state = read_state()
        if request['action'] == 'list':
            return state
        if set(request) != {'action', 'id', 'enabled', 'expected_enabled'}:
            raise ValueError('Invalid toggle fields')
        if type(request['enabled']) is not bool or type(request['expected_enabled']) is not bool:
            raise ValueError('Invalid toggle state')
        item = next((item for item in state['services'] if item['id'] == request['id']), None)
        if item is None:
            raise ValueError('Unknown service; refresh inventory')
        if item['enabled'] != request['expected_enabled']:
            raise ValueError('Service changed in another session; refresh the page')
        if item['stale'] and request['enabled']:
            raise ValueError('Cannot enable a stale service; wait for successful discovery')
        previous_enabled = item['enabled']
        item['enabled'] = request['enabled']
        apply_firewall(state['services'])
        state['revision'] += 1
        try:
            atomic_json(STATE_DIR / 'inventory.json', state)
        except Exception:
            item['enabled'] = previous_enabled
            apply_firewall(state['services'])
            raise
        return {'id': item['id'], 'enabled': item['enabled'], 'revision': state['revision']}


def integration_call(target: str, payload: dict) -> dict:
    guest, interpreter = {
        'panel': ('100', '/opt/domain-router-ui/venv/bin/python'),
        'gateway': ('107', '/usr/bin/python3'),
    }[target]
    result = subprocess.run(['pct', 'exec', guest, '--', interpreter,
                             '/opt/domain-router-integrations/agent.py'],
                            input=json.dumps(payload), capture_output=True,
                            text=True, timeout=70, check=False)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('Integration agent unavailable') from exc
    if result.returncode or response.get('error'):
        raise ValueError(str(response.get('error', 'Integration operation failed'))[:300])
    return response


def integration_request(request: dict) -> dict:
    if request == {'action': 'integrations-status'}:
        result = {'errors': []}
        for target in ['panel', 'gateway']:
            try:
                response = integration_call(target, {'action': 'status' if target == 'panel' else 'tailscale-status'})
                result[target] = response['tailscale']
                if target == 'panel':
                    result['cloudflare'] = response['cloudflare']
            except Exception:
                result['errors'].append(target)
        return result
    if set(request) != {'action', 'target', 'operation'} or request['target'] not in {'panel', 'gateway'}:
        raise ValueError('Invalid integration target')
    operation = request['operation']
    if not isinstance(operation, dict):
        raise ValueError('Invalid integration operation')
    action = operation.get('action')
    fields = {
        'tailscale-connect': {'action'}, 'panel-serve': {'action'},
        'cloudflare-apply': {'action', 'mode', 'secret', 'expected_revision'},
        'cloudflare-rollback': {'action', 'expected_revision'},
    }
    if action not in fields or set(operation) != fields[action]:
        raise ValueError('Invalid integration fields')
    if request['target'] == 'gateway' and action != 'tailscale-connect':
        raise ValueError('Gateway operation forbidden')
    return integration_call(request['target'], operation)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync', action='store_true')
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise PermissionError('Root required')
        if args.sync:
            state = synchronize()
            print(json.dumps({'services': len(state['services']), 'errors': state['errors']}))
        else:
            data = sys.stdin.buffer.read(16385)
            if len(data) > 16384:
                raise ValueError('Request too large')
            print(json.dumps(handle_request(json.loads(data)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:300]}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
