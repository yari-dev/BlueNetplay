#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import struct
import sys
import time

from bumble import core, hci, hid, sdp
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.sdp import DataElement, ServiceAttribute
from bumble.transport import open_transport

log = logging.getLogger("gamepad")


CLASS_OF_DEVICE = 0x002508

PNP_VERSION = 0x0100
PNP_VENDOR_ID_SOURCE_USB = 0x0002

RESERVED_NAME_FRAGMENTS = (
    "PLAYSTATION(R)3", "Xbox Wireless Controller", "Wireless Controller",
    "Nintendo RVL-CNT-01", "Pro Controller", "Joy-Con", "SNES Controller",
    "HVC Controller", "NES Controller", "N64 Controller", "MD/Gen Control Pad",
    "8Bitdo", "Retro Bit Bluetooth Controller", "Joy Controller",
    "BlueN64 Gamepad", "Hyperkin Pad", "OUYA Game Controller", "DeviceName",
)


REPORT_ID = 0x01

HID_REPORT_MAP = bytes([
    0x05, 0x01,
    0x09, 0x05,
    0xA1, 0x01,
    0x85, REPORT_ID,

    0x05, 0x09,
    0x19, 0x01,
    0x29, 0x10,
    0x15, 0x00,
    0x25, 0x01,
    0x75, 0x01,
    0x95, 0x10,
    0x81, 0x02,

    0x05, 0x01,
    0x09, 0x39,
    0x15, 0x00,
    0x25, 0x07,
    0x35, 0x00,
    0x46, 0x3B, 0x01,
    0x65, 0x14,
    0x75, 0x04,
    0x95, 0x01,
    0x81, 0x42,
    0x65, 0x00,

    0x75, 0x04,
    0x95, 0x01,
    0x81, 0x03,

    0x09, 0x30,
    0x09, 0x31,
    0x15, 0x00,
    0x26, 0xFF, 0x00,
    0x75, 0x08,
    0x95, 0x02,
    0x81, 0x02,

    0xC0,
])

REPORT_LEN = 5


N64_A       = 1
N64_C_DOWN  = 2
N64_C_RIGHT = 3
N64_B       = 4
N64_C_LEFT  = 5
N64_C_UP    = 6
N64_L       = 7
N64_R       = 8
N64_Z       = 9
N64_START   = 12
N64_PAK     = 13
N64_BANK    = 16

HAT_N, HAT_NE, HAT_E, HAT_SE, HAT_S, HAT_SW, HAT_W, HAT_NW = range(8)
HAT_CENTER = 0x08

AXIS_CENTER = 0x80


class PadState:
    __slots__ = ("buttons", "hat", "x", "y")

    def __init__(self, buttons=0, hat=HAT_CENTER, x=AXIS_CENTER, y=AXIS_CENTER):
        self.buttons = buttons
        self.hat = hat
        self.x = x
        self.y = y

    def press(self, button: int) -> None:
        self.buttons |= 1 << (button - 1)

    def payload(self) -> bytes:
        return bytes([
            self.buttons & 0xFF,
            (self.buttons >> 8) & 0xFF,
            self.hat & 0x0F,
            self.x & 0xFF,
            self.y & 0xFF,
        ])

    def describe(self) -> str:
        names = {
            N64_A: "A", N64_B: "B", N64_Z: "Z", N64_START: "Start",
            N64_L: "L", N64_R: "R", N64_C_UP: "C^", N64_C_DOWN: "Cv",
            N64_C_LEFT: "C<", N64_C_RIGHT: "C>", N64_PAK: "Pak", N64_BANK: "Bank",
        }
        pressed = [n for b, n in names.items() if self.buttons & (1 << (b - 1))]
        dpad = "" if self.hat == HAT_CENTER else \
            f" D:{['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][self.hat]}"
        return f"stick=({self.x:3d},{self.y:3d}){dpad} {' '.join(pressed)}".rstrip()


