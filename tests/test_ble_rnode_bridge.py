#!/usr/bin/env python3
# tests for utils/system/ble_rnode_bridge.py
#
# The decisive test drives a REAL python-RNS RNodeInterface against a FAKE
# dongle speaking the RNode KISS contract the microretcon firmware implements
# (byte-identical to the RAK R1 responder + the radio-config echo contract):
#
#   DETECT probe                  -> DETECT_RESP, FW 1.52, PLATFORM_NRF52, MCU_NRF52
#   CMD_FREQUENCY/BANDWIDTH/...   -> echo stored values back (validateRadioState
#                                    requires the echo or the iface never arms)
#   CMD_DATA                      -> captured (transport test)
#
# No BLE, no BlueZ, no hardware: RNodeInterface is opened on a PTY pair whose
# master is the fake dongle. PYTHONPATH must include the vendored RNS site-
# packages (crns tests venv).

import os
import select
import sys
import threading
import time

VENV = os.environ.get(
    "BLE_TEST_RNS_SITE_PACKAGES",
    "/home/beard/RETCON/crns/tests/vectors/.venv/lib/python3.14/site-packages")
sys.path.insert(0, VENV)

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "utils", "system"))

import ble_rnode_bridge as br


# -- RNode KISS constants (byte-exact with RNode_Firmware/Utilities.h) --------

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_DATA = 0x00
CMD_FREQUENCY = 0x01
CMD_BANDWIDTH = 0x02
CMD_TXPOWER = 0x03
CMD_SF = 0x04
CMD_CR = 0x05
CMD_RADIO_STATE = 0x06
CMD_DETECT = 0x08
CMD_ST_ALOCK = 0x0B
CMD_LT_ALOCK = 0x0C
DETECT_REQ = 0x73
DETECT_RESP = 0x46
CMD_FW_VERSION = 0x50
CMD_PLATFORM = 0x48
CMD_MCU = 0x49
PLATFORM_NRF52 = 0x70
MCU_NRF52 = 0x71
FW_MAJ, FW_MIN = 1, 52
RADIO_STATE_ON = 0x01


def frame(cmd, data=b""):
    out = bytes([FEND, cmd])
    for b in data:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out += bytes([b])
    return out + bytes([FEND])


class FakeDongle:
    """Speaks the RNode KISS contract over the master end of a PTY. Records
    everything the host sends, echoes radio-config per the RNode contract."""

    def __init__(self, master_fd, platform=PLATFORM_NRF52, echo_config=True):
        self.master_fd = master_fd
        self.echo_config = echo_config  # False = answers DETECT only
        self.received_data = []
        self.platform = platform
        self.received_data = []          # CMD_DATA payloads (host -> dongle)
        self.sent_to_host = []           # frames written back to the iface
        self.thread = None
        self._alive = False
        self._stored = {}                # last radio config seen

    def start(self):
        self._alive = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self._alive = False
        if self.thread:
            self.thread.join(timeout=2)

    def _write(self, frame):
        self.sent_to_host.append(frame)
        os.write(self.master_fd, frame)

    def _run(self):
        """KISS stream parser with resync (buf may start mid-stream). Calls
        self._handle(cmd, unescaped_body) per complete frame."""
        sel = select.select
        buf = b""
        while self._alive:
            r, _, _ = sel([self.master_fd], [], [], 0.1)
            if not r:
                continue
            try:
                buf += os.read(self.master_fd, 1024)
            except OSError:
                # EIO: the slave has no reader yet (the RNodeInterface opens
                # it later) - a real dongle survives this; so do we.
                time.sleep(0.05)
                continue
            while True:
                if not buf:
                    break
                start = buf.find(FEND)
                if start < 0:
                    buf = b""  # garbage before any frame: drop, resync
                    break
                if start > 0:
                    buf = buf[start:]
                end = buf.find(FEND, 1)
                if end < 0:
                    break  # frame not complete yet
                # NOTE: python RNS's probe SHARES the boundary FEND between
                # consecutive frames (FEND cmd data FEND cmd data FEND) —
                # the closing FEND also opens the next frame. Keep it in the
                # buffer and let the bare-FEND guard handle line resets.
                chunk = buf[: end + 1]
                buf = buf[end:]
                if len(chunk) <= 2:
                    continue  # bare FEND = line reset
                cmd = chunk[1]
                body = chunk[2:-1]
                body = body.replace(bytes([FESC, TFEND]), bytes([FEND]))
                body = body.replace(bytes([FESC, TFESC]), bytes([FESC]))
                self._handle(cmd, body)

    def _handle(self, cmd, body):
        if cmd == CMD_DETECT and body == bytes([DETECT_REQ]):
            self._write(frame(CMD_DETECT, bytes([DETECT_RESP])))
            self._write(frame(CMD_FW_VERSION, bytes([FW_MAJ, FW_MIN])))
            self._write(frame(CMD_PLATFORM, bytes([self.platform])))
            self._write(frame(CMD_MCU, bytes([MCU_NRF52])))
        elif self.echo_config and cmd in (CMD_FREQUENCY, CMD_BANDWIDTH):
            if len(body) == 4:
                self._stored[cmd] = body
                self._write(frame(cmd, body))
        elif self.echo_config and cmd in (CMD_TXPOWER, CMD_SF, CMD_CR) and len(body) == 1:
            self._stored[cmd] = body
            self._write(frame(cmd, body))
        elif self.echo_config and cmd == CMD_RADIO_STATE and len(body) == 1:
            self._stored[cmd] = body
            self._write(frame(cmd, bytes([RADIO_STATE_ON])))
        elif cmd == CMD_DATA:
            self.received_data.append(body)


