#!/usr/bin/env python3

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from bumble import core, gatt, hci
from bumble.core import AdvertisingData
from bumble.device import Connection, Device

import gamepad
from gamepad import PadState

log = logging.getLogger("ble_pads")

APPEARANCE_GAMEPAD = 0x03C4
REPORT_ID = 1
GATT_MAX_MTU = 185


def random_static_address() -> hci.Address:
    raw = bytearray(secrets.token_bytes(6))
    raw[0] |= 0xC0
    return hci.Address(":".join(f"{b:02X}" for b in raw), hci.Address.RANDOM_DEVICE_ADDRESS)


@dataclass
class BlePad:
    player: int
    address: hci.Address
    ltk: bytes | None = None
    ediv: int | None = None
    rand: bytes | None = None
    connection: Connection | None = None
    subscribed: bool = False
    live: bool = False
    neutral_sent: int = 0
    last_sent: bytes | None = None
    last_send_time: float = 0.0
    pair_requested: bool = False
    last_error: str = ""
    sending: bool = field(default=False, repr=False)

    @property
    def paired(self) -> bool:
        return self.ltk is not None

    def to_json(self) -> dict:
        return {"player": self.player, "address": str(self.address).split("/")[0],
                "ltk": self.ltk.hex() if self.ltk else None,
                "ediv": self.ediv, "rand": self.rand.hex() if self.rand else None}

    def state_name(self) -> str:
        if self.live:
            return "live"
        if self.connection is not None:
            return "calibrating" if self.subscribed else "connecting"
        if self.pair_requested and not self.paired:
            return "pairing"
        return "waiting" if self.paired else "unpaired"