def hat_from_dirs(up: bool, down: bool, left: bool, right: bool) -> int:
    if up and down:
        up = down = False
    if left and right:
        left = right = False
    table = {
        (True, False, False, False): HAT_N,
        (True, False, False, True): HAT_NE,
        (False, False, False, True): HAT_E,
        (False, True, False, True): HAT_SE,
        (False, True, False, False): HAT_S,
        (False, True, True, False): HAT_SW,
        (False, False, True, False): HAT_W,
        (True, False, True, False): HAT_NW,
    }
    return table.get((up, down, left, right), HAT_CENTER)


XBOX_REPORT_MAP = bytes([
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01,
    0x85, 0x01,
    0x09, 0x01, 0xA1, 0x00,
    0x09, 0x30, 0x09, 0x31,
    0x15, 0x00, 0x27, 0xFF, 0xFF, 0x00, 0x00,
    0x95, 0x02, 0x75, 0x10, 0x81, 0x02,
    0xC0,
    0x09, 0x01, 0xA1, 0x00,
    0x09, 0x32, 0x09, 0x35,
    0x15, 0x00, 0x27, 0xFF, 0xFF, 0x00, 0x00,
    0x95, 0x02, 0x75, 0x10, 0x81, 0x02,
    0xC0,
    0x05, 0x02, 0x09, 0xC5,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x95, 0x01, 0x75, 0x0A, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x06, 0x95, 0x01, 0x81, 0x03,
    0x05, 0x02, 0x09, 0xC4,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x95, 0x01, 0x75, 0x0A, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x06, 0x95, 0x01, 0x81, 0x03,
    0x05, 0x01, 0x09, 0x39,
    0x15, 0x01, 0x25, 0x08, 0x35, 0x00, 0x46, 0x3B, 0x01, 0x66, 0x14, 0x00,
    0x75, 0x04, 0x95, 0x01, 0x81, 0x42,
    0x75, 0x04, 0x95, 0x01, 0x15, 0x00, 0x25, 0x00, 0x35, 0x00, 0x45, 0x00,
    0x65, 0x00, 0x81, 0x03,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x0F,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x0F, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x01, 0x95, 0x01, 0x81, 0x03,
    0x05, 0x0C, 0x0A, 0x24, 0x02,
    0x15, 0x00, 0x25, 0x01, 0x95, 0x01, 0x75, 0x01, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x07, 0x95, 0x01, 0x81, 0x03,
    0x85, 0x02,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01,
    0x0A, 0x23, 0x02,
    0x15, 0x00, 0x25, 0x01, 0x95, 0x01, 0x75, 0x01, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x07, 0x95, 0x01, 0x81, 0x03,
    0xC0,
    0xC0,
])

XB_A, XB_B, XB_X, XB_Y, XB_LB, XB_RB, XB_VIEW, XB_MENU, XB_LS, XB_RS = range(10)

XBOX_BUTTONS = {
    N64_A: XB_A,
    N64_B: XB_X,
    N64_C_DOWN: XB_B,
    N64_C_LEFT: XB_Y,
    N64_L: XB_LB,
    N64_R: XB_RB,
    N64_START: XB_MENU,
}
XBOX_REPORT_LEN = 16


def _axis16(value8: int) -> int:
    return max(0, min(0xFFFF, 0x8000 + round((value8 - AXIS_CENTER) * 32767 / 127)))


class GamepadProfile:
    key = ""
    default_name = ""
    descriptor = b""
    vendor_id = 0
    product_id = 0
    primary_report_id = 1

    def reports(self, state: PadState) -> dict[int, bytes]:
        raise NotImplementedError


class GenericProfile(GamepadProfile):
    key = "generic"
    default_name = "BlueNetplay Pad"
    descriptor = HID_REPORT_MAP
    vendor_id = 0x1209
    product_id = 0x6464

    def reports(self, state: PadState) -> dict[int, bytes]:
        return {REPORT_ID: state.payload()}


