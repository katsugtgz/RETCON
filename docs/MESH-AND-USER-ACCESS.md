# RETCON mesh + user access planes

How nodes link to each other (the mesh plane) and how a human reaches a
node (the user plane). Both are designed around the same rule: **know two
values — the mesh identity and its secret — and you're in.**

## The mesh plane: `mesh_mode` in `[[wifi]]`

### `mesh_mode = tcp` (default, proven)

The original architecture: every node hosts an AP (`uap0`, NM `ipv4.method
shared`, dnsmasq) AND joins neighbors' APs (`wlan0`). Reticulum rides
Backbone (TCP) interfaces — a server on the AP side (port 4242), a client
to `retcon.gateway` (resolved by dnsmasq/hosts). The scan loop picks the
strongest matching SSID (prefix + channel), transports break the
symmetry by preferring lexicographically-larger SSIDs, and `restart_rnsd()`
re-creates the crns host after any change.

- Pro: deterministic discovery (SSID prefix scan), user-facing AP for
  meshchat in client mode, multi-hop via the AP/STA chain.
- Con: the layer-3 scaffolding (DHCP wait, `/etc/hosts` rewrite, host
  restart) and NM dbus complexity.

### `mesh_mode = udp` (new, experimental — the IBSS "shout")

One ad-hoc cell, one Reticulum interface, no DHCP/DNS/hosts on the mesh:

1. **Join**: `nmcli` brings up `retcon_ibss` on `client_iface` —
   `802-11-wireless.mode adhoc`, fixed `ibss_ssid` (default
   `<prefix>MESH`), channel from `freq`, **open** (no WPA — IFAC gates
   the medium instead, see below).
2. **Address**: deterministic link-local from the node's MAC:
   `169.254.<mac[4]>.<mac[5]>/16` (same derivation in `retcon.py` and the
   plugin; the SSID already encodes the MAC, so a neighbor's SSID *is*
   its address — no discovery protocol at all).
3. **Data**: a single `UDPInterface` in the generated `~/.reticulum/config`
   — `listen_ip` = the node's link-local, `forward_ip = 169.254.255.255`
   (directed broadcast), port 4242, IFAC-wrapped with
   `ifac_netname = prefix`, `ifac_netkey = psk`.

That is the whole mesh. Announces are the discovery protocol; RNS
transport is the routing protocol; the kernel's directed broadcast is the
delivery. Debugging is one command away:

```
tcpdump -i wlan0 -n udp port 4242        # every RNS packet, on-air
tcpdump -i wlan0 not udp port 4242       # everything else (should be ~nothing)
iw dev wlan0 info                        # are we actually in the IBSS?
iw dev wlan0 link                        # which peers does the BSS see?
```

**Multi-hop without L2 relay**: 802.11 IBSS never forwards frames, but
Reticulum doesn't need it to. A transport node that hears an announce
re-broadcasts it on *all* of its interfaces (hop-count capped, per-iface
rate-limited, `PATHFINDER_M` = 128); DATA follows learned paths with
transport headers. So a node between two out-of-range peers bridges at
the RNS layer — the same reason RNS meshes fine over LoRa, where there
is no L2 at all. Nodes in *disjoint* IBSS cells (out of radio range
entirely) are bridged by any node with a second interface (RNode, USB
wifi, another radio).

**The secret**: IFAC on the shared medium. Know `prefix` + `psk` → your
packets decrypt; anyone else sees nothing (the IFAC header itself gates
decoding — packets without the right key are dropped before RNS parses
them). This replaces WPA entirely for the mesh plane; RNS payloads are
end-to-end encrypted regardless.

**Caveats**:

