"""Read-only status UI with one fixed, isolated backup-start command."""
import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import secrets
import socket
import socketserver
import struct
import time
from urllib.parse import urlsplit

from .common import STATE, command, read_json

CONTROL = '/run/proxmox-usb-backup-control/control.sock'
STATIC = Path(__file__).parent / 'static'
GUIDE = STATIC / 'recovery-guide.md'


def csrf_token(secret, identity, stamp=None):
    stamp = int(time.time()) if stamp is None else stamp
    body = f'{stamp}:{identity}'.encode()
    return str(stamp) + '.' + hmac.new(secret, body, hashlib.sha256).hexdigest()


def valid_csrf(secret, identity, token):
    try:
        stamp = int(token.split('.', 1)[0])
        return (0 <= time.time() - stamp <= 1800
                and hmac.compare_digest(token, csrf_token(secret, identity, stamp)))
    except (TypeError, ValueError, AttributeError):
        return False


def start_backup():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(CONTROL)
        client.sendall(b'{"action":"start"}\n')
        reply = json.loads(client.recv(2048))
        if not reply.get('ok'):
            raise RuntimeError('Backup start was rejected')


def ui_handler(config, secret):
    allowed = {name.lower() for name in config['allowed_logins']}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(8)

        def respond(self, status, body, content_type='application/json; charset=utf-8'):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def identity(self):
            identity = self.headers.get('Tailscale-User-Login', '').strip().lower()
            if identity not in allowed:
                self.respond(403, {'error': 'Access requires an authorized Tailscale identity'})
                return None
            return identity

        def do_GET(self):
            identity = self.identity()
            if not identity:
                return
            path = urlsplit(self.path).path
            if path == '/api/status':
                self.respond(200, dict(disk=read_json(STATE / 'disk.json', {}),
                                       job=read_json(STATE / 'status.json', {}),
                                       csrf=csrf_token(secret, identity)))
                return
            if path == '/recovery-guide':
                self.respond(200, GUIDE.read_bytes(), 'text/plain; charset=utf-8')
                return
            files = {'/': ('index.html', 'text/html; charset=utf-8')}
            for file in STATIC.iterdir():
                if file.is_file() and file.suffix in ('.css', '.js', '.svg'):
                    mime = {'.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml'}[file.suffix]
                    files['/assets/' + file.name] = (file.name, mime + '; charset=utf-8')
            if path not in files:
                self.respond(404, {'error': 'Not found'})
                return
            file, content_type = files[path]
            self.respond(200, (STATIC / file).read_bytes(), content_type)

        def do_POST(self):
            identity = self.identity()
            if not identity:
                return
            if (urlsplit(self.path).path != '/api/run' or self.headers.get('Origin') != config['origin']
                    or not valid_csrf(secret, identity, self.headers.get('X-CSRF-Token', ''))):
                self.respond(403, {'error': 'Request verification failed'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 128:
                    raise ValueError('Invalid body size')
                body = self.rfile.read(length)
                if json.loads(body or b'{}') != {}:
                    raise ValueError('No command parameters are accepted')
                start_backup()
            except (OSError, ValueError, RuntimeError) as error:
                self.respond(409, {'error': str(error)[:200]})
                return
            self.respond(202, {'ok': True})

        def log_message(self, *_args):
            pass

    return Handler


def control_server():
    if os.geteuid() != 0:
        raise SystemExit('Control service must run as root')
    allowed_uid = pwd.getpwnam('pve-backup-ui').pw_uid

    class Control(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            try:
                if uid not in (0, allowed_uid):
                    raise ValueError('Peer is not authorized')
                request = self.rfile.readline(129)
                if len(request) > 128 or json.loads(request) != {'action': 'start'}:
                    raise ValueError('Only the fixed start operation is permitted')
                command('systemctl', 'start', '--no-block', 'proxmox-usb-backup.service')
                self.wfile.write(b'{"ok":true}\n')
            except (OSError, ValueError, RuntimeError):
                self.wfile.write(b'{"ok":false}\n')

    Path(CONTROL).unlink(missing_ok=True)
    with socketserver.UnixStreamServer(CONTROL, Control) as server:
        os.chmod(CONTROL, 0o660)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['serve', 'control'])
    args = parser.parse_args()
    if args.action == 'control':
        control_server()
        return
    config = read_json('/etc/proxmox-usb-backup/ui.json')
    server = ThreadingHTTPServer(('127.0.0.1', 9130), ui_handler(config, secrets.token_bytes(32)))
    server.serve_forever()


if __name__ == '__main__':
    main()
