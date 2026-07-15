#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
load_env "$PROJECT_DIR/.env"
require_root
require_command qm
require_command pvesm
require_command openssl

VMID=${CONTENT_FACTORY_VMID:-101}
VM_NAME=${CONTENT_FACTORY_NAME:-content-factory}
VM_IP=${CONTENT_FACTORY_IP:-10.77.0.10/24}
VM_GATEWAY=${CONTENT_FACTORY_GATEWAY:-10.77.0.2}
VM_CORES=${CONTENT_FACTORY_CORES:-10}
VM_MEMORY=${CONTENT_FACTORY_MEMORY:-24576}
VM_DISK_GB=${CONTENT_FACTORY_DISK_GB:-300}
VM_STORAGE=${CONTENT_FACTORY_STORAGE:-local-lvm}
VM_BRIDGE=${PRIVATE_BRIDGE:-vmbr1}
DOMAIN_ZONE=${DOMAIN_ZONE:-content-factory-vps.win}
IMAGE_DIR=/var/lib/vz/template/iso
IMAGE_NAME=noble-server-cloudimg-amd64.img
IMAGE_URL=https://cloud-images.ubuntu.com/noble/current/$IMAGE_NAME
CHECKSUM_URL=https://cloud-images.ubuntu.com/noble/current/SHA256SUMS
SNIPPET_DIR=/var/lib/vz/snippets
SNIPPET_NAME=content-factory-user.yml

if qm status "$VMID" >/dev/null 2>&1; then
    die "VM $VMID already exists; refusing to modify it"
fi

root_password=${CONTENT_FACTORY_ROOT_PASSWORD:-$(openssl rand -hex 14)}
password_hash=$(printf '%s\n' "$root_password" | openssl passwd -6 -stdin)

log "Downloading and verifying Ubuntu Server 24.04 cloud image"
install -d -m 0755 "$IMAGE_DIR"
curl -fL --retry 3 -o "$IMAGE_DIR/$IMAGE_NAME" "$IMAGE_URL"
curl -fL --retry 3 -o "$IMAGE_DIR/noble-SHA256SUMS" "$CHECKSUM_URL"
(
    cd "$IMAGE_DIR"
    grep " \*$IMAGE_NAME\$" noble-SHA256SUMS | sha256sum -c -
)

log "Preparing cloud-init user data"
pvesm set local --content iso,vztmpl,backup,import,snippets
install -d -m 0755 "$SNIPPET_DIR"
sed "s|__ROOT_PASSWORD_HASH__|$password_hash|" \
    "$PROJECT_DIR/config/cloud-init/content-factory-user-data.yml.template" \
    >"$SNIPPET_DIR/$SNIPPET_NAME"
chmod 0600 "$SNIPPET_DIR/$SNIPPET_NAME"

log "Creating VM $VMID ($VM_NAME)"
qm create "$VMID" \
    --name "$VM_NAME" \
    --description "Ubuntu Server 24.04 LTS - $DOMAIN_ZONE" \
    --ostype l26 \
    --machine q35 \
    --cpu host \
    --sockets 1 \
    --cores "$VM_CORES" \
    --memory "$VM_MEMORY" \
    --balloon 0 \
    --net0 "virtio,bridge=$VM_BRIDGE,firewall=0" \
    --onboot 1 \
    --startup order=2

qm importdisk "$VMID" "$IMAGE_DIR/$IMAGE_NAME" "$VM_STORAGE"
qm set "$VMID" \
    --scsihw virtio-scsi-single \
    --scsi0 "$VM_STORAGE:vm-$VMID-disk-0,discard=on,iothread=1,ssd=1"
qm resize "$VMID" scsi0 "${VM_DISK_GB}G"
qm set "$VMID" \
    --ide2 "$VM_STORAGE:cloudinit" \
    --boot order=scsi0 \
    --serial0 socket \
    --vga serial0
qm set "$VMID" \
    --agent enabled=1,fstrim_cloned_disks=1 \
    --cicustom "user=local:snippets/$SNIPPET_NAME" \
    --ipconfig0 "ip=$VM_IP,gw=$VM_GATEWAY" \
    --nameserver 1.1.1.1 \
    --searchdomain "$DOMAIN_ZONE"
qm cloudinit update "$VMID"
qm start "$VMID"

cat <<EOF

VM $VMID started.
SSH: ssh root@${VM_IP%/*}
One-time generated root password: $root_password

Wait for cloud-init before use. Verify from Proxmox with:
  qm guest exec $VMID -- cloud-init status --long
EOF