- brcmfmac (the Zero 2 W's CYW43430) IBSS support is historically
  unreliable — this mode is an experiment. Test with
  `iw dev wlan0 ibss join` before committing a fleet.
- Single radio = single IBSS. Nodes in radio range of each other share
  one BSS; there is no L2 relay between cells (use RNS transport nodes
  with a second interface to bridge).
- The loopback meshchat interface (127.0.0.1:4243) still exists in this
  mode — on-device apps join as always over TCP loopback.

## The user plane

Client-mode nodes used to serve meshchat from their AP. In IBSS mode
there's no AP, so users reach a node by:

### USB gadget (default, always on) — "the cable is the console"

`dtoverlay=dwc2` + libcomposite (RNDIS for Windows/Android, ECM for
macOS/modern Linux) enumerates a USB NIC on the host. NM runs
`ipv4.method shared` on `usb0` at `10.55.0.1/24`, so the laptop gets
DHCP, DNS names resolve (`retcon.local`, `retcon.gateway`), and meshchat
+ the admin web UI are one browser tab away. Also the fleet-provisioning
path: flash, plug into any laptop, `ssh retcon@retcon.local`, configure,
deploy.

**Only the middle USB port (the OTG/data port) carries data.** A Zero
powered through PWR IN will not enumerate as a gadget — the service
logs "no UDC available" and stays out of the way.

### BT PAN (opt-in) — `btpan on` over the LXMF admin console

Starts `retcon-bt-nap.service` (bluez `NetworkServer1` NAP on `br-bt`)
+ `retcon-bt-dnsmasq.service` (DHCP `10.56.0.10-200`, DNS to
`10.56.0.1`). Pair, join the PAN, browse `retcon.local`. Works for
Android/Linux/macOS/Windows; **iPhones cannot join a BT PAN** (Apple
dropped client support) — use the cable. Not enabled by default.

### Address plan (no overlaps)

| Plane | Subnet | Interface |
|---|---|---|
| IBSS mesh (udp mode) | `169.254.0.0/16` (link-local, MAC-derived) | `wlan0` |
| Client-mode AP shared | `10.42.0.0/24` or `10.<mac>.<mac>.0/24` (transport) | `uap0` |
| USB gadget | `10.55.0.0/24` | `usb0` |
| BT PAN | `10.56.0.0/24` | `br-bt` |
| Loopback apps | `127.0.0.1` | `lo` |

## Build-time wiring

- `retcon-apps` layer: `dtoverlay=dwc2` appended to config.txt, gadget
  script + systemd unit enabled, `retcon-usb0.nmconnection` profile,
  BT PAN units installed (disabled), bluez installed.
- `crns` branch `retcon/posix-build-fixes` gained the pieces the IBSS
  shape needs: `SO_BROADCAST` on UDP interfaces (broadcast sendto
  otherwise fails with EACCES), `UDPInterface` type alias (rnsd
  spelling), and `ifac_netname`/`ifac_netkey` config aliases (previously
  silently dropped — an IFAC iface would have come up un-gated).
## The BLE bearer: automagic bridge (RAK4631 / ESP32-S3 peripherals)

Third medium, for the devices that have no wifi shout at all (RAK4631 =
LoRa+BLE+USB) or are far from any AP. A Pi-class node runs
`utils/system/ble_rnode_bridge.py` and any BLE in range "just works":

- **The device side is a BLE peripheral advertisting the NUART-shaped
  GATT service** (`6e400001-…`, RX `6e400002` host→device, TX `6e400003`
  device→host notify) carrying the **same RNode KISS byte protocol it
  speaks on USB**. No pairing: the firmware never requests security, and
  the bridge never calls Pair — plain unencrypted GATT connect. Note:
  RNode_Firmware's own BLE flavor ENFORCES MITM passkey pairing (ESP32
  gates writes on `bt_client_authenticated()`; the nRF52 side deliberately
  debonds "just works" pairs) and is Android-locked (python RNS has no
  BLEInterface — Sideband's BLE RNode rides the Android-only
  RNodeInterface). Ours intentionally drops the gate: trust rides
  Reticulum — open medium + IFAC (same posture as the IBSS wifi shout),
  packets RNS-crypto end to end.
- **Automagic discovery**: the bridge scans for the service UUID, connects
  to anything new (name-prefix filtered), and does the RNode DETECT
  handshake over the KISS stream; RNS announces then propagate both ways.
  Walking into range = a link, seconds later. No UI anywhere.
- **Graceful out-of-range**: BLE supervision timeout drops the GATT
  connection → the bridge detaches that interface and closes the PTY;
  re-entry re-arms it. The bridge runs its OWN python-RNS instance (own
  config dir, `share_instance = No`) and joins the mesh over the proven
  loopback pattern (TCP client to the main crns host's :4242 server) —
  the meshchat pattern, so the main host stays untouched.
- **Load-bearing firmware contract** (proven by
  `tests/test_ble_rnode_bridge.py` against a fake dongle): the device
  must (1) answer DETECT with `DETECT_RESP`/FW 1.52/PLATFORM/MCU, AND
  (2) **echo the radio-config commands back** (FREQUENCY/BANDWIDTH/
  TXPOWER/SF/CR/RADIO_STATE) — python RNS's `validateRadioState` gates
  `interface_ready` on the echo. Also: python RNS's probe SHARES the
  boundary FEND between consecutive KISS frames (FEND cmd data FEND cmd
  data FEND) — firmware parsers must treat a closing FEND as also
  opening the next frame (the R1 responder already does).
- Bandwidth class: BLE NUART ≈ up to ~10 KB/s — announces, paths and
  small messages; big pushes ride the fastest elected path (wifi/
  ESP-NOW/USB).
