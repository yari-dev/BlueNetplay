#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import collections
import hmac
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType, web

log = logging.getLogger("relay")

MSG_H264 = 0x03
MSG_OPUS = 0x04

VIDEO_BACKLOG = 6
AUDIO_BACKLOG = 10
SOCKET_SNDBUF = 64 * 1024
DEFAULT_PUBLIC_URL = "http://localhost:8650"


class Invites:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = []
        self._mtime = None
        self.load()

    def load(self) -> None:
        try:
            self._mtime = self.path.stat().st_mtime
            self.items = json.loads(self.path.read_text()).get("invites", [])
        except (OSError, ValueError):
            self.items = []

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self.load()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"invites": self.items}, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def find(self, token: str) -> dict | None:
        if not token:
            return None
        self._refresh()
        for inv in self.items:
            if hmac.compare_digest(inv["token"], token):
                return inv
        return None

    def add(self, name: str, role: str, player: int | None) -> dict:
        inv = {"token": secrets.token_urlsafe(18), "name": name, "role": role,
               "player": player, "created": int(time.time())}
        self.items.append(inv)
        self.save()
        return inv

    def revoke(self, key: str) -> int:
        before = len(self.items)
        self.items = [i for i in self.items if i["token"] != key and i["name"] != key]
        self.save()
        return before - len(self.items)


class Viewer:
    def __init__(self, ws: web.WebSocketResponse, vid: int, invite: dict):
        self.ws = ws
        self.id = vid
        self.invite = invite
        self.role = invite["role"]
        self.player = invite.get("player") or 1
        self.want_audio = False
        self.text: collections.deque = collections.deque()
        self.video: collections.deque = collections.deque()
        self.audio: collections.deque = collections.deque(maxlen=AUDIO_BACKLOG)
        self.need_keyframe = True
        self.wake = asyncio.Event()

    @property
    def can_play(self) -> bool:
        return self.role in ("host", "player")

    def push_text(self, msg: dict) -> None:
        self.text.append(json.dumps(msg))
        self.wake.set()

    def push_video(self, data: bytes, keyframe: bool) -> None:
        if self.need_keyframe:
            if not keyframe:
                return
            self.need_keyframe = False
        if len(self.video) >= VIDEO_BACKLOG:
            self.video.clear()
            self.need_keyframe = True
            if not keyframe:
                return
            self.need_keyframe = False
        self.video.append(data)
        self.wake.set()

    def push_audio(self, data: bytes) -> None:
        if self.want_audio:
            self.audio.append(data)
            self.wake.set()

    async def sender(self) -> None:
        try:
            while not self.ws.closed:
                await self.wake.wait()
                self.wake.clear()
                while self.text:
                    await self.ws.send_str(self.text.popleft())
                while self.audio:
                    await self.ws.send_bytes(self.audio.popleft())
                while self.video:
                    await self.ws.send_bytes(self.video.popleft())
                    while self.text:
                        await self.ws.send_str(self.text.popleft())
                    while self.audio:
                        await self.ws.send_bytes(self.audio.popleft())
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            pass


