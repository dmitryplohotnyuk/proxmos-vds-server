# Private service access through Tailscale

## Architecture

LXC 107 `tailscale-gateway` is a dedicated subnet router. Cloudflare Tunnel,
Caddy, the domain admin panel, and the VM internet gateway remain on LXC 100.
Proxmox keeps its own Tailscale client as a recovery path. There is no exit node,
public Funnel, or router port forwarding in this design.

```text
Remote Tailscale client -> encrypted tunnel -> LXC 107 -> private service IP
Internet -> Cloudflare Tunnel -> LXC 100 -> published VM web service
LAN client -> existing 192.168.0.x service IP (unchanged)
```

Gateway: unprivileged Debian 12, 1 CPU, 512 MiB RAM, 256 MiB swap, 4 GiB disk.
Tailscale IP: `100.85.164.61` (assigned by this tailnet; do not hardcode on rebuild).
`eth0` uses DHCP on `vmbr0`; `lan0` is `10.77.0.3/24` on `vmbr1`.
TUN is passed with Proxmox `dev0`; nesting is enabled for systemd namespacing.
Autostart: order 2. Root password is locked; manage with `pct exec 107`.
Tailscale state and keys stay in `/var/lib/tailscale` in the container, never Git.

Advertised routes: `10.77.0.0/24`, `192.168.0.222/32` (Home Assistant only).
No default route or full home-LAN route is advertised. Default SNAT is retained:
private services see `10.77.0.3`, and Home Assistant sees the gateway's LAN IP.
Their default gateways do not need changing.

## Service Inventory

| Service | Existing LAN access | Private access | Allowed TCP ports |
| --- | --- | --- | --- |
| Jellyfin, LXC 104 | `192.168.0.156:8096` | `10.77.0.14:8096` | 22, 8096 |
| TorrServer, LXC 105 | `192.168.0.174:8090` | `10.77.0.15:8090` | 22, 8090, 9080 |
| AdGuard, LXC 106 | `192.168.0.5:3000` | `10.77.0.53:3000` | 22, 53, 3000 |
| content-factory, VM 101 | Existing routes unchanged | `10.77.0.10` | 22, 80, 443 |
| Meshtastic, VM 103 | Existing routes unchanged | `10.77.0.12` | 22 |
| Home Assistant, VM 102 | `192.168.0.222:8123` | Same address, /32 route | 8123 |

AdGuard also allows UDP 53. Selected private hosts allow ICMP for diagnostics.
SMB, torrent peer ports, discovery broadcasts and unrelated LAN devices are not
forwarded through the new gateway. Application login and SSH authentication are
still required; subnet routing does not authenticate into applications.

The Home Assistant /32 route can overlap the same IP at a remote location.
It is a compatibility exception, not the preferred address model for new services.
Keep HA's LAN connection for IoT discovery; plan a second internal interface
separately if a collision-free private address is needed.

## Rebuild

Run from this repository on Proxmox as root. Scripts refuse to overwrite an
existing CT or an existing secondary network interface. Inspect and restore an
existing deployment instead of deleting it to rerun the scripts.

```bash
# Select an available Debian 12 template from pveam list local:
export TAILSCALE_GATEWAY_TEMPLATE=local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst
bash scripts/create-tailscale-gateway.sh
bash scripts/add-service-internal-network.sh 104 10.77.0.14/24
bash scripts/add-service-internal-network.sh 105 10.77.0.15/24
bash scripts/add-service-internal-network.sh 106 10.77.0.53/24
pct push 104 scripts/configure-jellyfin-private-network.py /root/configure-jellyfin-private-network.py
pct exec 104 -- python3 /root/configure-jellyfin-private-network.py
pct push 105 scripts/configure-torrserver-private-network.py /root/configure-torrserver-private-network.py
pct exec 105 -- python3 /root/configure-torrserver-private-network.py
pct exec 107 -- tailscale up --hostname=tailscale-gateway --accept-dns=false --accept-routes=false --advertise-routes=10.77.0.0/24,192.168.0.222/32
```

The template filename above is the recorded deployment version, not a guarantee
it exists on a new host. Use `pveam update`, `pveam available --section system`
and `pveam download local <filename>` first, then set the actual volume ID.
The advanced inventory/integration agents require CT 100/107 despite the gateway
creator's CT ID override; do not change that override without adapting all agents.

Authenticate the printed URL with the intended owner account. In the Tailscale
console, approve both routes for `tailscale-gateway`. With explicit owner approval,
disable its device key expiry for unattended operation; otherwise schedule manual
reauthentication before the expiry date. Route approval is independent of access policy.
Do not copy another node's Tailscale state or store reusable auth keys in Git.

Add owner-scoped grants for the destinations/ports above. Preserve required
existing device permissions and SSH policy; do not replace the whole policy
without reviewing its impact. A default `* -> *` grant makes additional narrow
grants ineffective. Review it explicitly before enabling access for other users.

After the new router is approved and reachable, withdraw ONLY the old subnet
advertisement, preserving the old node's HTTPS panel and SSH configuration:

```bash
pct exec 100 -- tailscale set --advertise-routes=
```

Run verification from a separate Tailscale client. Do not call the migration
complete while the old router is still the only approved path.

For a Codex-assisted rebuild, ask it to read this document and the scripts,
inventory existing CTs/addresses, create only missing components, request account
authentication when required, approve the routes, and perform the checks below.

