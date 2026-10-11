#!/usr/bin/env python3
"""Save a credential in an owner-unlocked Bitwarden CLI. Never rotate a service password."""
import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import UUID


class VaultError(Exception):
    pass


def bw(*arguments, data=None):
    try:
        result = subprocess.run(['bw', *arguments], input=data, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise VaultError('Bitwarden CLI unavailable or timed out. No service was changed.') from None
    if result.returncode:
        # CLI output can contain a decrypted item or session; never forward it to logs.
        raise VaultError('Bitwarden operation failed. Check login/unlock and network in the CLI.')
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise VaultError('Invalid Bitwarden response. Check the vault before retrying.') from None


def save(name, username, password, uri, server):
    if not os.environ.get('BW_SESSION'):
        raise VaultError('Unlock your vault first; BW_SESSION is required and must not be stored on the server.')
    parsed = urlsplit(server)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise VaultError('An explicit HTTPS vault origin is required.')
    status = bw('status')
    if status.get('status') != 'unlocked' or status.get('serverUrl', '').rstrip('/') != server.rstrip('/'):
        raise VaultError('CLI is locked or connected to another server. Nothing was saved.')
    if not name.strip() or len(name) > 120 or len(username) > 120 or not password or len(password) > 4096:
        raise VaultError('Invalid credential name, username or password length.')
    if uri and (urlsplit(uri).scheme not in ('https', 'http', 'ssh') or urlsplit(uri).username or urlsplit(uri).password):
        raise VaultError('Service URI must use HTTP, HTTPS or SSH without embedded credentials.')
    item = dict(type=1, name=name, notes='', favorite=False, reprompt=0,
                login=dict(username=username, password=password, totp=None,
                           uris=[dict(uri=uri, match=None)] if uri else []),
                fields=[], folderId=None, organizationId=None)
    encoded = base64.b64encode(json.dumps(item).encode()).decode()
    result = bw('create', 'item', data=encoded)
    try:
        identifier = str(UUID(result['id']))
    except (KeyError, ValueError, TypeError):
        raise VaultError('The save result is uncertain. Inspect the vault before retrying.') from None
    return dict(vault_item_id=identifier, title=name, username=username, method='password')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--uri', default='')
    parser.add_argument('--server', required=True)
    args = parser.parse_args()
    try:
        password = getpass.getpass('Existing/new service password (not changed): ') if sys.stdin.isatty() else sys.stdin.read(4097)
        receipt = save(args.name, args.username, password, args.uri, args.server)
    except VaultError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