class XboxProfile(GamepadProfile):
    key = "xbox"
    default_name = "Xbox Wireless Controller"
    descriptor = XBOX_REPORT_MAP
    vendor_id = 0x045E
    product_id = 0x02E0

    def reports(self, state: PadState) -> dict[int, bytes]:
        pressed = lambda b: bool(state.buttons & (1 << (b - 1)))
        buttons = 0
        for n64, xb in XBOX_BUTTONS.items():
            if pressed(n64):
                buttons |= 1 << xb
        rx = 0xFFFF if pressed(N64_C_RIGHT) else 0x8000
        ry = 0x0000 if pressed(N64_C_UP) else 0x8000
        lt = 0x3FF if pressed(N64_Z) else 0
        hat = 0 if state.hat == HAT_CENTER else state.hat + 1
        report1 = struct.pack("<6HBHB", _axis16(state.x), _axis16(state.y), rx, ry,
                              lt, 0, hat, buttons, 0)
        report2 = bytes([0x01 if pressed(N64_PAK) else 0x00])
        return {1: report1, 2: report2}


PROFILES = {p.key: p for p in (XboxProfile(), GenericProfile())}


HID_SDP_HANDLE = 0x00010001
PNP_SDP_HANDLE = 0x00010002


def hid_sdp_record(name: str, descriptor: bytes = HID_REPORT_MAP) -> list[ServiceAttribute]:
    u8, u16, u32 = (DataElement.unsigned_integer_8,
                    DataElement.unsigned_integer_16,
                    DataElement.unsigned_integer_32)
    seq, uuid, boolean = DataElement.sequence, DataElement.uuid, DataElement.boolean

    def text(s: str | bytes) -> DataElement:
        return DataElement.text_string(s.encode() if isinstance(s, str) else s)

    return [
        ServiceAttribute(sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID, u32(HID_SDP_HANDLE)),
        ServiceAttribute(sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
                         seq([uuid(core.BT_HUMAN_INTERFACE_DEVICE_SERVICE)])),
        ServiceAttribute(sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID, seq([
            seq([uuid(core.BT_L2CAP_PROTOCOL_ID), u16(hid.HID_CONTROL_PSM)]),
            seq([uuid(core.BT_HIDP_PROTOCOL_ID)]),
        ])),
        ServiceAttribute(sdp.SDP_BROWSE_GROUP_LIST_ATTRIBUTE_ID,
                         seq([uuid(sdp.SDP_PUBLIC_BROWSE_ROOT)])),
        ServiceAttribute(sdp.SDP_LANGUAGE_BASE_ATTRIBUTE_ID_LIST_ATTRIBUTE_ID,
                         seq([u16(0x656E), u16(0x006A), u16(0x0100)])),
        ServiceAttribute(sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID, seq([
            seq([uuid(core.BT_HUMAN_INTERFACE_DEVICE_SERVICE), u16(0x0101)]),
        ])),
        ServiceAttribute(sdp.SDP_ADDITIONAL_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID, seq([
            seq([
                seq([uuid(core.BT_L2CAP_PROTOCOL_ID), u16(hid.HID_INTERRUPT_PSM)]),
                seq([uuid(core.BT_HIDP_PROTOCOL_ID)]),
            ]),
        ])),
        ServiceAttribute(0x0100, text(name)),
        ServiceAttribute(0x0101, text("Gamepad")),
        ServiceAttribute(0x0102, text("BlueNetplay")),
        ServiceAttribute(0x0201, u16(0x0111)),
        ServiceAttribute(0x0202, u8(0x08)),
        ServiceAttribute(0x0203, u8(0x00)),
        ServiceAttribute(0x0204, boolean(True)),
        ServiceAttribute(0x0205, boolean(True)),
        ServiceAttribute(0x0206, seq([seq([u8(0x22), text(descriptor)])])),
        ServiceAttribute(0x0207, seq([seq([u16(0x0409), u16(0x0100)])])),
        ServiceAttribute(0x0209, boolean(True)),
        ServiceAttribute(0x020A, boolean(True)),
        ServiceAttribute(0x020C, u16(0x0C80)),
        ServiceAttribute(0x020D, boolean(True)),
        ServiceAttribute(0x020E, boolean(True)),
    ]