class BlePadManager:
    NEUTRAL_REPORTS_BEFORE_LIVE = 6
    KEEPALIVE_INTERVAL = 0.05
    COMBO_RETRY_TIME = 15.0
    COMBO_MAX_TRIES = 3

    def __init__(self, device: Device, classic, store_path: Path, players: int = 3,
                 name: str = "BlueNetplay Pad"):
        self.device = device
        self.classic = classic
        self.store_path = Path(store_path)
        self.name = name
        self.pads: list[BlePad] = []
        self.advertising_for: BlePad | None = None
        self._combo_tries = 0
        self._combo_at = 0.0
        self._load(players)
        self._build_gatt()

        self._fallback_ltk = device.host.long_term_key_provider
        device.host.long_term_key_provider = self._long_term_key
        device.on(device.EVENT_CONNECTION, self._on_connection)


    def _load(self, players: int) -> None:
        saved = {}
        try:
            data = json.loads(self.store_path.read_text())
            saved = {p["player"]: p for p in data.get("pads", [])}
        except (OSError, ValueError):
            pass
        self._other_pads = [p for n, p in saved.items() if not 2 <= n < 2 + players]
        for player in range(2, 2 + players):
            s = saved.get(player)
            if s:
                pad = BlePad(player, hci.Address(s["address"], hci.Address.RANDOM_DEVICE_ADDRESS),
                             bytes.fromhex(s["ltk"]) if s.get("ltk") else None, s.get("ediv"),
                             bytes.fromhex(s["rand"]) if s.get("rand") else None)
            else:
                pad = BlePad(player, random_static_address())
            self.pads.append(pad)
        self._save()

    def _save(self) -> None:
        tmp = self.store_path.with_suffix(".tmp")
        pads = [p.to_json() for p in self.pads] + list(self._other_pads)
        pads.sort(key=lambda p: p["player"])
        tmp.write_text(json.dumps({"pads": pads}, indent=2))
        os.replace(tmp, self.store_path)

    def forget(self, player: int) -> None:
        for pad in self.pads:
            if pad.player == player:
                pad.ltk = pad.ediv = pad.rand = None
                pad.address = random_static_address()
                pad.pair_requested = False
        self._save()


    def _build_gatt(self) -> None:
        C, A = gatt.Characteristic, gatt.Characteristic
        neutral = bytes(PadState().payload())
        self.report = gatt.Characteristic(
            gatt.GATT_REPORT_CHARACTERISTIC,
            C.Properties.READ | C.Properties.NOTIFY,
            A.READABLE,
            neutral,
            [gatt.Descriptor(gatt.GATT_REPORT_REFERENCE_DESCRIPTOR, gatt.Descriptor.READABLE,
                             bytes([REPORT_ID, 0x01]))],
        )
        self.report.on(self.report.EVENT_SUBSCRIPTION, self._on_subscription)
        hid = gatt.Service(gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE, [
            gatt.Characteristic(gatt.GATT_HID_INFORMATION_CHARACTERISTIC, C.Properties.READ,
                                A.READABLE, bytes([0x11, 0x01, 0x00, 0x02])),
            gatt.Characteristic(gatt.GATT_REPORT_MAP_CHARACTERISTIC, C.Properties.READ,
                                A.READABLE, gamepad.HID_REPORT_MAP),
            gatt.Characteristic(gatt.GATT_HID_CONTROL_POINT_CHARACTERISTIC,
                                C.Properties.WRITE_WITHOUT_RESPONSE, A.WRITEABLE, bytes([0])),
            gatt.Characteristic(gatt.GATT_PROTOCOL_MODE_CHARACTERISTIC,
                                C.Properties.READ | C.Properties.WRITE_WITHOUT_RESPONSE,
                                A.READABLE | A.WRITEABLE, bytes([0x01])),
            self.report,
        ])
        dis = gatt.Service(gatt.GATT_DEVICE_INFORMATION_SERVICE, [
            gatt.Characteristic(gatt.GATT_PNP_ID_CHARACTERISTIC, C.Properties.READ, A.READABLE,
                                bytes([0x02, 0x09, 0x12, 0x64, 0x64, 0x00, 0x01])),
        ])
        self.device.add_services([hid, dis])
        self.device.gatt_server.max_mtu = GATT_MAX_MTU

    def _adv_data(self, pad: BlePad) -> bytes:
        return bytes(AdvertisingData([
            (AdvertisingData.FLAGS, bytes([0x06])),
            (AdvertisingData.APPEARANCE, APPEARANCE_GAMEPAD.to_bytes(2, "little")),
            (AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
             bytes(gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE)),
            (AdvertisingData.COMPLETE_LOCAL_NAME, f"{self.name[:12]} P{pad.player}".encode()),
        ]))


    def _pad_for(self, connection: Connection) -> BlePad | None:
        addr = str(connection.self_address).split("/")[0]
        return next((p for p in self.pads if str(p.address).split("/")[0] == addr), None)

    def _on_connection(self, connection: Connection) -> None:
        if connection.transport != core.PhysicalTransport.LE:
            return
        pad = self._pad_for(connection)
        if pad is None:
            log.warning("LE connection on unknown address %s", connection.self_address)
            return
        self.advertising_for = None
        pad.connection = connection
        pad.subscribed = pad.live = False
        pad.neutral_sent = 0
        pad.last_sent = None
        log.info("Player %d: BLE connected (%s)", pad.player, connection.peer_address)
        connection.on(connection.EVENT_DISCONNECTION, lambda reason, p=pad: self._on_disconnection(p, reason))
        connection.on(connection.EVENT_PAIRING, lambda keys, p=pad: self._on_pairing(p, keys))
        connection.on(connection.EVENT_PAIRING_FAILURE,
                      lambda reason, p=pad: log.warning("Player %d: pairing failed (%s)", p.player, reason))

    def _on_disconnection(self, pad: BlePad, reason: int) -> None:
        log.warning("Player %d: BLE disconnected (%s)", pad.player, hci.HCI_Constant.error_name(reason))
        pad.connection = None
        pad.subscribed = pad.live = False

    def _on_pairing(self, pad: BlePad, keys) -> None:
        key = keys.ltk_central or keys.ltk
        if key is None or not key.value:
            log.warning("Player %d: paired but got no encryption key to store", pad.player)
            return
        pad.ltk, pad.ediv, pad.rand = key.value, key.ediv, key.rand
        pad.pair_requested = False
        self._combo_tries = 0
        self._save()
        log.info("Player %d: paired with BlueRetro (key saved)", pad.player)

    def _on_subscription(self, bearer, notify_enabled: bool, indicate_enabled: bool) -> None:
        connection = getattr(bearer, "connection", bearer)
        pad = self._pad_for(connection)
        if pad is not None:
            pad.subscribed = notify_enabled
            log.info("Player %d: input reports %s", pad.player, "enabled" if notify_enabled else "disabled")

    async def _long_term_key(self, handle: int, rand: bytes, ediv: int) -> bytes | None:
        connection = self.device.lookup_connection(handle)
        if connection is not None and connection.transport == core.PhysicalTransport.LE:
            pad = self._pad_for(connection)
            if pad is not None and pad.ltk and pad.ediv == ediv and pad.rand == rand:
                return pad.ltk
        return await self._fallback_ltk(handle, rand, ediv) if self._fallback_ltk else None


    def request_pairing(self, player: int) -> None:
        for pad in self.pads:
            if pad.player == player and not pad.paired:
                pad.pair_requested = True
                self._combo_tries = 0
                self._combo_at = 0.0

    def _next_target(self) -> BlePad | None:
        for pad in self.pads:
            if pad.connection is not None:
                continue
            if pad.paired or pad.pair_requested:
                return pad
            return None
        return None

    async def tick(self) -> None:
        target = self._next_target() if self.classic.live else None
        if target is not self.advertising_for:
            await self.device.stop_advertising()
            self.advertising_for = None
            if target is not None:
                await self.device.send_sync_command(
                    hci.HCI_LE_Set_Random_Address_Command(random_address=target.address))
                self.device.random_address = target.address
                await self.device.start_advertising(
                    own_address_type=hci.OwnAddressType.RANDOM,
                    advertising_data=self._adv_data(target),
                    scan_response_data=b"",
                    advertising_interval_min=30, advertising_interval_max=50)
                self.advertising_for = target
                log.info("Player %d: advertising as %s (%s)", target.player, target.address,
                         "paired, waiting for BlueRetro" if target.paired else "pairing")

        now = time.monotonic()
        if target is not None and not target.paired and now - self._combo_at > self.COMBO_RETRY_TIME:
            if self._combo_tries >= self.COMBO_MAX_TRIES:
                target.pair_requested = False
                target.last_error = "BlueRetro didn't pick up the pad"
                log.warning("Player %d: gave up pairing after %d tries", target.player, self._combo_tries)
                return
            self._combo_tries += 1
            self._combo_at = now
            log.info("Player %d: asking BlueRetro to search (combo, try %d)", target.player, self._combo_tries)
            self.classic.press_inquiry_combo()


    def pump(self, states: dict[int, PadState]) -> None:
        now = time.monotonic()
        for pad in self.pads:
            if pad.connection is None or not pad.subscribed or pad.sending:
                continue
            if not pad.live:
                if now - pad.last_send_time >= 0.02:
                    self._notify(pad, PadState().payload(), now)
                    pad.neutral_sent += 1
                    if pad.neutral_sent >= self.NEUTRAL_REPORTS_BEFORE_LIVE:
                        pad.live = True
                        log.info("Player %d: LIVE", pad.player)
                continue
            payload = states.get(pad.player, PadState()).payload()
            if payload != pad.last_sent or now - pad.last_send_time >= self.KEEPALIVE_INTERVAL:
                self._notify(pad, payload, now)

    def _notify(self, pad: BlePad, payload: bytes, now: float) -> None:
        import asyncio

        async def send():
            try:
                await self.device.notify_subscriber(pad.connection, self.report, payload)
            except Exception as error:
                log.debug("Player %d: notify failed: %s", pad.player, error)
            finally:
                pad.sending = False

        pad.sending = True
        pad.last_sent = payload
        pad.last_send_time = now
        asyncio.get_running_loop().create_task(send())

    def status(self) -> list[dict]:
        return [{"player": p.player, "state": p.state_name(), "paired": p.paired,
                 "address": str(p.address).split("/")[0], "error": p.last_error} for p in self.pads]