class Relay:
    def __init__(self, invites: Invites, console_token: str):
        self.invites = invites
        self.console_token = console_token
        self.viewers: dict[int, Viewer] = {}
        self.next_id = 1
        self.console: web.WebSocketResponse | None = None
        self.console_status: dict | None = None
        self.video_config: dict | None = None
        self.audio_config: dict | None = None


    def status_for(self, v: Viewer) -> dict:
        base = self.console_status or {
            "bt": {"state": "off", "detail": "The host is offline", "peer": None, "profile": ""},
            "video": {"state": "error", "fps": 0.0, "detail": "The host is offline"},
            "audio": {"state": "off", "detail": ""},
            "players": [],
        }
        return {**base, "type": "status", "clients": len(self.viewers),
                "console": self.console is not None}

    def broadcast_status(self) -> None:
        for v in self.viewers.values():
            v.push_text(self.status_for(v))

    async def to_console(self, msg: dict) -> None:
        if self.console is not None and not self.console.closed:
            try:
                await self.console.send_str(json.dumps(msg))
            except (ConnectionResetError, RuntimeError):
                pass


    async def console_ws(self, request: web.Request) -> web.WebSocketResponse:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not self.console_token or not hmac.compare_digest(token, self.console_token):
            log.warning("Rejected console connection from %s", request.remote)
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        if self.console is not None:
            await self.console.close(code=4000, message=b"replaced by a new console connection")
        self.console = ws
        log.info("Host connected (%s)", request.headers.get("X-Forwarded-For", request.remote))
        for v in self.viewers.values():
            v.need_keyframe = True
        for v in self.viewers.values():
            await self.to_console({"type": "viewer", "viewer": v.id, "player": v.player,
                                   "name": v.invite["name"], "role": v.role})
        self.broadcast_status()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    data = msg.data
                    if not data:
                        continue
                    if data[0] == MSG_H264:
                        keyframe = bool(data[1] & 0x01)
                        for v in self.viewers.values():
                            v.push_video(data, keyframe)
                    elif data[0] == MSG_OPUS:
                        for v in self.viewers.values():
                            v.push_audio(data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        m = json.loads(msg.data)
                    except ValueError:
                        continue
                    kind = m.get("type")
                    if kind == "ping":
                        await ws.send_str(json.dumps({"type": "pong", "t": m.get("t")}))
                    elif kind == "status":
                        m.pop("type", None)
                        self.console_status = m
                        self.broadcast_status()
                    elif kind == "video_config":
                        self.video_config = m
                        for v in self.viewers.values():
                            v.need_keyframe = True
                            v.push_text(m)
                    elif kind == "audio_config":
                        self.audio_config = m
                        for v in self.viewers.values():
                            v.push_text(m)
        finally:
            if self.console is ws:
                self.console = None
                self.console_status = None
                log.warning("Host disconnected")
                self.broadcast_status()
        return ws


    async def viewer_ws(self, request: web.Request) -> web.WebSocketResponse:
        invite = self.invites.find(request.query.get("invite", ""))
        if invite is None:
            raise web.HTTPForbidden(text="This page needs a valid invite link.")
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=64 * 1024)
        await ws.prepare(request)
        _limit_send_buffer(request)
        v = Viewer(ws, self.next_id, invite)
        self.next_id += 1
        self.viewers[v.id] = v
        who = request.headers.get("X-Forwarded-For", request.remote)
        log.info("Viewer %d '%s' (%s, player %s) joined from %s",
                 v.id, invite["name"], v.role, v.player, who)
        v.push_text({"type": "hello", "id": v.id, "role": v.role, "name": invite["name"],
                     "player": v.player, "can_choose": v.role == "host",
                     "can_pair": v.role == "host", "relay": True})
        if self.video_config:
            v.push_text(self.video_config)
        if self.audio_config:
            v.push_text(self.audio_config)
        await self.to_console({"type": "viewer", "viewer": v.id, "player": v.player,
                               "name": invite["name"], "role": v.role})
        self.broadcast_status()
        sender = asyncio.create_task(v.sender())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                kind = m.get("type")
                if kind == "input" and v.can_play:
                    await self.to_console({"type": "input", "viewer": v.id, "player": v.player,
                                           "buttons": m.get("buttons", []), "dpad": m.get("dpad", []),
                                           "x": m.get("x", 0), "y": m.get("y", 0)})
                elif kind == "player" and v.role == "host":
                    try:
                        p = int(m.get("player"))
                    except (TypeError, ValueError):
                        continue
                    if 1 <= p <= 4 and p != v.player:
                        v.player = p
                        await self.to_console({"type": "viewer", "viewer": v.id, "player": p,
                                               "name": invite["name"], "role": v.role})
                        self.broadcast_status()
                elif kind == "pair" and v.role == "host":
                    await self.to_console({"type": "pair", "player": m.get("player")})
                elif kind == "audio":
                    v.want_audio = bool(m.get("enabled"))
                    v.audio.clear()
                elif kind == "ping":
                    v.push_text({"type": "pong", "t": m.get("t")})
        finally:
            sender.cancel()
            self.viewers.pop(v.id, None)
            await self.to_console({"type": "viewer_left", "viewer": v.id})
            self.broadcast_status()
            log.info("Viewer %d '%s' left", v.id, invite["name"])
        return ws


