#!/usr/bin/env python3

import asyncio
import logging
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bumble import core, hci, l2cap
from bumble.controller import Controller
from bumble.device import Device, DeviceConfiguration
from bumble.link import LocalLink

import gamepad
from gamepad import BluetoothGamepad, PadState

GAMEPAD_ADDR = "F0:F1:F2:F3:F4:F5"
HOST_ADDR = "E0:E1:E2:E3:E4:E5"
BLUERETRO_MTU = 672

failures = []


def check(cond, what):
    print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        failures.append(what)


def blueretro_sdp_parser(sdp_data: bytes, pnp_data: bytes):
    vid_at = pnp_data.find(bytes([0x09, 0x02, 0x01]))
    pid_at = pnp_data.find(bytes([0x09, 0x02, 0x02]))
    if vid_at < 0 or pid_at < 0:
        return None, None, None
    vid = struct.unpack_from(">H", pnp_data, vid_at + 4)[0]
    pid = struct.unpack_from(">H", pnp_data, pid_at + 4)[0]

    at = sdp_data.find(bytes([0x09, 0x02, 0x06]))
    if at < 0:
        return vid, pid, None
    i = at + 3
    i += {0x35: 2, 0x36: 3, 0x37: 5}.get(sdp_data[i], 0)
    i += {0x35: 4, 0x36: 5, 0x37: 7}.get(sdp_data[i], 0)
    kind = sdp_data[i]
    i += 1
    if kind == 0x25:
        length = sdp_data[i]; i += 1
    elif kind == 0x26:
        length = struct.unpack_from(">H", sdp_data, i)[0]; i += 2
    elif kind == 0x27:
        length = struct.unpack_from(">I", sdp_data, i)[0]; i += 4
    else:
        return vid, pid, None
    return vid, pid, sdp_data[i:i + length]


HID_PARSER_ITEMS = {
    0x00: 0, 0x01: 1, 0x03: 4, 0x05: 1, 0x06: 2, 0x07: 4, 0x09: 1, 0x19: 1,
    0x0A: 2, 0x1A: 2, 0x0D: 1, 0x15: 1, 0x16: 2, 0x17: 4, 0x25: 1, 0x26: 2,
    0x27: 4, 0x29: 1, 0x2A: 2, 0x2B: 4, 0x35: 1, 0x45: 1, 0x46: 2, 0x55: 1,
    0x65: 1, 0x66: 2, 0x75: 1, 0x7C: 0, 0x91: 1, 0x81: 1, 0x85: 1, 0x95: 1,
    0x96: 2, 0xA1: 1, 0xA4: 0, 0xB1: 1, 0xB2: 2, 0xB4: 0, 0xC0: 0, 0xFF: 4,
}
GD_COLLECTION_USAGES = {0x01, 0x02, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x3A, 0x80}


