#!/usr/bin/env python3
"""Decrypt to a stream, validate archive paths and restore/check SQLite in a private temporary directory."""
import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile


def valid_member(member):
    path = PurePosixPath(member.name)
    if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
        return False
    return (member.name in ('vaultwarden.env', 'image.ref', 'data', 'data/')
            or member.name.startswith('data/'))


def verify(archive, identity, age):
    with tempfile.TemporaryDirectory(prefix='vaultwarden-restore-check-') as directory:
        process = subprocess.Popen([age, '--decrypt', '--identity', str(identity), str(archive)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        database_path = Path(directory) / 'db.sqlite3'
        count = 0
        seen = set()
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|') as stream:
                for member in stream:
                    if not valid_member(member) or member.name in seen:
                        raise ValueError('Unsafe or duplicate archive path')
                    seen.add(member.name)
                    count += 1
                    if member.name in ('data/db.sqlite3', 'data/db.sqlite3-wal'):
                        if member.size > 512 * 1024 * 1024:
                            raise ValueError('Database too large for this restore check')
                        with stream.extractfile(member) as source, (Path(directory) / PurePosixPath(member.name).name).open('wb') as target:
                            shutil.copyfileobj(source, target)
            # Drain any trailing encrypted content so authentication of the entire age stream completes.
            while process.stdout.read(65536):
                pass
            if process.wait(timeout=30) != 0:
                raise ValueError('Decryption failed; verify identity and archive')
            if not database_path.exists() or not {'vaultwarden.env', 'image.ref'} <= seen:
                raise ValueError('Incomplete Vaultwarden backup')
            with sqlite3.connect(f'file:{database_path}?mode=ro', uri=True) as connection:
                if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Restored SQLite integrity check failed')
                if connection.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('Restored SQLite foreign key check failed')
                users = connection.execute('SELECT COUNT(*) FROM users').fetchone()[0]
                ciphers = connection.execute('SELECT COUNT(*) FROM ciphers').fetchone()[0]
            return dict(archive_members=count, sqlite_integrity='ok', accounts=users, encrypted_items=ciphers)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--age', default='age')
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.archive, args.identity, args.age)))
    except (OSError, ValueError, tarfile.TarError, sqlite3.Error, subprocess.SubprocessError):
        raise SystemExit('Backup verification failed. No live service was modified; inspect archive and identity securely.') from None


if __name__ == '__main__':
    main()
