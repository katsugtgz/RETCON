#!/usr/bin/env python3
# ble_rnode_bridge.py — automatic BLE <-> Reticulum bridge for RETCON.
#
# Lets any RETCON host (Pi-class) talk to Reticulum nodes in BLE range with
# ZERO user interaction: scan -> connect -> a full Reticulum interface comes
# up, announce-propagates both ways, and tears down gracefully when the
# device leaves radio range. Nothing to pair, nothing to accept, nothing to
# click.
#
# The far end is a BLE peripheral running the RNode-compatible KISS
# responder over the Nordic-UART GATT service (6e400001-b5a3-f393-e0a9-
# e50e24dcca9e) — the same byte protocol the device already speaks on USB
# (microretcon firmware R1; RNode_Firmware's own BLESerial is the precedent).
#
# NO PAIRING: the firmware never requests security and this bridge never
# calls Pair — plain unencrypted GATT connect + notify/write. Trust rides
# Reticulum: the interface carries IFAC (ifac_netname/ifac_netkey) exactly
# like the IBSS wifi shout, and packets are RNS-crypto end to end.
#
# Why a separate process: it runs its OWN python-RNS instance (own config
# dir, share_instance=no — python rns defaults share_instance yes and would
# otherwise attach to whatever holds the local shared-instance port) and
# joins the RETCON mesh over the proven loopback pattern (TCP client to the
# main crns host's :4242 server). Meshchat uses the same shape.
#
# Dynamic interface lifecycle (python RNS supports runtime registration):
#   device discovered -> connect -> PTY pair -> RNodeInterface(port=pty)
#   -> reticulum._add_interface(ifac keyed)
#   device lost (GATT drop / supervision timeout) -> iface.detach(), PTY gone
#   device returns -> fresh interface (name carries the device tag)
#
# The dongle answers the RNode DETECT probe and ECHOES the radio-config
# commands the host sends (FREQUENCY/BANDWIDTH/TXPOWER/SF/CR/RADIO_STATE) —
# that echo is the RNode KISS contract RNodeInterface's validateRadioState
#() requires; without it the interface never reaches interface_ready. A
# dongle with a real radio applies the config first; a pure bridge (ESP-NOW
# radio, no LoRa) stores + echoes.

import asyncio
import inspect
import os
import selectors
import threading
import time

# Env-configurable, keep module importable without bleak/bluez/hardware.
BLE_SERVICE_UUID = os.environ.get("BLE_SERVICE_UUID", "6e400001-b5a3-f393-e0a9-e50e24dcca9e")
BLE_RX_CHAR_UUID = os.environ.get("BLE_RX_CHAR_UUID", "6e400002-b5a3-f393-e0a9-e50e24dcca9e")  # host -> device
BLE_TX_CHAR_UUID = os.environ.get("BLE_TX_CHAR_UUID", "6e400003-b5a3-f393-e0a9-e50e24dcca9e")  # device -> host (notify)
NAME_PREFIX = os.environ.get("BLE_NAME_PREFIX", "RETCON-")
SCAN_INTERVAL_S = float(os.environ.get("BLE_SCAN_INTERVAL_S", "3"))
IFAC_NETNAME = os.environ.get("BLE_IFAC_NETNAME", None)
IFAC_NETKEY = os.environ.get("BLE_IFAC_NETKEY", None)
LOOPBACK_HOST = os.environ.get("BLE_LOOPBACK_HOST", "127.0.0.1")
LOOPBACK_PORT = int(os.environ.get("BLE_LOOPBACK_PORT", "4242"))
RNS_CONFIG_DIR = os.environ.get("BLE_RNS_CONFIG_DIR", "~/.retcon-ble-rns")
MAX_DEVICES = int(os.environ.get("BLE_MAX_DEVICES", "6"))


class DeviceLink:
    def __init__(self, address, name, client=None, master_fd=None, slave_path=None):
        self.address = address
        self.name = name or ""
        self.client = client        # bleak Link or test double
        self.master_fd = master_fd  # bridge-owned end of the PTY pair
        self.slave_path = slave_path
        self.iface = None
        self.reader = None          # pty reader thread; set on bring_up
        self.alive = True

    @property
    def tag(self):
        return (self.address or self.name or "?").replace(":", "").replace("-", "")[-6:].lower()

    @property
    def iface_name(self):
        return f"RNode BLE {self.tag}"


