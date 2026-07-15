#!/bin/sh
set -eu

state_file=/run/domain-router-network-failures
check_url=https://1.1.1.1/cdn-cgi/trace

if curl -4 --interface eth0 --fail --silent --show-error \
    --connect-timeout 5 --max-time 10 "$check_url" >/dev/null 2>&1; then
    printf '0\n' > "$state_file"
    exit 0
fi

failures=0
if [ -r "$state_file" ]; then
    failures=$(cat "$state_file" 2>/dev/null || printf '0')
fi
case "$failures" in
    ''|*[!0-9]*) failures=0 ;;
esac
failures=$((failures + 1))
printf '%s\n' "$failures" > "$state_file"

if [ "$failures" -lt 3 ]; then
    exit 0
fi

logger -t domain-router-network "Internet unavailable; renewing DHCP lease on eth0"
printf '0\n' > "$state_file"
/sbin/ifdown --force eth0 || true
sleep 2
/sbin/ifup eth0

if systemctl is-enabled --quiet cloudflared.service 2>/dev/null; then
    systemctl restart cloudflared.service
fi