def _limit_send_buffer(request: web.Request) -> None:
    import socket
    try:
        sock = request.transport.get_extra_info("socket") if request.transport else None
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SNDBUF)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


INVITE_NEEDED = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>BlueNetplay</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e0f13;color:#e8e9ee;
font:16px system-ui,sans-serif}main{max-width:420px;padding:24px;text-align:center}
h1{font-size:22px}p{color:#9097a6}</style></head><body><main><h1>BlueNetplay</h1>
<p>This page needs an invite link. Ask the host for yours.</p></main></body></html>"""


def build_app(relay: Relay, web_dir: Path) -> web.Application:
    @web.middleware
    async def headers(request, handler):
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def index(request: web.Request):
        if relay.invites.find(request.query.get("invite", "")) is None:
            return web.Response(text=INVITE_NEEDED, content_type="text/html", status=403)
        return web.FileResponse(web_dir / "index.html")

    async def health(request: web.Request):
        return web.json_response({"ok": True, "console": relay.console is not None,
                                  "viewers": len(relay.viewers)})

    app = web.Application(middlewares=[headers])
    app.router.add_get("/", index)
    app.router.add_get("/ws", relay.viewer_ws)
    app.router.add_get("/console", relay.console_ws)
    app.router.add_get("/healthz", health)
    app.router.add_static("/static", web_dir)
    return app


def read_console_token(args) -> str:
    if args.console_token_file:
        return Path(args.console_token_file).read_text().strip()
    return os.environ.get("BLUENETPLAY_CONSOLE_TOKEN", "").strip()


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="BlueNetplay relay server")
    parser.add_argument("--data", default=os.environ.get("BLUENETPLAY_RELAY_DATA", str(here / "data")),
                        help="folder for invites.json")
    parser.add_argument("--public-url", default=os.environ.get("BLUENETPLAY_PUBLIC_URL", DEFAULT_PUBLIC_URL))
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the relay")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("BLUENETPLAY_RELAY_PORT", "8650")))
    s.add_argument("--web-dir", default=os.environ.get("BLUENETPLAY_WEB_DIR", str(here.parent / "web")))
    s.add_argument("--console-token-file", help="file with the host's secret (else $BLUENETPLAY_CONSOLE_TOKEN)")

    i = sub.add_parser("invite", help="create an invite link")
    i.add_argument("--name", required=True)
    g = i.add_mutually_exclusive_group(required=True)
    g.add_argument("--player", type=int, choices=(1, 2, 3, 4))
    g.add_argument("--host", action="store_true", help="may pick any player and pair pads")
    g.add_argument("--watch", action="store_true", help="video and sound only")

    sub.add_parser("invites", help="list invites")
    r = sub.add_parser("revoke", help="delete an invite")
    r.add_argument("key", help="token or name")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    invites = Invites(Path(args.data) / "invites.json")

    if args.cmd == "invite":
        role = "host" if args.host else "watch" if args.watch else "player"
        inv = invites.add(args.name, role, 1 if args.host else args.player)
        print(f"{args.public_url.rstrip('/')}/?invite={inv['token']}")
        return
    if args.cmd == "invites":
        for inv in invites.items:
            what = f"player {inv['player']}" if inv["role"] == "player" else inv["role"]
            print(f"{inv['name']:<16} {what:<10} {args.public_url.rstrip('/')}/?invite={inv['token']}")
        return
    if args.cmd == "revoke":
        print(f"removed {invites.revoke(args.key)} invite(s)")
        return

    token = read_console_token(args)
    if len(token) < 24:
        sys.exit("Set a console token of at least 24 characters (BLUENETPLAY_CONSOLE_TOKEN or --console-token-file).")
    web_dir = Path(args.web_dir)
    if not (web_dir / "index.html").exists():
        sys.exit(f"Web files not found in {web_dir}")
    relay = Relay(invites, token)
    log.info("Relay on http://%s:%d (web files: %s, %d invites)", args.host, args.port,
             web_dir, len(invites.items))
    web.run_app(build_app(relay, web_dir), host=args.host, port=args.port, access_log=None,
                print=None)


if __name__ == "__main__":
    main()
