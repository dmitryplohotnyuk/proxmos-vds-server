from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

STATE = Path('/var/lib/domain-router-integrations')
ROLE_FILE = Path('/etc/domain-router-integrations/role')
OVERRIDE = Path('/etc/systemd/system/cloudflared.service.d/90-domain-router.conf')
LEGACY_CONFIG = Path('/etc/cloudflared/config.yml')
MANAGED = Path('/etc/cloudflared/router-managed')
ROUTES = ['10.77.0.0/24', '192.168.0.222/32']
CHECK_UNIT = 'domain-router-cloudflare-check'
GUARD_UNIT = 'domain-router-cloudflare-rollback'


class IntegrationError(Exception):
    pass


def command(*args: str, timeout: int = 12) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        # Daemon output may contain credentials. Never return it to the UI.
        raise IntegrationError('Системная команда не выполнена. Проверьте состояние сервисов с root-доступом.')
    return result.stdout


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value).encode())


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


@contextmanager
def locked():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / 'lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegrationError('Уже выполняется другая операция. Обновите страницу позже.') from exc
        yield


class LocalConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/tailscale/tailscaled.sock')


def local_api(method: str, endpoint: str, payload: dict | None = None) -> dict:
    connection = LocalConnection('local-tailscaled.sock', timeout=4)
    try:
        connection.request(method, '/localapi/v0/' + endpoint,
                           body=json.dumps(payload) if payload is not None else None,
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = response.read(1048577)
        if response.status not in {200, 204} or len(body) > 1048576:
            raise IntegrationError('Tailscale не подтвердил операцию.')
        return json.loads(body) if body else {}
    finally:
        connection.close()


def safe_auth_url(value: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(
        r'https://login\.tailscale\.com/a/[A-Za-z0-9]+/?', value) else ''


def tailscale_status() -> dict:
    data = local_api('GET', 'status?peers=false')
    prefs = local_api('GET', 'prefs')
    profile = local_api('GET', 'profiles/current')
    node = data.get('Self') or {}
    tailnet = data.get('CurrentTailnet') or {}
    return {'state': data.get('BackendState', 'Unknown'),
            'online': bool(node.get('Online')),
            'hostname': prefs.get('Hostname') or node.get('HostName', ''),
            'dns_name': str(node.get('DNSName', '')).rstrip('.'),
            'ips': data.get('TailscaleIPs') or [],
            'routes': prefs.get('AdvertiseRoutes') or [],
            'tailnet': tailnet.get('MagicDNSSuffix', ''),
            'profile_present': bool(profile.get('ID')),
            'auth_url': safe_auth_url(data.get('AuthURL', ''))}


def tailscale_connect(role: str) -> dict:
    before = tailscale_status()
    if before['state'] in {'Running', 'Starting', 'NeedsMachineAuth'}:
        return before
    if before['state'] not in {'Stopped', 'NeedsLogin', 'NoState'}:
        raise IntegrationError('Неизвестное состояние Tailscale. Подключение не изменено.')
    patch = {'WantRunning': True, 'WantRunningSet': True}
    # Existing profiles keep their routes, SSH, DNS and identity preferences.
    if not before.get('profile_present', True):
        patch.update(Hostname='domain-router' if role == 'panel' else 'tailscale-gateway',
                     HostnameSet=True, CorpDNS=False, CorpDNSSet=True,
                     RouteAll=False, RouteAllSet=True,
                     AdvertiseRoutes=ROUTES if role == 'gateway' else [],
                     AdvertiseRoutesSet=True)
    if before['state'] == 'NoState':
        local_api('POST', 'start', {})
    local_api('PATCH', 'prefs', patch)
    if before['state'] in {'NeedsLogin', 'NoState'} and not before['auth_url']:
        local_api('POST', 'login-interactive', {})
    for _ in range(10):
        current = tailscale_status()
        if current['state'] in {'Running', 'NeedsMachineAuth'} or current['auth_url']:
            return current
        time.sleep(.2)
    return current


def valid_credentials(mode: str, secret: str) -> dict:
    if not isinstance(secret, str) or not 1 <= len(secret) <= 8192:
        raise IntegrationError('Укажите credentials JSON или connector token.')
    try:
        data = json.loads(secret) if mode == 'local' else json.loads(
            base64.b64decode(secret.strip(), validate=True))
        if mode == 'remote':
            if not isinstance(data, dict) or set(data) - {'a', 's', 't', 'e'} or data.get('e'):
                raise ValueError()
            data = dict(AccountTag=data['a'], TunnelSecret=data['s'], TunnelID=data['t'])
        elif mode != 'local' or not isinstance(data, dict) or set(data) - {
            'AccountTag', 'TunnelSecret', 'TunnelID', 'TunnelName', 'Endpoint'} or data.get('Endpoint'):
            raise ValueError()
        if any(not isinstance(data.get(key), str) for key in ['TunnelID', 'AccountTag', 'TunnelSecret']):
            raise ValueError()
        tunnel_id = str(uuid.UUID(data['TunnelID']))
        if not re.fullmatch(r'[a-fA-F0-9]{32}', data['AccountTag']):
            raise ValueError()
        if len(base64.b64decode(data['TunnelSecret'], validate=True)) != 32:
            raise ValueError()
        return dict(AccountTag=data['AccountTag'], TunnelSecret=data['TunnelSecret'], TunnelID=tunnel_id)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise IntegrationError('Некорректный формат ключа Tunnel. API token и cert.pem здесь не подходят.') from exc


def cf_revision() -> str:
    digest = hashlib.sha256()
    for path in [LEGACY_CONFIG, OVERRIDE, STATE / 'cloudflare.json']:
        digest.update(path.read_bytes() if path.exists() else b'absent')
    return digest.hexdigest()


def ready(port: int) -> bool:
    # Ignore proxy environment variables for the loopback health endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/ready', timeout=.4) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def cloudflare_status() -> dict:
    metadata = read_json(STATE / 'cloudflare.json')
    mode, tunnel_id = metadata.get('mode', 'local'), metadata.get('tunnel_id', '')
    if not metadata and LEGACY_CONFIG.exists():
        import yaml
        config = yaml.safe_load(LEGACY_CONFIG.read_text()) or {}
        try:
            tunnel_id = str(uuid.UUID(str(config.get('tunnel', ''))))
        except ValueError:
            tunnel_id = ''
    active = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', 'cloudflared'],
                            timeout=4, check=False).returncode == 0
    return {'mode': mode, 'tunnel_id': tunnel_id, 'configured': bool(tunnel_id),
            'active': active, 'connected': active and any(ready(p) for p in range(20241, 20246)),
            'dns_target': f'{tunnel_id}.cfargotunnel.com' if tunnel_id else '',
            'revision': cf_revision(), 'pending': (STATE / 'pending.json').exists(),
            'can_rollback': (STATE / 'last-backup.json').exists()}


def service_command(binary: str, version: Path, mode: str, tunnel_id: str, port: int) -> list[str]:
    args = [binary, '--no-autoupdate', '--config', str(version / 'config.json'),
            '--loglevel', 'warn', '--metrics', f'127.0.0.1:{port}', 'tunnel', 'run']
    return args + (['--token-file', str(version / 'connector-token')] if mode == 'remote' else [tunnel_id])


def owns_metrics(unit: str, port: int) -> bool:
    try:
        pid = int(command('/usr/bin/systemctl', 'show', unit, '--property=MainPID', '--value', timeout=4).strip())
        if pid <= 1:
            return False
        inodes = {line.split()[9] for line in Path('/proc/net/tcp').read_text().splitlines()[1:]
                  if line.split()[1] == f'0100007F:{port:04X}' and line.split()[3] == '0A'}
        return any(os.readlink(entry) in {f'socket:[{inode}]' for inode in inodes}
                   for entry in Path(f'/proc/{pid}/fd').iterdir())
    except (OSError, ValueError, IntegrationError):
        return False


def wait_ready(port: int, timeout: int = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if owns_metrics(CHECK_UNIT if port == 20246 else 'cloudflared', port) and ready(port):
            return True
        time.sleep(.4)
    return False


def snapshot() -> dict:
    return {'override': base64.b64encode(OVERRIDE.read_bytes()).decode() if OVERRIDE.exists() else None,
            'metadata': base64.b64encode((STATE / 'cloudflare.json').read_bytes()).decode()
            if (STATE / 'cloudflare.json').exists() else None,
            'was_active': subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', 'cloudflared'],
                                         timeout=4, check=False).returncode == 0}


def restore(backup: dict) -> None:
    for path, key in [(OVERRIDE, 'override'), (STATE / 'cloudflare.json', 'metadata')]:
        if backup[key] is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, base64.b64decode(backup[key]))
    command('/usr/bin/systemctl', 'daemon-reload')
    command('/usr/bin/systemctl', 'restart' if backup['was_active'] else 'stop', 'cloudflared')
    (STATE / 'pending.json').unlink(missing_ok=True)


