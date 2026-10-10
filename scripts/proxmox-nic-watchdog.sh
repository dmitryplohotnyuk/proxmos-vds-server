#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin

interface=${WATCHDOG_INTERFACE:-nic0}
bridge=${WATCHDOG_BRIDGE:-vmbr0}
pci_address=${WATCHDOG_PCI_ADDRESS:-0000:00:1f.6}
driver=${WATCHDOG_DRIVER:-e1000e}
check_window=${WATCHDOG_CHECK_WINDOW:-45}
cooldown=${WATCHDOG_COOLDOWN:-300}
auto_reboot=${WATCHDOG_AUTO_REBOOT:-0}
force=${WATCHDOG_FORCE:-0}
dry_run=${WATCHDOG_DRY_RUN:-0}

state_dir=/run/proxmox-nic-watchdog
last_attempt_file=$state_dir/last-attempt
mkdir -p "$state_dir"

exec 9>"$state_dir/lock"
flock -n 9 || exit 0

log() {
    printf '%s\n' "$*"
}

recent_hang() {
    journalctl -q -k -b --since "$check_window seconds ago" --no-pager |
        grep -F "$driver 0000:${pci_address#0000:} $interface: Detected Hardware Unit Hang" \
            >/dev/null 2>&1
}

gateway_address() {
    ip -4 route show default dev "$bridge" 2>/dev/null |
        awk '$1 == "default" && $2 == "via" { print $3; exit }'
}

network_is_healthy() {
    gateway=$(gateway_address)
    [ -n "$gateway" ] || return 1
    ip link show dev "$interface" >/dev/null 2>&1 || return 1
    ping -I "$bridge" -c 2 -W 2 "$gateway" >/dev/null 2>&1
}

wait_for_network() {
    attempts=0
    while [ "$attempts" -lt 4 ]; do
        sleep 3
        if network_is_healthy; then
            return 0
        fi
        attempts=$((attempts + 1))
    done
    return 1
}

capture_diagnostics() {
    log "Kernel messages around the incident:"
    journalctl -q -k -b --since "2 minutes ago" --no-pager | tail -40 || true
    log "Interface state:"
    ip -s link show dev "$interface" || true
    ethtool "$interface" || true
    bridge link show || true
}

if [ "$force" != 1 ] && ! recent_hang; then
    exit 0
fi

if [ "$dry_run" = 1 ]; then
    log "Forced watchdog dry run for $interface"
    capture_diagnostics
    log "Dry run enabled; recovery actions were not executed"
    exit 0
fi

now=$(date +%s)
last_attempt=0
if [ -r "$last_attempt_file" ]; then
    last_attempt=$(cat "$last_attempt_file" 2>/dev/null || printf '0')
fi
case "$last_attempt" in
    ''|*[!0-9]*) last_attempt=0 ;;
esac

if [ $((now - last_attempt)) -lt "$cooldown" ]; then
    log "Recovery is in cooldown; skipping duplicate event"
    exit 0
fi
printf '%s\n' "$now" >"$last_attempt_file"

log "Detected $driver hardware unit hang on $interface"
capture_diagnostics

if [ "$force" != 1 ] && network_is_healthy; then
    log "Network recovered without watchdog action"
    exit 0
fi

log "Recovery step 1/3: requesting an ethtool device reset"
if timeout 10 ethtool --reset "$interface" all; then
    sleep 2
    timeout 20 ifreload -a || true
    if wait_for_network; then
        log "Network recovered after ethtool reset"
        exit 0
    fi
else
    log "The driver did not accept an ethtool reset"
fi

log "Recovery step 2/3: cycling the physical link"
ip link set dev "$interface" down || true
sleep 2
ip link set dev "$interface" up || true
timeout 20 ifreload -a || true
if wait_for_network; then
    log "Network recovered after link cycle"
    exit 0
fi

log "Recovery step 3/3: rebinding the PCI device to $driver"
driver_path=/sys/bus/pci/drivers/$driver
if [ -w "$driver_path/unbind" ] && [ -w "$driver_path/bind" ]; then
    ip link set dev "$interface" down || true
    if printf '%s' "$pci_address" >"$driver_path/unbind"; then
        sleep 3
        printf '%s' "$pci_address" >"$driver_path/bind" || true
        udevadm settle --timeout=10 || true
        timeout 20 ifreload -a || true
        if wait_for_network; then
            log "Network recovered after PCI driver rebind"
            exit 0
        fi
    fi
else
    log "PCI driver bind controls are unavailable"
fi

log "ERROR: all NIC recovery steps failed"
if [ "$auto_reboot" = 1 ]; then
    log "Automatic reboot is enabled; requesting an orderly reboot"
    systemctl reboot
fi
exit 1