def pnp_sdp_record(vendor_id: int, product_id: int) -> list[ServiceAttribute]:
    u16, u32 = DataElement.unsigned_integer_16, DataElement.unsigned_integer_32
    seq, uuid, boolean = DataElement.sequence, DataElement.uuid, DataElement.boolean
    return [
        ServiceAttribute(sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID, u32(PNP_SDP_HANDLE)),
        ServiceAttribute(sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
                         seq([uuid(core.BT_PNP_INFORMATION_SERVICE)])),
        ServiceAttribute(sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID, seq([
            seq([uuid(core.BT_L2CAP_PROTOCOL_ID), u16(0x0001)]),
            seq([uuid(core.BT_SDP_PROTOCOL_ID)]),
        ])),
        ServiceAttribute(sdp.SDP_BROWSE_GROUP_LIST_ATTRIBUTE_ID,
                         seq([uuid(sdp.SDP_PUBLIC_BROWSE_ROOT)])),
        ServiceAttribute(0x0200, u16(0x0103)),
        ServiceAttribute(0x0201, u16(vendor_id)),
        ServiceAttribute(0x0202, u16(product_id)),
        ServiceAttribute(0x0203, u16(PNP_VERSION)),
        ServiceAttribute(0x0204, boolean(True)),
        ServiceAttribute(0x0205, u16(PNP_VENDOR_ID_SOURCE_USB)),
    ]


