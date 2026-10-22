#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time

import aiohttp

from gamepad import PadState

log = logging.getLogger("stream")

MSG_H264 = 0x03
MSG_OPUS = 0x04


def _now_us() -> int:
    return int(time.monotonic() * 1_000_000)


def _bits(rate: str) -> int:
    rate = rate.strip().lower()
    mult = {"k": 1_000, "m": 1_000_000}.get(rate[-1], 1)
    return int(float(rate[:-1] if rate[-1] in "km" else rate) * mult)


def h264_output_args(args, port: int) -> list[str]:
    fps = float(args.fps)
    gop = max(1, round(fps))
    rate = args.relay_bitrate
    vbv = str(max(1, int(_bits(rate) / fps * 4)))
    if args.h264_encoder == "h264_nvenc":
        enc = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-zerolatency", "1",
               "-rc", "cbr", "-b:v", rate, "-maxrate", rate, "-bufsize", vbv,
               "-g", str(gop), "-forced-idr", "1", "-bf", "0", "-profile:v", "main"]
    elif args.h264_encoder == "h264_v4l2m2m":
        enc = ["-c:v", "h264_v4l2m2m", "-b:v", rate, "-g", str(gop),
               "-num_output_buffers", "4", "-num_capture_buffers", "4", "-flags", "+global_header"]
    else:
        enc = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
               "-b:v", rate, "-maxrate", rate, "-bufsize", vbv, "-g", str(gop), "-bf", "0"]
    vf = ["-vf", args.video_filter] if getattr(args, "video_filter", None) else []
    return ["-map", "0:v", "-an", *vf, "-pix_fmt", "yuv420p", *enc,
            "-f", "flv", "-flvflags", "no_duration_filesize+no_metadata",
            f"tcp://127.0.0.1:{port}"]


def opus_output_args(args, port: int) -> list[str]:
    return ["-map", "0:a", "-vn", "-ac", "2", "-ar", "48000",
            "-c:a", "libopus", "-b:a", args.relay_audio_bitrate, "-application", "lowdelay",
            "-frame_duration", "20", "-f", "ogg", "-page_duration", "20000",
            f"tcp://127.0.0.1:{port}"]


class _TcpReceiver:
    def __init__(self, label: str):
        self.label = label
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.debug("%s encoder connected", self.label)
        try:
            await self.handle(reader)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            log.exception("%s stream error", self.label)
        finally:
            writer.close()
            log.debug("%s encoder disconnected", self.label)

    async def handle(self, reader: asyncio.StreamReader) -> None:
        raise NotImplementedError


class FlvH264Receiver(_TcpReceiver):
    def __init__(self, link: "RelayLink", width: int, height: int):
        super().__init__("H.264")
        self.link = link
        self.width = width
        self.height = height

    async def handle(self, reader: asyncio.StreamReader) -> None:
        header = await reader.readexactly(9)
        if header[:3] != b"FLV":
            raise ValueError("not an FLV stream")
        await reader.readexactly(int.from_bytes(header[5:9], "big") - 9 + 4)
        while True:
            tag = await reader.readexactly(11)
            size = int.from_bytes(tag[1:4], "big")
            data = await reader.readexactly(size)
            await reader.readexactly(4)
            if tag[0] != 9 or len(data) < 5 or (data[0] & 0x0F) != 7:
                continue
            keyframe = (data[0] >> 4) == 1
            if data[1] == 0:
                avcc = data[5:]
                codec = f"avc1.{avcc[1]:02x}{avcc[2]:02x}{avcc[3]:02x}"
                self.link.set_video_config({
                    "type": "video_config", "codec": codec, "width": self.width,
                    "height": self.height, "description": base64.b64encode(avcc).decode()})
            elif data[1] == 1:
                self.link.send_media(bytes([MSG_H264, 1 if keyframe else 0])
                                     + struct.pack(">Q", _now_us()) + data[5:],
                                     keyframe=keyframe)


class OggOpusReceiver(_TcpReceiver):
    def __init__(self, link: "RelayLink"):
        super().__init__("Opus")
        self.link = link

    async def handle(self, reader: asyncio.StreamReader) -> None:
        packet = b""
        index = 0
        while True:
            head = await reader.readexactly(27)
            if head[:4] != b"OggS":
                raise ValueError("lost Ogg sync")
            lacing = await reader.readexactly(head[26])
            body = await reader.readexactly(sum(lacing))
            pos = 0
            for seg in lacing:
                packet += body[pos:pos + seg]
                pos += seg
                if seg < 255:
                    if index == 0 and packet.startswith(b"OpusHead"):
                        self.link.set_audio_config({
                            "type": "audio_config", "codec": "opus", "sampleRate": 48000,
                            "numberOfChannels": 2,
                            "description": base64.b64encode(packet).decode()})
                    elif index >= 2:
                        self.link.send_media(bytes([MSG_OPUS]) + struct.pack(">QI", _now_us(), 20000)
                                             + packet)
                    index += 1
                    packet = b""


