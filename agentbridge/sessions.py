"""Managed pty sessions plus read-only watchers (agent processes, log tails)."""
from __future__ import annotations

import asyncio
import glob
import json
import os
import pty
import shlex
import signal
import subprocess
import time
import uuid

from . import hooks

KNOWN_AGENTS = ("claude", "codex", "cursor-agent", "copilot", "gemini",
                "aichat", "opencode", "goose", "amp", "droid", "crush",
                "kilocode", "roo", "qwen", "chatgpt")
IDLE_AFTER = 120
BUF_CAP = 200 * 1024
CHUNK_CAP = 8 * 1024


class Session:
    def __init__(self, command: str, cwd: str) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.command = command
        self.cwd = cwd
        self.master_fd = -1
        self.proc: subprocess.Popen | None = None
        self.buf: list[str] = []
        self.buf_len = 0
        self.status = "running"
        self.exit_code: int | None = None
        self.last_out = time.time()
        self.idle_flag = False

    def info(self) -> dict:
        return {"id": self.id, "command": self.command, "cwd": self.cwd,
                "status": self.status, "exit_code": self.exit_code,
                "idleSec": int(time.time() - self.last_out)}

    def append(self, text: str) -> None:
        self.buf.append(text)
        self.buf_len += len(text)
        while self.buf and self.buf_len > BUF_CAP:
            self.buf_len -= len(self.buf.pop(0))