class FakeClient:
    """Bridge client double: no BLE, records GATT writes."""

    def __init__(self):
        self.written = []
        self.name = "FakeDongle"
        self.connected = True

    def is_connected(self):
        return self.connected

    def write_payload(self, data):
        self.written.append(bytes(data))

    def on_payload(self, cb):
        self._cb = cb

    def feed(self, data):
        self._cb(data)

    async def disconnect(self):
        self.connected = False


# -- fixtures ------------------------------------------------------------------

@pytest.fixture(scope="module")
def rns_mod():
    import RNS
    return RNS


# Module-scoped: RNS 1.2.x has a module-level singleton (Reticulum.__instance
# raises on a second instance in-process), so tests share one instance.
@pytest.fixture(scope="module")
def host_instance(rns_mod, tmp_path_factory):
    from RNS import Reticulum
    tmp = tmp_path_factory.mktemp("rns")
    cfg = tmp / "config"
    # share_instance=No - the real bridge config does too; otherwise 1.2.6
    # runs a shared-instance server and _add_interface lands nowhere.
    cfg.write_text("[reticulum]\n  enable_transport = No\n  share_instance = No\n")
    ret = Reticulum(str(tmp))
    return ret  # 1.2.x runs from __init__; crns binding has start(), RNS doesn't


def _find_iface(ret, name_part):
    for i in ret.interfaces:
        if name_part in str(getattr(i, "name", "")):
            return i
    return None


# -- tests ----------------------------------------------------------------------

def test_bring_up_detects_firmware_and_arms_interface(host_instance, rns_mod):
    """RNodeInterface over a BLE-shaped PTY: DETECT + the radio-config echo
    contract pass -> interface armed. The no-pairing BLE path IS a full
    reticulum interface (this is exactly what the firmware runs + the Pi
    bridge serves)."""
    from RNS.Interfaces import RNodeInterface
    m, s = os.openpty()
    slave_path = os.ttyname(s)
    os.close(s)                       # RNodeInterface opens the slave itself
    dongle = FakeDongle(m).start()    # dongle owns the master end

    iface = rns_mod.Interfaces.RNodeInterface.RNodeInterface(host_instance, {
        "name": "Test BLE RNode",
        "frequency": 914875000, "bandwidth": 125000, "txpower": 14,
        "spreadingfactor": 8, "codingrate": 5, "flow_control": False,
        "port": slave_path,
    })
    assert iface.detected is True
    host_instance._add_interface(iface, ifac_netname="rt",
                                 ifac_netkey="testkey",
                                 configured_bitrate=10000)
    assert iface in rns_mod.Transport.interfaces
    iface.initRadio()
    assert iface.validateRadioState() is True   # the echo contract held
    dongle.stop()


