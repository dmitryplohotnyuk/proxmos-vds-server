#!/usr/bin/env python3
"""Run as root inside CT 100; reapply and restore the existing local Tunnel."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import yaml


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root inside domain-router CT 100')
    spec = importlib.util.spec_from_file_location('integrations', '/opt/domain-router-integrations/agent.py')
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    before = manager.cloudflare_status()
    if before['mode'] != 'local' or not before['connected'] or before['pending']:
        raise SystemExit('Expected healthy local Tunnel with no pending operation')
    original_revision = before['revision']
    configuration = yaml.safe_load(manager.LEGACY_CONFIG.read_text())
    credentials = Path(configuration['credentials-file']).read_text()
    committed = False
    try:
        result = manager.handle(dict(action='cloudflare-apply', mode='local', secret=credentials,
                                     expected_revision=original_revision), 'panel')['cloudflare']
        committed = True
        assert result['tunnel_id'] == before['tunnel_id'] and result['connected']
        assert result['can_rollback'] and not result['pending']
        print('Existing Tunnel: apply and connector readiness OK', flush=True)
    finally:
        if committed:
            manager.handle(dict(action='cloudflare-rollback', expected_revision=manager.cf_revision()), 'panel')
            assert manager.wait_ready(20241)
            assert manager.cf_revision() == original_revision
            assert not (manager.STATE / 'pending.json').exists()
            print('Original Tunnel: configuration and connectivity restored', flush=True)
    if '--guard' in sys.argv[1:]:
        with manager.locked():
            manager.write_json(manager.STATE / 'pending.json', manager.snapshot())
            manager.command('/usr/bin/systemctl', 'enable', '--now', manager.GUARD_UNIT + '.timer')
        try:
            print('Independent rollback timer: waiting for recovery', flush=True)
            deadline = time.monotonic() + 110
            while (manager.STATE / 'pending.json').exists() and time.monotonic() < deadline:
                time.sleep(2)
            assert not (manager.STATE / 'pending.json').exists()
            assert manager.wait_ready(20241)
            assert manager.cf_revision() == original_revision
            print('Independent rollback timer: recovery OK', flush=True)
        finally:
            if (manager.STATE / 'pending.json').exists():
                with manager.locked():
                    manager.restore(manager.read_json(manager.STATE / 'pending.json'))
                    manager.stop_guard()
    before_ts = manager.tailscale_status()
    if before_ts['state'] == 'Running':
        after_ts = manager.handle({'action': 'tailscale-connect'}, 'panel')['tailscale']
        assert before_ts == after_ts
        print('Connected Tailscale: connect is a no-op, identity and preferences preserved', flush=True)


if __name__ == '__main__':
    main()
