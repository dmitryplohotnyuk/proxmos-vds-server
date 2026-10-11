#!/usr/bin/env python3
"""Explicit owner-requested root SSH setup; password is accepted only on stdin."""
import argparse
import json
import subprocess
import sys


def guest(kind, identifier, command, stdin=None):
    args = (['pct', 'exec', str(identifier), '--'] if kind == 'lxc' else
            ['qm', 'guest', 'exec', str(identifier), '--timeout', '30',
             *(['--pass-stdin', '1'] if stdin is not None else []), '--'])
    result = subprocess.run(args + command, input=stdin, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError('Guest command failed; inspect guest locally (output suppressed)')
    if kind == 'qemu':
        response = json.loads(result.stdout)
        if not response.get('exited') or response.get('exitcode') != 0:
            raise RuntimeError('Guest agent command did not complete successfully (output suppressed)')


SETUP = r'''
set -eu
test -x /usr/sbin/sshd
test -f /etc/ssh/sshd_config
/usr/sbin/sshd -t
backup=/root/ssh-access-before-password-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 0700 "$backup"
cp -p /etc/ssh/sshd_config "$backup/sshd_config"
printf '%s\n' "$backup" > /run/domain-router-ssh-backup
chmod 0600 /run/domain-router-ssh-backup
if grep -q '^# BEGIN DOMAIN ROUTER ROOT SSH$' /etc/ssh/sshd_config; then
  echo 'Managed block already exists; refusing to overwrite' >&2
  exit 1
fi
printf '\n# BEGIN DOMAIN ROUTER ROOT SSH\nMatch User root\n    PermitRootLogin yes\n    PasswordAuthentication yes\n    KbdInteractiveAuthentication no\n# END DOMAIN ROUTER ROOT SSH\n' >> /etc/ssh/sshd_config
if ! /usr/sbin/sshd -t; then cp -p "$backup/sshd_config" /etc/ssh/sshd_config; exit 1; fi
settings=$(/usr/sbin/sshd -T -C user=root,host=localhost,addr=127.0.0.1)
if ! printf '%s\n' "$settings" | grep -qx 'permitrootlogin yes' || ! printf '%s\n' "$settings" | grep -qx 'passwordauthentication yes'; then
  cp -p "$backup/sshd_config" /etc/ssh/sshd_config
  exit 1
fi
'''

ROLLBACK = r'''
set -eu
backup=$(cat /run/domain-router-ssh-backup)
cp -p "$backup/sshd_config" /etc/ssh/sshd_config
/usr/sbin/sshd -t
'''

RELOAD = r'''
set -eu
/usr/sbin/sshd -t
if systemctl is-active --quiet ssh; then systemctl reload ssh; else systemctl reload sshd; fi
test "$(passwd -S root | cut -d ' ' -f 2)" = P
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['lxc', 'qemu'], required=True)
    parser.add_argument('--id', type=int, required=True)
    parser.add_argument('--allow-root-password', action='store_true', required=True)
    args = parser.parse_args()
    if not 100 <= args.id <= 999999999:
        parser.error('Invalid guest ID')
    password = sys.stdin.buffer.read(4097)
    if not 20 <= len(password) <= 4096 or any(c in password for c in (b'\n', b'\r', b':', b'\0')):
        parser.error('Provide an exact strong password without line terminators on stdin')
    guest(args.kind, args.id, ['sh', '-c', SETUP])
    try:
        guest(args.kind, args.id, ['/usr/sbin/chpasswd'], b'root:' + password + b'\n')
    except Exception:
        guest(args.kind, args.id, ['sh', '-c', ROLLBACK])
        raise
    guest(args.kind, args.id, ['sh', '-c', RELOAD])
    print(json.dumps(dict(kind=args.kind, id=args.id, root_password_set=True, ssh_reloaded=True)))


if __name__ == '__main__':
    main()
