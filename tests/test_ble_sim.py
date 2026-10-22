#!/usr/bin/env python3

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import gatt, hci
from bumble.controller import Controller
from bumble.core import AdvertisingData
from bumble.device import Device, DeviceConfiguration, Peer
from bumble.link import LocalLink
from bumble.pairing import PairingConfig, PairingDelegate

import gamepad
from ble_pads import APPEARANCE_GAMEPAD, BlePadManager
from gamepad import BluetoothGamepad, PadState
from test_gamepad_sim import blueretro_hid_parser, decode_generic

PAD_PUBLIC = "F0:F1:F2:F3:F4:F5"
HOST_ADDR = "E0:E1:E2:E3:E4:E5"
failures = []


def check(cond, what):
    print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        failures.append(what)


def _addr(a) -> str:
    return str(a).split("/")[0]


def patch_local_link() -> None:
    original = LocalLink.send_acl_data

    def send_acl_data(self, sender_controller, destination_address, transport, data):
        from bumble import core
        if transport != core.PhysicalTransport.LE:
            return original(self, sender_controller, destination_address, transport, data)
        conn = next((c for c in sender_controller.le_connections.values()
                     if _addr(c.peer_address) == _addr(destination_address)), None)
        source = conn.self_address if conn is not None else sender_controller.random_address
        dest = self.find_le_controller(destination_address)
        if dest is not None:
            asyncio.get_running_loop().call_soon(
                lambda: dest.on_link_acl_data(source, transport, data))

    LocalLink.send_acl_data = send_acl_data


class FakeClassic:
    live = True
    combos = 0

    def press_inquiry_combo(self):
        FakeClassic.combos += 1
        return True


async def find_gamepad(host: Device, exclude: set, timeout=5.0):
    found = asyncio.get_running_loop().create_future()

    def on_adv(adv):
        appearance = adv.data.get(AdvertisingData.APPEARANCE)
        if appearance is not None and not found.done() and str(adv.address) not in exclude:
            value = int(appearance) if isinstance(appearance, int) else int.from_bytes(bytes(appearance), "little")
            if value == APPEARANCE_GAMEPAD:
                found.set_result(adv.address)

    host.on("advertisement", on_adv)
    await host.start_scanning(active=False)
    try:
        return await asyncio.wait_for(found, timeout)
    finally:
        host.remove_listener("advertisement", on_adv)
        await host.stop_scanning()


async def hid_session(host: Device, address, reconnect: bool, current: dict):
    conn = await host.connect(address)
    if reconnect:
        await conn.encrypt()
        print("  re-encrypted with the stored key")
    else:
        await conn.pair()
        print("  paired")
    check(conn.is_encrypted, "link encrypted")

    peer = Peer(conn)
    mtu = await peer.request_mtu(247)
    check(mtu <= 247, f"server MTU {mtu} <= BlueRetro's request")
    await peer.discover_services()
    hid = peer.get_services_by_uuid(gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE)
    check(len(hid) == 1, "one HID service (0x1812)")
    await hid[0].discover_characteristics()
    report_map_c = hid[0].get_characteristics_by_uuid(gatt.GATT_REPORT_MAP_CHARACTERISTIC)[0]
    report_c = hid[0].get_characteristics_by_uuid(gatt.GATT_REPORT_CHARACTERISTIC)[0]
    report_map = bytes(await report_map_c.read_value())
    check(report_map == gamepad.HID_REPORT_MAP, "Report Map read intact")
    await report_c.discover_descriptors()
    ref = report_c.get_descriptor(gatt.GATT_REPORT_REFERENCE_DESCRIPTOR)
    ref_value = bytes(await ref.read_value()) if ref else b""
    check(ref_value == bytes([1, 1]), "Report Reference = ID 1, input")
    reports = blueretro_hid_parser(report_map)
    check(1 in reports, "BlueRetro's parser finds report ID 1")

    received = asyncio.Queue()
    await report_c.subscribe(lambda value: received.put_nowait(bytes(value)))
    first = []
    current["state"] = PadState(x=0, y=0)
    current["state"].press(gamepad.N64_A)
    try:
        while len(first) < 8:
            first.append(await asyncio.wait_for(received.get(), 3))
    except asyncio.TimeoutError:
        pass
    decoded = [decode_generic(reports[1], v) for v in first]
    neutral = lambda d: d["x"] == 0 and d["y"] == 0 and not d["buttons"]
    check(len(decoded) >= 3 and all(neutral(d) for d in decoded[:3]),
          "first 3 notifications neutral (calibration)")
    check(any("A" in d["buttons"] for d in decoded), "held A arrives once live")

    s = PadState(hat=gamepad.HAT_N)
    s.press(gamepad.N64_START); s.press(gamepad.N64_Z); s.press(gamepad.N64_C_UP)
    current["state"] = s
    got = None
    for _ in range(40):
        got = decode_generic(reports[1], await asyncio.wait_for(received.get(), 3))
        if got["buttons"] == {"Start", "Z", "C-Up"}:
            break
    check(got and got["buttons"] == {"Start", "Z", "C-Up"} and got["dpad"] == "Up",
          f"Start + Z + C-Up + D-pad up: {got}")
    current["state"] = PadState()
    return conn


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="    log: %(message)s")
    logging.getLogger("ble_pads").setLevel(logging.INFO)

    patch_local_link()
    link = LocalLink()
    pad_ctrl = Controller("gamepad", link=link, public_address=PAD_PUBLIC)
    host_ctrl = Controller("blueretro", link=link, public_address=HOST_ADDR)

    classic = BluetoothGamepad.create(pad_ctrl, pad_ctrl, "BlueNetplay Pad", le=True)
    await classic.start()
    store = Path(tempfile.mkdtemp()) / "ble_pads.json"
    mgr = BlePadManager(classic.device, FakeClassic(), store, players=2)

    host = Device.from_config_with_hci(
        DeviceConfiguration(name="BlueRetro", classic_enabled=False, le_enabled=True,
                            address=hci.Address(HOST_ADDR)), host_ctrl, host_ctrl)
    host.pairing_config_factory = lambda c: PairingConfig(
        sc=False, mitm=False, bonding=True,
        delegate=PairingDelegate(PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT))
    await host.power_on()

    states = {2: {"state": PadState()}, 3: {"state": PadState()}}

    async def loop():
        while True:
            await mgr.tick()
            mgr.pump({p: s["state"] for p, s in states.items()})
            await asyncio.sleep(0.004)
    task = asyncio.create_task(loop())

    print("\n== Nothing advertised until a player asks to pair ==")
    await asyncio.sleep(0.3)
    check(mgr.advertising_for is None, "no BLE advertising for unpaired pads by default")

    conns = {}
    for player in (2, 3):
        print(f"\n== Player {player}: first pairing ==")
        mgr.request_pairing(player)
        await asyncio.sleep(0.3)
        check(mgr.advertising_for is not None and mgr.advertising_for.player == player,
              f"advertising player {player} only")
        check(FakeClassic.combos >= 1, "player 1 asked BlueRetro to search (combo)")
        addr = await find_gamepad(host, exclude={str(c.peer_address) for c in conns.values()})
        print(f"  found {addr}")
        check(str(addr).split("/")[0] == str(mgr.pads[player - 2].address).split("/")[0],
              f"advertised with player {player}'s own address")
        conns[player] = await hid_session(host, addr, False, states[player])
        await asyncio.sleep(0.2)
        check(mgr.pads[player - 2].paired, f"player {player} key stored")

    own = [_addr(p.connection.self_address) for p in mgr.pads if p.connection]
    check(len(own) == 2 and own[0] != own[1] and own == [_addr(p.address) for p in mgr.pads],
          f"each pad's link uses its own address: {own}")
    check(store.exists() and store.read_text().count('"ltk": "') == 2, "both keys persisted to file")
    task.cancel()

    await reconnect_test()


