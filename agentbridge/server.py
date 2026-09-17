#!/usr/bin/env python3
"""AgentBridge server: stdlib-only WebSocket bridge between Mac agents and phone."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import time

TOKEN_FILE = os.path.expanduser("~/.agentbridge/token")
DEFAULT_PORT = 9876
MAX_MSG = 1 << 20
WS_MAGIC = "258EA5-E905-47DA-9CAC-CA0C11B96E8"


def load_token() -> str:
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
            if tok:
                return tok
    tok = secrets.token_hex(16)
    with open(TOKEN_FILE, "w") as f:
        f.write(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip not in ips:
            ips.append(ip)
        s.close()
    except OSError:
        pass
    return ips or ["127.0.0.1"]


def build_frame(opcode: int, payload: bytes) -> bytes:
    out = bytearray()
    out.append(0x80 | opcode)
    n = len(payload)
    if n < 126:
        out.append(n)
    elif n < (1 << 16):
        out.append(126)
        out += n.to_bytes(2, "big")
    else:
        out.append(127)
        out += n.to_bytes(8, "big")
    out += payload
    return bytes(out)


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes, bool]:
    """Returns (opcode, payload, fin). Raises on disconnect/protocol error."""
    hdr = await reader.readexactly(2)
    b1, b2 = hdr[0], hdr[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    if length > MAX_MSG:
        raise ValueError("frame too large")
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length) if length else b""
    if mask:
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return opcode, payload, fin


class ClientConn:
    def __init__(self, server: "BridgeServer", reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.authed = False
        self.outbox: asyncio.Queue[str] = asyncio.Queue()
        self.addr = writer.get_extra_info("peername")

    async def send_loop(self) -> None:
        try:
            while True:
                data = await self.outbox.get()
                self.writer.write(build_frame(0x1, data.encode("utf-8")))
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    def push(self, msg: dict) -> None:
        if self.authed:
            self.outbox.put_nowait(json.dumps(msg))


class BridgeServer:
    def __init__(self, token: str, manager, hooks) -> None:
        self.token = token
        self.manager = manager
        self.hooks = hooks
        self.clients: set[ClientConn] = set()

    def broadcast(self, msg: dict) -> None:
        if msg.get("type") == "approval-request":
            print(f"[bridge] tx approval {msg.get('rid')}", flush=True)
        for c in list(self.clients):
            c.push(msg)

    async def handle_http(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> bool:
        """Read the HTTP upgrade request. Returns True if it is a WS handshake."""
        headers: dict[str, str] = {}
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if not line or line in (b"\r\n", b"\n"):
                    break
                if b":" in line:
                    k, v = line.decode("latin1").split(":", 1)
                    headers[k.strip().lower()] = v.strip()
        except (asyncio.TimeoutError, ConnectionError):
            return False
        if headers.get("upgrade", "").lower() != "websocket":
            return False
        key = headers.get("sec-websocket-key", "")
        print(f"[bridge] hs t={time.strftime('%H:%M:%S')} key={key!r} hdrs={sorted(headers)}",
              flush=True)
        accept = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
        writer.write(
            ("HTTP/1.1 101 Switching Protocols\r\n"
             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        return True

    async def handle_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        if not await self.handle_http(reader, writer):
            writer.close()
            return
        conn = ClientConn(self, reader, writer)
        self.clients.add(conn)
        sender = asyncio.create_task(conn.send_loop())
        message = bytearray()
        try:
            while True:
                opcode, payload, fin = await read_frame(reader)
                if opcode == 0x8:  # close
                    try:
                        writer.write(build_frame(0x8, payload))
                        await writer.drain()
                    except ConnectionError:
                        pass
                    break
                if opcode == 0x9:  # ping
                    writer.write(build_frame(0xA, payload))
                    await writer.drain()
                    continue
                if opcode == 0xA:  # pong
                    continue
                if opcode not in (0x0, 0x1):
                    continue
                if opcode == 0x1:
                    message = bytearray(payload)
                else:
                    message += payload
                if not fin:
                    continue
                try:
                    text = bytes(message).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                finally:
                    message = bytearray()
                await self.on_message(conn, text)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            sender.cancel()
            self.clients.discard(conn)
            try:
                writer.close()
            except ConnectionError:
                pass

    async def on_message(self, conn: ClientConn, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        if not conn.authed:
            if msg.get("type") == "hello" and secrets.compare_digest(
                    str(msg.get("token", "")), self.token):
                conn.authed = True
                print(f"[bridge] hello ok {conn.addr}", flush=True)
                conn.push({"type": "hello-ok",
                           "sessions": self.manager.list_sessions(),
                           "watchers": self.manager.list_watchers(),
                           "pending": self.hooks.pending_requests()})
            else:
                print(f"[bridge] hello denied {conn.addr}", flush=True)
                conn.writer.close()
            return
        mtype = msg.get("type")
        print(f"[bridge] rx {mtype} {conn.addr}", flush=True)
        try:
            if mtype == "ping":
                conn.push({"type": "pong"})
            elif mtype == "list":
                conn.push({"type": "session-list",
                           "sessions": self.manager.list_sessions(),
                           "watchers": self.manager.list_watchers()})
            elif mtype == "spawn":
                sess = self.manager.spawn(str(msg.get("command", "")),
                                          str(msg.get("cwd", "") or os.path.expanduser("~")))
                conn.push({"type": "spawned", "session": sess})
            elif mtype == "input":
                self.manager.write_input(str(msg.get("id", "")), str(msg.get("text", "")))
            elif mtype == "kill":
                self.manager.kill(str(msg.get("id", "")))
            elif mtype == "watch":
                w = self.manager.watch_file(str(msg.get("path", "")))
                conn.push({"type": "watch-added", "watch": w})
            elif mtype == "unwatch":
                self.manager.unwatch(str(msg.get("id", "")))
            elif mtype == "approval-verdict":
                rid = str(msg.get("rid", ""))
                verdict = str(msg.get("verdict", "deny"))
                if verdict not in ("approve", "deny"):
                    verdict = "deny"
                self.hooks.write_verdict(rid, verdict)
                self.broadcast({"type": "approval-resolved", "rid": rid, "verdict": verdict})
            else:
                conn.push({"type": "error", "message": f"unknown type: {mtype}"})
        except Exception as exc:  # never drop the connection on handler errors
            conn.push({"type": "error", "message": str(exc)})


async def serve(port: int = DEFAULT_PORT) -> None:
    from . import hooks as hooks_mod
    from . import sessions as sessions_mod

    token = load_token()
    server = BridgeServer(token, None, hooks_mod)  # type: ignore[arg-type]
    manager = sessions_mod.SessionManager(server.broadcast)
    server.manager = manager
    manager.start()
    # Replay anything the hooks filed while we were away.
    for req in hooks_mod.pending_requests():
        if req.get("kind") == "approval":
            server.broadcast({"type": "approval-request", **req})
        else:
            server.broadcast({"type": "notify", "kind": "info",
                              "title": req.get("title", ""),
                              "body": req.get("detail", "")})
    srv = await asyncio.start_server(server.handle_client, "0.0.0.0", port)
    print("AgentBridge listening")
    for ip in lan_ips():
        print(f"  ws://{ip}:{port}")
    print("Pair with the token from ~/.agentbridge/token (shown once below).")
    print(f"TOKEN: {token}")
    async with srv:
        await srv.serve_forever()