## Security And Lifecycle

`config/tailscale-gateway.nft` is the initial port allowlist. After installing
the automatic inventory in [step 12](12-internal-services.md), the agent's
persistent decisions generate the live firewall; manual edits will be overwritten.
Its own table is reloaded
atomically without flushing Tailscale-managed rules. `tailscaled` requires the
firewall service to start successfully. Do not enable Debian's stock nftables
service with a full `flush ruleset` on this container.

Jellyfin trusts both `192.168.0.0/24` and `10.77.0.0/24`; its existing
`EnableRemoteAccess=false` setting is preserved. TorrServer's firewall allows
its existing LAN plus gateway source addresses `10.77.0.3` and the old
`10.77.0.2` for rollback compatibility. Local DNS/filtering stays unchanged.

The WireGuard protection ends at the gateway. Use SSH/HTTPS for encryption on
the service-side network when needed. The HTTP Jellyfin address is for private
LAN/Tailscale use only. It is not published through Cloudflare.

On macOS/iOS/Windows, subnet routes normally work automatically. On a Linux
client enable `tailscale set --accept-routes=true`. An exit node is not needed.
For mobile access the Jellyfin app server URL is `http://10.77.0.14:8096`.

Do not change global Tailscale DNS just to access Jellyfin. Private DNS names
require a separate split-DNS plan; existing home.arpa answers may point to LAN
addresses which are not advertised.

For a new service: attach a free, documented `10.77.0.X/24` secondary address,
leave its existing default route intact, add the minimum firewall and owner
grant ports, update application trusted networks if needed, then test from a
Tailscale client. No per-service Tailscale agent or new route approval is needed
when the service is already in the approved /24.

## Verification

```bash
# On a Tailscale client, including outside the home LAN:
bash scripts/verify-tailscale-services.sh
# At home, also verify the original LAN path:
CHECK_LAN=1 bash scripts/verify-tailscale-services.sh
# Optional authenticated media test with an external credentials JSON:
python3 scripts/verify-jellyfin-stream.py --credentials /secure/path/jellyfin.json
# Confirm a VPN interface, not an accidental LAN route (macOS):
route -n get 10.77.0.14
# On Proxmox:
pct exec 107 -- tailscale status
pct exec 107 -- nft list table inet tailscale_gateway
pct exec 107 -- systemctl is-active tailscaled tailscale-gateway-firewall
pct exec 100 -- systemctl is-active cloudflared caddy domain-router-ui
```

After switching, check gateway firewall counters while downloading from
Jellyfin, test an authenticated media byte range, verify SSH on content-factory,
and ensure SMB 445 is blocked. Reboot only the new gateway and repeat checks.
A test from a home client over a VPN interface proves subnet forwarding, but
does not replace a final mobile-network test for an external NAT/DERP path.

## Rollback

Before network additions, CT configs and routes are saved under
`/root/tailscale-gateway-backups/` on Proxmox. Application scripts keep timestamped
backups beside the modified configs. No existing VM, disk or LAN address is removed.

If the new gateway fails, restore the previous advertisement:

```bash
pct exec 100 -- tailscale set --advertise-routes=10.77.0.0/24
```

The old route was already approved; recheck approval if needed. Disable the new
node's advertised routes in the console while diagnosing. The old router does
not have the new port allowlist, so this is a temporary recovery mode.
Proxmox's own Tailscale path remains independent of either CT.

## Deployment State: 2026-10-10

- CT 107 created; Tailscale 1.104.1, dedicated firewall and autostart installed.
- New container OS packages updated. Secondary service addresses configured.
- Jellyfin trusted subnet and TorrServer gateway firewall rule applied.
- Jellyfin API at `10.77.0.14:8096` tested successfully from the Mac over `utun4`.
- Authenticated Jellyfin media byte range over that VPN route: HTTP 206, 65536 bytes.
- CT 107 rebooted; firewall and tailscaled start successfully, no failed systemd units.
- New gateway can reach Jellyfin, TorrServer, AdGuard DNS/UI, and Home Assistant.
- Jellyfin LAN web access remains HTTP 200. All existing application services active.
- New gateway authenticated; both advertised routes approved in the console.
- Old CT 100 subnet advertisement withdrawn. HTTPS admin panel and SSH remain intact.
- Gateway-only client checks passed for all service endpoints, DNS filtering, SSH
  on content-factory, and authenticated Jellyfin media range. Firewall counters
  confirm traffic uses CT 107; forbidden SMB forwarding is dropped.
- Tailnet policy changes have NOT been saved. Existing default policy is preserved;
  port restrictions are enforced by CT 107. Owner-only policy replacement requires
  explicit approval; the unsaved editor draft was discarded.
- Key expiry remains enabled (6 months from registration). Disabling it requires
  explicit approval; do not work around this by tagging/re-registering the node.
- Domain-router application test suite: 8 passed. Bash/Python syntax checks passed.
- After the authenticated gateway reboot, all client endpoint checks, DNS filtering,
  negative SMB test, and authenticated media-range test passed again.
- Existing public domain `content-factory-vps.win` returned HTTP 404 both before
  and after this migration; its backend IP returned HTTP 302. This is a pre-existing
  public publication issue, not validated as healthy by this service-access work.
- A real mobile-data test is still recommended; no physical LAN disconnect or
  router reconfiguration was performed during verification.
