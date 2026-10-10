#!/usr/bin/env python3
"""Exercise first login against a temporary userspace daemon, never the live socket."""
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root inside CT 107')
    spec = importlib.util.spec_from_file_location('manager', '/opt/domain-router-integrations/agent.py')
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    with tempfile.TemporaryDirectory(prefix='tailscale-login-test-') as directory:
        path = str(Path(directory) / 'tailscaled.sock')
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(4)
            self.sock.connect(path)
        manager.LocalConnection.connect = connect
        process = subprocess.Popen(['/usr/sbin/tailscaled', '--tun=userspace-networking',
                                    '--socket=' + path, '--state=' + directory + '/state', '--port=0'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 8
            while not Path(path).exists() and time.monotonic() < deadline:
                time.sleep(.1)
            result = manager.tailscale_connect('gateway')
            # The authorization URL is intentionally not printed or retained.
            assert result['state'] == 'NeedsLogin' and result['auth_url']
            prefs = manager.local_api('GET', 'prefs')
            assert prefs['AdvertiseRoutes'] == manager.ROUTES
            assert prefs['Hostname'] == 'tailscale-gateway'
            print('Isolated first login: safe authorization URL and fixed gateway profile OK')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