def stop_guard() -> None:
    subprocess.run(['/usr/bin/systemctl', 'disable', '--now', GUARD_UNIT + '.timer'],
                   capture_output=True, timeout=8, check=False)


def apply_cloudflare(request: dict) -> dict:
    if set(request) != {'action', 'mode', 'secret', 'expected_revision'}:
        raise IntegrationError('Некорректные поля запроса.')
    current = cloudflare_status()
    if current['pending'] or current['revision'] != request['expected_revision']:
        raise IntegrationError('Конфигурация изменилась или восстанавливается. Обновите страницу.')
    mode = request['mode']
    credentials = valid_credentials(mode, request['secret'])
    tunnel_id = credentials['TunnelID']
    if mode == 'remote' and current['mode'] == 'local' and current['tunnel_id'] == tunnel_id:
        raise IntegrationError('Нельзя незаметно преобразовать действующий локальный Tunnel. Используйте новый remotely-managed Tunnel или credentials JSON текущего.')
    binary = shutil.which('cloudflared', path='/usr/local/bin:/usr/bin')
    if binary not in {'/usr/local/bin/cloudflared', '/usr/bin/cloudflared'}:
        raise IntegrationError('cloudflared не установлен в стандартном расположении.')
    version = MANAGED / uuid.uuid4().hex
    version.mkdir(mode=0o700, parents=True)
    configuration = {'ingress': [{'service': 'http://127.0.0.1:8080'}]} if mode == 'local' else {}
    if mode == 'local':
        configuration.update(tunnel=tunnel_id, **{'credentials-file': str(version / 'credentials.json')})
        write_json(version / 'credentials.json', credentials)
    else:
        atomic_write(version / 'connector-token', request['secret'].strip().encode())
    write_json(version / 'config.json', configuration)
    candidate = service_command(binary, version, mode, tunnel_id, 20246)
    # A separate, time-limited connector checks authentication before replacement.
    checked = False
    try:
        command('/usr/bin/systemd-run', '--quiet', '--collect', '--unit=' + CHECK_UNIT,
                '--property=RuntimeMaxSec=30', '--property=StandardOutput=null',
                '--property=StandardError=null', '--', *candidate)
        if not wait_ready(20246):
            raise IntegrationError('Новый Tunnel не подключился. Прежняя конфигурация сохранена.')
        checked = True
    finally:
        subprocess.run(['/usr/bin/systemctl', 'stop', CHECK_UNIT],
                       capture_output=True, timeout=8, check=False)
        if not checked:
            shutil.rmtree(version)
    backup = snapshot()
    write_json(STATE / 'pending.json', backup)
    # Independent timer restores the original service if this process is killed.
    try:
        command('/usr/bin/systemctl', 'enable', '--now', GUARD_UNIT + '.timer')
        main_command = service_command(binary, version, mode, tunnel_id, 20241)
        atomic_write(OVERRIDE, ('[Service]\nExecStart=\nExecStart=' + ' '.join(main_command) + '\n').encode())
        command('/usr/bin/systemctl', 'daemon-reload')
        command('/usr/bin/systemctl', 'restart', 'cloudflared')
        if not wait_ready(20241):
            raise IntegrationError('Новый connector не прошёл проверку. Восстановлена прежняя конфигурация.')
        write_json(STATE / 'cloudflare.json', {'mode': mode, 'tunnel_id': tunnel_id, 'version': version.name})
        write_json(STATE / 'last-backup.json', backup)
        (STATE / 'pending.json').unlink()
        stop_guard()
    except Exception:
        restore(backup)
        stop_guard()
        raise
    return cloudflare_status()


