#!/usr/bin/env python3
"""Opt-in workaround for the verified RTL9210B UAS failure on this host."""
import argparse
import os
from pathlib import Path
import re
import subprocess

DEVICE = '0bda:9210'


def merge_quirks(current):
    entries = []
    flags = {'u'}
    for entry in filter(None, current.strip().split(',')):
        if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{4}:[A-Za-z]*', entry):
            raise ValueError('Unrecognized existing usb_storage quirks; inspect manually')
        identifier, value = entry.rsplit(':', 1)
        if identifier.lower() == DEVICE:
            flags.update(value)
        else:
            entries.append(entry)
    entries.append(DEVICE + ':' + ''.join(sorted(flags)))
    return ','.join(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply or os.geteuid() != 0:
        parser.error('Review this script, then run as root with --apply')
    devices = []
    for vendor in Path('/sys/bus/usb/devices').glob('*/idVendor'):
        product = vendor.parent / 'idProduct'
        if vendor.read_text().strip() + ':' + product.read_text().strip() == DEVICE:
            devices.append(vendor.parent)
    if len(devices) != 1:
        parser.error('Expected exactly one attached RTL9210B adapter')
    parameter = Path('/sys/module/usb_storage/parameters/quirks')
    value = merge_quirks(parameter.read_text())
    parameter.write_text(value)
    target = Path('/etc/modprobe.d/proxmox-usb-backup-compat.conf')
    content = '# RTL9210B: opt-in IGNORE_UAS; preserve other active quirks.\n'
    content += 'options usb_storage quirks=' + value + '\n'
    target.write_text(content)
    target.chmod(0o644)
    subprocess.run(['update-initramfs', '-u', '-k', 'all'], check=True, timeout=600)
    print('Persisted:', value)
    print('No device/controller reset performed. If still using UAS, unmount before reconnecting.')


if __name__ == '__main__':
    main()
