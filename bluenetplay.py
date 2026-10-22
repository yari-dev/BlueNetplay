#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import collections
import ipaddress
import json
import logging
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType, web

import gamepad
from gamepad import AXIS_CENTER, PROFILES, BluetoothGamepad, PadState, hat_from_dirs

log = logging.getLogger("bluenetplay")

WEB_DIR = Path(__file__).resolve().parent / "web"

MSG_VIDEO = 0x01
MSG_AUDIO = 0x02

AUDIO_RATE = 48000
AUDIO_CHUNK = AUDIO_RATE // 50 * 4

BUTTONS = {
    "A": gamepad.N64_A, "B": gamepad.N64_B, "Z": gamepad.N64_Z,
    "START": gamepad.N64_START, "L": gamepad.N64_L, "R": gamepad.N64_R,
    "CU": gamepad.N64_C_UP, "CD": gamepad.N64_C_DOWN,
    "CL": gamepad.N64_C_LEFT, "CR": gamepad.N64_C_RIGHT,
    "PAK": gamepad.N64_PAK,
}

INPUT_TIMEOUT = 0.5


class Client:
    def __init__(self, ws: web.WebSocketResponse, cid: int):
        self.ws = ws
        self.id = cid
        self.player = 1
        self.state = PadState()
        self.last_input = 0.0
        self.want_audio = False
        self.video: bytes | None = None
        self.audio: collections.deque = collections.deque(maxlen=25)
        self.text: collections.deque = collections.deque()
        self.wake = asyncio.Event()

    def push_video(self, frame: bytes) -> None:
        self.video = frame
        self.wake.set()

    def push_audio(self, chunk: bytes) -> None:
        if self.want_audio:
            self.audio.append(chunk)
            self.wake.set()

    def push_text(self, msg: dict) -> None:
        self.text.append(json.dumps(msg))
        self.wake.set()

    async def sender(self) -> None:
        try:
            while not self.ws.closed:
                await self.wake.wait()
                self.wake.clear()
                while self.text:
                    await self.ws.send_str(self.text.popleft())
                while self.audio:
                    await self.ws.send_bytes(bytes([MSG_AUDIO]) + self.audio.popleft())
                if self.video is not None:
                    frame, self.video = self.video, None
                    await self.ws.send_bytes(bytes([MSG_VIDEO]) + frame)
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            pass


class Hub:
    def __init__(self, profile_key: str, players: int = 1):
        self.clients: dict[int, Client] = {}
        self.next_id = 1
        self.profile_key = profile_key
        self.players = players
        self.ble = None
        self.relay = None
        self.remote: dict[int, dict] = {}
        self.status = {
            "bt": {"state": "off", "detail": "Bluetooth disabled", "peer": None,
                   "profile": profile_key},
            "video": {"state": "off", "fps": 0.0, "detail": "Video disabled"},
            "audio": {"state": "off", "detail": "Audio disabled"},
            "relay": {"rtt_ms": None},
            "clients": 0,
            "players": [{"player": p, "state": "off", "paired": p == 1, "tabs": 0}
                        for p in range(1, players + 1)],
        }
        self._frames = 0
        self._fps_t0 = time.monotonic()

    def add(self, ws: web.WebSocketResponse) -> Client:
        client = Client(ws, self.next_id)
        self.next_id += 1
        self.clients[client.id] = client
        self.status["clients"] = len(self.clients)
        self.broadcast_status()
        return client

    def remove(self, client: Client) -> None:
        self.clients.pop(client.id, None)
        self.status["clients"] = len(self.clients)
        self.broadcast_status()

    def set_status(self, key: str, **values) -> None:
        current = self.status[key]
        if any(current.get(k) != v for k, v in values.items()):
            current.update(values)
            self.broadcast_status()

    def broadcast_status(self) -> None:
        msg = {"type": "status", **self.status}
        for c in self.clients.values():
            c.push_text(msg)
        if self.relay is not None:
            self.relay.send_json(msg)

    def publish_video(self, frame: bytes) -> None:
        for c in self.clients.values():
            c.push_video(frame)
        self._frames += 1
        now = time.monotonic()
        if now - self._fps_t0 >= 1.0:
            fps = round(self._frames / (now - self._fps_t0), 1)
            self._frames, self._fps_t0 = 0, now
            self.set_status("video", state="ok", fps=fps, detail="")

    def publish_audio(self, chunk: bytes) -> None:
        for c in self.clients.values():
            c.push_audio(chunk)

    def set_players(self, players: list[dict]) -> None:
        tabs = collections.Counter(c.player for c in self.clients.values())
        tabs.update(r["player"] for r in self.remote.values() if r.get("role") != "watch")
        for p in players:
            p["tabs"] = tabs.get(p["player"], 0)
        if players != self.status["players"]:
            self.status["players"] = players
            self.broadcast_status()

    def merged_state(self, player: int = 1) -> PadState:
        now = time.monotonic()
        out = PadState()
        sources = [(c.player, c.state, c.last_input) for c in self.clients.values()]
        sources += [(r["player"], r["state"], r["last_input"]) for r in self.remote.values()]
        for p, s, last_input in sources:
            if p != player or now - last_input > INPUT_TIMEOUT:
                continue
            out.buttons |= s.buttons
            if out.hat == gamepad.HAT_CENTER:
                out.hat = s.hat
            if out.x == AXIS_CENTER and out.y == AXIS_CENTER:
                out.x, out.y = s.x, s.y
        return out