async def reconnect_test() -> None:
    print("\n== Reconnect with the stored key (single pad, fresh link) ==")
    link = LocalLink()
    pad_ctrl = Controller("gamepad2", link=link, public_address="F0:F1:F2:F3:F4:F6")
    host_ctrl = Controller("blueretro2", link=link, public_address="E0:E1:E2:E3:E4:E6")
    classic = BluetoothGamepad.create(pad_ctrl, pad_ctrl, "BlueNetplay Pad", le=True)
    await classic.start()
    store = Path(tempfile.mkdtemp()) / "ble_pads.json"
    mgr = BlePadManager(classic.device, FakeClassic(), store, players=1)
    host = Device.from_config_with_hci(
        DeviceConfiguration(name="BlueRetro", classic_enabled=False, le_enabled=True,
                            address=hci.Address("E0:E1:E2:E3:E4:E6")), host_ctrl, host_ctrl)
    host.pairing_config_factory = lambda c: PairingConfig(
        sc=False, mitm=False, bonding=True,
        delegate=PairingDelegate(PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT))
    await host.power_on()
    from bumble.keys import MemoryKeyStore
    host.keystore = MemoryKeyStore()
    state = {"state": PadState()}

    async def loop():
        while True:
            await mgr.tick()
            mgr.pump({2: state["state"]})
            await asyncio.sleep(0.004)
    task = asyncio.create_task(loop())

    mgr.request_pairing(2)
    addr = await find_gamepad(host, exclude=set())
    conn = await hid_session(host, addr, False, state)
    await asyncio.sleep(0.2)
    stored = [name for name, _ in await host.keystore.get_all()]
    check(stored == [_addr(mgr.pads[0].address)],
          f"BlueRetro side files the key under the pad's own address only: {stored}")
    await conn.disconnect()
    await asyncio.sleep(0.5)

    mgr2_pads = json_pads(store)
    check(mgr2_pads and mgr2_pads[0]["ltk"], "key survives in the file for the next run")
    combos_before = FakeClassic.combos
    addr = await find_gamepad(host, exclude=set())
    check(_addr(addr) == _addr(mgr.pads[0].address), "paired pad re-advertises by itself")
    await hid_session(host, addr, True, state)
    check(FakeClassic.combos == combos_before, "no search combo needed for a paired pad")
    task.cancel()


def json_pads(path: Path):
    import json
    return json.loads(path.read_text()).get("pads", [])


async def _run() -> int:
    await main()
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