def handle(request: dict, role: str) -> dict:
    if not isinstance(request, dict):
        raise IntegrationError('Некорректный запрос.')
    action = request.get('action')
    if action in {'status', 'tailscale-status'} and set(request) == {'action'}:
        result = {'tailscale': tailscale_status()}
        if action == 'status' and role == 'panel':
            result['cloudflare'] = cloudflare_status()
        return result
    with locked():
        if action == 'tailscale-connect' and set(request) == {'action'}:
            return {'tailscale': tailscale_connect(role)}
        if action == 'panel-serve' and set(request) == {'action'} and role == 'panel':
            if tailscale_status()['state'] != 'Running':
                raise IntegrationError('Сначала подключите Tailscale.')
            command('/usr/bin/tailscale', 'serve', '--bg', 'http://127.0.0.1:8090')
            return {'tailscale': tailscale_status()}
        if action == 'cloudflare-apply' and role == 'panel':
            return {'cloudflare': apply_cloudflare(request)}
        if action == 'cloudflare-rollback' and role == 'panel' and set(request) == {'action', 'expected_revision'}:
            if cloudflare_status()['revision'] != request['expected_revision'] or (STATE / 'pending.json').exists():
                raise IntegrationError('Конфигурация изменилась. Обновите страницу.')
            backup = read_json(STATE / 'last-backup.json')
            if not backup:
                raise IntegrationError('Нет сохранённой предыдущей конфигурации.')
            restore(backup)
            (STATE / 'last-backup.json').unlink(missing_ok=True)
            return {'cloudflare': cloudflare_status()}
    raise IntegrationError('Операция не разрешена.')


def main():
    os.environ['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    try:
        if os.geteuid() != 0:
            raise IntegrationError('Требуется root.')
        role = ROLE_FILE.read_text().strip()
        if role not in {'panel', 'gateway'}:
            raise IntegrationError('Неизвестная роль узла.')
        if sys.argv[1:] == ['--rollback'] and role == 'panel':
            with locked():
                backup = read_json(STATE / 'pending.json')
                if backup:
                    restore(backup)
                stop_guard()
            return
        if sys.argv[1:]:
            raise IntegrationError('Аргументы не разрешены.')
        data = sys.stdin.buffer.read(16385)
        if len(data) > 16384:
            raise IntegrationError('Слишком большой запрос.')
        print(json.dumps(handle(json.loads(data), role), ensure_ascii=False))
    except IntegrationError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    except Exception:
        print(json.dumps({'error': 'Операция не подтверждена. Обновите состояние; при замене Tunnel действует автоматическое восстановление.'}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
