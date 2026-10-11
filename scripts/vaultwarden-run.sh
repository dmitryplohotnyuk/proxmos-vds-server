#!/bin/bash
set -euo pipefail
image=$(cat /opt/vaultwarden/image.ref)
[[ $image =~ ^ghcr.io/dani-garcia/vaultwarden@sha256:[a-f0-9]{64}$ ]] || { echo 'Invalid pinned image' >&2; exit 1; }
docker rm -f vaultwarden >/dev/null 2>&1 || true
exec docker run --rm --name vaultwarden --pull=never \
    --env-file /opt/vaultwarden/vaultwarden.env \
    --network host --volume /opt/vaultwarden/data:/data \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --memory 768m --cpus 1 --pids-limit 256 \
    --log-driver json-file --log-opt max-size=5m --log-opt max-file=2 "$image"