class BluetoothGamepad:
    NEUTRAL_REPORTS_BEFORE_LIVE = 6
    SDP_QUIET_TIME = 0.3
    SDP_FALLBACK_TIME = {"generic": 4.0, "xbox": 0.5}
    KEEPALIVE_INTERVAL = 0.05

    def __init__(self, device: Device, name: str, profile: GamepadProfile):
        self.device = device
        self.name = name
        self.profile = profile
        self.hid = hid.Device(device)
        self.connection: Connection | None = None

        self.intr_open_time: float | None = None
        self.last_sdp_activity: float | None = None
        self.neutral_sent = 0
        self.live = False
        self.last_sent: dict[int, bytes] = {}
        self.last_send_time = 0.0
        self.reports_sent = 0
        self._combo_until = 0.0

        self._register_hid_callbacks()
        self._watch_sdp()
        device.on(device.EVENT_CONNECTION, self._on_connection)


    @classmethod
    def create(cls, hci_source, hci_sink, name: str,
               profile: GamepadProfile | None = None, le: bool = False) -> "BluetoothGamepad":
        profile = profile or PROFILES["generic"]
        config = DeviceConfiguration(
            name=name,
            class_of_device=CLASS_OF_DEVICE,
            classic_enabled=True,
            le_enabled=le,
            classic_ssp_enabled=True,
            classic_sc_enabled=True,
            classic_smp_enabled=False,
            classic_accept_any=True,
            connectable=True,
            discoverable=True,
            gap_service_enabled=le,
            gatt_service_enabled=le,
        )
        device = Device.from_config_with_hci(config, hci_source, hci_sink)
        device.sdp_service_records = {
            HID_SDP_HANDLE: hid_sdp_record(name, profile.descriptor),
            PNP_SDP_HANDLE: pnp_sdp_record(profile.vendor_id, profile.product_id),
        }
        enc_only = PairingDelegate.KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY

        def pairing_config(connection):
            if connection.transport == core.PhysicalTransport.LE:
                delegate = PairingDelegate(PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT,
                                           local_initiator_key_distribution=enc_only,
                                           local_responder_key_distribution=enc_only)
            else:
                delegate = PairingDelegate(PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT)
            return PairingConfig(sc=True, mitm=False, bonding=True, delegate=delegate)

        device.pairing_config_factory = pairing_config
        return cls(device, name, profile)

    async def start(self) -> None:
        await self.device.power_on()
        self.device.inquiry_response = bytes(core.AdvertisingData([
            (core.AdvertisingData.COMPLETE_LOCAL_NAME, self.name.encode()),
            (core.AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
             bytes(core.BT_HUMAN_INTERFACE_DEVICE_SERVICE)),
        ]))
        await self.device.set_discoverable(True)
        await self.device.set_connectable(True)
        log.info("Bluetooth address: %s", self.device.public_address)

    def _register_hid_callbacks(self) -> None:
        ok = hid.Device.GetSetReturn.SUCCESS

        def get_report(report_id, report_type, buffer_size):
            neutral = self.profile.reports(PadState())
            if report_type == hid.Message.ReportType.INPUT_REPORT and report_id in neutral:
                return hid.Device.GetSetStatus(data=neutral[report_id], status=ok)
            return hid.Device.GetSetStatus(status=hid.Device.GetSetReturn.REPORT_ID_NOT_FOUND)

        def set_report(report_id, report_type, report_size, data):
            log.info("SET_REPORT id=%d type=%d data=%s", report_id, report_type, data.hex())
            return hid.Device.GetSetStatus(status=ok)

        def get_protocol():
            return hid.Device.GetSetStatus(
                data=bytes([hid.Message.ProtocolMode.REPORT_PROTOCOL]), status=ok)

        def set_protocol(mode):
            log.info("SET_PROTOCOL %s", "report" if mode else "boot")
            return hid.Device.GetSetStatus(status=ok)

        self.hid.register_get_report_cb(get_report)
        self.hid.register_set_report_cb(set_report)
        self.hid.register_get_protocol_cb(get_protocol)
        self.hid.register_set_protocol_cb(set_protocol)
        self.hid.on(self.hid.EVENT_INTERRUPT_DATA,
                    lambda pdu: log.debug("Output report from BlueRetro: %s", pdu.hex()))
        self.hid.on(self.hid.EVENT_VIRTUAL_CABLE_UNPLUG,
                    lambda: log.info("BlueRetro sent virtual cable unplug"))

    def _watch_sdp(self) -> None:
        server = self.device.sdp_server
        original = server.on_pdu

        def on_pdu(pdu):
            self.last_sdp_activity = time.monotonic()
            try:
                request = sdp.SDP_PDU.from_bytes(pdu)
                log.info("SDP request from host: %s", request.name)
            except Exception:
                pass
            original(pdu)

        server.on_pdu = on_pdu


    def _on_connection(self, connection: Connection) -> None:
        if connection.transport != core.PhysicalTransport.BR_EDR:
            return
        self.connection = connection
        self._reset_session()
        log.info("Connected: %s (we are %s)", connection.peer_address, connection.role_name)
        connection.on(connection.EVENT_DISCONNECTION, self._on_disconnection)
        connection.on(connection.EVENT_CLASSIC_PAIRING,
                      lambda: log.info("Paired (Just Works)"))
        connection.on(connection.EVENT_CLASSIC_PAIRING_FAILURE,
                      lambda error: log.warning("Pairing failed: %s", error))
        connection.on(connection.EVENT_CONNECTION_ENCRYPTION_CHANGE,
                      lambda: log.info("Encryption: %s", "on" if connection.is_encrypted else "off"))
        connection.on(connection.EVENT_ROLE_CHANGE,
                      lambda role: log.info("Role changed: we are now %s", connection.role_name))

    def _on_disconnection(self, reason: int) -> None:
        try:
            reason_name = hci.HCI_Constant.error_name(reason)
        except Exception:
            reason_name = f"0x{reason:02X}"
        log.warning("Disconnected (%s). Waiting for BlueRetro to reconnect...", reason_name)
        self.connection = None
        self._reset_session()

    def _reset_session(self) -> None:
        self.intr_open_time = None
        self.last_sdp_activity = None
        self.neutral_sent = 0
        self.live = False
        self.last_sent = {rid: payload for rid, payload in self.profile.reports(PadState()).items()
                          if rid != self.profile.primary_report_id}
        self.reports_sent = 0


    @property
    def channels_open(self) -> bool:
        return (self.hid.l2cap_ctrl_channel is not None
                and self.hid.l2cap_intr_channel is not None)

    def _sdp_done(self, now: float) -> bool:
        if self.last_sdp_activity is not None:
            return now - self.last_sdp_activity >= self.SDP_QUIET_TIME
        return self.intr_open_time is not None and \
            now - self.intr_open_time >= self.SDP_FALLBACK_TIME[self.profile.key]

    def pump(self, state: PadState) -> None:
        now = time.monotonic()
        if not self.channels_open:
            self.intr_open_time = None
            return
        if self.intr_open_time is None:
            self.intr_open_time = now
            log.info("HID control + interrupt channels open")

        primary = self.profile.primary_report_id
        if not self.live:
            if self._sdp_done(now) and now - self.last_send_time >= 0.02:
                self._send(primary, self.profile.reports(PadState())[primary], now)
                self.neutral_sent += 1
                if self.neutral_sent >= self.NEUTRAL_REPORTS_BEFORE_LIVE:
                    self.live = True
                    log.info("Calibrated - controller is LIVE, inputs now go to the console")
            return

        if now < self._combo_until:
            state = self._inquiry_combo_state()

        for report_id, payload in self.profile.reports(state).items():
            changed = payload != self.last_sent.get(report_id)
            keepalive = report_id == primary and now - self.last_send_time >= self.KEEPALIVE_INTERVAL
            if changed or keepalive:
                self._send(report_id, payload, now)

    INQUIRY_COMBO_BUTTONS = (9, 10, 12, 2)
    INQUIRY_COMBO_HOLD = 0.3

    def _inquiry_combo_state(self) -> PadState:
        state = PadState()
        for button in self.INQUIRY_COMBO_BUTTONS:
            state.press(button)
        return state

    def press_inquiry_combo(self) -> bool:
        if self.profile.key != "generic" or not self.live:
            log.warning("Can't send BlueRetro's search combo (needs the generic profile, live)")
            return False
        self._combo_until = time.monotonic() + self.INQUIRY_COMBO_HOLD
        log.info("Sending BlueRetro's 'search for controllers' combo")
        return True

    def _send(self, report_id: int, payload: bytes, now: float) -> None:
        try:
            self.hid.send_data(bytes([report_id]) + payload)
        except Exception as error:
            log.warning("Send failed: %s", error)
            return
        self.last_sent[report_id] = payload
        if report_id == self.profile.primary_report_id:
            self.last_send_time = now
        self.reports_sent += 1

    async def close(self) -> None:
        if self.connection is not None:
            try:
                await self.connection.disconnect()
            except Exception:
                pass