def state_from_message(msg: dict) -> PadState:
    state = PadState()
    for name in msg.get("buttons", []):
        if name in BUTTONS:
            state.press(BUTTONS[name])
    dpad = set(msg.get("dpad", []))
    state.hat = hat_from_dirs("up" in dpad, "down" in dpad, "left" in dpad, "right" in dpad)

    def axis(v) -> int:
        try:
            v = max(-1.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            v = 0.0
        return max(0, min(255, AXIS_CENTER + round(v * 127)))

    state.x = axis(msg.get("x", 0))
    state.y = axis(msg.get("y", 0))
    return state


async def _log_stderr(stream, label: str, sink: list) -> None:
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode(errors="replace").strip()
        if text:
            sink.append(text)
            log.debug("%s: %s", label, text)


IS_WINDOWS = sys.platform == "win32"
GVUSB2_ALSA = "hw:CARD=GVUSB2,DEV=0"


def find_gvusb2_video() -> str | None:
    for node in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            if (node / "name").read_text().strip() == "gvusb2":
                return f"/dev/{node.name}"
        except OSError:
            pass
    return None


def video_input_args(args) -> list[str]:
    if IS_WINDOWS:
        return ["-f", "dshow", "-rtbufsize", "64M",
                "-video_size", args.video_size, "-framerate", str(args.fps),
                "-i", f"video={args.video_device}"]
    width, height = (int(v) for v in args.video_size.split("x"))
    pal = height == 576
    if args.video_mode == "fields":
        height //= 2
        rate = "50" if pal else "60000/1001"
    else:
        rate = "25" if pal else "30000/1001"
    return ["-f", "v4l2", "-thread_queue_size", "16", "-input_format", "uyvy422",
            "-framerate", rate, "-video_size", f"{width}x{height}", "-i", args.video_device]


def video_filter(args) -> str | None:
    if IS_WINDOWS:
        return None
    if args.video_mode == "fields":
        return f"scale={args.video_size.replace('x', ':')}:flags=neighbor"
    if args.fps > 30:
        return "bwdif=mode=send_field"
    return None


def audio_input_args(args) -> list[str]:
    if IS_WINDOWS:
        return ["-f", "dshow", "-audio_buffer_size", "20", "-i", f"audio={args.audio_device}"]
    return ["-f", "alsa", "-sample_rate", "48000", "-channels", "2",
            "-thread_queue_size", "64", "-i", args.audio_device]


async def video_task(hub: Hub, args) -> None:
    vf = ["-vf", args.video_filter] if args.video_filter else []
    cmd = [args.ffmpeg, "-hide_banner", "-loglevel", "error", *video_input_args(args),
           "-map", "0:v", "-an", *vf,
           "-c:v", "mjpeg", "-q:v", str(args.quality), "-f", "mjpeg", "pipe:1"]
    if hub.relay is not None:
        import stream
        width, height = (int(v) for v in args.video_size.split("x"))
        receiver = stream.FlvH264Receiver(hub.relay, width, height)
        await receiver.start()
        cmd += stream.h264_output_args(args, receiver.port)
    while True:
        hub.set_status("video", state="starting", fps=0.0, detail="Starting capture...")
        errors: list[str] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            hub.set_status("video", state="error", detail="ffmpeg not found (install it or pass --ffmpeg)")
            return
        err_task = asyncio.create_task(_log_stderr(proc.stderr, "video", errors))
        buf = bytearray()
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        buf.clear()
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start:
                            del buf[:start]
                        break
                    hub.publish_video(bytes(buf[start:end + 2]))
                    del buf[:end + 2]
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            await err_task
        detail = next((e for e in reversed(errors) if "real-time buffer" not in e), "Capture stopped")
        if "I/O error" in detail or "Could not" in detail or "busy" in detail.lower():
            detail += " - is OBS or another program using the GV-USB2?"
        elif not IS_WINDOWS and ("No such file" in detail or "not a video" in detail.lower()):
            detail += " - is the gvusb2 driver installed and the GV-USB2 plugged in?"
        log.warning("Video capture ended: %s", detail)
        hub.set_status("video", state="error", fps=0.0, detail=detail)
        await asyncio.sleep(3)


async def audio_task(hub: Hub, args) -> None:
    cmd = [args.ffmpeg, "-hide_banner", "-loglevel", "error", *audio_input_args(args),
           "-map", "0:a", "-vn", "-ac", "2", "-ar", str(AUDIO_RATE), "-f", "s16le", "pipe:1"]
    if hub.relay is not None:
        import stream
        receiver = stream.OggOpusReceiver(hub.relay)
        await receiver.start()
        cmd += stream.opus_output_args(args, receiver.port)
    while True:
        hub.set_status("audio", state="starting", detail="Starting audio...")
        errors: list[str] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            hub.set_status("audio", state="error", detail="ffmpeg not found")
            return
        err_task = asyncio.create_task(_log_stderr(proc.stderr, "audio", errors))
        got_data = False
        try:
            while True:
                chunk = await proc.stdout.readexactly(AUDIO_CHUNK)
                if not got_data:
                    got_data = True
                    hub.set_status("audio", state="ok", detail="")
                hub.publish_audio(chunk)
        except asyncio.IncompleteReadError:
            pass
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            await err_task
        detail = errors[-1] if errors else "Audio capture stopped"
        log.warning("Audio capture ended: %s", detail)
        hub.set_status("audio", state="error", detail=detail)
        await asyncio.sleep(3)


def bt_state(pad: BluetoothGamepad) -> tuple[str, str, str | None]:
    conn = pad.connection
    if conn is None:
        return "waiting", "Waiting for BlueRetro to connect", None
    peer = str(conn.peer_address).split("/")[0]
    if not pad.channels_open:
        return "connecting", "BlueRetro is connecting", peer
    if not pad.live:
        return "calibrating", "Calibrating", peer
    return "live", "Connected - inputs go to the console", peer


async def bluetooth_task(hub: Hub, args) -> None:
    from bumble.transport import open_transport

    profile = PROFILES[args.profile]
    name = args.name or profile.default_name
    hub.set_status("bt", state="starting", detail=f"Opening dongle ({args.transport})...")
    try:
        transport = await open_transport(args.transport)
    except Exception as error:
        hint = ("on the WinUSB driver" if IS_WINDOWS
                else "allowed by the udev rule (linux/99-bluenetplay.rules)")
        hub.set_status("bt", state="error",
                       detail=f"Can't open the Bluetooth dongle: {error}. Is it plugged in, {hint}, "
                              "and not used by gamepad.py?")
        log.error("Bluetooth dongle: %s", error)
        return

    sources = []
    if args.local_input and IS_WINDOWS:
        sources = [gamepad.KeyboardInput(), gamepad.XInputGamepad()]

    async with transport:
        extra = hub.players > 1
        if extra and profile.key != "generic":
            log.warning("Players 2-4 need the generic profile (BlueRetro's search combo); "
                        "only player 1 will work")
            extra = False
        pad = BluetoothGamepad.create(transport.source, transport.sink, name, profile, le=extra)
        await pad.start()
        log.info("'%s' (%s profile) is discoverable; waiting for BlueRetro", name, profile.key)
        if extra:
            from ble_pads import BlePadManager
            hub.ble = BlePadManager(pad.device, pad, Path(__file__).resolve().parent / "ble_pads.json",
                                    players=hub.players - 1, name=profile.default_name)
            log.info("Players 2-%d: BLE gamepads (%s)", hub.players,
                     ", ".join(f"P{p.player} {'paired' if p.paired else 'not paired'}"
                               for p in hub.ble.pads))
        tick = 1.0 / 250
        loops = 0
        while True:
            state = hub.merged_state(1)
            for source in sources:
                source.read(state)
            pad.pump(state)
            st, detail, peer = bt_state(pad)
            hub.set_status("bt", state=st, detail=detail, peer=peer)
            players = [{"player": 1, "state": st, "paired": True}]
            if hub.ble:
                hub.ble.pump({p: hub.merged_state(p) for p in range(2, hub.players + 1)})
                if loops % 25 == 0:
                    try:
                        await hub.ble.tick()
                    except Exception as error:
                        log.warning("BLE: %s", error)
                players += hub.ble.status()
            hub.set_players(players)
            loops += 1
            await asyncio.sleep(tick)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def websocket(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=64 * 1024)
    await ws.prepare(request)
    client = hub.add(ws)
    log.info("Browser %d connected (%s)", client.id, request.remote)
    client.push_text({"type": "hello", "id": client.id, "role": "host", "can_choose": True,
                      "can_pair": True, "relay": False,
                      "buttons": list(BUTTONS), "audio_rate": AUDIO_RATE})
    client.push_text({"type": "status", **hub.status})
    sender = asyncio.create_task(client.sender())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "input":
                client.state = state_from_message(data)
                client.last_input = time.monotonic()
            elif kind == "audio":
                client.want_audio = bool(data.get("enabled"))
                client.audio.clear()
            elif kind == "player":
                try:
                    player = int(data.get("player"))
                except (TypeError, ValueError):
                    continue
                if 1 <= player <= hub.players:
                    client.player = player
                    client.state = PadState()
                    log.info("Browser %d now controls player %d", client.id, player)
            elif kind == "pair":
                if hub.ble:
                    try:
                        hub.ble.request_pairing(int(data.get("player")))
                    except (TypeError, ValueError):
                        pass
            elif kind == "ping":
                client.push_text({"type": "pong", "t": data.get("t")})
    finally:
        sender.cancel()
        hub.remove(client)
        log.info("Browser %d disconnected", client.id)
    return ws


def lan_address() -> str | None:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


@web.middleware
async def no_cache(request, handler):
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    return response


async def main_async(args) -> None:
    hub = Hub(args.profile, players=args.players)
    if args.relay:
        import stream
        token = args.relay_token or Path(args.relay_token_file).read_text().strip()
        hub.relay = stream.RelayLink(hub, args.relay, token, state_from_message)
    app = web.Application(middlewares=[no_cache])
    app["hub"] = hub
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket)
    app.router.add_static("/static", WEB_DIR)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    if args.host in ("0.0.0.0", "::", ""):
        shown_host = lan_address() or "localhost"
        log.info("Open http://localhost:%d here, or http://%s:%d from other devices",
                 args.port, shown_host, args.port)
    else:
        shown_host = "localhost" if args.host in ("127.0.0.1", "localhost", "::1") else args.host
        log.info("Open http://%s:%d in your browser", shown_host, args.port)
    try:
        if not ipaddress.ip_address(args.host).is_loopback:
            log.warning("Listening on %s: anyone on your network can open the page and "
                        "control the console. There is no password.", args.host)
    except ValueError:
        pass

    tasks = []
    if hub.relay is not None:
        tasks.append(asyncio.create_task(hub.relay.run()))
    if not args.no_video:
        tasks.append(asyncio.create_task(video_task(hub, args)))
    if not args.no_audio:
        tasks.append(asyncio.create_task(audio_task(hub, args)))
    if not args.no_bt:
        tasks.append(asyncio.create_task(bluetooth_task(hub, args)))
    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="BlueNetplay: play a real console from a browser")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1; 0.0.0.0 = whole network)")
    parser.add_argument("--port", type=int, default=8064, help="web port (default 8064)")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="generic",
                        help="Bluetooth controller profile (default generic, for BlueRetro v25.04)")
    parser.add_argument("--name", help="Bluetooth device name (default: the profile's name)")
    parser.add_argument("--players", type=int, default=1, choices=(1, 2, 3, 4),
                        help="controllers to provide (default 1). Player 1 is Bluetooth "
                             "Classic, players 2-4 are BLE gamepads on the same dongle")
    parser.add_argument("--transport", default="usb:0", help="Bumble transport (default usb:0)")
    parser.add_argument("--video-device",
                        help="capture device (Windows default 'GV-USB2, Analog Capture'; "
                             "Linux: the gvusb2 /dev/video node, found automatically)")
    parser.add_argument("--audio-device",
                        help=f"audio device (Windows default 'GV-USB2, Analog WaveIn'; "
                             f"Linux default {GVUSB2_ALSA})")
    parser.add_argument("--video-size", default="720x480", help="picture size (720x576 for PAL)")
    parser.add_argument("--fps", type=float,
                        help="frame rate, 29.97 or 59.94 (default: Windows 29.97, Linux 59.94; "
                             "50 / 25 for PAL)")
    parser.add_argument("--video-mode", choices=("fields", "interlaced"), default="fields",
                        help="Linux: 'fields' (default) captures every field as a frame and doubles "
                             "its lines, ideal for 240p consoles like the N64; 'interlaced' captures whole "
                             "frames (sharper for 480i games, deinterlaced at 59.94)")
    parser.add_argument("--quality", type=int, default=5, help="JPEG quality 2 (best) - 31 (default 5)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="path to ffmpeg")
    parser.add_argument("--no-bt", action="store_true", help="don't use the Bluetooth dongle")
    parser.add_argument("--no-video", action="store_true", help="don't capture video")
    parser.add_argument("--no-audio", action="store_true", help="don't capture audio")
    parser.add_argument("--relay", metavar="URL",
                        help="also stream to an internet relay, e.g. "
                             "wss://relay.example.com/console (see relay/DEPLOY.md)")
    parser.add_argument("--relay-token-file", default=str(Path(__file__).resolve().parent / "relay_token.txt"),
                        help="file with the relay's console token (default relay_token.txt)")
    parser.add_argument("--relay-token", help=argparse.SUPPRESS)
    parser.add_argument("--relay-bitrate", default="4M", help="H.264 bitrate for the relay (default 4M)")
    parser.add_argument("--relay-audio-bitrate", default="96k", help="Opus bitrate (default 96k)")
    parser.add_argument("--h264-encoder", choices=("h264_nvenc", "libx264", "h264_v4l2m2m"),
                        help="H.264 encoder for the relay (default: Windows h264_nvenc = NVIDIA GPU, "
                             "Linux libx264; h264_v4l2m2m = Raspberry Pi 4 hardware, experimental)")
    parser.add_argument("--local-input", action="store_true",
                        help="Windows: also read this PC's keyboard (global) and XInput pads")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose > 1 else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("bumble").setLevel(logging.INFO if args.verbose else logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    pal = args.video_size.endswith("x576")
    if IS_WINDOWS:
        args.video_device = args.video_device or "GV-USB2, Analog Capture"
        args.audio_device = args.audio_device or "GV-USB2, Analog WaveIn"
        args.fps = args.fps or 29.97
        args.h264_encoder = args.h264_encoder or "h264_nvenc"
    else:
        args.video_device = args.video_device or find_gvusb2_video() or "/dev/video0"
        args.audio_device = args.audio_device or GVUSB2_ALSA
        full = 50.0 if pal else 59.94
        args.fps = full if args.video_mode == "fields" else (args.fps or full)
        args.h264_encoder = args.h264_encoder or "libx264"
        if args.local_input:
            log.warning("--local-input only works on Windows; ignored")
        if not args.no_video:
            log.info("Video: %s (%s mode, %g fps)", args.video_device, args.video_mode, args.fps)
    args.video_filter = video_filter(args)

    gamepad.high_res_timer(True)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        gamepad.high_res_timer(False)


if __name__ == "__main__":
    main()