class Bridge:
    """Scan/connect/lifecycle core. bleak is injected so the class is fully
    testable without BlueZ or hardware (tests use a fake dongle over a PTY)."""

    def __init__(self, reticulum=None, rnode_if_mod=None, log=print):
        self.rnode_if = rnode_if_mod
        self.ret = reticulum
        self.log = log
        self.links = {}  # address -> DeviceLink
        self._stop = False

    def bring_up(self, address, name, client):
        """Wire one device into Reticulum. Sync; starts the PTY reader."""
        master, slave = os.openpty()
        slave_path = os.ttyname(slave)
        cfg = {
            # The dongle echoes these back (RNode KISS contract); a bridge
            # dongle without a real radio treats them as stored metadata.
            "name": f"RNode BLE {(address or 'x')[-5:].replace(':', '')}",
            "frequency": 914875000,
            "bandwidth": 125000,
            "txpower": 14,
            "spreadingfactor": 8,
            "codingrate": 5,
            "flow_control": False,
        }
        iface = self.rnode_if.RNodeInterface(self.ret, cfg)
        self.ret._add_interface(
            iface,
            ifac_netname=IFAC_NETNAME,
            ifac_netkey=IFAC_NETKEY,
            configured_bitrate=10000,  # BLE NUART class; paces announces
        )
        link = DeviceLink(address, name, client=client, master_fd=master,
                          slave_path=slave_path)
        link.iface = iface
        self.links[address] = link

        # GATT notify -> PTY (the RNodeInterface's serial port reads it)
        client.on_payload(lambda data: self.from_device(link, data))

        # PTY -> GATT write (RX char), thread-based so the core has no
        # asyncio dependency; bleak writes hop into the loop via threadsafe
        # scheduling in the Link.
        link.reader = threading.Thread(target=self.pty_reader, args=(link,),
                                       daemon=True, name=f"ble-pty-{link.tag}")
        link.reader.start()
        self.log(f"[ble-bridge] {link.iface_name}: interface up on {slave_path}")
        return link

    def tear_down(self, address, reason=""):
        link = self.links.pop(address, None)
        if not link:
            return
        link.alive = False
        try:
            link.iface.detach()
        except Exception as e:
            self.log(f"[ble-bridge] detach {link.iface_name}: {e}")
        self.log(f"[ble-bridge] {link.iface_name}: down ({reason})")

    # -- data path ---------------------------------------------------------

    def from_device(self, link, data):
        """GATT notify -> PTY master (RNodeInterface's serial reads the slave)."""
        try:
            os.write(link.master_fd, data)
        except OSError:
            self.tear_down(link.address, "pty write failed")

    def pty_reader(self, link):
        """PTY master -> GATT write (the RNodeInterface's KISS output)."""
        sel = selectors.DefaultSelector()
        sel.register(link.master_fd, selectors.EVENT_READ)
        try:
            while link.alive:
                events = sel.select(timeout=0.15)
                for _key, _mask in events:
                    try:
                        data = os.read(link.master_fd, 512)
                    except OSError:
                        link.alive = False
                        break
                    if not data:
                        link.alive = False
                        break
                    self.to_device(link, data)
        finally:
            try:
                os.close(link.master_fd)
            except OSError:
                pass
            try:
                sel.unregister(link.master_fd)
            except Exception:
                pass

    def to_device(self, link, data):
        res = link.client.write_payload(data)
        if inspect.iscoroutine(res):
            _schedule(res)

    # -- lifecycle loop (async: bleak is async) ----------------------------

    async def run(self, scanner, connector):
        self.log(f"[ble-bridge] scanning for {BLE_SERVICE_UUID} (prefix {NAME_PREFIX!r})"
                 + (f", IFAC {IFAC_NETNAME!r}" if IFAC_NETNAME else ", open medium")
                 + " — no pairing, connect+talk")
        while not self._stop:
            try:
                for address, name in await scanner():
                    if self._stop:
                        break
                    if address in self.links or len(self.links) >= MAX_DEVICES:
                        continue
                    if name and NAME_PREFIX and not name.startswith(NAME_PREFIX):
                        continue
                    client = await connector(address)
                    if client is None:
                        continue
                    self.log(f"[ble-bridge] connected {address} ({client.name or name or 'unnamed'})")
                    try:
                        self.bring_up(address, name, client)
                    except Exception as e:
                        self.log(f"[ble-bridge] bring_up {address} failed: {e}")
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
            except Exception as e:
                self.log(f"[ble-bridge] scan error: {e}")
            # prune links whose GATT connection dropped (supervision timeout
            # fired on either side) — graceful out-of-range disconnect
            for address in list(self.links):
                client = self.links[address].client
                if client.is_connected() is False:
                    self.tear_down(address, "connection lost")
            await asyncio.sleep(SCAN_INTERVAL_S)

    def stop(self):
        self._stop = True
        for address in list(self.links):
            self.tear_down(address, "shutdown")