class InputSource:
    def read(self, state: PadState) -> None:
        raise NotImplementedError


class KeyboardInput(InputSource):
    VK = {
        "W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44,
        "J": 0x4A, "K": 0x4B, "Q": 0x51, "E": 0x45,
        "I": 0x49, "M": 0x4D, "U": 0x55, "O": 0x4F,
        "P": 0x50, "B": 0x42,
        "SPACE": 0x20, "RETURN": 0x0D, "SHIFT": 0x10,
        "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
        "NUM8": 0x68, "NUM2": 0x62, "NUM4": 0x64, "NUM6": 0x66,
        "F8": 0x77,
    }

    def __init__(self):
        self._user32 = ctypes.WinDLL("user32")
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self.enabled = True
        self._f8_was_down = False

    def _down(self, key: str) -> bool:
        return bool(self._user32.GetAsyncKeyState(self.VK[key]) & 0x8000)

    def read(self, state: PadState) -> None:
        f8 = self._down("F8")
        if f8 and not self._f8_was_down:
            self.enabled = not self.enabled
            log.info("Keyboard input %s", "ENABLED" if self.enabled else "PAUSED (F8)")
        self._f8_was_down = f8
        if not self.enabled:
            return

        d = self._down
        for key, button in (
            ("J", N64_A), ("K", N64_B), ("SPACE", N64_Z), ("RETURN", N64_START),
            ("Q", N64_L), ("E", N64_R),
            ("NUM8", N64_C_UP), ("NUM2", N64_C_DOWN),
            ("NUM4", N64_C_LEFT), ("NUM6", N64_C_RIGHT),
            ("I", N64_C_UP), ("M", N64_C_DOWN), ("U", N64_C_LEFT), ("O", N64_C_RIGHT),
            ("P", N64_PAK),
        ):
            if d(key):
                state.press(button)

        hat = hat_from_dirs(d("UP"), d("DOWN"), d("LEFT"), d("RIGHT"))
        if hat != HAT_CENTER:
            state.hat = hat

        reach = 64 if d("SHIFT") else 127
        dx = (reach if d("D") else 0) - (reach if d("A") else 0)
        dy = (reach if d("S") else 0) - (reach if d("W") else 0)
        if dx:
            state.x = max(0, min(255, AXIS_CENTER + dx))
        if dy:
            state.y = max(0, min(255, AXIS_CENTER + dy))