def _limit_send_buffer(ws, size: int) -> None:
    import socket
    try:
        transport = ws._response.connection.transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, size)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception as error:
        log.debug("could not limit the send buffer: %s", error)


class RelayLink:
    MAX_QUEUE = 12
    SOCKET_SNDBUF = 64 * 1024
    PING_INTERVAL = 2.0

    def __init__(self, hub, url: str, token: str, parse_input):
        self.hub = hub
        self.url = url
        self.token = token
        self.parse_input = parse_input
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.video_config: dict | None = None
        self.audio_config: dict | None = None
        self.need_keyframe = True
        self.state = "connecting"
        self.dropped = 0


    def set_video_config(self, cfg: dict) -> None:
        if cfg != self.video_config:
            log.info("Relay video: %s %dx%d", cfg["codec"], cfg["width"], cfg["height"])
        self.video_config = cfg
        self.send_json(cfg)

    def set_audio_config(self, cfg: dict) -> None:
        self.audio_config = cfg
        self.send_json(cfg)

    def send_json(self, msg: dict) -> None:
        if self.ws is not None:
            self.queue.put_nowait(json.dumps(msg))

    def send_media(self, data: bytes, keyframe: bool = False) -> None:
        if self.ws is None:
            return
        if data[0] == MSG_H264:
            if self.queue.qsize() > self.MAX_QUEUE:
                self._drop_queued_video()
                self.need_keyframe = True
            if self.need_keyframe and not keyframe:
                return
            self.need_keyframe = False
        self.queue.put_nowait(data)

    def _drop_queued_video(self) -> None:
        keep = []
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if isinstance(item, str) or item[0] != MSG_H264:
                keep.append(item)
        for item in keep:
            self.queue.put_nowait(item)
        self.dropped += 1
        log.info("Relay uplink fell behind: skipped to the next keyframe (%d times)", self.dropped)


    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                            self.url, headers={"Authorization": f"Bearer {self.token}"},
                            heartbeat=10, max_msg_size=64 * 1024, compress=0) as ws:
                        log.info("Connected to the relay %s", self.url)
                        self.state = "online"
                        self.ws = ws
                        _limit_send_buffer(ws, self.SOCKET_SNDBUF)
                        self.need_keyframe = True
                        delay = 1.0
                        self.queue = asyncio.Queue()
                        for cfg in (self.video_config, self.audio_config):
                            if cfg:
                                self.queue.put_nowait(json.dumps(cfg))
                        self.queue.put_nowait(json.dumps({"type": "status", **self.hub.status}))
                        sender = asyncio.create_task(self._sender(ws))
                        pinger = asyncio.create_task(self._pinger())
                        try:
                            await self._receiver(ws)
                        finally:
                            sender.cancel()
                            pinger.cancel()
                            self.hub.set_status("relay", rtt_ms=None)
            except aiohttp.WSServerHandshakeError as error:
                self.state = "rejected" if error.status in (401, 403) else "error"
                log.warning("Relay refused the connection (HTTP %s)%s", error.status,
                            " - check the relay token" if error.status == 401 else "")
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
                self.state = "offline"
                log.warning("Relay connection failed: %s", error)
            finally:
                self.ws = None
                self.hub.remote.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _pinger(self) -> None:
        while True:
            self.send_json({"type": "ping", "t": time.monotonic()})
            await asyncio.sleep(self.PING_INTERVAL)

    async def _sender(self, ws) -> None:
        while True:
            item = await self.queue.get()
            if isinstance(item, str):
                await ws.send_str(item)
            else:
                await ws.send_bytes(item)

    async def _receiver(self, ws) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            kind = m.get("type")
            if kind == "pong":
                try:
                    rtt = round((time.monotonic() - float(m.get("t"))) * 1000)
                except (TypeError, ValueError):
                    continue
                old = self.hub.status["relay"].get("rtt_ms")
                if old is None or abs(rtt - old) >= 5:
                    self.hub.set_status("relay", rtt_ms=rtt)
            elif kind == "viewer":
                r = self.hub.remote.setdefault(m["viewer"], {"state": PadState(), "last_input": 0.0})
                new = dict(player=int(m.get("player") or 1), name=m.get("name"), role=m.get("role"))
                if any(r.get(k) != v for k, v in new.items()):
                    r.update(new)
                    log.info("Remote %s '%s' -> player %s", new["role"], new["name"], new["player"])
            elif kind == "viewer_left":
                gone = self.hub.remote.pop(m.get("viewer"), None)
                if gone:
                    log.info("Remote '%s' left", gone.get("name"))
            elif kind == "input":
                r = self.hub.remote.get(m.get("viewer"))
                if r is not None and r.get("role") != "watch":
                    r["player"] = int(m.get("player") or r["player"])
                    r["state"] = self.parse_input(m)
                    r["last_input"] = time.monotonic()
            elif kind == "pair":
                if self.hub.ble is not None:
                    try:
                        self.hub.ble.request_pairing(int(m.get("player")))
                    except (TypeError, ValueError):
                        pass