def _schedule(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    loop.create_task(coro)


# -- bleak wiring (imported only where BlueZ+bleak exist) ----------------------

class BleakLink:
    """The client shape Bridge expects, wrapping a bleak client."""

    def __init__(self, client, address, name=""):
        self.client = client
        self.address = address
        self.name = name
        self._cb = None
        self._loop = asyncio.get_event_loop()

    def _dispatch(self, _char, data):
        if self._cb:
            self._cb(data)

    async def start_notify(self):
        await self.client.start_notify(BLE_TX_CHAR_UUID, self._dispatch)

    def is_connected(self):
        return bool(self.client.is_connected)

    def write_payload(self, data):
        # called from the PTY reader thread: hop into the bleak loop
        return asyncio.run_coroutine_threadsafe(
            self.client.write_gatt_char(BLE_RX_CHAR_UUID, data), self._loop)

    def on_payload(self, cb):
        self._cb = cb

    async def disconnect(self):
        try:
            await self.client.stop_notify(BLE_TX_CHAR_UUID)
        except Exception:
            pass
        await self.client.disconnect()


def bleak_scanner():
    from bleak import BleakScanner

    async def _scan():
        found = {}
        for d, adv in (await BleakScanner.discover(
                timeout=2.0, return_adv=True,
                service_uuids=[BLE_SERVICE_UUID])).values():
            found[d.address] = (d.address, d.name or adv.local_name or "")
        return list(found.values())
    return _scan


def bleak_connector():
    from bleak import BleakClient

    async def _connect(address):
        client = BleakClient(address)
        try:
            await client.connect()
            # NOTE: no Pair(). The firmware never requests security, so this
            # is a plain GATT connect — no user prompt on any OS.
            link = BleakLink(client, address)
            await link.start_notify()
            return link
        except Exception as e:
            print(f"[ble-bridge] connect {address} failed: {e}", flush=True)
            try:
                await client.disconnect()
            except Exception:
                pass
            return None
    return _connect


# -- main ----------------------------------------------------------------------

def main():
    import RNS  # python rns via pip (in the image)
    from RNS.Interfaces import RNodeInterface

    cfg_dir = os.path.expanduser(RNS_CONFIG_DIR)
    os.makedirs(cfg_dir, exist_ok=True)
    cfg_path = os.path.join(cfg_dir, "config")
    if not os.path.isfile(cfg_path):
        with open(cfg_path, "w") as f:
            f.write(
                "[reticulum]\n"
                "  enable_transport = No\n"
                "  share_instance = No\n"
                "\n"
                "[interfaces]\n"
                "  [[retcon-main]]\n"
                "    type = TCPClientInterface\n"
                "    enabled = yes\n"
                f"    target_host = {LOOPBACK_HOST}\n"
                f"    target_port = {LOOPBACK_PORT}\n"
            )
        print(f"[ble-bridge] wrote {cfg_path}")

    ret = RNS.Reticulum(cfg_dir)  # 1.2.x runs from __init__
    bridge = Bridge(reticulum=ret, rnode_if_mod=RNodeInterface)
    import signal
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: bridge.stop())

    print(f"[ble-bridge] own RNS instance live in {cfg_dir}; "
          f"loopback -> {LOOPBACK_HOST}:{LOOPBACK_PORT}", flush=True)
    try:
        asyncio.run(bridge.run(bleak_scanner(), bleak_connector()))
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        print("[ble-bridge] stopped", flush=True)


if __name__ == "__main__":
    main()
