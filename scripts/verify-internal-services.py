#!/usr/bin/env python3
"""Run on a Tailscale client with Proxmox root SSH access; restores all changes."""
import argparse
from html.parser import HTMLParser
import json
import shlex
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.forms = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'form':
            self.current = {} if attrs.get('action') == '/internal/toggle' else None
        if tag == 'input' and self.current is not None and attrs.get('type') == 'hidden':
            self.current[attrs['name']] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form' and self.current is not None:
            self.forms.append(self.current)
            self.current = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='root@192.168.0.176')
    parser.add_argument('--ssh-socket')
    parser.add_argument('--base-url', default='https://domain-router.tail6ace7f.ts.net')
    args = parser.parse_args()
    ssh = ['ssh', '-o', 'BatchMode=yes']
    if args.ssh_socket:
        ssh += ['-S', args.ssh_socket]
    ssh += [args.host]

    def remote(command, data=None):
        result = subprocess.run(ssh + [shlex.join(command)], input=data, text=True,
                                capture_output=True, check=True, timeout=180)
        return result.stdout

    create_code = """from domain_router_ui.database import Database
from pathlib import Path
import json
db=Database(Path('/var/lib/domain-router-ui/app.db'))
with db.connect() as c:
    uid=c.execute('SELECT id FROM users WHERE disabled=0 ORDER BY id LIMIT 1').fetchone()[0]
token,session=db.create_session(uid,1)
print(json.dumps({'token':token}))
"""
    python = ['pct', 'exec', '100', '--', 'runuser', '-u', 'domain-router-ui', '--',
              '/opt/domain-router-ui/venv/bin/python', '-c']
    token = json.loads(remote(python + [create_code]))['token']
    # Token remains only in memory and is revoked even when a test fails.
    cookie = 'dr_session=' + token

    def request(path, payload=None):
        data = urllib.parse.urlencode(payload).encode() if payload is not None else None
        req = urllib.request.Request(args.base_url + path, data=data, headers={'Cookie': cookie})
        with urllib.request.urlopen(req, timeout=35) as response:
            return response.read().decode()

    def jellyfin_form():
        forms = Forms()
        forms.feed(request('/internal'))
        return next(form for form in forms.forms if form['id'].startswith('lxc:104:') and form['id'].endswith(':8096'))

    def http_ok(url):
        with urllib.request.urlopen(url, timeout=10) as response:
            assert response.status == 200, url

    disabled = False
    try:
        form = jellyfin_form()
        assert form['expected_enabled'] == 'true', 'Jellyfin must start enabled'
        try:
            request('/internal/toggle', {**form, 'csrf_token': 'invalid'})
        except urllib.error.HTTPError as error:
            assert error.code == 400
        else:
            raise AssertionError('Invalid CSRF accepted')
        http_ok('http://10.77.0.14:8096/System/Info/Public')
        request('/internal/toggle', form)
        disabled = True
        assert jellyfin_form()['expected_enabled'] == 'false'
        try:
            with socket.create_connection(('10.77.0.14', 8096), timeout=3):
                raise AssertionError('Disabled VPN port is reachable')
        except (OSError, TimeoutError):
            pass
        http_ok('http://192.168.0.156:8096/System/Info/Public')
        http_ok('http://10.77.0.15:8090/')
        print('VPN OFF blocks Jellyfin; LAN and other VPN services remain available')
        remote(['systemctl', 'start', 'service-inventory-sync.service'])
        assert jellyfin_form()['expected_enabled'] == 'false', 'Refresh undid toggle'
        print('Automatic discovery preserves OFF state')
    finally:
        try:
            if disabled:
                form = jellyfin_form()
                if form['expected_enabled'] == 'false':
                    request('/internal/toggle', form)
                http_ok('http://10.77.0.14:8096/System/Info/Public')
                print('VPN ON restored and verified')
        finally:
            remote(python + ["from domain_router_ui.database import Database; from pathlib import Path; import sys; Database(Path('/var/lib/domain-router-ui/app.db')).delete_session(sys.stdin.read())"], token)
            print('Temporary test session revoked')


if __name__ == '__main__':
    main()