def blueretro_hid_parser(desc: bytes):
    reports = {}
    page = lmin = lmax = size = count = 0
    report_id = 0
    usages = []
    bit = 0
    i = 0
    while i < len(desc):
        item = desc[i]
        i += 1
        if item not in HID_PARSER_ITEMS:
            raise ValueError(f"Unknown HID marker: {item:02X}")
        n = HID_PARSER_ITEMS[item]
        raw = desc[i:i + n]
        i += n
        val = int.from_bytes(raw, "little") if n else 0
        sval = int.from_bytes(raw, "little", signed=True) if n else 0
        if item == 0x05:
            page = val
        elif item in (0x09, 0x19, 0x0A):
            if not (page == 0x01 and val in GD_COLLECTION_USAGES):
                usages.append(val)
        elif item == 0x15:
            lmin = sval
        elif item in (0x25, 0x26, 0x27):
            lmax = sval
        elif item == 0x75:
            size = val
        elif item == 0x95:
            count = val
        elif item == 0x85:
            report_id = val
            bit = 0
        elif item == 0x81:
            fields = reports.setdefault(report_id, [])
            if not (val & 0x01) and usages:
                merge = len(usages) == 1 and size == 1 and count > 1
                if merge:
                    fields.append(dict(page=page, usage=usages[0], bit=bit,
                                       size=count, lmin=lmin, lmax=lmax))
                    bit += count
                else:
                    for k in range(count):
                        usage = usages[0] if len(usages) == 1 else usages[k]
                        fields.append(dict(page=page, usage=usage, bit=bit,
                                           size=size, lmin=lmin, lmax=lmax))
                        bit += size
            else:
                bit += size * count
            usages = []
    out = {}
    for rid, fields in reports.items():
        end = max((f["bit"] + f["size"] for f in fields), default=0)
        out[rid] = dict(fields=fields, length=(end + 7) // 8)
    if 0 in out:
        out[1] = out.pop(0)
    return out


HAT_NAMES = ["Up", "Up-Right", "Right", "Down-Right", "Down", "Down-Left", "Left", "Up-Left"]

HID_BUTTON_TO_N64 = {
    1: "A", 2: "C-Down", 3: "C-Right", 4: "B", 5: "C-Left", 6: "C-Up",
    7: "L", 8: "R", 9: "Z", 10: "Z", 12: "Start", 13: "Pak", 16: "Bank",
}


def decode_generic(report: dict, data: bytes):
    bits = int.from_bytes(data, "little")
    out = {"buttons": set(), "dpad": None, "x": None, "y": None}
    for f in report["fields"]:
        value = (bits >> f["bit"]) & ((1 << f["size"]) - 1)
        if f["page"] == 0x09:
            for n in range(f["size"]):
                if value & (1 << n):
                    out["buttons"].add(HID_BUTTON_TO_N64.get(f["usage"] + n, f"Btn{f['usage'] + n}"))
        elif f["page"] == 0x01 and f["usage"] == 0x39:
            hat = (value - f["lmin"]) & 0xF
            out["dpad"] = HAT_NAMES[hat] if hat < 8 else None
        elif f["page"] == 0x01 and f["usage"] in (0x30, 0x31):
            neutral = f["lmax"] // 2 + 1
            n64 = round((value - neutral) * 84 / (f["lmax"] - neutral) * 0.95)
            if f["usage"] == 0x31:
                n64 = -n64
            out["x" if f["usage"] == 0x30 else "y"] = n64
    return out


XB1_BTN_TO_N64 = {0: "A", 1: "C-Down", 2: "B", 3: "C-Left", 4: "L", 5: "R", 7: "Start"}
XB1_STICK_ABS = 0x7FFF * 0.95
XB1_TRIG_ABS = 0x3FF * 0.95


def decode_xbox(report_id: int, data: bytes, cal: dict):
    out = {"buttons": set(), "dpad": None, "x": None, "y": None}
    if report_id == 2:
        if data[0] & 0x01:
            out["buttons"].add("Pak")
        return out
    lx, ly, rx, ry, lt, rt, hat, buttons, _ = struct.unpack("<6HBHB", data[:16])
    axes = [lx, ly, rx, ry, lt, rt]
    neutral = [0x8000] * 4 + [0, 0]
    if not cal:
        cal.update({i: -(axes[i] - neutral[i]) for i in range(6)})
    v = [axes[i] - neutral[i] + cal[i] for i in range(6)]
    for bit, name in XB1_BTN_TO_N64.items():
        if buttons & (1 << bit):
            out["buttons"].add(name)
    h = (hat - 1) & 0xF
    out["dpad"] = HAT_NAMES[h] if h < 8 else None
    if v[4] >= 0.5 * XB1_TRIG_ABS or v[5] >= 0.5 * XB1_TRIG_ABS:
        out["buttons"].add("Z")
    if v[2] >= 0.5 * XB1_STICK_ABS:
        out["buttons"].add("C-Right")
    elif v[2] <= -0.5 * XB1_STICK_ABS:
        out["buttons"].add("C-Left")
    if -v[3] >= 0.5 * XB1_STICK_ABS:
        out["buttons"].add("C-Up")
    elif -v[3] <= -0.5 * XB1_STICK_ABS:
        out["buttons"].add("C-Down")
    scale = lambda d: max(-84, min(84, round(d * 84 / XB1_STICK_ABS)))
    out["x"], out["y"] = scale(v[0]), scale(-v[1])
    return out


async def sdp_search_attr_raw(channel, queue, uuid16: int) -> bytes:
    data = b""
    cont = b"\x00"
    tid = 0
    while True:
        tid += 1
        params = (bytes([0x35, 0x03, 0x19]) + struct.pack(">H", uuid16)
                  + struct.pack(">H", 1024)
                  + bytes([0x35, 0x05, 0x0A, 0x00, 0x00, 0xFF, 0xFF])
                  + cont)
        channel.write(bytes([0x06]) + struct.pack(">HH", tid, len(params)) + params)
        rsp = await asyncio.wait_for(queue.get(), 5)
        assert rsp[0] == 0x07, f"unexpected SDP PDU {rsp[0]:02X}"
        count = struct.unpack_from(">H", rsp, 5)[0]
        data += rsp[7:7 + count]
        cont_len = rsp[7 + count]
        cont = rsp[7 + count:7 + count + 1 + cont_len]
        if cont_len == 0:
            return data


async def run_profile(key: str, link: LocalLink, n: int) -> None:
    profile = gamepad.PROFILES[key]
    print(f"\n######## {key} profile ########")
    pad_addr = GAMEPAD_ADDR[:-1] + str(n)
    host_addr = HOST_ADDR[:-1] + str(n)
    pad_ctrl = Controller(f"gamepad-{key}", link=link, public_address=pad_addr)
    host_ctrl = Controller(f"blueretro-{key}", link=link, public_address=host_addr)

    pad = BluetoothGamepad.create(pad_ctrl, pad_ctrl, profile.default_name, profile)
    await pad.start()

    host = Device.from_config_with_hci(
        DeviceConfiguration(name="BlueRetro", classic_enabled=True, le_enabled=False,
                            address=hci.Address(host_addr), class_of_device=0x1C010C),
        host_ctrl, host_ctrl)
    await host.power_on()

    print("\n== Connection ==")
    conn = await host.connect(pad_addr, transport=core.PhysicalTransport.BR_EDR)
    check(conn is not None, "BR/EDR connection established")
    name = await host.request_remote_name(conn)
    print(f"  remote name: {name!r}")
    if key == "xbox":
        check("xbox wireless controller" in name.lower(),
              "name selects BlueRetro's Xbox decoder (BT_XBOX)")
    else:
        check(not any(f.lower() in name.lower() for f in gamepad.RESERVED_NAME_FRAGMENTS),
              "name keeps BlueRetro on the generic HID path")
    try:
        await asyncio.wait_for(conn.authenticate(), 5)
        print("  authentication ok")
    except Exception as error:
        print(f"  (authentication not simulated: {error!r})")

    print("\n== HID L2CAP channels (control, then interrupt) ==")
    reports_rx = asyncio.Queue()
    ctrl = await conn.create_l2cap_channel(l2cap.ClassicChannelSpec(0x11, mtu=BLUERETRO_MTU))
    ctrl.sink = lambda pdu: reports_rx.put_nowait(("ctrl", pdu))
    intr = await conn.create_l2cap_channel(l2cap.ClassicChannelSpec(0x13, mtu=BLUERETRO_MTU))
    intr.sink = lambda pdu: reports_rx.put_nowait(("intr", pdu))
    check(True, "control 0x11 and interrupt 0x13 open")

    current = {"state": PadState()}

    async def pump_loop():
        while True:
            pad.pump(current["state"])
            await asyncio.sleep(0.004)
    pump_task = asyncio.create_task(pump_loop())

    held = PadState(x=0, y=0)
    held.press(gamepad.N64_A)
    current["state"] = held

    generic_report = None
    if key == "generic":
        print("\n== SDP (as BlueRetro does) ==")
        sdp_q = asyncio.Queue()
        sdp_ch = await conn.create_l2cap_channel(l2cap.ClassicChannelSpec(0x0001, mtu=BLUERETRO_MTU))
        sdp_ch.sink = sdp_q.put_nowait
        sdp_data = await sdp_search_attr_raw(sdp_ch, sdp_q, 0x0100)
        pnp_data = await sdp_search_attr_raw(sdp_ch, sdp_q, 0x1200)
        await sdp_ch.disconnect()
        check(len(sdp_data) <= 2048 and len(pnp_data) <= 256, "SDP data fits BlueRetro's buffers")
        vid, pid, desc = blueretro_sdp_parser(sdp_data, pnp_data)
        check(vid == profile.vendor_id and pid == profile.product_id, "PnP VID/PID read correctly")
        check(desc == profile.descriptor, "HID descriptor extracted intact from attribute 0x0206")
        reports = blueretro_hid_parser(desc)
        check(list(reports) == [1] and reports[1]["length"] == gamepad.REPORT_LEN,
              "one input report, ID 1, expected length")
        generic_report = reports[1]
    else:
        reports = blueretro_hid_parser(profile.descriptor)
        check(reports.get(1, {}).get("length") == gamepad.XBOX_REPORT_LEN,
              f"descriptor describes the {gamepad.XBOX_REPORT_LEN}-byte report 1")

    cal = {}

    def decode(pdu):
        if key == "xbox":
            return decode_xbox(pdu[1], pdu[2:], cal)
        return decode_generic(generic_report, pdu[2:])

    print("\n== Input reports ==")
    received = []

    async def collect(seconds):
        end = asyncio.get_running_loop().time() + seconds
        while (left := end - asyncio.get_running_loop().time()) > 0:
            try:
                received.append(await asyncio.wait_for(reports_rx.get(), left))
            except asyncio.TimeoutError:
                break

    await collect(1.2)
    intr_reports = [p for ch, p in received if ch == "intr"]
    check(all(p[0] == 0xA1 and p[1] == 1 for p in intr_reports[:6]),
          "setup reports are 'A1 01 ...' on the interrupt channel")
    check(not any(ch == "ctrl" for ch, _ in received), "nothing on the control channel")
    if key == "xbox":
        check(all(len(p) == 2 + gamepad.XBOX_REPORT_LEN for p in intr_reports if p[1] == 1),
              "Xbox report 1 is 16 bytes")
    decoded = [decode(p) for p in intr_reports]
    neutral = lambda d: d["x"] == 0 and d["y"] == 0 and not d["buttons"] and d["dpad"] is None
    check(len(decoded) >= 3 and all(neutral(d) for d in decoded[:3]),
          "first 3 reports neutral (calibration sample) even with A + stick held")
    check(any("A" in d["buttons"] and d["x"] < -70 and d["y"] > 70 for d in decoded),
          "held A + stick up-left arrive once live")

    async def expect(state, want, label):
        current["state"] = state
        received.clear()
        await collect(0.15)
        got = [decode(p) for ch, p in received if ch == "intr" and p[1] == 1]
        ok = bool(got) and all(got[-1][k] == v for k, v in want.items())
        check(ok, f"{label}: {got[-1] if got else 'no report'}")

    s = PadState(); s.press(gamepad.N64_Z); s.press(gamepad.N64_START)
    await expect(s, {"buttons": {"Z", "Start"}, "x": 0, "y": 0}, "Z + Start")
    await expect(PadState(x=255, y=0), {"buttons": set(), "dpad": None}, "stick full right + up")
    await expect(PadState(hat=gamepad.HAT_W), {"dpad": "Left", "buttons": set()}, "D-pad left")
    await expect(PadState(hat=gamepad.HAT_NE), {"dpad": "Up-Right"}, "D-pad up-right")
    s = PadState()
    for b in (gamepad.N64_C_UP, gamepad.N64_C_RIGHT, gamepad.N64_L, gamepad.N64_R, gamepad.N64_B):
        s.press(b)
    await expect(s, {"buttons": {"C-Up", "C-Right", "L", "R", "B"}}, "C-Up + C-Right + L + R + B")
    s = PadState()
    for b in (gamepad.N64_C_DOWN, gamepad.N64_C_LEFT):
        s.press(b)
    await expect(s, {"buttons": {"C-Down", "C-Left"}}, "C-Down + C-Left")
    await expect(PadState(), {"buttons": set(), "x": 0, "y": 0, "dpad": None}, "release all")

    stick = PadState(x=255, y=128)
    current["state"] = stick
    received.clear()
    await collect(0.15)
    last = [decode(p) for ch, p in received if ch == "intr" and p[1] == 1][-1]
    print(f"  stick full right -> N64 x={last['x']} (N64 range is +/-84)")
    check(70 <= last["x"] <= 84, "full stick tilt reaches near N64 maximum")

    if key == "xbox":
        s = PadState(); s.press(gamepad.N64_PAK)
        current["state"] = s
        received.clear()
        await collect(0.15)
        r2 = [p for ch, p in received if ch == "intr" and p[1] == 2]
        check(len(r2) == 1 and r2[0][2] == 0x01, "Pak toggle sent once as report 2 (Xbox button)")
        current["state"] = PadState()
        await collect(0.1)

    print("\n== Disconnect / reconnect ==")
    await conn.disconnect()
    await asyncio.sleep(0.2)
    check(pad.connection is None and not pad.live, "pad resets its session on disconnect")
    conn = await host.connect(pad_addr, transport=core.PhysicalTransport.BR_EDR)
    await conn.create_l2cap_channel(l2cap.ClassicChannelSpec(0x11, mtu=BLUERETRO_MTU))
    intr = await conn.create_l2cap_channel(l2cap.ClassicChannelSpec(0x13, mtu=BLUERETRO_MTU))
    rx = asyncio.Queue()
    intr.sink = rx.put_nowait
    try:
        pdu = await asyncio.wait_for(rx.get(), max(pad.SDP_FALLBACK_TIME.values()) + 1)
        check(pdu[:2] == bytes([0xA1, 1]), "reports resume after reconnect")
    except asyncio.TimeoutError:
        check(False, "reports resume after reconnect")

    pump_task.cancel()
    await conn.disconnect()


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="    log: %(message)s")
    gamepad.log.setLevel(logging.INFO)
    keys = sys.argv[1:] or ["xbox", "generic"]
    link = LocalLink()
    for n, key in enumerate(keys):
        await run_profile(key, link, n)
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