def test_data_path_host_to_device(host_instance, rns_mod):
    """KISS DATA written by the RNodeInterface reaches the dongle's CMD_DATA
    handler — the transport half of 'BLE is just a serial rnode'."""
    from RNS.Interfaces import RNodeInterface
    m, s = os.openpty()
    slave_path = os.ttyname(s)
    os.close(s)
    dongle = FakeDongle(m).start()
    iface = rns_mod.Interfaces.RNodeInterface.RNodeInterface(host_instance, {
        "name": "DataPath BLE RNode",
        "frequency": 914875000, "bandwidth": 125000, "txpower": 14,
        "spreadingfactor": 8, "codingrate": 5, "flow_control": False,
        "port": slave_path,
    })
    assert iface.detected is True
    iface.initRadio()
    assert iface.validateRadioState() is True

    deadline = time.time() + 2.0
    while time.time() < deadline and not iface.online:
        time.sleep(0.05)   # "powered up" sleeps 0.3 before online=True
    assert iface.online is True

    payload = bytes(range(64))
    iface.process_outgoing(payload)   # sends KISS DATA + escape
    deadline = time.time() + 2.0
    while time.time() < deadline and not dongle.received_data:
        time.sleep(0.02)
    assert dongle.received_data, "dongle never received CMD_DATA"
    assert payload in dongle.received_data
    dongle.stop()


def test_echo_contract_required_for_ready(host_instance, rns_mod):
    """A dongle that answers DETECT but does NOT echo radio config fails
    validateRadioState — proving why the echo is load-bearing firmware."""
    from RNS.Interfaces import RNodeInterface
    m, s = os.openpty()
    slave_path = os.ttyname(s)
    os.close(s)

    dongle = FakeDongle(m, echo_config=False).start()
    iface = rns_mod.Interfaces.RNodeInterface.RNodeInterface(host_instance, {
        "name": "Silent Dongle",
        "frequency": 914875000, "bandwidth": 125000, "txpower": 14,
        "spreadingfactor": 8, "codingrate": 5, "flow_control": False,
        "port": slave_path,
    })
    assert iface.detected is True            # DETECT passed…
    assert iface.validateRadioState() is False  # …validation fails without echo
    dongle.stop()


def test_bridge_lifecycle_prunes_lost_links():
    """Bridge.tear_down detaches and drops the link; run()-loop prunes
    is_connected()=False clients (graceful out-of-range path)."""
    class _Mod:
        RNodeInterface = None
    bridge = br.Bridge(rnode_if_mod=_Mod, log=lambda *a, **k: None)
    fake = FakeClient()
    link = br.DeviceLink("AA:BB:CC:DD:EE:01", "RETCON-01", client=fake)
    bridge.links["AA:BB:CC:DD:EE:01"] = link
    link.alive = False
    bridge.tear_down("AA:BB:CC:DD:EE:01", "test")
    assert "AA:BB:CC:DD:EE:01" not in bridge.links
    assert link.alive is False

    fake2 = FakeClient(); fake2.connected = False
    link2 = br.DeviceLink("AA:BB:CC:DD:EE:02", "RETCON-02", client=fake2)
    bridge.links["AA:BB:CC:DD:EE:02"] = link2
    # prune step normally lives in run(); replicate its prune block here
    for address in list(bridge.links):
        client = bridge.links[address].client
        if client.is_connected() is False:
            bridge.tear_down(address, "connection lost")
    assert "AA:BB:CC:DD:EE:02" not in bridge.links


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))