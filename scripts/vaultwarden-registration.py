#!/usr/bin/env python3
"""Bounded first-account signup window; never handle a master password."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

BASE = Path('/opt/vaultwarden')
WINDOW = Path('/etc/vaultwarden/registration-window.json')


def accounts():
    db = BASE / 'data/db.sqlite3'
    if not db.exists():
        return 0
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as connection:
        return connection.execute('SELECT COUNT(*) FROM users WHERE password_hash != ?', ('',)).fetchone()[0]


def configure(enabled):
    path = BASE / 'vaultwarden.env'
    text = path.read_text()
    value = 'SIGNUPS_ALLOWED=' + str(enabled).lower()
    updated = '\n'.join(value if line.startswith('SIGNUPS_ALLOWED=') else line for line in text.splitlines()) + '\n'
    if updated != text:
        temporary = path.with_suffix('.new')
        temporary.write_text(updated)
        temporary.chmod(0o600)
        temporary.replace(path)
        subprocess.run(['systemctl', 'restart', 'vaultwarden'], check=True)


def main():
    if os.geteuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in ('open', 'close', 'check', 'status'):
        raise SystemExit('Usage (root): vaultwarden-registration open|close|check|status')
    with open('/run/vaultwarden-registration.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        action = sys.argv[1]
        count = accounts()
        if action == 'open':
            if count:
                raise SystemExit('An account already exists; refusing to reopen first-account registration.')
            status = json.loads(subprocess.check_output(['tailscale', 'status', '--json']))
            if status.get('BackendState') != 'Running' or 'DOMAIN=https://' not in (BASE / 'vaultwarden.env').read_text():
                raise SystemExit('Configure private Tailscale HTTPS before opening registration.')
            WINDOW.write_text(json.dumps({'expires_at': time.time() + 900}))
            WINDOW.chmod(0o600)
            configure(True)
            print('First-account registration open for at most 15 minutes, private tailnet only.')
        elif action == 'close':
            WINDOW.unlink(missing_ok=True)
            configure(False)
        elif action == 'check':
            try:
                expires = json.loads(WINDOW.read_text())['expires_at']
            except (FileNotFoundError, ValueError, KeyError):
                expires = 0
            if count or expires <= time.time():
                configure(False)
                WINDOW.unlink(missing_ok=True)
        else:
            print(json.dumps({'accounts': count, 'registration_open':
                              'SIGNUPS_ALLOWED=true' in (BASE / 'vaultwarden.env').read_text()}))


if __name__ == '__main__':
    main()