class _XInputGamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _XInputGamepad)]


class XInputGamepad(InputSource):
    DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT = 0x0001, 0x0002, 0x0004, 0x0008
    START, BACK = 0x0010, 0x0020
    LB, RB = 0x0100, 0x0200
    BTN_A, BTN_B, BTN_X, BTN_Y = 0x1000, 0x2000, 0x4000, 0x8000
    STICK_DEADZONE = 7849
    CSTICK_THRESHOLD = 16384
    TRIGGER_THRESHOLD = 64

    def __init__(self):
        self._dll = None
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self._dll = ctypes.WinDLL(name)
                break
            except OSError:
                continue
        self._pad_index: int | None = None
        self._last_scan = 0.0

    def _poll(self, index: int) -> _XInputState | None:
        state = _XInputState()
        if self._dll.XInputGetState(index, ctypes.byref(state)) == 0:
            return state
        return None

    def read(self, state: PadState) -> None:
        if self._dll is None:
            return
        now = time.monotonic()
        xs = None
        if self._pad_index is not None:
            xs = self._poll(self._pad_index)
            if xs is None:
                log.info("XInput controller %d disconnected", self._pad_index)
                self._pad_index = None
        if self._pad_index is None:
            if now - self._last_scan < 2.0:
                return
            self._last_scan = now
            for index in range(4):
                xs = self._poll(index)
                if xs is not None:
                    self._pad_index = index
                    log.info("XInput controller %d connected", index)
                    break
            if xs is None:
                return

        g = xs.Gamepad
        b = g.wButtons
        for mask, button in (
            (self.BTN_A, N64_A), (self.BTN_X, N64_B),
            (self.BTN_B, N64_C_DOWN), (self.BTN_Y, N64_C_LEFT),
            (self.START, N64_START), (self.LB, N64_L), (self.RB, N64_R),
            (self.BACK, N64_PAK),
        ):
            if b & mask:
                state.press(button)
        if g.bLeftTrigger > self.TRIGGER_THRESHOLD or g.bRightTrigger > self.TRIGGER_THRESHOLD:
            state.press(N64_Z)

        if g.sThumbRY > self.CSTICK_THRESHOLD:
            state.press(N64_C_UP)
        elif g.sThumbRY < -self.CSTICK_THRESHOLD:
            state.press(N64_C_DOWN)
        if g.sThumbRX > self.CSTICK_THRESHOLD:
            state.press(N64_C_RIGHT)
        elif g.sThumbRX < -self.CSTICK_THRESHOLD:
            state.press(N64_C_LEFT)

        hat = hat_from_dirs(bool(b & self.DPAD_UP), bool(b & self.DPAD_DOWN),
                            bool(b & self.DPAD_LEFT), bool(b & self.DPAD_RIGHT))
        if hat != HAT_CENTER:
            state.hat = hat

        def axis(v: int) -> int:
            if abs(v) < self.STICK_DEADZONE:
                return AXIS_CENTER
            return max(0, min(255, AXIS_CENTER + v // 256))

        if abs(g.sThumbLX) >= self.STICK_DEADZONE:
            state.x = axis(g.sThumbLX)
        if abs(g.sThumbLY) >= self.STICK_DEADZONE:
            state.y = 255 - axis(g.sThumbLY)


def print_controls() -> None:
    print("""
  +--------------------------------------------------------+
  |  Keyboard              N64          Xbox pad           |
  +--------------------------------------------------------+
  |  W A S D               Stick        Left stick         |
  |  Shift + WASD          Half tilt                       |
  |  Arrow keys            D-pad        D-pad              |
  |  J                     A            A                  |
  |  K                     B            X                  |
  |  Space                 Z            LT / RT            |
  |  Q / E                 L / R        LB / RB            |
  |  Enter                 Start        Start              |
  |  Numpad 8 2 4 6        C buttons    Right stick        |
  |   or I M U O                        (Y = C<, B = Cv)   |
  |  P                     Pak toggle   Back               |
  |  F8                    pause/resume keyboard capture   |
  |  Ctrl+C                quit                            |
  +--------------------------------------------------------+
""")


def high_res_timer(enable: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        winmm = ctypes.WinDLL("winmm")
        (winmm.timeBeginPeriod if enable else winmm.timeEndPeriod)(1)
    except OSError:
        pass


async def run(args) -> None:
    if args.snoop:
        os.environ["BUMBLE_SNOOPER"] = f"btsnoop:file:{os.path.abspath(args.snoop)}"
        log.info("Writing HCI log to %s (open with Wireshark)", args.snoop)

    log.info("Opening Bluetooth dongle on %s ...", args.transport)
    try:
        transport = await open_transport(args.transport)
    except Exception as error:
        log.error("Could not open %s: %s", args.transport, error)
        log.error("Is the dongle plugged in and using the WinUSB driver (Zadig)? "
                  "See README.md")
        return

    async with transport:
        profile = PROFILES[args.profile]
        pad = BluetoothGamepad.create(transport.source, transport.sink, args.name, profile)
        await pad.start()
        log.info("'%s' is discoverable as a Bluetooth gamepad (%s profile).", args.name, profile.key)
        log.info("Waiting for BlueRetro... (power on the console / press BlueRetro's pairing button)")
        print_controls()

        sources: list[InputSource] = []
        if sys.platform == "win32":
            if not args.no_keyboard:
                sources.append(KeyboardInput())
            if not args.no_xinput:
                sources.append(XInputGamepad())

        tick = 1.0 / args.rate
        last_desc = ""
        try:
            while True:
                state = PadState()
                for source in sources:
                    source.read(state)
                pad.pump(state)
                if args.show_input and pad.live:
                    desc = state.describe()
                    if desc != last_desc:
                        print(f"  {desc}")
                        last_desc = desc
                await asyncio.sleep(tick)
        finally:
            await pad.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bluetooth gamepad for BlueRetro (Bumble)")
    parser.add_argument("--transport", default="usb:0",
                        help="Bumble transport, e.g. usb:0 or usb:04B4:F901 (default usb:0)")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="generic",
                        help="controller to pretend to be (default generic; xbox for v24.04-era firmware)")
    parser.add_argument("--name", help="device name (default: the profile's name, "
                        f"'{PROFILES['xbox'].default_name}' or '{PROFILES['generic'].default_name}')")
    parser.add_argument("--rate", type=float, default=250.0, help="input poll rate in Hz (default 250)")
    parser.add_argument("--no-keyboard", action="store_true", help="ignore the PC keyboard")
    parser.add_argument("--no-xinput", action="store_true", help="ignore XInput gamepads")
    parser.add_argument("--show-input", action="store_true", help="print the pad state when it changes")
    parser.add_argument("--snoop", metavar="FILE", help="write a btsnoop HCI log (for Wireshark)")
    parser.add_argument("--verbose", "-v", action="count", default=0,
                        help="-v: Bumble info logs, -vv: full Bumble debug logs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("bumble").setLevel(
        (logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)])

    profile = PROFILES[args.profile]
    args.name = args.name or profile.default_name
    if profile.key == "generic":
        for fragment in RESERVED_NAME_FRAGMENTS:
            if fragment.lower() in args.name.lower():
                log.warning("Name '%s' contains '%s': BlueRetro will NOT treat this as a "
                            "generic HID gamepad and inputs will likely be ignored.",
                            args.name, fragment)
                break
    elif profile.default_name.lower() not in args.name.lower():
        log.warning("BlueRetro only uses its Xbox decoder for the name '%s'.",
                    profile.default_name)

    high_res_timer(True)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        high_res_timer(False)


if __name__ == "__main__":
    main()