class SessionManager:
    def __init__(self, broadcast) -> None:
        self.broadcast = broadcast
        self.sessions: dict[str, Session] = {}
        self.watches: dict[str, dict] = {}
        self._log_offsets: dict[str, int] = {}
        self._seen_pids: set[int] = set()
        self._done_notified: set[str] = set()
        self._delivered: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # MARK: - lifecycle

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop.create_task(self._idle_ticker())
        self._loop.create_task(self._watch_ticker())

    # MARK: - managed sessions

    def list_sessions(self) -> list[dict]:
        return [s.info() for s in self.sessions.values()]

    def spawn(self, command: str, cwd: str) -> dict:
        command = command.strip()
        if not command:
            raise ValueError("empty command")
        if not cwd or not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        sess = Session(command, cwd)
        master, slave = pty.openpty()
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        try:
            proc = subprocess.Popen(
                shlex.split(command), stdin=slave, stdout=slave, stderr=subprocess.STDOUT,
                cwd=cwd, env=env, start_new_session=True, close_fds=True)
        except (OSError, ValueError) as exc:
            os.close(master)
            os.close(slave)
            raise ValueError(f"cannot start: {exc}") from exc
        os.close(slave)
        sess.master_fd = master
        sess.proc = proc
        self.sessions[sess.id] = sess
        assert self._loop is not None
        self._loop.add_reader(master, self._on_readable, sess)
        self.broadcast({"type": "session-list", "sessions": self.list_sessions()})
        return sess.info()

    def _on_readable(self, sess: Session) -> None:
        try:
            data = os.read(sess.master_fd, 65536)
        except OSError:
            self._reap(sess)
            return
        if not data:
            self._reap(sess)
            return
        text = data.decode("utf-8", errors="replace")
        sess.append(text)
        sess.last_out = time.time()
        sess.idle_flag = False
        rest = text
        while rest:
            self.broadcast({"type": "output", "id": sess.id, "chunk": rest[:CHUNK_CAP]})
            rest = rest[CHUNK_CAP:]

    def _reap(self, sess: Session) -> None:
        if sess.status != "running":
            return
        try:
            if self._loop is not None:
                self._loop.remove_reader(sess.master_fd)
        except (OSError, ValueError):
            pass
        try:
            os.close(sess.master_fd)
        except OSError:
            pass
        code = sess.proc.poll() if sess.proc else None
        sess.status = "exited"
        sess.exit_code = code if code is not None else -1
        self.broadcast({"type": "session-ended", "id": sess.id, "code": sess.exit_code})

    def write_input(self, sid: str, text: str) -> None:
        sess = self.sessions.get(sid)
        if sess is None or sess.status != "running":
            raise ValueError("session not running")
        if not text.endswith("\n"):
            text += "\n"
        try:
            os.write(sess.master_fd, text.encode("utf-8", errors="replace"))
        except OSError as exc:
            raise ValueError(f"write failed: {exc}") from exc

    def kill(self, sid: str) -> None:
        sess = self.sessions.get(sid)
        if sess is None or sess.status != "running" or sess.proc is None:
            return
        try:
            sess.proc.terminate()
        except OSError:
            pass
        if self._loop is not None:
            def force() -> None:
                if sess.status == "running" and sess.proc is not None:
                    try:
                        sess.proc.kill()
                    except OSError:
                        pass
            self._loop.call_later(5, force)

    async def _idle_ticker(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = time.time()
            for sess in list(self.sessions.values()):
                if sess.status == "running" and not sess.idle_flag \
                        and now - sess.last_out > IDLE_AFTER:
                    sess.idle_flag = True
                    self.broadcast({"type": "session-idle", "id": sess.id,
                                    "idleSec": int(now - sess.last_out)})

    # MARK: - watchers (read-only)

    def list_watchers(self) -> list[dict]:
        out = self._scan_processes()
        for path in sorted(self._log_sources()):
            out.append({"kind": "log", "path": path})
        for wid, w in self.watches.items():
            out.append({"kind": "file", "id": wid, "path": w["path"]})
        return out

    def _scan_processes(self) -> list[dict]:
        try:
            proc = subprocess.run(["ps", "-ax", "-o", "pid=", "-o", "comm=", "-o", "args="],
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        found = []
        for line in (proc.stdout or "").splitlines():
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            comm = parts[1].lower()
            args = parts[2] if len(parts) > 2 else ""
            name = next((a for a in KNOWN_AGENTS
                         if comm == a or comm.endswith("/" + a) or f"/{a} " in f" {args} "), None)
            if name:
                found.append({"kind": "process", "pid": pid, "name": name,
                              "preview": args[:160]})
        return found

    def _log_sources(self) -> list[str]:
        home = os.path.expanduser("~")
        paths: list[str] = []
        for transcript in glob.glob(home + "/.claude/projects/*/*.jsonl"):
            paths.append(transcript)
        codex = sorted(glob.glob(home + "/.codex/sessions/*.jsonl"),
                       key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        paths.extend(codex[-2:])
        # Newest few only; cap the surfacing noise.
        paths.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        return paths[-6:]

    def _tail_new(self, path: str) -> list[str]:
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        last = self._log_offsets.get(path, max(0, size - 32768))
        if size < last:
            last = 0  # rotated/truncated
        if size == last:
            return []
        try:
            with open(path, "rb") as f:
                f.seek(last)
                raw = f.read(32768)
                self._log_offsets[path] = last + len(raw)
        except OSError:
            return []
        lines = []
        for line in raw.decode("utf-8", errors="replace").splitlines()[-30:]:
            text = self._human_line(line)
            if text:
                lines.append(text)
        return lines[-12:]

    @staticmethod
    def _human_line(line: str) -> str:
        line = line.strip()
        if not line:
            return ""
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return line[:200]
        texts: list[str] = []

        def walk(o) -> None:
            if isinstance(o, dict):
                if o.get("type") == "text" and isinstance(o.get("text"), str):
                    texts.append(o["text"])
                if o.get("type") in ("tool_use", "tool_result") and o.get("name"):
                    texts.append(f"[{o['name']}]")
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(obj)
        joined = " ".join(texts).strip()
        return joined[:300] if joined else ""

    def watch_file(self, path: str) -> dict:
        path = os.path.expanduser(path.strip())
        if not path or not os.path.isfile(path):
            raise ValueError("not a readable file")
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            raise ValueError(f"cannot read: {exc}") from exc
        wid = uuid.uuid4().hex[:8]
        self.watches[wid] = {"path": path}
        self._log_offsets[path] = os.path.getsize(path)
        return {"kind": "file", "id": wid, "path": path}

    def unwatch(self, wid: str) -> None:
        self.watches.pop(wid, None)

    async def _watch_ticker(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                current = {w["pid"] for w in self._scan_processes()
                           if w["kind"] == "process"}
                for req in hooks.pending_requests():
                    rid = req.get("rid", "")
                    if not rid or rid in self._delivered:
                        continue
                    self._delivered.add(rid)
                    if req.get("kind") == "approval":
                        self.broadcast({"type": "approval-request", **req})
                    else:
                        self.broadcast({"type": "notify", "kind": "info",
                                        "title": req.get("title", ""),
                                        "body": req.get("detail", "")})
                if len(self._delivered) > 1000:
                    self._delivered = set(list(self._delivered)[-300:])
                for path in self._log_sources():
                    for text in self._tail_new(path):
                        self.broadcast({"type": "watcher-output",
                                        "source": path, "lines": [text]})
                for wid, w in list(self.watches.items()):
                    for text in self._tail_new(w["path"]):
                        self.broadcast({"type": "watcher-output",
                                        "source": w["path"], "lines": [text],
                                        "watch": wid})
                gone = self._seen_pids - current
                for pid in gone:
                    key = f"pid:{pid}"
                    if key not in self._done_notified:
                        self._done_notified.add(key)
                        self.broadcast({"type": "notify", "kind": "done",
                                        "title": "Agent process ended",
                                        "body": f"PID {pid} is gone."})
                self._seen_pids = current
            except Exception:
                pass
            await asyncio.sleep(30)
