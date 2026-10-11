#!/usr/bin/env python3
"""Check an encrypted control-plane archive without restoring it over live services."""
import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

DATABASE = 'ct100/var/lib/domain-router-ui/app.db'
REQUIRED = {DATABASE, 'manifest.json', 'ct100/etc/domain-router/routes.yml',
            'ct100/etc/caddy/Caddyfile', 'ct107/etc/tailscale-gateway.nft',
            'host/var/lib/service-inventory/inventory.json', 'host/etc/network/interfaces'}


def valid_member(member):
    path = PurePosixPath(member.name)
    return (bool(path.parts) and not path.is_absolute() and '..' not in path.parts
            and path.parts[0] in ('ct100', 'ct107', 'host', 'manifest.json')
            and (member.isfile() or member.isdir())
            and not member.issym() and not member.islnk())


def verify(archive, identity, age):
    with tempfile.TemporaryDirectory(prefix='control-plane-restore-check-') as directory:
        process = subprocess.Popen([age, '--decrypt', '--identity', str(identity), str(archive)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        database = Path(directory) / 'app.db'
        seen = set()
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|*') as stream:
                for member in stream:
                    if not valid_member(member) or member.name in seen:
                        raise ValueError('Unsafe or duplicate member')
                    seen.add(member.name)
                    if member.name == DATABASE:
                        if member.size > 512 * 1024 * 1024:
                            raise ValueError('Database exceeds verification limit')
                        with stream.extractfile(member) as source, database.open('wb') as target:
                            shutil.copyfileobj(source, target)
            while process.stdout.read(65536):
                pass
            if process.wait(timeout=30) or not REQUIRED <= seen:
                raise ValueError('Decryption failed or incomplete backup')
            with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
                if (connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                        or connection.execute('PRAGMA foreign_key_check').fetchall()):
                    raise ValueError('SQLite verification failed')
                accounts = connection.execute('SELECT COUNT(*) FROM access_links').fetchone()[0]
                users = connection.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            return dict(archive_members=len(seen), sqlite_integrity='ok',
                        access_links=accounts, administrators=users)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--age', default='age')
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.archive, args.identity, args.age)))
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError):
        raise SystemExit('Backup verification failed; no live configuration was modified.') from None
